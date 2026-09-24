from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.recruitment_mail import EmailMessage, MailIdentity, RecruitmentMailStore
from packages.recruitment_mail import run_service as runs
from packages.recruitment_mail.storage import RecruitmentMailRecord
from packages.storage import Storage
from packages.storage.models import TaskRun, ToolCall


def case(count=12):
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    store = RecruitmentMailStore(storage)
    records = [store.upsert(EmailMessage(identity=MailIdentity(message_id=f"fixture-{index}"),
        subject=f"招聘通知 {index}", body_text="这是用于离线测试的招聘通知。",
        received_at=datetime(2026, 9, 20, tzinfo=timezone.utc))) for index in range(count)]
    repo = SimpleNamespace(list_applications=lambda: [])
    service = runs.MailProcessingRunService(store, repo, SimpleNamespace(write_enabled=True))
    return store, records, service


def processor(calls, after_item=None):
    def process(store, _repo, _settings, *, record_ids, progress, should_stop, **_kwargs):
        calls.append(list(record_ids))
        count = 0
        progress({"phase": "triage"})
        for identifier in record_ids:
            if should_stop():
                break
            store.update_processing_status(identifier, "processed")
            progress({"phase": "analysis", "result": {"record_id": identifier, "state": "processed"}})
            count += 1
            if after_item:
                after_item(identifier)
        return {"status": "completed", "processed": count}
    return process


def test_start_background_contract_and_readonly_status_never_sync_or_process(monkeypatch):
    store, records, service = case(1)
    calls = []
    service.sync_mail = lambda: calls.append("sync")
    monkeypatch.setattr(runs, "process_pending_mail", lambda *_args, **_kwargs: pytest.fail("status invoked model"))
    assert service.status() == {"runs": [], "run": None}
    accepted = service.start(background=False, thread_id="thread-a")
    for _ in range(3):
        status = service.status(accepted["run_id"])["run"]
        assert status["status"] == "accepted"
        assert status["unit"] == "封"
    assert calls == []
    assert service.status(thread_id="another") == {"runs": [], "run": None}


@pytest.mark.parametrize("count", [23, 51, 86, 120])
def test_one_stable_run_frozen_scope_bounded_waves_and_results(monkeypatch, count):
    store, records, service = case(count)
    calls = []
    monkeypatch.setattr(runs, "process_pending_mail", processor(calls))
    accepted = service.start(background=False, thread_id="thread-a")
    result = service.run(accepted["run_id"])
    assert result["run_id"] == accepted["run_id"]
    assert result["status"] == "completed"
    assert result["completed"] == result["total"] == count
    assert result["remaining"] == 0
    assert [len(call) for call in calls] == [min(10, count - offset) for offset in range(0, count, 10)]
    assert len({identifier for call in calls for identifier in call}) == count
    assert service.status() == {"runs": [], "run": None}


def test_pause_resume_keeps_counts_and_excludes_new_mail(monkeypatch):
    store, records, service = case(12)
    calls = []
    accepted = service.start(background=False, thread_id="old-thread")
    run_id = accepted["run_id"]
    paused = False
    def stop_once(_identifier):
        nonlocal paused
        if not paused:
            paused = True
            service.control(run_id, "pause", background=False)
    monkeypatch.setattr(runs, "process_pending_mail", processor(calls, stop_once))
    first = service.run(run_id)
    assert first["status"] == "paused"
    assert first["completed"] == 1 and first["remaining"] == 11
    extra = store.upsert(EmailMessage(identity=MailIdentity(message_id="new-after-pause"), subject="新邮件", body_text="后来才同步的邮件"))
    resumed = service.control(run_id, "resume", thread_id="new-thread", turn_id="turn2", background=False)
    assert resumed["run_id"] == run_id and resumed["completed"] == 1
    second = service.run(run_id)
    assert second["status"] == "completed"
    assert second["completed"] == second["total"] == 12
    assert second["thread_id"] == "new-thread"
    assert extra.id not in {identifier for call in calls for identifier in call}
    assert store.get(extra.id).processing_status == "pending"


