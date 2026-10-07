"""Synthetic fixtures for human-approved one-mail/many-application processing."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.approval import ApprovalRegistry
from packages.approval.adapters import AgentApplicationWriteAdapter
from packages.approval.executor import ApprovedWriteExecutor
from packages.approval.persistence import SqlAlchemyApprovalPersistence
from packages.recruitment_mail import EmailMessage, MailIdentity, RecruitmentMailStore
from packages.recruitment_mail.analysis_store import save_model_analysis
from packages.recruitment_mail.binding import (
    BINDING_KEY, binding_candidates, binding_preview, bound_application_ids, confirmed_binding_matches,
)
from packages.recruitment_mail.model_analysis import MAIL_ANALYSIS_VERSION
from packages.recruitment_mail.processing import process_pending_mail, historical_processing_result
from packages.recruitment_mail.run_service import MailProcessingRunService
from packages.recruitment_mail.storage import RecruitmentMailRecord
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import ApplicationSnapshot, Storage
from packages.storage.models import ScheduleEventSnapshot
from packages.tools.recruitment_mail import (
    RecruitmentMailBindingProposeInput, RecruitmentMailDetailInput, RecruitmentMailReviewInput,
    _summary, get_recruitment_mail, review_recruitment_mail,
)
from tests.test_mail_model_processing import Client


def multi_case(tmp_path, event="written_test"):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'multi.db'}", initialize=True)
    store = RecruitmentMailStore(storage)
    body = "示例公司邀请你参加校招统一笔试，请于2026年10月8日14:00参加。"
    record = store.upsert(EmailMessage(identity=MailIdentity(message_id="multi-mail"),
        sender="synthetic-recruitment", subject="示例公司统一笔试通知", body_text=body,
        received_at=datetime(2026, 10, 4, tzinfo=timezone.utc)), source="imap_readonly")
    with storage.write_transaction() as session:
        for identifier, title, stage, company in (
            ("a", "Python开发工程师", "applied", "示例公司"),
            ("b", "C++开发工程师", "applied", "示例公司"),
            ("c", "测试工程师", "interview1", "示例公司"),
            ("d", "算法工程师", "applied", "其他公司"),
        ):
            session.add(ApplicationSnapshot(id=identifier, company_name=company, job_title=title,
                stage=stage, stage_history=[], source="fixture", source_ref=identifier, idempotency_key=identifier))
    proposal = dict(record_id=record.id, content_digest=record.content_digest, company_name="示例公司",
        job_title=None, job_code=None, event_type=event, event_time="2026年10月8日14:00", deadline=None,
        evidence_quotes=[body], candidate_application_id=None, match_reason="公司统一考试", action_summary=None)
    save_model_analysis(store, record.id, record.content_digest, MAIL_ANALYSIS_VERSION, proposal, "proposed")
    registry = ApprovalRegistry(SqlAlchemyApprovalPersistence(storage))
    executor = ApprovedWriteExecutor(registry, AgentApplicationWriteAdapter(storage))
    triage = [dict(record_id=record.id, content_digest=record.content_digest, relevance="relevant", reason="统一考试")]
    return store, store.get(record.id), PostgresRecruitmentRepository(storage), registry, executor, triage, proposal


def approve(store, record, registry, executor, ids, action="bind"):
    preview = binding_preview(store, record.id, application_ids=ids, action=action)
    decision = registry.issue(preview)
    registry.approve(decision.token.token_id)
    executor.execute(decision.token.token_id, operator="local-human")
    return preview


def test_multi_preview_requires_real_human_and_returns_paginated_current_selection(tmp_path):
    store, record, repo, registry, executor, _, _ = multi_case(tmp_path)
    request = RecruitmentMailBindingProposeInput(record_id=record.id, application_ids=["a", "b"],
        content_digest=record.content_digest, binding_revision=0)
    preview = binding_preview(store, request.record_id, application_ids=request.application_ids)
    assert preview.after["application_ids"] == ["a", "b"]
    issued = registry.issue(preview)
    assert bound_application_ids(store.get(record.id)) == []
    with pytest.raises(PermissionError):
        executor.execute(issued.token.token_id, operator="model")
    registry.approve(issued.token.token_id)
    executor.execute(issued.token.token_id, operator="local-human")
    saved = store.get(record.id)
    assert saved.application_id == "a" and bound_application_ids(saved) == ["a", "b"]
    assert all(confirmed_binding_matches(saved, app) for app in repo.list_applications() if app.id in {"a", "b"})
    candidates = binding_candidates(store, record.id, query="测试", limit=1)
    assert candidates["allows_multiple"] and candidates["selection_scope"] == "company_event"
    assert {app["application_id"] for app in candidates["current_applications"]} == {"a", "b"}
    assert candidates["current_application_ids"] == ["a", "b"]
    assert _summary(saved).application_ids == ["a", "b"]
    assert get_recruitment_mail(RecruitmentMailDetailInput(record_id=record.id), store).data.applications[1]["application_id"] == "b"
    review = review_recruitment_mail(RecruitmentMailReviewInput(record_id=record.id), store, repo)
    assert review.data.association.status == "matched_group"
    assert not review.data.association.requires_confirmation
    assert review.data.application_ids == ["a", "b"] and not review.data.approval_previews


@pytest.mark.parametrize("event", ["written_test", "assessment"])
def test_group_process_checks_each_app_keeps_later_stage_and_creates_only_one_schedule(tmp_path, event):
    store, record, repo, registry, executor, triage, proposal = multi_case(tmp_path, event)
    approve(store, record, registry, executor, ["a", "b", "c"])
    client = Client([triage, proposal])
    result = process_pending_mail(store, repo, SimpleNamespace(write_enabled=True), client=client)
    row = result["results"][0]
    assert row["application_ids"] == ["a", "b", "c"]
    assert len(row["application_results"]) == 3
    stages = {app.id: app.stage.value for app in repo.list_applications()}
    assert stages == {"a": "written" if event == "written_test" else "applied",
                      "b": "written" if event == "written_test" else "applied", "c": "interview1", "d": "applied"}
    assert row["application_results"][2]["reason"] == "later_stage_retained"
    assert result["schedule_items_created"] == 1
    schedules = repo.list_schedule()
    assert len(schedules) == 1 and schedules[0].application_ids == ["a", "b", "c"]
    assert len(schedules[0].associated_jobs) == 3
    assert row["schedule_item"]["application_ids"] == ["a", "b", "c"]
    assert process_pending_mail(store, repo, SimpleNamespace(write_enabled=True), client=client)["processed"] == 0
    assert client.calls == 2
    assert store.get(record.id).application_id == "a"


def test_multi_selection_cannot_borrow_specific_job_evidence_or_cross_company(tmp_path):
    store, record, repo, registry, executor, _, proposal = multi_case(tmp_path)
    with pytest.raises(ValueError, match="company_mismatch"):
        binding_preview(store, record.id, application_ids=["a", "d"])
    specific = dict(proposal, job_title="Python开发工程师")
    save_model_analysis(store, record.id, record.content_digest, MAIL_ANALYSIS_VERSION + ":specific-fixture", specific, "proposed")
    assert not binding_candidates(store, record.id)["allows_multiple"]
    with pytest.raises(ValueError, match="single_application"):
        binding_preview(store, record.id, application_ids=["a", "b"])


def test_read_only_review_rejects_legacy_group_with_specific_source_job(tmp_path):
    from packages.recruitment_mail.binding import application_identity, _digest
    store, record, repo, registry, executor, _, proposal = multi_case(tmp_path)
    body = "示例公司邀请Python开发工程师参加笔试，请于2026年10月8日14:00参加。"
    specific = store.upsert(EmailMessage(identity=MailIdentity(message_id="multi-mail"),
        sender=record.sender, subject="示例公司岗位笔试", body_text=body,
        received_at=record.received_at), source="imap_readonly")
    omitted_title_proposal = dict(proposal, content_digest=specific.content_digest, evidence_quotes=[body])
    save_model_analysis(store, specific.id, specific.content_digest, MAIL_ANALYSIS_VERSION,
                        omitted_title_proposal, "proposed")
    # Legacy/corrupt group metadata can exist without today's approval guards.
    # The read-only projection must recheck source applicability, not certify it.
    apps = [app for app in repo.list_applications() if app.id in {"a", "b"}]
    with store.storage.write_transaction() as session:
        row = session.get(RecruitmentMailRecord, specific.id)
        row.application_id = "a"
        metadata = dict(row.raw_metadata)
        metadata[BINDING_KEY] = {"revision": 1, "state": "bound", "authority": "human_approval",
            "approval_key": "legacy-fixture", "content_digest": specific.content_digest,
            "application_id": "a", "application_ids": ["a", "b"],
            "identity_digests": {app.id: _digest(application_identity(app)) for app in apps}}
        row.raw_metadata = metadata
    before = {app.id: app.stage for app in repo.list_applications()}
    review = review_recruitment_mail(RecruitmentMailReviewInput(record_id=specific.id), store, repo)
    assert review.success
    assert review.data.association.status == "review_required"
    assert review.data.association.requires_confirmation
    assert review.data.association.review_reasons == ["multi_binding_event_scope_invalid"]
    assert review.data.approval_previews == [] and review.data.association.schedule_drafts == []
    assert {app.id: app.stage for app in repo.list_applications()} == before
    assert repo.list_schedule() == []


def test_group_correct_and_unbind_replace_all_ids_and_existing_schedule(tmp_path):
    store, record, repo, registry, executor, triage, proposal = multi_case(tmp_path)
    old = approve(store, record, registry, executor, ["a", "b"])
    process_pending_mail(store, repo, SimpleNamespace(write_enabled=True), client=Client([triage, proposal]))
    approve(store, store.get(record.id), registry, executor, ["b", "c"], action="correct")
    assert bound_application_ids(store.get(record.id)) == ["b", "c"]
    assert repo.list_schedule()[0].application_ids == ["b", "c"]
    with pytest.raises(ValueError):
        AgentApplicationWriteAdapter(store.storage).bind_recruitment_mail(old.payload)
    approve(store, store.get(record.id), registry, executor, [], action="unbind")
    assert store.get(record.id).application_id is None
    assert bound_application_ids(store.get(record.id)) == []
    assert repo.list_schedule()[0].application_ids == []
    assert len(repo.list_schedule()) == 1


def test_secondary_identity_change_blocks_entire_approval_and_writer_stays_disabled(tmp_path):
    store, record, repo, registry, executor, triage, proposal = multi_case(tmp_path)
    preview = binding_preview(store, record.id, application_ids=["a", "b"])
    token = registry.issue(preview).token
    registry.approve(token.token_id)
    with store.storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "b").job_title = "Different role"
    with pytest.raises(RuntimeError):
        executor.execute(token.token_id, operator="local-human")
    assert not bound_application_ids(store.get(record.id))
    result = process_pending_mail(store, repo, SimpleNamespace(write_enabled=False), client=Client([triage, proposal]))
    assert result["status"] == "blocked" and not repo.list_schedule()


def test_one_failed_application_does_not_discard_other_success_receipts(tmp_path, monkeypatch):
    store, record, repo, registry, executor, triage, proposal = multi_case(tmp_path)
    approve(store, record, registry, executor, ["a", "b"])
    from packages.tools import application_status_update as writer
    original = writer.update_application_status
    def partial(request, *args, **kwargs):
        if request.application_id == "b":
            return SimpleNamespace(success=False, data=None, error_message="synthetic conflict",
                                   model_dump=lambda **_kwargs: {"success": False})
        return original(request, *args, **kwargs)
    monkeypatch.setattr(writer, "update_application_status", partial)
    result = process_pending_mail(store, repo, SimpleNamespace(write_enabled=True), client=Client([triage, proposal]))
    row = result["results"][0]
    assert row["state"] == "failed_terminal" and row["schedule_item"]["created"]
    assert [r["state"] for r in row["application_results"]] == ["updated", "blocked"]
    assert {app.id: app.stage.value for app in repo.list_applications()}["a"] == "written"
    assert len(repo.list_schedule()) == 1


def test_unbind_after_schedule_deletion_clears_metadata_and_cannot_resurrect_old_jobs(tmp_path):
    store, record, repo, registry, executor, triage, proposal = multi_case(tmp_path, "assessment")
    approve(store, record, registry, executor, ["a", "b"])
    process_pending_mail(store, repo, SimpleNamespace(write_enabled=True), client=Client([triage, proposal]))
    with store.storage.write_transaction() as session:
        session.delete(session.scalar(select(ScheduleEventSnapshot)))
    approve(store, store.get(record.id), registry, executor, [], action="unbind")
    assert store.get(record.id).raw_metadata["schedule_associations"]["application_ids"] == []
    process_pending_mail(store, repo, SimpleNamespace(write_enabled=True), client=Client([triage, proposal]))
    assert len(repo.list_schedule()) == 1
    assert repo.list_schedule()[0].application_ids == []
    assert repo.list_schedule()[0].associated_jobs == []


def test_unexpected_second_write_failure_preserves_first_result_and_unexecuted_targets(tmp_path, monkeypatch):
    store, record, repo, registry, executor, triage, proposal = multi_case(tmp_path)
    approve(store, record, registry, executor, ["a", "b", "c"])
    from packages.tools import application_status_update as writer
    original = writer.update_application_status
    def fail(request, *args, **kwargs):
        if request.application_id == "b":
            raise RuntimeError("synthetic failure with private content")
        return original(request, *args, **kwargs)
    monkeypatch.setattr(writer, "update_application_status", fail)
    result = process_pending_mail(store, repo, SimpleNamespace(write_enabled=True), client=Client([triage, proposal]))
    row = result["results"][0]
    assert [r["state"] for r in row["application_results"]] == ["updated", "blocked", "not_executed"]
    assert row["schedule_item"]["application_ids"] == ["a", "b", "c"]
    history = historical_processing_result(store.get(record.id))
    assert history["application_results"] == row["application_results"]
    assert history["schedule_item"]["id"] == row["schedule_item"]["id"]
    assert "private content" not in str(history)
    assert {app.id: app.stage.value for app in repo.list_applications()}["a"] == "written"


def test_cooperative_stop_keeps_committed_receipt_and_does_not_finish_entire_mail(tmp_path, monkeypatch):
    store, record, repo, registry, executor, triage, proposal = multi_case(tmp_path)
    approve(store, record, registry, executor, ["a", "b"])
    from packages.tools import application_status_update as writer
    original = writer.update_application_status
    stopped = False
    def write_once(request, *args, **kwargs):
        nonlocal stopped
        outcome = original(request, *args, **kwargs)
        stopped = True
        return outcome
    monkeypatch.setattr(writer, "update_application_status", write_once)
    result = process_pending_mail(store, repo, SimpleNamespace(write_enabled=True),
        client=Client([triage, proposal]), should_stop=lambda: stopped)
    assert result["status"] == "partial" and result["processed"] == 0
    saved = store.get(record.id)
    assert saved.processing_status == "pending"
    assert [r["state"] for r in historical_processing_result(saved)["application_results"]] == ["updated", "not_executed"]
    assert {app.id: app.stage.value for app in repo.list_applications()}["a"] == "written"


def test_confirmed_primary_order_is_stable_when_repository_order_differs(tmp_path):
    store, record, repo, registry, executor, triage, proposal = multi_case(tmp_path)
    approve(store, record, registry, executor, ["b", "a"])
    result = process_pending_mail(store, repo, SimpleNamespace(write_enabled=True), client=Client([triage, proposal]))
    row = result["results"][0]
    assert row["application_ids"] == ["b", "a"] and row["application_id"] == "b"
    assert row["write_result"]["data"]["application_id"] == "b"
    assert repo.list_schedule()[0].application_id == "b"
    assert repo.list_schedule()[0].application_ids == ["b", "a"]


def test_edit_shared_schedule_note_time_or_status_preserves_group_identity(tmp_path):
    from datetime import date
    from packages.tools.schedule_manage import ScheduleManager, ScheduleEventPatchFields, ScheduleValidationError
    store, record, repo, registry, executor, triage, proposal = multi_case(tmp_path)
    approve(store, record, registry, executor, ["a", "b"])
    process_pending_mail(store, repo, SimpleNamespace(write_enabled=True), client=Client([triage, proposal]))
    before = repo.list_schedule()[0]
    manager = ScheduleManager(store.storage)
    after = manager.update(before.id, ScheduleEventPatchFields(note="changed", event_date=date(2026, 10, 9), status="completed")).event
    assert after.job_title == before.job_title
    assert after.application_ids == before.application_ids == ["a", "b"]
    assert after.associated_jobs == before.associated_jobs
    assert after.status == "completed"
    with pytest.raises(ScheduleValidationError, match="mail binding confirmation"):
        manager.update(before.id, ScheduleEventPatchFields(application_id="a"))


def test_multi_confirmation_resumes_one_original_record_once(tmp_path):
    store, record, repo, registry, executor, triage, proposal = multi_case(tmp_path)
    service = MailProcessingRunService(store, repo, SimpleNamespace(write_enabled=True), client=Client([triage, proposal]))
    try:
        run_id = service.start(record_ids=[record.id], refresh=False, thread_id="multi-chat", background=False)["run_id"]
        assert service.run(run_id)["status"] == "awaiting_confirmation"
        approve(store, store.get(record.id), registry, executor, ["a", "b"])
        args = dict(thread_id="multi-chat", record_id=record.id, action="confirmed", content_digest=record.content_digest, background=False)
        settled = service.resolve_confirmation(run_id, **args)
        assert settled["remaining"] == 1
        assert settled["confirmation_results"][0]["application_ids"] == ["a", "b"]
        assert service.resolve_confirmation(run_id, **args)["confirmation_version"] == 1
        service.client = Client([triage, proposal])
        done = service.run(run_id)
        assert done["status"] == "completed" and done["processed"] == 1
        assert len(done["confirmation_results"][0]["application_results"]) == 2
        assert done["schedule_items_created"] == 1
        assert len(repo.list_schedule()) == 1
    finally:
        service.close()
