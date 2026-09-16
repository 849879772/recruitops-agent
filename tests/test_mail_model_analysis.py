from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from packages.recruitment_mail.model_analysis import (
    MAIL_ANALYSIS_VERSION,
    MailAnalysisProposal,
    MailAnalysisValidationError,
    MailEventType,
    MailRelevance,
    MailTriageProposal,
    validate_mail_analysis_proposal,
    validate_mail_proposal,
)


CONTENT_DIGEST = "a" * 64


def _stored_mail(*, digest: str = CONTENT_DIGEST) -> SimpleNamespace:
    return SimpleNamespace(
        id="mail-record-1",
        content_digest=digest,
        subject="星河科技面试邀请",
        body_text="请于 2026-09-10 10:00 参加后端开发工程师面试。",
    )


def _analysis(**overrides: object) -> MailAnalysisProposal:
    values: dict[str, object] = {
        "record_id": "mail-record-1",
        "content_digest": CONTENT_DIGEST,
        "event_type": MailEventType.INTERVIEW,
        "evidence_quotes": ["请于 2026-09-10 10:00 参加后端开发工程师面试。"],
        "match_reason": "正文明确提到面试安排。",
    }
    values.update(overrides)
    return MailAnalysisProposal.model_validate(values)


def test_proposals_are_versioned_strict_and_missing_identity_stays_null() -> None:
    assert MAIL_ANALYSIS_VERSION == "recruitops.mail_analysis.v2"

    analysis = _analysis()
    assert analysis.company_name is None
    assert analysis.job_title is None
    assert analysis.job_code is None
    assert analysis.candidate_application_id is None
    assert "verified" not in MailAnalysisProposal.model_fields

    triage = MailTriageProposal(
        record_id="mail-record-1",
        content_digest=CONTENT_DIGEST,
        relevance=MailRelevance.RELEVANT,
        reason="招聘邮件",
    )
    validate_mail_proposal(triage, _stored_mail())


def test_malformed_schema_and_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        _analysis(event_type="not-an-event")
    with pytest.raises(ValidationError):
        _analysis(verified=True)
    with pytest.raises(ValidationError):
        MailTriageProposal(
            record_id="mail-record-1",
            content_digest="not-a-sha256",
            relevance="relevant",
            reason="招聘邮件",
        )


def test_quote_validation_allows_whitespace_normalization_only() -> None:
    proposal = _analysis(evidence_quotes=["请于 2026-09-10 10:00\n参加后端开发工程师面试。"])

    validate_mail_analysis_proposal(proposal, _stored_mail())


def test_nonexistent_quotation_is_rejected_without_semantic_inference() -> None:
    proposal = _analysis(evidence_quotes=["公司保证一定录用。"])

    with pytest.raises(MailAnalysisValidationError, match="evidence_quotes") as exc_info:
        validate_mail_analysis_proposal(proposal, _stored_mail())

    assert exc_info.value.code == "evidence_quote_not_found"


def test_stale_digest_is_rejected_even_when_quote_is_present() -> None:
    proposal = _analysis()

    with pytest.raises(MailAnalysisValidationError, match="content_digest") as exc_info:
        validate_mail_analysis_proposal(proposal, _stored_mail(digest="b" * 64))

    assert exc_info.value.code == "content_digest_mismatch"
