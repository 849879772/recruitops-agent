from datetime import datetime, timedelta, timezone

import pytest

from packages.approval import (
    ApprovalPreview,
    ApprovalStatus,
    OperationName,
    PolicyErrorCode,
    approve_token,
    consume_token,
    issue_approval_token,
    normalize_operation,
    reject_token,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)


def _preview(**updates: object) -> ApprovalPreview:
    values: dict[str, object] = {
        "task_id": "task-1",
        "operation": "schedule_create",
        "idempotency_key": "write:1",
        "evidence_summary": "Official 2027 campus job page confirms the role and JD.",
        "expires_at": NOW + timedelta(hours=1),
        "cohort": 2027,
        "cohort_status": "confirmed",
        "jd_raw": "Responsibilities\nQualifications",
        "payload": {"job_id": "job-1"},
    }
    values.update(updates)
    return ApprovalPreview(**values)


def _approved_token(preview: ApprovalPreview | None = None):
    issued = issue_approval_token(preview or _preview(), now=NOW, token_id="token-1")
    assert issued.allowed is True
    assert issued.token is not None
    approved = approve_token(issued.token, now=NOW)
    assert approved.allowed is True
    assert approved.token is not None
    return approved.token


def test_operation_names_are_normalized_to_one_canonical_value() -> None:
    with pytest.raises(ValueError, match="unsupported operation"):
        normalize_operation("send-lujie")
    assert normalize_operation("company integration") is OperationName.COMPANY_CONFIG_UPDATE
    assert _preview().operation is OperationName.SCHEDULE_CREATE


def test_approve_and_consume_binds_task_operation_evidence_and_consumes_once() -> None:
    preview = _preview()
    token = _approved_token(preview)

    consumed = consume_token(token, preview, now=NOW)

    assert consumed.allowed is True
    assert consumed.status is ApprovalStatus.CONSUMED
    assert consumed.token is not None and consumed.token.consumed is True
    second = consume_token(consumed.token, preview, now=NOW)
    assert second.allowed is False
    assert second.error_code is PolicyErrorCode.TOKEN_ALREADY_CONSUMED


def test_rejected_token_cannot_be_approved_or_consumed() -> None:
    issued = issue_approval_token(_preview(), now=NOW, token_id="token-rejected")
    assert issued.token is not None

    rejected = reject_token(issued.token, now=NOW)

    assert rejected.allowed is False
    assert rejected.status is ApprovalStatus.REJECTED
    assert rejected.error_code is PolicyErrorCode.TOKEN_REJECTED
    assert rejected.token is not None
    approved = approve_token(rejected.token, now=NOW)
    assert approved.allowed is False
    assert approved.error_code is PolicyErrorCode.TOKEN_REJECTED


def test_expired_token_is_rejected() -> None:
    token = _approved_token(_preview(expires_at=NOW + timedelta(minutes=1)))

    result = approve_token(token, now=NOW + timedelta(minutes=1))

    assert result.allowed is False
    assert result.status is ApprovalStatus.EXPIRED
    assert result.error_code is PolicyErrorCode.TOKEN_EXPIRED


def test_existing_idempotency_key_rejects_duplicate_write() -> None:
    result = issue_approval_token(
        _preview(),
        now=NOW,
        existing_idempotency_keys={"write:1"},
    )

    assert result.allowed is False
    assert result.error_code is PolicyErrorCode.DUPLICATE_WRITE


def test_stage_regression_is_rejected() -> None:
    preview = _preview(
        operation=OperationName.APPLICATION_STAGE_UPDATE,
        current_stage="interview1",
        target_stage="applied",
    )

    result = issue_approval_token(preview, now=NOW)

    assert result.allowed is False
    assert result.error_code is PolicyErrorCode.STAGE_REGRESSION


def test_missing_evidence_is_rejected() -> None:
    result = issue_approval_token(_preview(evidence_summary=""), now=NOW)

    assert result.allowed is False
    assert result.error_code is PolicyErrorCode.EVIDENCE_MISSING


def test_unconfirmed_cohort_and_missing_jd_are_rejected() -> None:
    unconfirmed = issue_approval_token(
        _preview(cohort=2027, cohort_status="unconfirmed"),
        now=NOW,
    )
    missing_jd = issue_approval_token(
        _preview(cohort=2027, cohort_status="confirmed", jd_raw=""),
        now=NOW,
    )

    assert unconfirmed.error_code is PolicyErrorCode.COHORT_NOT_CONFIRMED
    assert missing_jd.error_code is PolicyErrorCode.INCOMPLETE_JD


def test_token_binding_mismatch_is_rejected_without_consuming_token() -> None:
    preview = _preview()
    token = _approved_token(preview)
    altered = _preview(evidence_summary="A different official evidence summary.")

    result = consume_token(token, altered, now=NOW)

    assert result.allowed is False
    assert result.error_code is PolicyErrorCode.TOKEN_BINDING_MISMATCH
    assert result.token is not None and result.token.consumed is False