def test_cancel_before_start_runs_no_model_and_can_explicitly_resume(monkeypatch):
    store, records, service = case(1)
    calls = []
    monkeypatch.setattr(runs, "process_pending_mail", processor(calls))
    run_id = service.start(background=False)["run_id"]
    cancelled = service.control(run_id, "cancel", background=False)
    assert cancelled["status"] == "cancelled"
    service.run(run_id)
    assert calls == []
    service.control(run_id, "resume", background=False)
    assert service.run(run_id)["completed"] == 1


def test_changed_source_after_freeze_is_checkpointed_without_processing(monkeypatch):
    store, records, service = case(2)
    calls = []
    run_id = service.start(background=False)["run_id"]
    def pause_after_one(identifier):
        service.control(run_id, "pause", background=False)
    monkeypatch.setattr(runs, "process_pending_mail", processor(calls, pause_after_one))
    assert service.run(run_id)["completed"] == 1
    with store.storage.session() as session:
        checkpoint = session.scalar(select(ToolCall).where(ToolCall.task_id == run_id))
        unfinished = next(item["record_id"] for item in checkpoint.arguments["scope"]
                          if item["record_id"] not in checkpoint.arguments["results"])
    with store.storage.write_transaction() as session:
        session.get(RecruitmentMailRecord, unfinished).content_digest = "a" * 64
    service.control(run_id, "resume", background=False)
    result = service.run(run_id)
    assert result["status"] == "partial" and result["blocked"] == 1
    assert result["completed"] == 2
    assert len(calls) == 1


def test_run_remains_partial_for_unresolved_records_without_retrying_model(monkeypatch):
    store, records, service = case(1)
    store.update_processing_status(records[0].id, "ambiguous_application")
    calls = []
    def terminal(*_args, **_kwargs):
        calls.append("batch-read")
        return {"processed": 0, "status": "partial"}
    monkeypatch.setattr(runs, "process_pending_mail", terminal)
    run_id = service.start(background=False)["run_id"]
    result = service.run(run_id)
    assert result["status"] == "partial"
    assert result["blocked"] == 1 and result["remaining"] == 0
    assert len(calls) == 1


def test_second_thread_cannot_silently_take_over_existing_mail_run():
    store, records, service = case(1)
    first = service.start(background=False, thread_id="a")
    assert service.start(background=False, thread_id="a")["run_id"] == first["run_id"]
    with pytest.raises(ValueError, match="another_mail_run_is_active"):
        service.start(background=False, thread_id="b")


def test_background_failure_reports_safe_code_and_preserves_pending(monkeypatch):
    store, records, service = case(1)
    def fail(*_args, **_kwargs):
        raise RuntimeError("secret body/credential must not be written")
    monkeypatch.setattr(runs, "process_pending_mail", fail)
    run_id = service.start(background=False)["run_id"]
    result = service.run(run_id)
    assert result["status"] == "failed" and result["can_resume"]
    assert "secret body" not in str(result)
    assert store.get(records[0].id).processing_status == "pending"


def test_status_lists_all_recoverable_candidates_without_starting_any_worker():
    store, records, service = case(1)
    identifiers = []
    for status in ("paused", "stopped", "failed", "cancelled"):
        run_id = service.start(background=False, thread_id="same-thread")["run_id"]
        identifiers.append(run_id)
        with store.storage.write_transaction() as session:
            session.get(TaskRun, run_id).status = status
    result = runs.project_mail_progress(store.storage, thread_id="same-thread", include_recoverable=True)
    assert {item["run_id"] for item in result["runs"]} == set(identifiers)
    assert result["run"] is None
    assert all(item["can_resume"] for item in result["runs"])
    assert not service.status()["runs"]


