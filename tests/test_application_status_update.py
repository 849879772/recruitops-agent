from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from packages.domain.models import Application, ApplicationStage
from packages.recruitment_mail import (
    EmailMessage,
    MailIdentity,
    RecruitmentMailStore,
    RecruitmentMessageCategory,
)
from packages.recruitment_mail.analysis_store import save_model_analysis
from packages.recruitment_mail.model_analysis import MAIL_ANALYSIS_VERSION
from packages.storage import ApplicationSnapshot, Storage
from packages.tools.application_status_update import (
    ApplicationStatusUpdateInput,
    _authenticated,
    update_application_status,
)
from packages.tools import application_status_update as status_update_module


class Repo:
    def __init__(self, application):
        self.application = application

    def list_applications(self):
        return [self.application]

    def get_job(self, _job_id):
        return None


def _setup(authenticated=True, category=RecruitmentMessageCategory.REJECTION,
           app_title="2027届具身智能算法工程师", mail_company="大华股份",
           mail_job_title=None):
    storage = Storage.from_url("sqlite+pysqlite:///:memory:")
    store = RecruitmentMailStore(storage)
    app = Application(
        id="8", company_name="浙江大华技术股份有限公司",
        job_title=app_title,
        stage=ApplicationStage.APPLIED, idempotency_key="application:8",
        source="fixture", source_ref="application-8",
    )
    with storage.write_transaction() as session:
        session.add(ApplicationSnapshot(
            id=app.id, company_name=app.company_name, job_title=app.job_title,
            job_id=None, record_url=None, stage=app.stage.value,
            idempotency_key=app.idempotency_key, stage_history=[],
            source="fixture", source_ref="application-8",
        ))
    received_at = datetime(2026, 9, 6, 13, 21, tzinfo=timezone.utc)
    proposal_job_title = mail_job_title
    if category is RecruitmentMessageCategory.APPLICATION_CONFIRMATION:
        subject = f"{mail_company}申请确认"
        body_text = f"{mail_company}已收到您的申请。"
        proposal_job_title = None
    else:
        proposal_job_title = proposal_job_title or app_title
        subject = f"{mail_company}辞谢信"
        body_text = f"感谢关注。很遗憾，您没有通过【研发中心】{proposal_job_title}的简历筛选。"
    message = EmailMessage(
        identity=MailIdentity(message_id="mail-dahua-1"),
        sender="大华股份招聘", subject=subject,
        body_text=body_text, received_at=received_at,
        source_metadata={"authentication_results": ([{
            "method": "dkim", "result": "pass", "authserv_id": "163.com",
            "identity_domain": "example.com", "aligned": True,
        }] if authenticated else [])},
    )
    record = store.upsert(
        message,
        source="imap_readonly",
        source_ref=message.identity.message_id,
    )
    event_type = (
        category.value
        if category is not RecruitmentMessageCategory.OTHER
        else "information"
    )
    save_model_analysis(
        store,
        record.id,
        record.content_digest,
        MAIL_ANALYSIS_VERSION,
        {
            "record_id": record.id,
            "content_digest": record.content_digest,
            "company_name": mail_company,
            "job_title": proposal_job_title,
            "job_code": None,
            "event_type": event_type,
            "event_time": None,
            "deadline": None,
            "evidence_quotes": [body_text],
            "candidate_application_id": "8",
            "match_reason": "fixture proposal is bound to the persisted mail source",
            "action_summary": None,
        },
        "proposed",
        "status-fixture",
    )
    return app, Repo(app), store, record


def _request(record_id):
    return ApplicationStatusUpdateInput(
        application_id="8", evidence_type="mail", evidence_id=record_id,
        target_status="rejected",
    )


