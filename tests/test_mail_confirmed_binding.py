from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from packages.approval import ApprovalRegistry
from packages.approval.adapters import AgentApplicationWriteAdapter
from packages.approval.executor import ApprovedWriteExecutor
from packages.approval.persistence import SqlAlchemyApprovalPersistence
from packages.domain.models import Application, ApplicationStage
from packages.recruitment_mail import EmailMessage, MailIdentity, RecruitmentMailStore
from packages.recruitment_mail.analysis_binding import model_application_matches
from packages.recruitment_mail.analysis_store import save_model_analysis, sync_analysis_labels
from packages.recruitment_mail.binding import binding_candidates, binding_preview, confirmed_binding_matches
from packages.recruitment_mail.model_analysis import MAIL_ANALYSIS_VERSION
from packages.recruitment_mail.presentation import mail_semantics
from packages.recruitment_mail.storage import RecruitmentMailRecord
from packages.storage import ApplicationSnapshot, Storage, JobSnapshot
from packages.tools.application_status_update import ApplicationStatusUpdateInput, update_application_status
from packages.tools.recruitment_mail import RecruitmentMailBindingProposeInput, recruitment_mail_binding_propose, _summary


def setup_binding(auth=True):
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    store = RecruitmentMailStore(storage)
    record = store.upsert(EmailMessage(identity=MailIdentity(message_id="binding-fixture"),
        sender="mail@example.test", subject="简称公司 测试岗 面试邀请",
        body_text="简称公司邀请测试岗参加面试，请按邮件安排参加。",
        received_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        source_metadata={"authentication_results": [{"method": "dkim", "result": "pass", "aligned": True,
                                                   "authserv_id": "example.test"}] if auth else []}),
        source="imap_readonly")
    apps = [Application(id="a", company_name="完整企业名称有限公司", job_title="软件测试工程师", stage=ApplicationStage.APPLIED,
                        source="fixture", source_ref="a", idempotency_key="a"),
            Application(id="b", company_name="简称公司", job_title="测试岗", stage=ApplicationStage.APPLIED,
                        source="fixture", source_ref="b", idempotency_key="b")]
    with storage.write_transaction() as session:
        for app in apps:
            session.add(ApplicationSnapshot(id=app.id, company_name=app.company_name, job_title=app.job_title,
                stage=app.stage.value, source=app.source, source_ref=app.source_ref, idempotency_key=app.idempotency_key))
    proposal = {"record_id": record.id, "content_digest": record.content_digest, "company_name": "简称公司",
        "job_title": "测试岗", "event_type": "interview", "evidence_quotes": [record.body_text],
        "candidate_application_id": "b", "match_reason": "source proposal"}
    save_model_analysis(store, record.id, record.content_digest, MAIL_ANALYSIS_VERSION, proposal, "proposed")
    repo = SimpleNamespace(list_applications=lambda: apps, get_job=lambda _id: None)
    registry = ApprovalRegistry(SqlAlchemyApprovalPersistence(storage))
    executor = ApprovedWriteExecutor(registry, AgentApplicationWriteAdapter(storage))
    return store, store.get(record.id), apps, proposal, repo, registry, executor


def confirm(store, record, registry, executor, *, target="a", action="bind"):
    preview = binding_preview(store, record.id, application_id=target, action=action)
    issued = registry.issue(preview)
    token_id = issued.token.token_id
    assert registry.approve(token_id).allowed
    executor.execute(token_id, operator="local-human")
    return preview, token_id


def test_preview_and_pending_approval_never_bind_and_model_cannot_confirm_itself():
    store, record, apps, proposal, repo, registry, executor = setup_binding()
    request = RecruitmentMailBindingProposeInput(record_id=record.id, application_id="a",
        content_digest=record.content_digest, binding_revision=0)
    result = recruitment_mail_binding_propose(request, store, registry)
    assert result.data["approval_status"] == "pending"
    assert store.get(record.id).application_id is None
    with pytest.raises(PermissionError):
        executor.execute(result.data["approval_id"], operator="model")
    with pytest.raises(ValidationError):
        RecruitmentMailBindingProposeInput(**request.model_dump(), confirmed=True)


def test_human_binding_overrides_names_and_model_target_but_never_applies_to_b():
    store, record, apps, proposal, repo, registry, executor = setup_binding()
    confirm(store, record, registry, executor)
    saved = store.get(record.id)
    assert model_application_matches(saved, proposal, apps[0])
    assert not model_application_matches(saved, proposal, apps[1])
    wrong = update_application_status(ApplicationStatusUpdateInput(application_id="b", evidence_type="mail",
        evidence_id=record.id, target_status="interview1"), repo, store)
    assert not wrong.success
    # The rejected cross-target attempt is terminal, but it cannot erase the
    # confirmed identity. A fresh explicit corrected binding grants reprocessing.
    confirm(store, store.get(record.id), registry, executor, action="correct")
    result = update_application_status(ApplicationStatusUpdateInput(application_id="a", evidence_type="mail",
        evidence_id=record.id, target_status="interview1"), repo, store)
    assert result.success and result.data.wrote
    with store.storage.session() as session:
        assert session.get(ApplicationSnapshot, "a").stage == "interview1"
        assert session.get(ApplicationSnapshot, "b").stage == "applied"


