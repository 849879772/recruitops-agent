from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api import main
from packages.mcp import task_tools
from packages.recruitment_mail import EmailMessage, MailIdentity, RecruitmentMailStore
from packages.recruitment_mail.binding import MailBindingAdapter, binding_preview
from packages.recruitment_mail import run_service as runs
from packages.storage import Storage, ApplicationSnapshot
from packages.storage.models import TaskRun, ToolCall
from packages.recruitment_mail.storage import RecruitmentMailRecord


@pytest.fixture
def case(tmp_path):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'confirmations.db'}", initialize=True)
    store = RecruitmentMailStore(storage)
    records = [store.upsert(EmailMessage(identity=MailIdentity(message_id=f"confirm-{i}"),
        subject=f"公司{i} 面试邀请", body_text=f"这是合成邮件{i}，请安排面试。")) for i in range(3)]
    with storage.write_transaction() as session:
        session.add(ApplicationSnapshot(id="app-1", company_name="示例公司", job_title="开发工程师",
            stage="applied", source="fixture", source_ref="fixture", idempotency_key="fixture-app", stage_history=[]))
    service = runs.MailProcessingRunService(store, SimpleNamespace(), SimpleNamespace(write_enabled=True))
    yield store, records, service
    service.close()
    storage.engine.dispose()


def ambiguous_processor(calls):
    def process(store, _repo, _settings, *, record_ids, progress, **_kwargs):
        calls.append(list(record_ids))
        for record_id in record_ids:
            store.update_processing_status(record_id, "ambiguous_application")
            progress({"result": {"record_id": record_id, "state": "ambiguous_application", "reason": "multiple_candidates"}})
        return {"status": "partial", "processed": len(record_ids)}
    return process


def wait_for_choice(case, monkeypatch, count=2):
    store, records, service = case
    calls = []
    monkeypatch.setattr(runs, "process_pending_mail", ambiguous_processor(calls))
    run_id = service.start(record_ids=[record.id for record in records[:count]],
        refresh=False, thread_id="chat-a", background=False)["run_id"]
    assert service.run(run_id)["status"] == "awaiting_confirmation"
    return run_id, calls


def bind(store, record):
    preview = binding_preview(store, record.id, application_id="app-1")
    MailBindingAdapter(store.storage).bind_recruitment_mail(preview.payload)


def resolve(service, run_id, record, action="confirmed", **kwargs):
    return service.resolve_confirmation(run_id, thread_id="chat-a", record_id=record.id,
        action=action, content_digest=record.content_digest, background=False, **kwargs)


def test_waiting_is_durable_thread_scoped_and_not_an_active_worker(case, monkeypatch):
    store, records, service = case
    run_id, calls = wait_for_choice(case, monkeypatch)
    before = deepcopy(calls)
    queue = runs.mail_confirmation_queue(store.storage, "chat-a")["runs"]
    assert len(queue) == 1 and len(queue[0]["items"]) == 2
    assert queue[0]["items"][0]["subject"]
    assert queue[0]["status"] not in runs.ACTIVE
    assert runs.mail_confirmation_queue(store.storage, "chat-b") == {"runs": []}
    assert service.wait(run_id, timeout_seconds=0)["confirmation_required"]
    restarted = runs.MailProcessingRunService(store, SimpleNamespace(), SimpleNamespace(write_enabled=True))
    try:
        assert restarted.status(run_id)["run"]["status"] == "awaiting_confirmation"
        assert runs.mail_confirmation_queue(store.storage, "chat-a")["runs"] == queue
        assert calls == before
    finally:
        restarted.close()
    from apps.api.daily_progress import task_progress
    assert task_progress(store.storage, thread_id="chat-a")["run"]["status"] == "awaiting_confirmation"


def test_confirm_resumes_same_run_only_selected_record_and_is_idempotent(case, monkeypatch):
    store, records, service = case
    run_id, calls = wait_for_choice(case, monkeypatch)
    bind(store, records[0])
    first = resolve(service, run_id, records[0])
    assert first["run_id"] == run_id and first["remaining"] == 1
    assert resolve(service, run_id, records[0])["confirmation_version"] == first["confirmation_version"]
    def process(_store, _repo, _settings, *, record_ids, progress, **_kwargs):
        calls.append(record_ids)
        assert record_ids == [records[0].id]
        progress({"result": {"record_id": record_ids[0], "state": "processed_unchanged"}})
        return {"processed": 1}
    monkeypatch.setattr(runs, "process_pending_mail", process)
    assert service.run(run_id)["status"] == "awaiting_confirmation"
    remaining = runs.mail_confirmation_queue(store.storage, "chat-a")["runs"][0]["items"]
    assert [item["record_id"] for item in remaining] == [records[1].id]
    assert len(calls) == 2
    saved = service.status(run_id)["run"]
    assert saved["result_counts"] == {"processed_unchanged": 1, "ambiguous_application": 1}
    assert saved["confirmation_results"][0]["company_name"] == "示例公司"
    assert saved["confirmation_results"][0]["job_title"] == "开发工程师"
    assert saved["confirmation_results"][0]["subject"] == records[0].subject


