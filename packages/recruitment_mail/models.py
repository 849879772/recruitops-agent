from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class MailModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class RecruitmentMessageCategory(StrEnum):
    APPLICATION_CONFIRMATION = "application_confirmation"
    ASSESSMENT = "assessment"
    WRITTEN_TEST = "written_test"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTION = "rejection"
    OTHER = "other"


class RecruitmentMailProcessingStatus(StrEnum):
    """Agent-owned processing state; it is independent from mailbox read state."""

    PENDING = "pending"
    PROCESSED_UPDATED = "processed_updated"
    PROCESSED_UNCHANGED = "processed_unchanged"
    IRRELEVANT = "irrelevant"
    PENDING_ASSOCIATION = "pending_association"
    NEEDS_AUTH_METADATA = "needs_auth_metadata"
    AMBIGUOUS_APPLICATION = "ambiguous_application"
    FAILED_TERMINAL = "failed_terminal"
    FAILED = "failed"

    # Values used by earlier local consumers remain valid during migration.
    PROCESSED = "processed"
    NEEDS_CONFIRMATION = "needs_confirmation"
    LINKED = "linked"
    IGNORED = "ignored"


MailProcessingStatus = RecruitmentMailProcessingStatus


class MailIdentity(MailModel):
    """Provider-neutral identifiers for one local mail observation."""

    message_id: str = Field(
        min_length=1,
        max_length=512,
        validation_alias=AliasChoices("message_id", "id", "uid"),
    )
    thread_id: str | None = Field(default=None, max_length=512)
    mailbox: str = Field(
        default="INBOX",
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices("mailbox", "folder"),
    )
    account_ref: str | None = Field(default=None, max_length=512)

    @property
    def id(self) -> str:
        return self.message_id


class MailCursor(MailModel):
    """Opaque state for an incremental reader; it performs no I/O."""

    mailbox: str = Field(
        default="INBOX",
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices("mailbox", "folder"),
    )
    token: str | None = Field(
        default=None,
        max_length=1024,
        validation_alias=AliasChoices(
            "token",
            "cursor",
            "position",
            "last_seen_id",
        ),
    )
    uid_validity: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices(
            "uid_validity",
            "uidvalidity",
            "uidValidity",
        ),
    )
    updated_at: datetime | None = None

    @property
    def cursor(self) -> str | None:
        return self.token

    @property
    def uidvalidity(self) -> str | None:
        return self.uid_validity


def _empty_source_metadata() -> dict[str, Any]:
    return {}


def _local_identity() -> MailIdentity:
    return MailIdentity(message_id="local-message")


class EmailMessage(MailModel):
    """Untrusted mail data supplied to sanitization and model interpretation."""

    identity: MailIdentity = Field(default_factory=_local_identity)
    sender: str | None = Field(default=None, max_length=1_000)
    recipients: list[str] = Field(default_factory=list, max_length=50)
    subject: str = Field(default="", max_length=2_000)
    body_text: str = Field(
        default="",
        max_length=200_000,
        validation_alias=AliasChoices("body_text", "body", "text"),
    )
    html_body: str | None = Field(
        default=None,
        max_length=400_000,
        validation_alias=AliasChoices("html_body", "html"),
    )
    received_at: datetime | None = None
    source_metadata: dict[str, Any] = Field(default_factory=_empty_source_metadata)
    mailbox_read: bool | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "mailbox_read",
            "mailbox_seen",
            "is_read",
            "seen",
        ),
    )

    @property
    def mailbox_seen(self) -> bool | None:
        return self.mailbox_read

    @property
    def message_id(self) -> str:
        return self.identity.message_id


class Candidate(MailModel):
    value: str = Field(min_length=1, max_length=500)
    evidence: str = Field(default="", max_length=1_000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class CompanyCandidate(Candidate):
    pass


class JobCandidate(Candidate):
    pass


class LocationCandidate(Candidate):
    pass


class TimeCandidate(Candidate):
    normalized: datetime | date | None = None
    ambiguous: bool = False


class DeadlineCandidate(TimeCandidate):
    pass


class LinkCandidate(MailModel):
    url: str = Field(min_length=1, max_length=4_096)
    label: str | None = Field(default=None, max_length=500)
    evidence: str = Field(default="", max_length=1_000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    @property
    def value(self) -> str:
        return self.url


class ParsedRecruitmentEmail(MailModel):
    """Redacted, side-effect-free interpretation of one email."""

    identity: MailIdentity
    sender: str | None = None
    recipients: list[str] = Field(default_factory=list, max_length=50)
    subject: str = ""
    body_text: str = Field(default="", max_length=200_000)
    received_at: datetime | None = None
    category: RecruitmentMessageCategory
    company_candidates: list[CompanyCandidate] = Field(default_factory=list, max_length=50)
    job_candidates: list[JobCandidate] = Field(default_factory=list, max_length=50)
    location_candidates: list[LocationCandidate] = Field(default_factory=list, max_length=100)
    time_candidates: list[TimeCandidate] = Field(default_factory=list, max_length=100)
    deadline_candidates: list[DeadlineCandidate] = Field(default_factory=list, max_length=50)
    link_candidates: list[LinkCandidate] = Field(default_factory=list, max_length=100)
    confidence: float = Field(ge=0.0, le=1.0)
    pending_confirmation_reasons: list[str] = Field(default_factory=list, max_length=30)
    safety_flags: list[str] = Field(default_factory=list, max_length=30)
    redacted_fields: list[str] = Field(default_factory=list, max_length=30)
    requires_confirmation: bool = False
    category_evidence: list[str] = Field(default_factory=list, max_length=20)

    @property
    def text(self) -> str:
        return self.body_text

    @property
    def clean_text(self) -> str:
        return self.body_text

    @property
    def message_type(self) -> RecruitmentMessageCategory:
        return self.category

    @property
    def needs_confirmation(self) -> bool:
        return self.requires_confirmation

    @property
    def pending_reasons(self) -> list[str]:
        return self.pending_confirmation_reasons

    @property
    def companies(self) -> list[CompanyCandidate]:
        return self.company_candidates

    @property
    def jobs(self) -> list[JobCandidate]:
        return self.job_candidates

    @property
    def locations(self) -> list[LocationCandidate]:
        return self.location_candidates

    @property
    def times(self) -> list[TimeCandidate]:
        return self.time_candidates

    @property
    def deadlines(self) -> list[DeadlineCandidate]:
        return self.deadline_candidates

    @property
    def links(self) -> list[LinkCandidate]:
        return self.link_candidates


# Short aliases keep the package easy to adopt without introducing a second model.
RecruitmentEmailCategory = RecruitmentMessageCategory
MailCategory = RecruitmentMessageCategory
RecruitmentEmail = EmailMessage
RecruitmentEmailInput = EmailMessage
RecruitmentMailResult = ParsedRecruitmentEmail
ParsedEmail = ParsedRecruitmentEmail


__all__ = [
    "Candidate",
    "CompanyCandidate",
    "DeadlineCandidate",
    "EmailMessage",
    "JobCandidate",
    "LocationCandidate",
    "LinkCandidate",
    "MailCategory",
    "MailCursor",
    "MailIdentity",
    "MailModel",
    "MailProcessingStatus",
    "ParsedEmail",
    "ParsedRecruitmentEmail",
    "RecruitmentEmail",
    "RecruitmentEmailCategory",
    "RecruitmentEmailInput",
    "RecruitmentMailResult",
    "RecruitmentMailProcessingStatus",
    "RecruitmentMessageCategory",
    "TimeCandidate",
]