def test_confirmation_does_not_skip_sender_authentication():
    store, record, apps, proposal, repo, registry, executor = setup_binding(auth=False)
    confirm(store, record, registry, executor)
    result = update_application_status(ApplicationStatusUpdateInput(application_id="a", evidence_type="mail",
        evidence_id=record.id, target_status="interview1"), repo, store)
    assert not result.success
    assert store.get(record.id).processing_status == "needs_auth_metadata"
    with store.storage.session() as session:
        assert session.get(ApplicationSnapshot, "a").stage == "applied"


def test_unbinding_revokes_old_identity_without_text_fallback_and_old_approval_replay():
    store, record, apps, proposal, repo, registry, executor = setup_binding()
    preview, token_id = confirm(store, record, registry, executor)
    confirm(store, store.get(record.id), registry, executor, target=None, action="unbind")
    saved = store.get(record.id)
    assert saved.application_id is None
    assert confirmed_binding_matches(saved, apps[0]) is False
    assert model_application_matches(saved, proposal, apps[1]) is False
    with pytest.raises(PermissionError):
        executor.execute(token_id, operator="local-human")
    with pytest.raises(ValueError):
        AgentApplicationWriteAdapter(store.storage).bind_recruitment_mail(preview.payload)
    assert len(executor.records()) == 2
    assert len(saved.raw_metadata["binding_recent_history"]) == 2


@pytest.mark.parametrize("change", ["mail", "application", "binding"])
def test_approval_snapshot_change_blocks_execution(change):
    store, record, apps, proposal, repo, registry, executor = setup_binding()
    preview = binding_preview(store, record.id, application_id="a")
    token = registry.issue(preview).token
    registry.approve(token.token_id)
    with store.storage.write_transaction() as session:
        if change == "mail":
            session.get(RecruitmentMailRecord, record.id).content_digest = "a" * 64
        elif change == "application":
            session.get(ApplicationSnapshot, "a").job_title = "different role"
        else:
            session.get(RecruitmentMailRecord, record.id).application_id = "b"
    with pytest.raises(RuntimeError):
        executor.execute(token.token_id, operator="local-human")
    assert store.get(record.id).application_id != "a"


def test_expired_confirmation_cannot_execute():
    store, record, apps, proposal, repo, registry, executor = setup_binding()
    preview = binding_preview(store, record.id, application_id="a")
    token = registry.issue(preview).token
    registry.approve(token.token_id)
    with pytest.raises(PermissionError):
        executor.execute(token.token_id, operator="local-human", now=datetime.now(timezone.utc) + timedelta(hours=1))
    assert store.get(record.id).application_id is None


def test_confirmed_scope_is_single_mail_and_identity_edit_invalidates_confirmation():
    store, record, apps, proposal, repo, registry, executor = setup_binding()
    confirm(store, record, registry, executor)
    other = store.upsert(EmailMessage(identity=MailIdentity(message_id="other"), subject=record.subject,
        body_text=record.body_text, sender=record.sender), source="imap_readonly")
    assert other.application_id is None
    assert confirmed_binding_matches(other, apps[0]) is None
    assert confirmed_binding_matches(store.get(record.id), apps[0].model_copy(update={"job_title": "changed"})) is False


def test_candidates_are_read_only_and_explicit_search_can_find_name_mismatch():
    store, record, apps, proposal, repo, registry, executor = setup_binding()
    candidates = binding_candidates(store, record.id)
    assert [item["application_id"] for item in candidates["candidates"]] == ["b"]
    assert binding_candidates(store, record.id, query="完整企业")["candidates"][0]["application_id"] == "a"
    assert store.get(record.id).application_id is None


