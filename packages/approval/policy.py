from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from packages.domain.models import ApplicationStage

from .models import (
    ApprovalDecision,
    ApprovalPreview,
    ApprovalStatus,
    ApprovalToken,
    OperationName,
    PolicyErrorCode,
    canonical_evidence_summary,
    evidence_digest,
    normalize_operation,
    utc_now,
)


_STAGE_ORDER = {
    ApplicationStage.INTERESTED: 0,
    ApplicationStage.APPLIED: 1,
    ApplicationStage.ASSESSMENT: 2,
    ApplicationStage.WRITTEN: 3,
    ApplicationStage.INTERVIEW1: 4,
    ApplicationStage.INTERVIEW2: 5,
    ApplicationStage.INTERVIEW3: 6,
    ApplicationStage.HR: 7,
    ApplicationStage.OFFER: 8,
    ApplicationStage.REJECTED: 9,
    ApplicationStage.WITHDRAWN: 9,
}
_TERMINAL_STAGES = frozenset({ApplicationStage.REJECTED, ApplicationStage.WITHDRAWN})


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _expired(expires_at: datetime, now: datetime) -> bool:
    return _utc(now) >= _utc(expires_at)


def _rejected(
    code: PolicyErrorCode,
    reason: str,
    *,
    status: ApprovalStatus = ApprovalStatus.REJECTED,
    token: ApprovalToken | None = None,
) -> ApprovalDecision:
    return ApprovalDecision(
        allowed=False,
        status=status,
        reason=reason,
        error_code=code,
        token=token,
    )


def _allowed(
    status: ApprovalStatus,
    reason: str,
    *,
    token: ApprovalToken | None = None,
) -> ApprovalDecision:
    return ApprovalDecision(
        allowed=True,
        status=status,
        reason=reason,
        token=token,
    )


def _payload_value(preview: ApprovalPreview, *names: str) -> Any:
    for name in names:
        value = getattr(preview, name, None)
        if value is not None:
            return value
    for name in names:
        if name in preview.payload and preview.payload[name] is not None:
            return preview.payload[name]
    job = preview.payload.get("job")
    if isinstance(job, dict):
        for name in names:
            if job.get(name) is not None:
                return job[name]
    return None


def _stage(preview: ApprovalPreview, *names: str) -> ApplicationStage | None:
    value = _payload_value(preview, *names)
    if value is None:
        return None
    try:
        return value if isinstance(value, ApplicationStage) else ApplicationStage(str(value))
    except ValueError:
        return None


def _job_context_present(preview: ApprovalPreview) -> bool:
    return any(
        _payload_value(preview, name) is not None
        for name in ("cohort", "cohort_status", "jd_raw", "jd", "complete_jd")
    )


def validate_preview(
    preview: ApprovalPreview,
    *,
    now: datetime | None = None,
    existing_idempotency_keys: Collection[str] = (),
) -> ApprovalDecision:
    """Evaluate a preview without issuing or persisting anything."""

    checked_at = now or utc_now()
    if not preview.evidence_summary or not canonical_evidence_summary(preview.evidence_summary):
        return _rejected(
            PolicyErrorCode.EVIDENCE_MISSING,
            "An approval preview must include an evidence summary.",
        )
    if _expired(preview.expires_at, checked_at):
        return _rejected(
            PolicyErrorCode.PREVIEW_EXPIRED,
            "The approval preview has expired.",
            status=ApprovalStatus.EXPIRED,
        )
    if preview.idempotency_key in existing_idempotency_keys:
        return _rejected(
            PolicyErrorCode.DUPLICATE_WRITE,
            "The idempotency key has already been used for a write.",
        )

    if preview.operation is OperationName.APPLICATION_STAGE_UPDATE:
        current = _stage(preview, "current_stage", "from_stage", "stage_from")
        target = _stage(
            preview,
            "target_stage",
            "requested_stage",
            "to_stage",
            "stage_to",
            "next_stage",
        )
        if current is not None and target is not None:
            if current in _TERMINAL_STAGES and target is not current:
                return _rejected(
                    PolicyErrorCode.STAGE_REGRESSION,
                    "A terminal application stage cannot move to another stage.",
                )
            if _STAGE_ORDER[target] < _STAGE_ORDER[current]:
                return _rejected(
                    PolicyErrorCode.STAGE_REGRESSION,
                    "Application stages may not move backwards.",
                )

    requires_job_gate = _job_context_present(preview)
    if requires_job_gate:
        cohort = _payload_value(preview, "cohort")
        cohort_status = _payload_value(preview, "cohort_status")
        if cohort != 2027 or str(cohort_status or "").casefold() != "confirmed":
            return _rejected(
                PolicyErrorCode.COHORT_NOT_CONFIRMED,
                "Only confirmed 2027 recruitment jobs may be written.",
            )
        jd = _payload_value(preview, "jd_raw", "jd", "complete_jd")
        if not isinstance(jd, str) or not jd.strip():
            return _rejected(
                PolicyErrorCode.INCOMPLETE_JD,
                "A complete JD is required before this write may be approved.",
            )

    return _allowed(
        ApprovalStatus.PENDING,
        "The preview satisfies the approval policy and may be presented for approval.",
    )


