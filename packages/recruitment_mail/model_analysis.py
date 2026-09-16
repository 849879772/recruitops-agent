"""Strict model proposals and deterministic provenance checks for recruitment mail.

The models in this module describe untrusted model output.  They never carry a
``verified`` flag and this module does not infer application identity or event
semantics.  Validation only binds a proposal to one persisted mail revision.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Final, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field


MAIL_ANALYSIS_VERSION: Final[str] = "recruitops.mail_analysis.v3"

MAX_RECORD_ID_LENGTH: Final[int] = 128
MAX_DIGEST_LENGTH: Final[int] = 64
MAX_IDENTITY_LENGTH: Final[int] = 512
MAX_QUOTE_COUNT: Final[int] = 20
MAX_QUOTE_LENGTH: Final[int] = 2_000
MAX_EXPLANATION_LENGTH: Final[int] = 2_000
MAX_TEMPORAL_VALUE_LENGTH: Final[int] = 512

_SHA256_HEX_PATTERN = r"^[0-9a-f]{64}$"


class _MailAnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MailRelevance(StrEnum):
    RELEVANT = "relevant"
    IRRELEVANT = "irrelevant"
    UNCERTAIN = "uncertain"


class MailEventType(StrEnum):
    APPLICATION_CONFIRMATION = "application_confirmation"
    ASSESSMENT = "assessment"
    WRITTEN_TEST = "written_test"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTION = "rejection"
    INFORMATION = "information"
    ACTION_REQUIRED = "action_required"
    UNKNOWN = "unknown"


_IdentityText = Annotated[
    str,
    Field(min_length=1, max_length=MAX_IDENTITY_LENGTH),
]
_TemporalText = Annotated[
    str,
    Field(min_length=1, max_length=MAX_TEMPORAL_VALUE_LENGTH),
]
_EvidenceQuote = Annotated[
    str,
    Field(min_length=1, max_length=MAX_QUOTE_LENGTH),
]


class MailTriageProposal(_MailAnalysisModel):
    """Untrusted model proposal for whether one persisted mail is relevant."""

    record_id: str = Field(min_length=1, max_length=MAX_RECORD_ID_LENGTH)
    content_digest: str = Field(
        min_length=MAX_DIGEST_LENGTH,
        max_length=MAX_DIGEST_LENGTH,
        pattern=_SHA256_HEX_PATTERN,
    )
    relevance: MailRelevance
    reason: str = Field(min_length=1, max_length=MAX_EXPLANATION_LENGTH)


class MailAnalysisProposal(_MailAnalysisModel):
    """Untrusted model proposal containing structured mail interpretation."""

    record_id: str = Field(min_length=1, max_length=MAX_RECORD_ID_LENGTH)
    content_digest: str = Field(
        min_length=MAX_DIGEST_LENGTH,
        max_length=MAX_DIGEST_LENGTH,
        pattern=_SHA256_HEX_PATTERN,
    )
    company_name: _IdentityText | None = None
    job_title: _IdentityText | None = None
    job_code: _IdentityText | None = None
    event_type: MailEventType
    event_time: _TemporalText | None = None
    deadline: _TemporalText | None = None
    evidence_quotes: list[_EvidenceQuote] = Field(
        default_factory=list,
        max_length=MAX_QUOTE_COUNT,
    )
    candidate_application_id: _IdentityText | None = None
    match_reason: str = Field(min_length=1, max_length=MAX_EXPLANATION_LENGTH)
    action_summary: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_EXPLANATION_LENGTH,
    )


class PersistedMail(Protocol):
    """Minimal read-only shape required by the pure provenance validator."""

    id: str
    content_digest: str
    subject: str
    body_text: str


PersistedMailLike: TypeAlias = PersistedMail | Mapping[str, object]
MailProposal: TypeAlias = MailTriageProposal | MailAnalysisProposal


class MailAnalysisValidationError(ValueError):
    """Raised when a proposal cannot be bound to its persisted mail revision."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _stored_value(record: PersistedMailLike, *names: str) -> object:
    if isinstance(record, Mapping):
        for name in names:
            if name in record:
                return record[name]
        return None
    for name in names:
        value = getattr(record, name, None)
        if value is not None:
            return value
    return None