@pytest.mark.parametrize("initial", ["accepted", "running", "stopped"])
def test_prior_desktop_boot_is_discoverable_resumable_and_does_not_block_start(monkeypatch, initial):
    monkeypatch.setenv("RECRUITOPS_DESKTOP_RUN_ID", "boot-one")
    store, records, service = case(1)
    old_id = service.start(background=False, thread_id="old-thread")["run_id"]
    with store.storage.write_transaction() as session:
        session.get(TaskRun, old_id).status = initial
        checkpoint = session.scalar(select(ToolCall).where(ToolCall.task_id == old_id))
        checkpoint.arguments = dict(checkpoint.arguments, owner="previous-process",
            lease_until=(datetime.now(timezone.utc) + timedelta(seconds=180)).isoformat())
    monkeypatch.setenv("RECRUITOPS_DESKTOP_RUN_ID", "boot-two")
    other_service = runs.MailProcessingRunService(store, service.repository, service.settings)
    prior = runs.project_mail_progress(store.storage, include_recoverable=True)["run"]
    assert prior["can_resume"] and prior["can_cancel"]
    calls = []
    monkeypatch.setattr(runs, "process_pending_mail", processor(calls))
    other_service.control(old_id, "resume", thread_id="new-thread", background=False)
    assert other_service.run(old_id)["completed"] == 1
    assert len(calls) == 1
    assert other_service.start(background=False, thread_id="another")["run_id"] != old_id


def test_two_services_share_live_claim_and_pause_waits_until_inflight_finishes(monkeypatch):
    store, records, first = case(2)
    second = runs.MailProcessingRunService(store, first.repository, first.settings)
    run_id = first.start(background=False, thread_id="thread")["run_id"]
    calls = []
    def pause_from_other_service(identifier):
        assert second.start(background=False, thread_id="thread")["run_id"] == run_id
        assert second.run(run_id) is None
        paused = second.control(run_id, "pause", background=False)
        assert paused["status"] == "pausing"
        assert second.control(run_id, "resume", background=False)["status"] == "pausing"
    monkeypatch.setattr(runs, "process_pending_mail", processor(calls, pause_from_other_service))
    assert first.run(run_id)["status"] == "paused"
    assert len(calls) == 1
    assert first.status(run_id)["run"]["completed"] == 1
    second.control(run_id, "resume", background=False)
    monkeypatch.setattr(runs, "process_pending_mail", processor(calls))
    assert second.run(run_id)["completed"] == 2


def test_resume_cannot_overlap_a_different_active_run():
    store, records, service = case(1)
    old_id = service.start(background=False)["run_id"]
    service.control(old_id, "pause", background=False)
    service.start(background=False)
    with pytest.raises(ValueError, match="another_mail_run_is_active"):
        service.control(old_id, "resume", background=False)


def test_wait_zero_is_readonly_and_does_not_start_an_accepted_run(monkeypatch):
    store, records, service = case(1)
    run_id = service.start(background=False)["run_id"]
    service.sync_mail = lambda: pytest.fail("wait synchronized mail")
    monkeypatch.setattr(runs, "process_pending_mail", lambda *_args, **_kwargs: pytest.fail("wait processed mail"))
    monkeypatch.setattr(runs, "sleep", lambda _seconds: pytest.fail("zero wait slept"))
    result = service.wait(run_id, timeout_seconds=0)
    assert result["status"] == "accepted" and result["completed"] == 0
    assert service._futures == {}
    assert store.get(records[0].id).processing_status == "pending"


def test_wait_timeout_returns_actual_running_status_within_bound(monkeypatch):
    store, records, service = case(1)
    run_id = service.start(background=False)["run_id"]
    elapsed = [0.0]
    waits = []
    monkeypatch.setattr(runs, "monotonic", lambda: elapsed[0])
    def advance(seconds):
        waits.append(seconds)
        elapsed[0] += seconds
    monkeypatch.setattr(runs, "sleep", advance)
    result = service.wait(run_id, timeout_seconds=1.2)
    assert result["status"] == "accepted"
    assert result["run_id"] == run_id
    assert elapsed[0] == pytest.approx(1.2)
    assert len(waits) == 3 and max(waits) <= 0.5
    assert service._futures == {}