def evaluate_preview(
    preview: ApprovalPreview,
    *,
    now: datetime | None = None,
    existing_idempotency_keys: Collection[str] = (),
) -> ApprovalDecision:
    """Alias for callers that use the policy-evaluation vocabulary."""

    return validate_preview(
        preview,
        now=now,
        existing_idempotency_keys=existing_idempotency_keys,
    )


def _default_token_id(preview: ApprovalPreview) -> str:
    material = "\x00".join(
        (
            preview.task_id,
            preview.operation.value,
            preview.idempotency_key,
            evidence_digest(preview.evidence_summary or ""),
            _utc(preview.expires_at).isoformat(),
        )
    )
    return sha256(material.encode("utf-8")).hexdigest()


def issue_approval_token(
    preview: ApprovalPreview,
    *,
    now: datetime | None = None,
    token_id: str | None = None,
    existing_idempotency_keys: Collection[str] = (),
) -> ApprovalDecision:
    """Create a token model after the preview passes all deterministic checks."""

    checked_at = now or utc_now()
    decision = validate_preview(
        preview,
        now=checked_at,
        existing_idempotency_keys=existing_idempotency_keys,
    )
    if not decision.allowed:
        return decision

    token = ApprovalToken(
        token_id=token_id or _default_token_id(preview),
        task_id=preview.task_id,
        operation=preview.operation,
        idempotency_key=preview.idempotency_key,
        evidence_summary=preview.evidence_summary or "",
        issued_at=checked_at,
        expires_at=preview.expires_at,
    )
    return _allowed(
        ApprovalStatus.PENDING,
        "Approval token issued and awaiting an operator decision.",
        token=token,
    )


def create_approval_token(
    preview: ApprovalPreview,
    *,
    now: datetime | None = None,
    token_id: str | None = None,
    existing_idempotency_keys: Collection[str] = (),
) -> ApprovalDecision:
    """Alias for token creation used by API-facing callers."""

    return issue_approval_token(
        preview,
        now=now,
        token_id=token_id,
        existing_idempotency_keys=existing_idempotency_keys,
    )


def approve_token(
    token: ApprovalToken,
    *,
    now: datetime | None = None,
) -> ApprovalDecision:
    checked_at = now or utc_now()
    if token.consumed:
        return _rejected(
            PolicyErrorCode.TOKEN_ALREADY_CONSUMED,
            "The approval token has already been consumed.",
            status=ApprovalStatus.CONSUMED,
            token=token,
        )
    if token.status is ApprovalStatus.EXECUTING:
        return _rejected(
            PolicyErrorCode.TOKEN_IN_PROGRESS,
            "The approval token is currently executing a write.",
            status=ApprovalStatus.EXECUTING,
            token=token,
        )
    if token.status is ApprovalStatus.REJECTED:
        return _rejected(
            PolicyErrorCode.TOKEN_REJECTED,
            "The approval token was rejected.",
            token=token,
        )
    if token.status is ApprovalStatus.EXPIRED or _expired(token.expires_at, checked_at):
        expired = token.model_copy(update={"status": ApprovalStatus.EXPIRED})
        return _rejected(
            PolicyErrorCode.TOKEN_EXPIRED,
            "The approval token has expired.",
            status=ApprovalStatus.EXPIRED,
            token=expired,
        )
    if token.status is ApprovalStatus.APPROVED:
        return _allowed(ApprovalStatus.APPROVED, "The approval token is approved.", token=token)
    approved = token.model_copy(update={"status": ApprovalStatus.APPROVED})
    return _allowed(ApprovalStatus.APPROVED, "The approval token was approved.", token=approved)