def _required_stored_text(record: PersistedMailLike, field: str, *aliases: str) -> str:
    value = _stored_value(record, field, *aliases)
    if not isinstance(value, str) or not value.strip():
        raise MailAnalysisValidationError(
            f"stored mail is missing non-empty {field}",
            code="stored_mail_invalid",
        )
    return value


def _stored_text(record: PersistedMailLike, field: str, *aliases: str) -> str:
    value = _stored_value(record, field, *aliases)
    if not isinstance(value, str):
        raise MailAnalysisValidationError(
            f"stored mail is missing string field {field}",
            code="stored_mail_invalid",
        )
    return value


def _normalized_whitespace(value: str) -> str:
    return " ".join(value.split())


def _validate_common_binding(
    proposal: MailProposal,
    stored_mail: PersistedMailLike,
) -> None:
    stored_record_id = _required_stored_text(stored_mail, "id", "record_id")
    if proposal.record_id != stored_record_id:
        raise MailAnalysisValidationError(
            "model proposal record_id does not match the persisted mail",
            code="record_id_mismatch",
        )

    stored_digest = _required_stored_text(stored_mail, "content_digest")
    if proposal.content_digest != stored_digest:
        raise MailAnalysisValidationError(
            "model proposal content_digest does not match the persisted mail",
            code="content_digest_mismatch",
        )
def _validate_quotes(proposal: MailAnalysisProposal, stored_mail: PersistedMailLike) -> None:
    subject = _stored_text(stored_mail, "subject")
    body_text = _stored_text(stored_mail, "body_text", "body", "text")
    sources = (_normalized_whitespace(subject), _normalized_whitespace(body_text))

    for index, quote in enumerate(proposal.evidence_quotes):
        normalized_quote = _normalized_whitespace(quote)
        if not any(normalized_quote in source for source in sources):
            raise MailAnalysisValidationError(
                f"evidence_quotes[{index}] is not present verbatim in persisted subject/body",
                code="evidence_quote_not_found",
            )


def validate_mail_proposal(proposal: MailProposal, stored_mail: PersistedMailLike) -> None:
    """Validate only revision provenance for a triage or analysis proposal.

    Success means the proposal references the current persisted record and, for
    full analysis, every supplied quote is an exact persisted quotation modulo
    whitespace.  It does not verify application identity, event type, or any
    other semantic claim.
    """

    if not isinstance(proposal, (MailTriageProposal, MailAnalysisProposal)):
        raise TypeError("proposal must be a MailTriageProposal or MailAnalysisProposal")
    _validate_common_binding(proposal, stored_mail)
    if isinstance(proposal, MailAnalysisProposal):
        _validate_quotes(proposal, stored_mail)


def validate_mail_triage_proposal(
    proposal: MailTriageProposal,
    stored_mail: PersistedMailLike,
) -> None:
    """Validate revision provenance for one triage proposal."""

    if not isinstance(proposal, MailTriageProposal):
        raise TypeError("proposal must be a MailTriageProposal")
    validate_mail_proposal(proposal, stored_mail)


def validate_mail_analysis_proposal(
    proposal: MailAnalysisProposal,
    stored_mail: PersistedMailLike,
) -> None:
    """Validate revision provenance and verbatim evidence for full analysis."""

    if not isinstance(proposal, MailAnalysisProposal):
        raise TypeError("proposal must be a MailAnalysisProposal")
    validate_mail_proposal(proposal, stored_mail)


__all__ = [
    "MAIL_ANALYSIS_VERSION",
    "MailAnalysisProposal",
    "MailAnalysisValidationError",
    "MailEventType",
    "MailProposal",
    "MailRelevance",
    "MailTriageProposal",
    "PersistedMail",
    "PersistedMailLike",
    "validate_mail_analysis_proposal",
    "validate_mail_proposal",
    "validate_mail_triage_proposal",
]
