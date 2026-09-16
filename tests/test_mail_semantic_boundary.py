"""Historical extracted fields cannot substitute for source-bound model analysis."""

from importlib.util import find_spec
from types import SimpleNamespace

import pytest

from packages.domain.models import Application, ApplicationStage
from packages.recruitment_mail import (
    CompanyCandidate, JobCandidate, MailIdentity, ParsedRecruitmentEmail,
    RecruitmentMailStore, RecruitmentMessageCategory,
)
from packages.storage import Storage
from packages.tools.application_status_update import (
    ApplicationStatusUpdateInput, update_application_status,
)
from packages.tools.recruitment_mail import RecruitmentMailReviewInput, review_recruitment_mail
from packages.tools.typed import ToolErrorCode


def test_obsolete_semantic_parser_is_not_importable():
    assert find_spec("packages.recruitment_mail.parser") is None


@pytest.mark.parametrize("settings", [None, SimpleNamespace(llm_enabled=False), SimpleNamespace(llm_enabled=True)])
def test_historical_candidates_never_authorize_write_or_preview(settings):
    store = RecruitmentMailStore(Storage.from_url("sqlite+pysqlite:///:memory:"))
    record = store.upsert(ParsedRecruitmentEmail(
        identity=MailIdentity(message_id="historical-keyword-result"),
        subject="Example rejection", body_text="Example rejected the Engineer application.",
        category=RecruitmentMessageCategory.REJECTION, confidence=1,
        company_candidates=[CompanyCandidate(value="Example", evidence="Example")],
        job_candidates=[JobCandidate(value="Engineer", evidence="Engineer")],
    ), source="imap_readonly")
    app = Application(id="1", company_name="Example", job_title="Engineer",
                      stage=ApplicationStage.APPLIED, source="fixture", source_ref="1",
                      idempotency_key="app1")
    repo = SimpleNamespace(list_applications=lambda: [app])
    result = update_application_status(ApplicationStatusUpdateInput(
        application_id="1", evidence_type="mail", evidence_id=record.id,
        target_status="rejected",
    ), repo, store, settings=settings)
    assert not result.success
    assert result.error_code is ToolErrorCode.INVALID_SOURCE
    assert "recruitment_mail_process" in result.error_message
    preview = review_recruitment_mail(RecruitmentMailReviewInput(record_id=record.id), store, repo)
    assert not preview.success
    assert preview.error_code is ToolErrorCode.INVALID_SOURCE
    assert store.get(record_id=record.id).processing_status == "pending"
    assert app.stage is ApplicationStage.APPLIED