@pytest.mark.parametrize("status", ["completed", "partial", "failed", "paused", "cancelled", "interrupted"])
def test_wait_returns_every_terminal_or_recoverable_state_without_sleep(monkeypatch, status):
    store, records, service = case(1)
    run_id = service.start(background=False)["run_id"]
    with store.storage.write_transaction() as session:
        session.get(TaskRun, run_id).status = status
    monkeypatch.setattr(runs, "sleep", lambda _seconds: pytest.fail("terminal wait slept"))
    assert service.wait(run_id)["status"] == status


@pytest.mark.parametrize("timeout", [-1, 20.01, float("nan"), float("inf"), "20", True])
def test_wait_rejects_unbounded_or_invalid_timeouts(timeout):
    store, records, service = case(1)
    with pytest.raises(ValueError, match="mail_wait_timeout_out_of_range"):
        runs.wait_mail_progress(store.storage, timeout_seconds=timeout)


def test_wait_observes_completion_written_by_another_service(monkeypatch):
    store, records, first = case(12)
    second = runs.MailProcessingRunService(store, first.repository, first.settings)
    run_id = first.start(background=False)["run_id"]
    calls = []
    monkeypatch.setattr(runs, "process_pending_mail", processor(calls))
    # The reader only waits; a separate, explicit worker advances durable state.
    monkeypatch.setattr(runs, "sleep", lambda _seconds: second.run(run_id))
    result = first.wait(run_id)
    assert result["status"] == "completed"
    assert result["completed"] == result["total"] == 12
    assert [len(call) for call in calls] == [10, 2]
    assert first._futures == second._futures == {}


def test_wait_fixes_selected_run_instead_of_following_a_replacement(monkeypatch):
    store, records, service = case(1)
    run_id = service.start(background=False, thread_id="thread")["run_id"]
    replacement = []
    def replace(_seconds):
        service.control(run_id, "cancel", background=False)
        replacement.append(service.start(background=False, thread_id="thread")["run_id"])
    monkeypatch.setattr(runs, "sleep", replace)
    result = runs.wait_mail_progress(store.storage, thread_id="thread")
    assert result["run"]["run_id"] == run_id
    assert result["run"]["status"] == "cancelled"
    assert len(replacement) == 1 and replacement[0] != run_id


def test_wait_without_selected_run_returns_immediately_even_if_ambiguous(monkeypatch):
    store, records, service = case(1)
    monkeypatch.setattr(runs, "sleep", lambda _seconds: pytest.fail("unselected wait slept"))
    assert runs.wait_mail_progress(store.storage) == {"runs": [], "run": None}
    ambiguous = {"runs": [{"run_id": "first"}, {"run_id": "second"}], "run": None}
    monkeypatch.setattr(runs, "project_mail_progress", lambda *_args: ambiguous)
    assert runs.wait_mail_progress(store.storage) == ambiguous


def test_service_wait_does_not_hide_uncaught_worker_failure():
    store, records, service = case(1)
    run_id = service.start(background=False)["run_id"]
    failed = Future()
    failed.set_exception(RuntimeError("worker_storage_failure"))
    service._futures[run_id] = failed
    with pytest.raises(RuntimeError, match="worker_storage_failure"):
        service.wait(run_id, timeout_seconds=0)


def test_wait_preserves_failed_result_and_missing_run_is_not_success(monkeypatch):
    store, records, service = case(1)
    def fail(*_args, **_kwargs):
        raise RuntimeError("private mail text")
    monkeypatch.setattr(runs, "process_pending_mail", fail)
    run_id = service.start(background=False)["run_id"]
    service.run(run_id)
    result = service.wait(run_id)
    assert result["status"] == "failed" and result["can_resume"]
    assert result["completed"] == 0 and result["remaining"] == 1
    assert "private mail text" not in str(result)
    with pytest.raises(KeyError, match="mail_run_not_found"):
        service.wait("missing", timeout_seconds=0)