def test_mail_evidence_updates_and_replays_idempotently():
    app, repo, store, record = _setup()
    first = update_application_status(_request(record.id), repo, store)
    assert first.success and first.data.state == "updated"
    assert first.data.wrote
    with store.storage.session() as session:
        snapshot = session.get(ApplicationSnapshot, "8")
        assert snapshot.stage == "rejected"
        assert snapshot.stage_history[-1]["source"] == "recruitment_mail"
    repo.application = app.model_copy(update={
        "stage": ApplicationStage.REJECTED,
        "source_status_synced_at": datetime(2026, 9, 6, 13, 21, tzinfo=timezone.utc),
    })
    replay = update_application_status(_request(record.id), repo, store)
    assert replay.success and replay.data.state == "unchanged"
    assert store.get(record_id=record.id).processing_status == "processed_updated"


def test_mail_exact_identity_does_not_update_another_job():
    _app, repo, store, record = _setup()
    repo.application = repo.application.model_copy(update={"job_title": "另一个岗位"})
    result = update_application_status(_request(record.id), repo, store)
    assert not result.success
    assert result.error_code.value == "ambiguous_match"


def test_mail_without_sender_authentication_is_linked_but_not_written():
    _app, repo, store, record = _setup(authenticated=False)
    result = update_application_status(_request(record.id), repo, store)
    assert not result.success
    assert result.error_code.value == "invalid_source"
    assert result.retryable is False
    assert store.get(record_id=record.id).application_id == "8"
    assert store.get(record_id=record.id).processing_status == "needs_auth_metadata"


def test_mail_update_backfills_authentication_once_before_writing(monkeypatch):
    _app, repo, store, record = _setup(authenticated=False)
    calls = []

    def backfill(_settings, target_store, record_id):
        calls.append(record_id)
        target_store.update_source_metadata(record_id, {
            "authentication_results": [{
                "method": "dkim", "result": "pass", "authserv_id": "gzchengxin8",
                "identity_domain": "shmail.ibeisen.com", "aligned": True,
            }],
        })

    monkeypatch.setattr(status_update_module, "backfill_mail_authentication", backfill)
    result = update_application_status(
        _request(record.id), repo, store, settings=SimpleNamespace(mail_enabled=True)
    )

    assert calls == [record.id]
    assert result.success and result.data.wrote


def test_unclassified_mail_cannot_change_application_stage():
    _app, repo, store, record = _setup(category=RecruitmentMessageCategory.OTHER)
    result = update_application_status(_request(record.id), repo, store)
    assert not result.success
    with store.storage.session() as session:
        assert session.get(ApplicationSnapshot, "8").stage == "applied"


def test_newer_passive_delivery_observation_does_not_veto_rejection():
    app, repo, store, record = _setup()
    repo.application = app.model_copy(update={
        "source_status": "投递",
        "source_status_synced_at": datetime(2026, 9, 7, tzinfo=timezone.utc),
    })
    result = update_application_status(_request(record.id), repo, store)
    assert result.success and result.data.wrote


def test_newer_explicit_mail_evidence_still_blocks_older_rejection():
    app, repo, store, record = _setup()
    repo.application = app.model_copy(update={
        "source_status": "投递",
        "source_status_synced_at": datetime(2026, 9, 7, tzinfo=timezone.utc),
        "stage_history": [{"source": "recruitment_mail", "stage": "applied"}],
    })
    result = update_application_status(_request(record.id), repo, store)
    assert result.success and result.data.reason_code == "stale_evidence"
    assert result.data.state == "unchanged" and not result.data.wrote
    assert store.get(record_id=record.id).processing_status == "processed_unchanged"


def test_old_company_only_confirmation_settles_without_reopening_terminal_application():
    app, repo, store, record = _setup(
        category=RecruitmentMessageCategory.APPLICATION_CONFIRMATION,
        app_title="机器人软件工程师(J13410)",
        mail_company="科大讯飞股份有限公司",
    )
    repo.application = app.model_copy(update={
        "company_name": "科大讯飞",
        "stage": ApplicationStage.REJECTED,
        "source_status_synced_at": datetime(2026, 9, 7, tzinfo=timezone.utc),
    })
    request = ApplicationStatusUpdateInput(
        application_id="8",
        evidence_type="mail",
        evidence_id=record.id,
        target_status="applied",
    )

    result = update_application_status(request, repo, store)

    assert result.success and result.data.state == "unchanged"
    assert result.data.reason_code == "stale_evidence"
    assert store.get(record_id=record.id).processing_status == "processed_unchanged"