def reject_token(
    token: ApprovalToken,
    *,
    now: datetime | None = None,
    reason: str = "The operator rejected this write preview.",
) -> ApprovalDecision:
    checked_at = now or utc_now()
    if token.consumed:
        return _rejected(
            PolicyErrorCode.TOKEN_ALREADY_CONSUMED,
            "The approval token has already been consumed.",
            status=ApprovalStatus.CONSUMED,
            token=token,
        )
    if token.status is ApprovalStatus.EXECUTING:
        return _rejected(
            PolicyErrorCode.TOKEN_IN_PROGRESS,
            "The approval token is currently executing a write.",
            status=ApprovalStatus.EXECUTING,
            token=token,
        )
    if token.status is ApprovalStatus.EXPIRED or _expired(token.expires_at, checked_at):
        expired = token.model_copy(update={"status": ApprovalStatus.EXPIRED})
        return _rejected(
            PolicyErrorCode.TOKEN_EXPIRED,
            "The approval token has expired.",
            status=ApprovalStatus.EXPIRED,
            token=expired,
        )
    rejected = token.model_copy(update={"status": ApprovalStatus.REJECTED})
    return _rejected(PolicyErrorCode.TOKEN_REJECTED, reason, token=rejected)


def consume_token(
    token: ApprovalToken,
    preview: ApprovalPreview,
    *,
    now: datetime | None = None,
    existing_idempotency_keys: Collection[str] = (),
) -> ApprovalDecision:
    """Authorize one matching write and return a consumed token copy.

    The caller must retain the returned token. No process-global ledger or external write is
    used here; persistence of the consumed token belongs to the integration layer.
    """

    checked_at = now or utc_now()
    if token.consumed:
        return _rejected(
            PolicyErrorCode.TOKEN_ALREADY_CONSUMED,
            "The approval token has already been consumed.",
            status=ApprovalStatus.CONSUMED,
            token=token,
        )
    if token.status is ApprovalStatus.EXECUTING:
        return _rejected(
            PolicyErrorCode.TOKEN_IN_PROGRESS,
            "The approval token is currently executing a write.",
            status=ApprovalStatus.EXECUTING,
            token=token,
        )
    if token.status is ApprovalStatus.REJECTED:
        return _rejected(
            PolicyErrorCode.TOKEN_REJECTED,
            "The approval token was rejected.",
            token=token,
        )
    if token.status is ApprovalStatus.EXPIRED or _expired(token.expires_at, checked_at):
        expired = token.model_copy(update={"status": ApprovalStatus.EXPIRED})
        return _rejected(
            PolicyErrorCode.TOKEN_EXPIRED,
            "The approval token has expired.",
            status=ApprovalStatus.EXPIRED,
            token=expired,
        )
    if token.status is not ApprovalStatus.APPROVED:
        return _rejected(
            PolicyErrorCode.TOKEN_NOT_APPROVED,
            "The approval token must be approved before a write can be consumed.",
            token=token,
        )

    if preview.idempotency_key in existing_idempotency_keys:
        return _rejected(
            PolicyErrorCode.DUPLICATE_WRITE,
            "The idempotency key has already been used for a write.",
            token=token,
        )
    if (
        preview.task_id != token.task_id
        or preview.operation is not token.operation
        or preview.idempotency_key != token.idempotency_key
        or not preview.evidence_summary
        or evidence_digest(preview.evidence_summary) != token.evidence_digest
    ):
        return _rejected(
            PolicyErrorCode.TOKEN_BINDING_MISMATCH,
            "The write preview does not match the approval token binding.",
            token=token,
        )

    policy = validate_preview(
        preview,
        now=checked_at,
        existing_idempotency_keys=(),
    )
    if not policy.allowed:
        return policy.model_copy(update={"token": token})

    consumed = token.model_copy(
        update={
            "status": ApprovalStatus.CONSUMED,
            "consumed": True,
            "consumed_at": checked_at,
        }
    )
    return _allowed(
        ApprovalStatus.CONSUMED,
        "The approved write is authorized once and the token is consumed.",
        token=consumed,
    )