def test_ats_candidates_require_company_code_and_platform_tenant_and_keep_ambiguity():
    store, record, apps, proposal, repo, registry, executor = setup_binding()
    with store.storage.write_transaction() as session:
        row = session.get(RecruitmentMailRecord, record.id)
        row.body_text += " 岗位编号 J1234 https://ats.example.test/tenant1/jobs/J1234"
        metadata = dict(row.raw_metadata)
        analysis = dict(metadata["model_analysis"])
        analysis["payload"] = dict(analysis["payload"], job_code="J1234")
        metadata["model_analysis"] = analysis
        row.raw_metadata = metadata
        session.add(JobSnapshot(id="job", company_id="company", title="不同展示标题", detail_url="https://ats.example.test/tenant1/jobs/J1234",
            cohort_status="confirmed", batch="formal", source="fixture", source_ref="job", source_platform="fixtureats",
            source_tenant="tenant1", native_job_id="J1234"))
        for identifier in ("b", "c"):
            if identifier == "b":
                session.get(ApplicationSnapshot, "b").job_id = "job"
            else:
                session.add(ApplicationSnapshot(id="c", company_name="简称公司", job_title="不同展示标题", job_id="job",
                    stage="applied", source="fixture", source_ref="c", idempotency_key="c"))
    candidates = binding_candidates(store, record.id)
    strong = [item for item in candidates["candidates"] if item["reason"] == "ats_identity_candidate"]
    assert {item["application_id"] for item in strong} == {"b", "c"}
    assert candidates["requires_user_confirmation"]
    assert store.get(record.id).application_id is None
    with store.storage.write_transaction() as session:
        session.get(JobSnapshot, "job").source_tenant = "another_tenant"
    assert not any(item["reason"] == "ats_identity_candidate" for item in binding_candidates(store, record.id)["candidates"])


def test_unassessed_and_analyzed_mail_do_not_present_legacy_zero_as_confidence():
    store, record, apps, proposal, repo, registry, executor = setup_binding()
    assert _summary(record).confidence is None
    assert _summary(record).analysis_state == "analyzed"
    assert _summary(record).legacy_confidence == 0
    with store.storage.write_transaction() as session:
        row = session.get(RecruitmentMailRecord, record.id)
        row.raw_metadata = {}
    assert _summary(store.get(record.id)).analysis_state == "unassessed"


@pytest.mark.parametrize("state,label", [("failed_terminal", "分析或核验失败"), ("needs_auth_metadata", "发件人待核验"),
                                          ("ambiguous_application", "待确认投递")])
def test_terminal_states_have_explicit_labels(state, label):
    store, record, apps, proposal, repo, registry, executor = setup_binding()
    store.update_processing_status(record.id, state)
    assert mail_semantics(store.get(record.id))["processing_label"] == label


def test_notification_is_not_required_to_bind():
    store, record, apps, proposal, repo, registry, executor = setup_binding()
    with store.storage.write_transaction() as session:
        row = session.get(RecruitmentMailRecord, record.id)
        metadata = dict(row.raw_metadata)
        analysis = dict(metadata["model_analysis"])
        analysis["payload"] = dict(analysis["payload"], event_type="information")
        metadata["model_analysis"] = analysis
        row.raw_metadata = metadata
    result = mail_semantics(store.get(record.id))
    assert result["binding_state"] == "not_required"
    assert result["association_required"] is False


@pytest.mark.parametrize("changed", [False, True])
def test_resync_cannot_erase_human_binding_or_revocation(changed):
    store, record, apps, proposal, repo, registry, executor = setup_binding()
    confirm(store, record, registry, executor)
    confirm(store, store.get(record.id), registry, executor, target=None, action="unbind")
    old_binding = store.get(record.id).raw_metadata["confirmed_application_binding"]
    synced = store.upsert(EmailMessage(identity=MailIdentity(message_id=record.message_id),
        subject=record.subject, sender=record.sender,
        body_text=record.body_text + (" 更新通知" if changed else ""), received_at=record.received_at), source=record.source)
    assert synced.id == record.id
    assert synced.raw_metadata["confirmed_application_binding"] == old_binding
    assert len(synced.raw_metadata["binding_recent_history"]) == 2
    assert confirmed_binding_matches(synced, apps[1]) is False


def test_confirmed_name_mismatch_tracks_target_identity_in_processing_version():
    from packages.recruitment_mail.processing import _input_digest
    store, record, apps, proposal, repo, registry, executor = setup_binding()
    confirm(store, record, registry, executor)
    record = store.get(record.id)
    original = _input_digest(record, apps)
    changed_apps = [apps[0].model_copy(update={"job_title": "updated title"}), apps[1]]
    assert _input_digest(record, changed_apps) != original


def test_frozen_scope_digest_is_checked_at_actual_processor_reload():
    from packages.recruitment_mail.processing import process_pending_mail
    store, record, apps, proposal, repo, registry, executor = setup_binding()
    class NoModel:
        def complete(self, **_kwargs):
            pytest.fail("new source version must not be sent to a model in the old run")
    events = []
    result = process_pending_mail(store, repo, SimpleNamespace(write_enabled=True), client=NoModel(),
        record_ids=[record.id], expected_digests={record.id: "a" * 64}, progress=events.append)
    assert result["processed"] == 1 and result["unresolved"] == 1
    assert events[0]["result"]["state"] == "source_changed"
    assert store.get(record.id).application_id is None