def test_reject_does_not_bind_delete_or_update_mail_and_summaries_are_leased(case, monkeypatch):
    store, records, service = case
    run_id, _ = wait_for_choice(case, monkeypatch, 1)
    original = store.get(records[0].id)
    result = resolve(service, run_id, records[0], "rejected")
    current = store.get(records[0].id)
    assert (current.application_id, current.processing_status, current.body_text) == (
        original.application_id, original.processing_status, original.body_text)
    assert result["status"] == "completed" and result["rejected"] == 1
    queue = runs.mail_confirmation_queue(store.storage, "chat-a")["runs"][0]
    assert queue["items"] == [] and queue["report_pending"]
    args = {"thread_id": "chat-a", "version": queue["confirmation_version"]}
    first = service.report_confirmation(run_id, **args, action="claim")
    assert first["claimed"]
    assert not service.report_confirmation(run_id, **args, action="claim")["claimed"]
    with pytest.raises(ValueError, match="claim_mismatch"):
        service.report_confirmation(run_id, **args, action="complete", claim_token="wrong")
    service.report_confirmation(run_id, **args, action="release", claim_token=first["claim_token"])
    second = service.report_confirmation(run_id, **args, action="claim")
    assert second["claimed"] and second["claim_token"] != first["claim_token"]
    assert service.report_confirmation(run_id, **args, action="complete", claim_token=second["claim_token"])["completed"]
    assert service.report_confirmation(run_id, **args, action="complete", claim_token=second["claim_token"])["completed"]
    assert runs.mail_confirmation_queue(store.storage, "chat-a") == {"runs": []}


@pytest.mark.parametrize("failure", ["thread", "digest", "record", "unapproved", "cancelled", "source_changed", "disabled"])
def test_resolution_rejects_unauthorized_stale_and_cancelled_work(case, monkeypatch, failure):
    store, records, service = case
    run_id, _ = wait_for_choice(case, monkeypatch, 1)
    args = {"thread_id": "chat-a", "record_id": records[0].id, "action": "confirmed",
        "content_digest": records[0].content_digest, "background": False}
    if failure != "unapproved":
        bind(store, records[0])
    if failure == "thread": args["thread_id"] = "chat-b"
    if failure == "digest": args["content_digest"] = "0" * 64
    if failure == "record": args["record_id"] = records[2].id
    if failure == "cancelled": service.control(run_id, "cancel", background=False)
    if failure == "source_changed":
        with store.storage.write_transaction() as session:
            session.get(RecruitmentMailRecord, records[0].id).content_digest = "f" * 64
    if failure == "disabled": service.settings.write_enabled = False
    with pytest.raises((ValueError, PermissionError)):
        service.resolve_confirmation(run_id, **args)
    assert service.status(run_id)["run"]["confirmation_version"] == 0


def test_new_default_run_skips_pending_scope_but_can_process_new_mail(case, monkeypatch):
    store, records, service = case
    old_id, calls = wait_for_choice(case, monkeypatch)
    with pytest.raises(ValueError, match="waiting_for_confirmation"):
        service.start(record_ids=[records[0].id], thread_id="chat-b", background=False)
    next_id = service.start(thread_id="chat-b", refresh=False, background=False)["run_id"]
    service.run(next_id)
    assert calls[-1] == [records[2].id]
    assert service.status(old_id)["run"]["confirmation_count"] == 2


def test_rejected_same_digest_does_not_reappear_in_next_default_batch(case, monkeypatch):
    store, records, service = case
    run_id, calls = wait_for_choice(case, monkeypatch, 1)
    resolve(service, run_id, records[0], "rejected")
    next_id = service.start(thread_id="chat-a", refresh=False, background=False)["run_id"]
    service.run(next_id)
    assert records[0].id not in calls[-1]
    service.control(next_id, "cancel", background=False)
    explicit = service.start(record_ids=[records[0].id], thread_id="chat-a", refresh=False, background=False)["run_id"]
    service.run(explicit)
    assert calls[-1] == [records[0].id]


