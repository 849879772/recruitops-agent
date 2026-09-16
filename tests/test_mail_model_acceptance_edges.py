"""Acceptance edges for the model-mail processing and status-write boundary.

Each test uses an in-memory database and a local fake model response; no
mailbox, API, or production state is touched.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from packages.domain.models import Application, ApplicationStage
from packages.recruitment_mail import (
    CompanyCandidate,
    EmailMessage,
    JobCandidate,
    MailIdentity,
    ParsedRecruitmentEmail,
    RecruitmentMailProcessingStatus,
    RecruitmentMailRecord,
    RecruitmentMailStore,
    RecruitmentMessageCategory,
)
from packages.recruitment_mail.analysis_store import save_model_analysis
from packages.recruitment_mail.model_analysis import MAIL_ANALYSIS_VERSION
from packages.recruitment_mail.processing import (
    _analyze_one,
    _claim,
    _input_digest,
    process_pending_mail,
)
from packages.storage import ApplicationSnapshot, Storage
from packages.tools.application_status_update import (
    ApplicationStatusUpdateInput,
    update_application_status,
)


class _Repository:
    def __init__(self, *applications):
        self.applications = list(applications)

    def list_applications(self):
        return list(self.applications)

    def get_job(self, _job_id):
        return None


class _TriageClient:
    model = "acceptance-fixture"

    def __init__(self, response):
        self.response = response
        self.calls = 0

    def complete(self, **_kwargs):
        self.calls += 1
        return SimpleNamespace(content=json.dumps(self.response), model=self.model)


class _FullAnalysisClient:
    model = "acceptance-fixture"

    def __init__(self, proposal):
        self.proposal = proposal

    def complete(self, **_kwargs):
        return SimpleNamespace(content=json.dumps(self.proposal), model=self.model)


def _setup_mail(*, authenticated=True, category=RecruitmentMessageCategory.ASSESSMENT):
    storage = Storage.from_url("sqlite+pysqlite:///:memory:")
    store = RecruitmentMailStore(storage)
    body = (
        "感谢参加 DJI 大疆校园招聘并投递AI DevOps 工程师（深圳），"
        "现诚邀您参与在线测评！"
    )
    identity = MailIdentity(message_id="acceptance-edge-mail")
    received_at = datetime(2026, 9, 6, tzinfo=timezone.utc)
    parsed = ParsedRecruitmentEmail(
        identity=identity,
        sender="recruitment@example.com",
        subject="来自DJI 大疆的测评邀请",
        body_text=body,
        received_at=received_at,
        category=category,
        company_candidates=[CompanyCandidate(value="DJI 大疆", evidence="主题", confidence=1)],
        job_candidates=[JobCandidate(value="AI DevOps 工程师（深圳）", evidence="正文", confidence=1)],
        confidence=1,
    )
    message = EmailMessage(
        identity=identity,
        sender=parsed.sender,
        subject=parsed.subject,
        body_text=body,
        received_at=received_at,
        source_metadata={
            "authentication_results": (
                [{"method": "dkim", "result": "pass", "authserv_id": "163.com", "aligned": True}]
                if authenticated
                else []
            )
        },
    )
    record = store.upsert(message, parsed=parsed, source="imap_readonly")
    application = Application(
        id="application-4",
        company_name="DJI 大疆",
        job_title="AI DevOps 工程师（深圳）",
        stage=ApplicationStage.APPLIED,
        source="fixture",
        source_ref="application-4",
        idempotency_key="application:4",
    )
    with storage.write_transaction() as session:
        session.add(
            ApplicationSnapshot(
                id=application.id,
                company_name=application.company_name,
                job_title=application.job_title,
                stage=application.stage.value,
                stage_history=[],
                source="fixture",
                source_ref=application.source_ref,
                idempotency_key=application.idempotency_key,
            )
        )
    return store, _Repository(application), application, record


def test_legacy_terminal_mail_migrates_once_then_is_not_sent_to_model_again():
    store, repository, _application, record = _setup_mail(authenticated=False)
    store.update_processing_status(
        record.id,
        RecruitmentMailProcessingStatus.NEEDS_AUTH_METADATA,
        processing_error="legacy authentication metadata required",
    )
    assert store.get(record_id=record.id).processing_status == (
        RecruitmentMailProcessingStatus.NEEDS_AUTH_METADATA.value
    )
    assert "model_processing" not in (store.get(record_id=record.id).raw_metadata or {})

    client = _TriageClient(
        [
            {
                "record_id": record.id,
                "content_digest": record.content_digest,
                "relevance": "irrelevant",
                "reason": "terminal-state replay probe",
            }
        ]
    )
    first = process_pending_mail(
        store,
        repository,
        SimpleNamespace(write_enabled=True, llm_enabled=True),
        client=client,
    )
    assert first["processed"] == 1
    assert client.calls == 1

    second = process_pending_mail(
        store,
        repository,
        SimpleNamespace(write_enabled=True, llm_enabled=True),
        client=client,
    )
    assert second["processed"] == 0
    assert client.calls == 1


def test_status_writer_rejects_model_candidate_id_for_another_application():
    store, repository, application, record = _setup_mail()
    proposal = {
        "record_id": record.id,
        "content_digest": record.content_digest,
        "company_name": application.company_name,
        "job_title": application.job_title,
        "job_code": None,
        "event_type": "assessment",
        "event_time": None,
        "deadline": None,
        "evidence_quotes": [record.body_text],
        "candidate_application_id": "application-from-model",
        "match_reason": "model selected a different application",
        "action_summary": None,
    }
    save_model_analysis(
        store,
        record.id,
        record.content_digest,
        MAIL_ANALYSIS_VERSION + ":acceptance-fixture",
        proposal,
        "proposed",
        "acceptance-fixture",
    )

    result = update_application_status(
        ApplicationStatusUpdateInput(
            application_id=application.id,
            evidence_type="mail",
            evidence_id=record.id,
            target_status="assessment",
        ),
        repository,
        store,
    )

    assert not result.success
    with store.storage.session() as session:
        assert session.get(ApplicationSnapshot, application.id).stage == ApplicationStage.APPLIED.value


def test_reclaimed_processing_claim_blocks_old_worker_status_write():
    store, repository, application, record = _setup_mail()
    settings = SimpleNamespace(write_enabled=True, llm_enabled=True)
    digest = _input_digest(record, [application])
    assert _claim(store, record, "old-owner", digest)

    with store.storage.write_transaction() as session:
        row = session.get(RecruitmentMailRecord, record.id)
        metadata = dict(row.raw_metadata)
        attempt = dict(metadata["model_processing"])
        attempt["started_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=181)
        ).isoformat()
        metadata["model_processing"] = attempt
        row.raw_metadata = metadata

    stale_record = store.get(record_id=record.id)
    assert _claim(store, stale_record, "new-owner", digest)

    proposal = {
        "record_id": record.id,
        "content_digest": record.content_digest,
        "company_name": application.company_name,
        "job_title": application.job_title,
        "job_code": None,
        "event_type": "assessment",
        "event_time": None,
        "deadline": None,
        "evidence_quotes": [record.body_text],
        "candidate_application_id": application.id,
        "match_reason": "unique fixture identity",
        "action_summary": None,
    }

    with pytest.raises(ValueError, match="processing_claim_lost"):
        _analyze_one(
            store,
            repository,
            settings,
            _FullAnalysisClient(proposal),
            stale_record,
            [application],
            "old-owner",
        )

    with store.storage.session() as session:
        assert session.get(ApplicationSnapshot, application.id).stage == ApplicationStage.APPLIED.value


__all__ = [
    "test_legacy_terminal_mail_migrates_once_then_is_not_sent_to_model_again",
    "test_status_writer_rejects_model_candidate_id_for_another_application",
    "test_reclaimed_processing_claim_blocks_old_worker_status_write",
]