def test_old_company_only_confirmation_without_auth_still_settles_noop():
    app, repo, store, record = _setup(
        authenticated=False,
        category=RecruitmentMessageCategory.APPLICATION_CONFIRMATION,
        app_title="机器人软件工程师(J13410)",
        mail_company="科大讯飞股份有限公司",
    )
    repo.application = app.model_copy(update={
        "company_name": "科大讯飞",
        "stage": ApplicationStage.REJECTED,
        "source_status_synced_at": datetime(2026, 9, 7, tzinfo=timezone.utc),
    })
    request = ApplicationStatusUpdateInput(
        application_id="8",
        evidence_type="mail",
        evidence_id=record.id,
        target_status="applied",
    )

    result = update_application_status(request, repo, store)

    assert result.success and result.data.state == "unchanged"
    assert store.get(record_id=record.id).processing_status == "processed_unchanged"


def test_replay_with_stale_repository_snapshot_does_not_duplicate_history():
    _app, repo, store, record = _setup()
    first = update_application_status(_request(record.id), repo, store)
    replay = update_application_status(_request(record.id), repo, store)
    assert first.success and replay.success
    assert replay.data.state == "unchanged" and not replay.data.wrote
    with store.storage.session() as session:
        history = session.get(ApplicationSnapshot, "8").stage_history
        assert len(history) == 1
        assert history[0]["audit_id"] == first.data.audit_id
    assert store.get(record_id=record.id).processing_status == "processed_updated"


def test_authentication_requires_connector_provenance_and_authserv():
    result = {"method": "dkim", "result": "pass", "authserv_id": "163.com", "aligned": True}
    metadata = {"transport": {"authentication_results": [result]}}
    assert not _authenticated(SimpleNamespace(source="local", raw_metadata=metadata))
    result.pop("authserv_id")
    assert not _authenticated(SimpleNamespace(source="imap_readonly", raw_metadata=metadata))


def test_authentication_requires_aligned_dkim():
    result = {"method": "dkim", "result": "pass", "authserv_id": "163.com", "aligned": False}
    metadata = {"transport": {"authentication_results": [result]}}
    assert not _authenticated(SimpleNamespace(source="imap_readonly", raw_metadata=metadata))
    result.update(method="spf", aligned=True)
    assert not _authenticated(SimpleNamespace(source="imap_readonly", raw_metadata=metadata))


def test_mail_processing_failure_rolls_back_application_change():
    _app, repo, store, record = _setup()

    def reject_processing_flush(session, *_args):
        if any(getattr(item, "processing_status", None) == "processed_updated"
               for item in session.dirty):
            raise RuntimeError("injected processing failure")

    event.listen(Session, "before_flush", reject_processing_flush)
    try:
        with pytest.raises(RuntimeError, match="injected processing failure"):
            update_application_status(_request(record.id), repo, store)
    finally:
        event.remove(Session, "before_flush", reject_processing_flush)
    with store.storage.session() as session:
        snapshot = session.get(ApplicationSnapshot, "8")
        assert snapshot.stage == "applied" and snapshot.stage_history == []
    assert store.get(record_id=record.id).processing_status != "processed_updated"


def test_actual_dahua_alias_department_and_code_can_update():
    _app, repo, store, record = _setup(
        app_title="【研发中心】2027届具身智能算法工程师(J24413)",
        mail_company="大华股份",
        mail_job_title="2027届具身智能算法工程师",
    )
    result = update_application_status(_request(record.id), repo, store)
    assert result.success and result.data.wrote


def test_duplicate_application_identity_blocks_automatic_write():
    app, repo, store, record = _setup()
    duplicate = app.model_copy(update={"id": "9"})
    repo.list_applications = lambda: [app, duplicate]
    result = update_application_status(_request(record.id), repo, store)
    assert not result.success and result.error_code.value == "ambiguous_match"