def begin_token(
    token: ApprovalToken,
    preview: ApprovalPreview,
    *,
    now: datetime | None = None,
    existing_idempotency_keys: Collection[str] = (),
) -> ApprovalDecision:
    """Claim an approved write before any external side effect is attempted."""

    decision = consume_token(
        token,
        preview,
        now=now,
        existing_idempotency_keys=existing_idempotency_keys,
    )
    if not decision.allowed or decision.token is None:
        return decision
    executing = decision.token.model_copy(
        update={
            "status": ApprovalStatus.EXECUTING,
            "consumed": False,
            "consumed_at": None,
        }
    )
    return _allowed(
        ApprovalStatus.EXECUTING,
        "The approved write was claimed for execution.",
        token=executing,
    )


def complete_token(
    token: ApprovalToken,
    *,
    now: datetime | None = None,
) -> ApprovalDecision:
    """Commit the one-time token after the claimed adapter write succeeds."""

    checked_at = now or utc_now()
    if token.consumed or token.status is ApprovalStatus.CONSUMED:
        return _rejected(
            PolicyErrorCode.TOKEN_ALREADY_CONSUMED,
            "The approval token has already been consumed.",
            status=ApprovalStatus.CONSUMED,
            token=token,
        )
    if token.status is not ApprovalStatus.EXECUTING:
        return _rejected(
            PolicyErrorCode.TOKEN_STATE_CONFLICT,
            "The approval token is not executing a write.",
            status=token.status,
            token=token,
        )
    consumed = token.model_copy(
        update={
            "status": ApprovalStatus.CONSUMED,
            "consumed": True,
            "consumed_at": checked_at,
        }
    )
    return _allowed(
        ApprovalStatus.CONSUMED,
        "The claimed write succeeded and the token is consumed.",
        token=consumed,
    )


def release_token(
    token: ApprovalToken,
    *,
    now: datetime | None = None,
) -> ApprovalDecision:
    """Release a failed, uncommitted claim so the approved write can be retried."""

    checked_at = now or utc_now()
    if token.consumed or token.status is ApprovalStatus.CONSUMED:
        return _rejected(
            PolicyErrorCode.TOKEN_ALREADY_CONSUMED,
            "The approval token has already been consumed.",
            status=ApprovalStatus.CONSUMED,
            token=token,
        )
    if token.status is not ApprovalStatus.EXECUTING:
        return _rejected(
            PolicyErrorCode.TOKEN_STATE_CONFLICT,
            "The approval token is not executing a write.",
            status=token.status,
            token=token,
        )
    if _expired(token.expires_at, checked_at):
        expired = token.model_copy(update={"status": ApprovalStatus.EXPIRED})
        return _rejected(
            PolicyErrorCode.TOKEN_EXPIRED,
            "The approval token expired before the failed write could be retried.",
            status=ApprovalStatus.EXPIRED,
            token=expired,
        )
    approved = token.model_copy(
        update={
            "status": ApprovalStatus.APPROVED,
            "consumed": False,
            "consumed_at": None,
        }
    )
    return _allowed(
        ApprovalStatus.APPROVED,
        "The failed write claim was released and may be retried.",
        token=approved,
    )


def authorize_write(
    token: ApprovalToken,
    preview: ApprovalPreview,
    *,
    now: datetime | None = None,
    existing_idempotency_keys: Collection[str] = (),
) -> ApprovalDecision:
    """Alias for consuming an approved token against a write preview."""

    return consume_token(
        token,
        preview,
        now=now,
        existing_idempotency_keys=existing_idempotency_keys,
    )


__all__ = [
    "approve_token",
    "authorize_write",
    "begin_token",
    "complete_token",
    "consume_token",
    "create_approval_token",
    "evaluate_preview",
    "issue_approval_token",
    "release_token",
    "reject_token",
    "validate_preview",
]
