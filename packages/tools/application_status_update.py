"""One guarded write boundary for application progress evidence.

Mail and browser observations reach the same application-stage adapter through
this module. Reading mail, reviewing mail, and observing a page remain separate
operations and never call this writer implicitly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Literal

from pydantic import Field, field_validator

from packages.approval import AgentApplicationWriteAdapter
from packages.domain.models import Application, ApplicationStage
from packages.recruitment_mail import (
    backfill_mail_authentication,
    find_stale_company_only_match,
    RecruitmentMailProcessingStatus,
    RecruitmentMailStore,
)
from packages.repositories.base import RecruitmentRepository

from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolModel, ToolResponse, ToolStatus


class ApplicationStatusUpdateInput(ToolInput):
    application_id: str = Field(min_length=1, max_length=255)
    evidence_type: Literal["mail", "page"]
    evidence_id: str = Field(min_length=1, max_length=255)
    target_status: Literal[
        "interested", "applied", "assessment", "written", "interview1", "interview2",
        "interview3", "hr", "offer", "rejected", "withdrawn"
    ]
    observed_label: str | None = Field(default=None, max_length=200)
    evidence: str | None = Field(default=None, max_length=2_000)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    captured_at: datetime | None = None

    @field_validator("application_id", "evidence_id")
    @classmethod
    def strip_ids(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("identifier is required")
        return value


class ApplicationStatusUpdateData(ToolModel):
    application_id: str
    current_stage: ApplicationStage
    target_stage: ApplicationStage
    state: Literal["updated", "unchanged", "blocked"]
    reason_code: str
    reason: str
    wrote: bool = False
    audit_id: str
    idempotency_key: str


class ApplicationStatusUpdateResponse(ToolResponse[ApplicationStatusUpdateData]):
    read_only: Literal[False] = False
    retryable: bool = False
    next_action: str | None = None


def _response(
    request: ApplicationStatusUpdateInput,
    *,
    status: ToolStatus,
    data: ApplicationStatusUpdateData | None,
    evidence: list[EvidenceSource],
    error_code: ToolErrorCode | None = None,
    error_message: str | None = None,
    next_action: str | None = None,
) -> ApplicationStatusUpdateResponse:
    return ApplicationStatusUpdateResponse(
        tool_name="application_status_update",
        status=status,
        success=status is ToolStatus.SUCCESS,
        data=data,
        evidence=evidence,
        error_code=error_code,
        error_message=error_message,
        timeout_ms=request.timeout_ms,
        elapsed_ms=0,
        read_only=False,
        retryable=False,
        next_action=next_action,
    )


def _application(repository: RecruitmentRepository, application_id: str) -> Application | None:
    for item in repository.list_applications():
        if str(item.id) == application_id:
            return item
    return None


def _authenticated(record: object) -> bool:
    if getattr(record, "source", None) != "imap_readonly":
        return False
    metadata = getattr(record, "raw_metadata", {})
    transport = metadata.get("transport", {}) if isinstance(metadata, dict) else {}
    from packages.recruitment_mail.authentication import has_aligned_authentication
    return has_aligned_authentication(transport)


def _audit_id(request: ApplicationStatusUpdateInput) -> str:
    value = f"{request.evidence_type}:{request.evidence_id}:{request.application_id}:{request.target_status}"
    return f"status-audit:{sha256(value.encode()).hexdigest()[:32]}"


def _mail_update(
    request: ApplicationStatusUpdateInput,
    application: Application,
    repository: RecruitmentRepository,
    store: RecruitmentMailStore,
    settings: object | None,
) -> ApplicationStatusUpdateResponse:
    record = store.get(record_id=request.evidence_id)
    evidence = [EvidenceSource(source="recruitment_mail", source_ref=request.evidence_id)]
    if record is None:
        return _response(request, status=ToolStatus.FAILURE, data=None, evidence=evidence,
                         error_code=ToolErrorCode.NOT_FOUND, error_message="Persisted mail evidence was not found.")
    from packages.recruitment_mail.analysis_binding import parsed_model_evidence, model_application_matches
    from packages.recruitment_mail.binding import confirmed_binding_matches, binding_revision
    from packages.recruitment_mail.model_analysis import MAIL_ANALYSIS_VERSION

    analysis = (record.raw_metadata or {}).get("model_analysis")
    attempt = (record.raw_metadata or {}).get("model_processing", {})
    if (record.processing_status in {"ambiguous_application", "failed_terminal", "needs_auth_metadata"}
            and attempt.get("state") != "running"):
        return _response(request, status=ToolStatus.FAILURE, data=None, evidence=evidence,
                         error_code=ToolErrorCode.INVALID_SOURCE,
                         error_message="Stored failure is terminal; use versioned mail processing after evidence changes.")
    if analysis is not None:
        try:
            if analysis.get("version", "").split(":", 1)[0] != MAIL_ANALYSIS_VERSION or analysis.get("digest") != record.content_digest:
                raise ValueError("stale_model_analysis")
            parsed = parsed_model_evidence(record, analysis["payload"])
            proposed_id = analysis["payload"].get("candidate_application_id")
            if (proposed_id is not None and proposed_id != application.id
                    and confirmed_binding_matches(record, application) is not True):
                raise ValueError("model_candidate_mismatch")
        except (ValueError, KeyError, TypeError, AttributeError):
            return _response(request, status=ToolStatus.FAILURE, data=None, evidence=evidence,
                             error_code=ToolErrorCode.INVALID_SOURCE,
                             error_message="Stored model analysis did not pass source validation.")
    else:
        return _response(request, status=ToolStatus.FAILURE, data=None, evidence=evidence,
                         error_code=ToolErrorCode.INVALID_SOURCE,
                         error_message="Process this mail with recruitment_mail_process before a status write.")
    if record.application_id and str(record.application_id) != application.id:
        store.update_processing_status(
            record.id,
            RecruitmentMailProcessingStatus.AMBIGUOUS_APPLICATION,
            processing_error="mail_bound_to_another_application; retryable=false",
        )
        return _response(request, status=ToolStatus.FAILURE, data=None, evidence=evidence,
                         error_code=ToolErrorCode.AMBIGUOUS_MATCH,
                         error_message="Mail is bound to another application; retryable=false.")
    applications = repository.list_applications()
    matches = [item for item in applications
               if model_application_matches(record, analysis["payload"], item)]
    stale_company_match = find_stale_company_only_match(parsed, applications)
    safe_stale_noop = (
        len(matches) == 0
        and "confirmed_application_binding" not in (record.raw_metadata or {})
        and stale_company_match is not None
        and str(stale_company_match.id) == str(application.id)
    )
    if not safe_stale_noop and (len(matches) != 1 or str(matches[0].id) != str(application.id)):
        store.update_processing_status(
            record.id,
            RecruitmentMailProcessingStatus.AMBIGUOUS_APPLICATION,
            processing_error="application_identity_not_unique; retryable=false",
        )
        return _response(request, status=ToolStatus.FAILURE, data=None, evidence=evidence,
                         error_code=ToolErrorCode.AMBIGUOUS_MATCH,
                         error_message="Company and job title do not exactly identify this application; retryable=false.")
    detail = repository.get_job(application.job_id) if application.job_id else None
    company_id = detail.job.company_id if detail is not None else None
    # A verified identity may be linked even when the status write is blocked
    # by missing sender authentication; the two outcomes are intentionally
    # separate in the mailbox state machine.
    store.update_associations(record.id, application_id=application.id, job_id=application.job_id, company_id=company_id)
    if record.source != "imap_readonly":
        store.update_processing_status(
            record.id,
            RecruitmentMailProcessingStatus.FAILED_TERMINAL,
            processing_error="source_not_imap_readonly; retryable=false",
        )
        return _response(
            request,
            status=ToolStatus.FAILURE,
            data=None,
            evidence=evidence,
            error_code=ToolErrorCode.INVALID_SOURCE,
            error_message="Mail was not persisted by the read-only IMAP connector; retryable=false.",
        )
    target = ApplicationStage(request.target_status)
    category_targets = {
        "application_confirmation": ApplicationStage.APPLIED,
        # An assessment invitation is not evidence of a written-test stage.
        "assessment": ApplicationStage.APPLIED,
        "written_test": ApplicationStage.WRITTEN,
        "interview": ApplicationStage.INTERVIEW1,
        "offer": ApplicationStage.OFFER,
        "rejection": ApplicationStage.REJECTED,
    }
    expected_target = category_targets.get(parsed.category.value)
    if expected_target is None or target is not expected_target:
        store.update_processing_status(
            record.id,
            RecruitmentMailProcessingStatus.FAILED_TERMINAL,
            processing_error="target_stage_category_mismatch; retryable=false",
        )
        return _response(request, status=ToolStatus.FAILURE, data=None, evidence=evidence,
                         error_code=ToolErrorCode.INVALID_INPUT,
                         error_message="Target stage does not match the persisted mail category; retryable=false.")
    if not _authenticated(record) and settings is not None:
        backfill_mail_authentication(settings, store, record.id)
        record = store.get(record_id=record.id)
        assert record is not None
    if not _authenticated(record):
        if safe_stale_noop:
            audit_id = _audit_id(request)
            base = dict(
                application_id=application.id,
                current_stage=application.stage,
                target_stage=target,
                state="unchanged",
                reason_code="stale_evidence",
                reason="Older mail cannot move the uniquely identified company application backwards.",
                wrote=False,
                audit_id=audit_id,
                idempotency_key=audit_id.removeprefix("status-audit:"),
            )
            store.update_processing_status(
                record.id,
                RecruitmentMailProcessingStatus.PROCESSED_UNCHANGED,
                processing_error=None,
            )
            return _response(
                request,
                status=ToolStatus.SUCCESS,
                data=ApplicationStatusUpdateData(**base),
                evidence=evidence,
            )
        store.update_processing_status(
            record.id,
            RecruitmentMailProcessingStatus.NEEDS_AUTH_METADATA,
            processing_error="mail_authentication_unavailable; retryable=false",
        )
        return _response(request, status=ToolStatus.FAILURE, data=None, evidence=evidence,
                         error_code=ToolErrorCode.INVALID_SOURCE,
                         error_message="Trusted aligned DKIM or strict aligned SPF authentication is unavailable; retryable=false.",
                         next_action="Do not retry this evidence unless its stored authentication metadata changes.")
    if parsed.received_at is None:
        store.update_processing_status(
            record.id,
            RecruitmentMailProcessingStatus.FAILED_TERMINAL,
            processing_error="trusted_event_timestamp_missing; retryable=false",
        )
        return _response(request, status=ToolStatus.FAILURE, data=None, evidence=evidence,
                         error_code=ToolErrorCode.INVALID_SOURCE,
                         error_message="Mail has no trusted event timestamp; retryable=false.")
    audit_id = _audit_id(request)
    idem = audit_id.removeprefix("status-audit:")
    base = dict(application_id=application.id, current_stage=application.stage,
                target_stage=target, state="blocked", reason_code="", reason="",
                wrote=False, audit_id=audit_id, idempotency_key=idem)
    if safe_stale_noop:
        store.update_processing_status(
            record.id,
            RecruitmentMailProcessingStatus.PROCESSED_UNCHANGED,
            processing_error=None,
        )
        base.update(
            state="unchanged",
            reason_code="stale_evidence",
            reason="Older mail cannot move the uniquely identified company application backwards.",
        )
        return _response(
            request,
            status=ToolStatus.SUCCESS,
            data=ApplicationStatusUpdateData(**base),
            evidence=evidence,
        )
    if application.stage is target:
        if record.processing_status != RecruitmentMailProcessingStatus.PROCESSED_UPDATED.value:
            store.update_processing_status(
                record.id,
                RecruitmentMailProcessingStatus.PROCESSED_UNCHANGED,
                processing_error=None,
            )
        base.update(state="unchanged", reason_code="already_at_target", reason="Application is already at the requested stage.")
        return _response(request, status=ToolStatus.SUCCESS, data=ApplicationStatusUpdateData(**base), evidence=evidence)
    if application.source_status_synced_at is not None:
        current_time = application.source_status_synced_at
        received = parsed.received_at
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        # A delivery-card observation has no hiring event time. It must not
        # veto an explicit rejection just because the card was fetched later.
        passive_delivery = (
            target is ApplicationStage.REJECTED
            and application.stage is ApplicationStage.APPLIED
            and (application.source_status or "").strip().casefold()
            in {"投递", "已投递", "投递成功", "applied"}
            and not any(
                item.get("source") == "recruitment_mail"
                or item.get("event_time")
                for item in application.stage_history
            )
        )
        if received <= current_time and not passive_delivery:
            base.update(
                state="unchanged",
                reason_code="stale_evidence",
                reason="Mail event is older than the persisted application evidence.",
            )
            store.update_processing_status(
                record.id,
                RecruitmentMailProcessingStatus.PROCESSED_UNCHANGED,
                processing_error=None,
            )
            return _response(
                request,
                status=ToolStatus.SUCCESS,
                data=ApplicationStatusUpdateData(**base),
                evidence=evidence,
            )
    adapter = AgentApplicationWriteAdapter(store.storage)
    try:
        effect = adapter.update_application_stage({
            "application_id": application.id,
            "current_stage": application.stage.value,
            "target_stage": target.value,
            "source_stage": target.value,
            "source_status": parsed.category.value,
            "source_status_synced_at": parsed.received_at.isoformat(),
            "result": "淘汰" if target is ApplicationStage.REJECTED else "进行中",
            "note": f"招聘邮件已核验：{record.subject}",
            "source": "recruitment_mail",
            "source_ref": record.id,
            "idempotency_key": idem,
            "audit_id": audit_id,
            "event_time": parsed.received_at.isoformat(),
            "mail_record_id": record.id,
            "mail_content_digest": record.content_digest,
            "mail_binding_revision": binding_revision(record),
        })
    except ValueError as exc:
        base.update(reason_code="write_conflict", reason=str(exc))
        store.update_processing_status(
            record.id,
            RecruitmentMailProcessingStatus.FAILED_TERMINAL,
            processing_error="write_conflict; retryable=false",
        )
        return _response(request, status=ToolStatus.FAILURE, data=ApplicationStatusUpdateData(**base), evidence=evidence,
                         error_code=ToolErrorCode.INVALID_SOURCE, error_message=str(exc))
    if effect.before == effect.after:
        base.update(state="unchanged", reason_code="evidence_already_applied", reason="This evidence was already applied.")
        return _response(request, status=ToolStatus.SUCCESS, data=ApplicationStatusUpdateData(**base), evidence=evidence)
    base.update(state="updated", reason_code="status_updated", reason="Application stage updated from verified mail evidence.", wrote=True)
    return _response(request, status=ToolStatus.SUCCESS, data=ApplicationStatusUpdateData(**base), evidence=evidence)


def update_application_status(
    request: ApplicationStatusUpdateInput,
    repository: RecruitmentRepository,
    mail_store: RecruitmentMailStore,
    browser_bridge: object | None = None,
    *,
    settings: object | None = None,
) -> ApplicationStatusUpdateResponse:
    """Update application progress from one persisted, typed evidence record."""

    application = _application(repository, request.application_id)
    evidence = [EvidenceSource(source=request.evidence_type, source_ref=request.evidence_id)]
    if application is None:
        return _response(request, status=ToolStatus.FAILURE, data=None, evidence=evidence,
                         error_code=ToolErrorCode.NOT_FOUND, error_message="Application was not found.")
    if request.evidence_type == "mail":
        return _mail_update(request, application, repository, mail_store, settings)
    if browser_bridge is None:
        return _response(request, status=ToolStatus.FAILURE, data=None, evidence=evidence,
                         error_code=ToolErrorCode.SOURCE_UNAVAILABLE, error_message="Browser evidence storage is unavailable.")
    if request.target_status in {"interview2", "interview3"}:
        return _response(request, status=ToolStatus.FAILURE, data=None, evidence=evidence,
                         error_code=ToolErrorCode.INVALID_INPUT,
                         error_message="Page evidence must identify the generic first interview stage.")
    if not request.observed_label or not request.evidence or request.captured_at is None:
        return _response(request, status=ToolStatus.FAILURE, data=None, evidence=evidence,
                         error_code=ToolErrorCode.INVALID_INPUT, error_message="Page evidence requires label, text and capture time.")
    from .application_status_evidence import VerifyApplicationStatusEvidenceInput, verify_application_status_evidence

    verified = verify_application_status_evidence(
        VerifyApplicationStatusEvidenceInput(
            application_id=request.application_id,
            observation_operation_id=request.evidence_id,
            observed_status=("interview" if request.target_status.startswith("interview") else request.target_status),
            observed_label=request.observed_label,
            evidence=request.evidence,
            confidence=request.confidence,
            captured_at=request.captured_at,
        ),
        browser_bridge,
    )
    if not verified.success or verified.verification is None or verified.verification.data is None:
        base = ApplicationStatusUpdateData(
            application_id=application.id, current_stage=application.stage,
            target_stage=ApplicationStage(request.target_status), state="blocked",
            reason_code=str(verified.error_code or "page_evidence_rejected"),
            reason=verified.error_message or "Persisted page evidence was rejected.",
            audit_id=f"status-audit:{request.evidence_id}", idempotency_key=request.evidence_id,
        )
        return _response(request, status=ToolStatus.FAILURE, data=base, evidence=evidence,
                         error_code=ToolErrorCode.INVALID_SOURCE, error_message=base.reason)
    page = verified.verification.data
    state = "updated" if page.wrote else "unchanged"
    return _response(request, status=ToolStatus.SUCCESS, evidence=evidence,
                     data=ApplicationStatusUpdateData(
                         application_id=application.id, current_stage=page.current_stage or application.stage,
                         target_stage=page.target_stage or ApplicationStage(request.target_status), state=state,
                         reason_code=page.reason_code, reason=page.reason, wrote=page.wrote,
                         audit_id=page.audit_id, idempotency_key=page.idempotency_key,
                     ))


__all__ = ["ApplicationStatusUpdateData", "ApplicationStatusUpdateInput", "ApplicationStatusUpdateResponse", "update_application_status"]
