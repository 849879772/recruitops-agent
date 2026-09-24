from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.domain.models import ApplicationStage


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class OperationName(StrEnum):
    """Canonical names for every approval-gated write."""

    COMPANY_CONFIG_UPDATE = "company_config_update"
    CRAWLER_RECIPE_UPDATE = "crawler_recipe_update"
    APPLICATION_CREATE = "application_create"
    APPLICATION_STAGE_UPDATE = "application_stage_update"
    SCHEDULE_CREATE = "schedule_create"
    BROWSER_ACTION = "browser_action"
    RECRUITMENT_MAIL_BINDING = "recruitment_mail_binding"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EXECUTING = "executing"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class PolicyErrorCode(StrEnum):
    EVIDENCE_MISSING = "evidence_missing"
    PREVIEW_EXPIRED = "preview_expired"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_ALREADY_CONSUMED = "token_already_consumed"
    TOKEN_IN_PROGRESS = "token_in_progress"
    TOKEN_REJECTED = "token_rejected"
    TOKEN_NOT_APPROVED = "token_not_approved"
    TOKEN_STATE_CONFLICT = "token_state_conflict"
    TOKEN_BINDING_MISMATCH = "token_binding_mismatch"
    DUPLICATE_WRITE = "duplicate_write"
    STAGE_REGRESSION = "stage_regression"
    COHORT_NOT_CONFIRMED = "cohort_not_confirmed"
    INCOMPLETE_JD = "incomplete_jd"
    UNSUPPORTED_OPERATION = "unsupported_operation"


class EvidenceRef(ApprovalModel):
    source: str = Field(min_length=1)
    source_ref: str | None = None
    summary: str | None = None


_OPERATION_ALIASES: dict[str, OperationName] = {
    "company_config_update": OperationName.COMPANY_CONFIG_UPDATE,
    "company_configuration_update": OperationName.COMPANY_CONFIG_UPDATE,
    "company_integration": OperationName.COMPANY_CONFIG_UPDATE,
    "company_onboarding": OperationName.COMPANY_CONFIG_UPDATE,
    "crawler_recipe_update": OperationName.CRAWLER_RECIPE_UPDATE,
    "application_create": OperationName.APPLICATION_CREATE,
    "create_application": OperationName.APPLICATION_CREATE,
    "record_application": OperationName.APPLICATION_CREATE,
    "application_stage_update": OperationName.APPLICATION_STAGE_UPDATE,
    "update_application_stage": OperationName.APPLICATION_STAGE_UPDATE,
    "stage_update": OperationName.APPLICATION_STAGE_UPDATE,
    "schedule_create": OperationName.SCHEDULE_CREATE,
    "create_schedule": OperationName.SCHEDULE_CREATE,
    "browser_action": OperationName.BROWSER_ACTION,
    "recruitment_mail_binding": OperationName.RECRUITMENT_MAIL_BINDING,
}


def normalize_operation(value: object) -> OperationName:
    if isinstance(value, OperationName):
        return value
    if not isinstance(value, str):
        raise ValueError("operation must be a supported string")
    key = value.strip().casefold().replace("-", "_").replace(" ", "_")
    try:
        return _OPERATION_ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"unsupported operation: {value}") from exc


def canonical_evidence_summary(value: str) -> str:
    return " ".join(value.split())


def evidence_digest(value: str) -> str:
    summary = canonical_evidence_summary(value)
    if not summary:
        raise ValueError("evidence summary cannot be empty")
    return sha256(summary.encode("utf-8")).hexdigest()


class ApprovalPreview(ApprovalModel):
    """A side-effect-free description of the write a human may approve."""

    task_id: str = Field(min_length=1)
    operation: OperationName
    idempotency_key: str = Field(min_length=1)
    evidence_summary: str | None = Field(
        default=None,
        validation_alias=AliasChoices("evidence_summary", "evidence_digest_summary"),
    )
    evidence: tuple[EvidenceRef | str, ...] = ()
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    target_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None

    # These fields are intentionally flat so policy callers do not need a repository model.
    cohort: int | None = None
    cohort_status: str | None = None
    jd_raw: str | None = Field(
        default=None,
        validation_alias=AliasChoices("jd_raw", "jd", "complete_jd"),
    )
    current_stage: ApplicationStage | None = Field(
        default=None,
        validation_alias=AliasChoices("current_stage", "from_stage", "stage_from"),
    )
    target_stage: ApplicationStage | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "target_stage",
            "requested_stage",
            "to_stage",
            "stage_to",
            "next_stage",
        ),
    )

    @field_validator("operation", mode="before")
    @classmethod
    def normalize_operation_value(cls, value: object) -> OperationName:
        return normalize_operation(value)

    @model_validator(mode="after")
    def derive_evidence_summary(self) -> ApprovalPreview:
        if self.evidence_summary:
            object.__setattr__(
                self,
                "evidence_summary",
                canonical_evidence_summary(self.evidence_summary),
            )
            return self
        if not self.evidence:
            return self

        values: list[str] = []
        for item in self.evidence:
            if isinstance(item, str):
                text = canonical_evidence_summary(item)
            else:
                text = item.summary or item.source_ref or item.source
                text = canonical_evidence_summary(text)
            if text:
                values.append(text)
        if values:
            object.__setattr__(self, "evidence_summary", "; ".join(values))
        return self


class ApprovalToken(ApprovalModel):
    """An immutable approval capability bound to one exact preview."""

    token_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    operation: OperationName
    idempotency_key: str = Field(min_length=1)
    evidence_summary: str = Field(min_length=1)
    evidence_digest: str = ""
    issued_at: datetime
    expires_at: datetime
    status: ApprovalStatus = ApprovalStatus.PENDING
    consumed: bool = False
    consumed_at: datetime | None = None

    @field_validator("operation", mode="before")
    @classmethod
    def normalize_operation_value(cls, value: object) -> OperationName:
        return normalize_operation(value)

    @field_validator("evidence_summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        return canonical_evidence_summary(value)

    @model_validator(mode="after")
    def validate_binding_digest(self) -> ApprovalToken:
        expected = evidence_digest(self.evidence_summary)
        if self.evidence_digest and self.evidence_digest != expected:
            raise ValueError("evidence_digest does not match evidence_summary")
        object.__setattr__(self, "evidence_digest", expected)
        if self.consumed != (self.status is ApprovalStatus.CONSUMED):
            raise ValueError("consumed and status must agree")
        if self.consumed_at is not None and not self.consumed:
            raise ValueError("consumed_at requires a consumed token")
        return self


class ApprovalDecision(ApprovalModel):
    allowed: bool
    status: ApprovalStatus
    reason: str = Field(min_length=1)
    error_code: PolicyErrorCode | None = None
    token: ApprovalToken | None = None

    @model_validator(mode="after")
    def validate_error_state(self) -> ApprovalDecision:
        if self.allowed and self.error_code is not None:
            raise ValueError("allowed decisions cannot contain an error code")
        if not self.allowed and self.error_code is None:
            raise ValueError("rejected decisions require an error code")
        return self

    @property
    def code(self) -> PolicyErrorCode | None:
        return self.error_code

    @property
    def message(self) -> str:
        return self.reason


__all__ = [
    "ApprovalDecision",
    "ApprovalModel",
    "ApprovalPreview",
    "ApprovalStatus",
    "ApprovalToken",
    "EvidenceRef",
    "OperationName",
    "PolicyErrorCode",
    "canonical_evidence_summary",
    "evidence_digest",
    "normalize_operation",
    "utc_now",
]