def test_confirmation_during_own_worker_runs_after_current_wave(case, monkeypatch):
    store, records, service = case
    run_id = service.start(record_ids=[records[0].id, records[1].id], thread_id="chat-a", background=False)["run_id"]
    calls = []
    def process(_store, _repo, _settings, *, record_ids, progress, **_kwargs):
        calls.append(list(record_ids))
        if len(calls) == 1:
            progress({"result": {"record_id": records[0].id, "state": "ambiguous_application"}})
            bind(store, records[0])
            resolve(service, run_id, records[0])
            progress({"result": {"record_id": records[1].id, "state": "processed"}})
        else:
            assert record_ids == [records[0].id]
            progress({"result": {"record_id": records[0].id, "state": "processed"}})
        return {"processed": len(record_ids)}
    monkeypatch.setattr(runs, "process_pending_mail", process)
    result = service.run(run_id)
    assert result["status"] == "completed" and result["completed"] == 2
    assert len(calls) == 2


def test_other_worker_leaves_confirmation_pending_for_retry(case, monkeypatch):
    store, records, service = case
    run_id, _ = wait_for_choice(case, monkeypatch, 1)
    other = service.start(record_ids=[records[2].id], thread_id="other-chat", background=False)["run_id"]
    bind(store, records[0])
    with pytest.raises(ValueError, match="another_mail_run_is_active"):
        resolve(service, run_id, records[0])
    assert service.status(run_id)["run"]["confirmation_count"] == 1
    service.control(other, "cancel", background=False)
    assert resolve(service, run_id, records[0])["status"] == "accepted"


def test_failed_reprocessing_is_not_resubmitted_by_polls_or_duplicate_confirmation(case, monkeypatch):
    store, records, service = case
    run_id, _ = wait_for_choice(case, monkeypatch, 1)
    bind(store, records[0])
    resolve(service, run_id, records[0])
    count = []
    def failed(*_args, record_ids, progress, **_kwargs):
        count.append(1)
        progress({"result": {"record_id": record_ids[0], "state": "failed", "reason": "model_failed"}})
        return {"processed": 1}
    monkeypatch.setattr(runs, "process_pending_mail", failed)
    assert service.run(run_id)["status"] == "partial"
    assert resolve(service, run_id, records[0])["status"] == "partial"
    service.wait(run_id, timeout_seconds=0)
    assert len(count) == 1


def test_confirm_does_not_retry_other_failed_records(case, monkeypatch):
    store, records, service = case
    calls = []
    def initial(_store, _repo, _settings, *, record_ids, progress, **_kwargs):
        progress({"result": {"record_id": records[0].id, "state": "ambiguous_application"}})
        progress({"result": {"record_id": records[1].id, "state": "failed", "reason": "model_unavailable"}})
        return {"processed": 2}
    monkeypatch.setattr(runs, "process_pending_mail", initial)
    run_id = service.start(record_ids=[records[0].id, records[1].id], thread_id="chat-a", background=False)["run_id"]
    assert service.run(run_id)["status"] == "awaiting_confirmation"
    bind(store, records[0])
    resolve(service, run_id, records[0])
    def continued(*_args, record_ids, progress, **_kwargs):
        calls.extend(record_ids)
        progress({"result": {"record_id": records[0].id, "state": "processed"}})
        return {"processed": 1}
    monkeypatch.setattr(runs, "process_pending_mail", continued)
    result = service.run(run_id)
    assert calls == [records[0].id]
    assert result["failed"] == 1 and result["status"] == "partial"


def test_mcp_missing_thread_fails_before_worker_and_waiting_is_not_completed(monkeypatch):
    monkeypatch.setattr(task_tools, "mail_run_service", lambda *_: pytest.fail("must not construct worker"))
    result = task_tools.recruitment_mail_run_start(task_tools.RecruitmentMailRunStartInput(), SimpleNamespace())
    assert not result.success and result.data["reason"] == "mail_conversation_thread_required"
    data = task_tools._mail_turn_data({"run": {"status": "awaiting_confirmation"}})
    assert data["confirmation_required"] and not data["continuation_required"]
    assert data["execution_mode"] == "waiting_for_user"


def test_api_queue_is_readonly_same_origin_and_resolve_requires_write_authority(case, monkeypatch):
    store, records, service = case
    run_id, _ = wait_for_choice(case, monkeypatch, 1)
    monkeypatch.setattr(main, "get_storage_engine", lambda: store.storage.engine)
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(write_enabled=True, api_token="fixture-secret"))
    monkeypatch.setattr(main, "mail_processing_run_service", lambda: service)
    client = TestClient(main.app, base_url="http://127.0.0.1")
    path = "/api/local-ui/mail-confirmations?thread_id=chat-a"
    assert client.get(path).status_code == 403
    headers = {"X-RecruitOps-Local-UI": "1", "Sec-Fetch-Site": "same-origin"}
    assert client.get(path, headers=headers).json()["runs"][0]["run_id"] == run_id
    payload = {"thread_id": "chat-a", "record_id": records[0].id, "action": "rejected", "content_digest": records[0].content_digest}
    target = f"/api/local-ui/mail-confirmations/{run_id}/resolve"
    assert client.post(target, json=payload).status_code == 401
    assert client.post(target, json=payload, headers={"Authorization": "Bearer fixture-secret"}).status_code == 200


def test_report_expired_lease_allows_retry_but_wrong_thread_and_version_do_not(case, monkeypatch):
    store, records, service = case
    run_id, _ = wait_for_choice(case, monkeypatch, 1)
    resolve(service, run_id, records[0], "rejected")
    args = {"thread_id": "chat-a", "version": 1, "action": "claim"}
    first = service.report_confirmation(run_id, **args)
    with pytest.raises(ValueError, match="thread_mismatch"):
        service.report_confirmation(run_id, **{**args, "thread_id": "chat-b"})
    with pytest.raises(ValueError, match="version_changed"):
        service.report_confirmation(run_id, **{**args, "version": 2})
    with store.storage.write_transaction() as session:
        checkpoint = session.scalar(select(ToolCall).where(ToolCall.task_id == run_id))
        values = deepcopy(checkpoint.arguments)
        values["report_claim"]["until"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        checkpoint.arguments = values
    second = service.report_confirmation(run_id, **args)
    assert second["claimed"] and second["claim_token"] != first["claim_token"]
    with pytest.raises(ValueError, match="claim_mismatch"):
        service.report_confirmation(run_id, thread_id="chat-a", version=1, action="complete", claim_token=first["claim_token"])


def test_late_confirmation_during_completion_is_not_lost(case, monkeypatch):
    store, records, service = case
    run_id = service.start(record_ids=[records[0].id], thread_id="chat-a", background=False)["run_id"]
    calls = []
    def process(_store, _repo, _settings, *, record_ids, progress, **_kwargs):
        calls.append(1)
        progress({"result": {"record_id": records[0].id,
            "state": "ambiguous_application" if len(calls) == 1 else "processed"}})
        return {"processed": 1}
    monkeypatch.setattr(runs, "process_pending_mail", process)
    original = service._mutate
    late = []
    def mutate(identifier, owner, change):
        if change.__name__ == "finished" and not late:
            late.append(True)
            bind(store, records[0])
            resolve(service, identifier, records[0])
        return original(identifier, owner, change)
    monkeypatch.setattr(service, "_mutate", mutate)
    assert service.run(run_id)["status"] == "completed"
    assert len(calls) == 2


def test_schedule_created_before_binding_is_not_lost_from_final_summary(case, monkeypatch):
    store, records, service = case
    run_id, _ = wait_for_choice(case, monkeypatch, 1)
    with store.storage.write_transaction() as session:
        checkpoint = session.scalar(select(ToolCall).where(ToolCall.task_id == run_id))
        values = deepcopy(checkpoint.arguments)
        values["results"][records[0].id]["schedule_item"] = {"id": "fixture-event", "created": True, "time_kind": "unspecified"}
        checkpoint.arguments = values
    bind(store, records[0])
    resolve(service, run_id, records[0])
    def process(*_args, record_ids, progress, **_kwargs):
        progress({"result": {"record_id": records[0].id, "state": "processed_updated",
            "schedule_item": {"id": "fixture-event", "created": False, "time_kind": "unspecified"}}})
        return {"processed": 1}
    monkeypatch.setattr(runs, "process_pending_mail", process)
    summary = service.run(run_id)
    assert summary["schedule_items_created"] == 1
    assert summary["schedule_items_reused"] == 0
    assert summary["schedule_items_time_unconfirmed"] == 1
