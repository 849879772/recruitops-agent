from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from typing import Any, Literal

from pydantic import Field

from packages.approval import ApprovalPreview, EvidenceRef, OperationName
from packages.domain.models import Application
from packages.recruitment_mail import (
    ParsedRecruitmentEmail,
    RecruitmentMailAssociation,
    RecruitmentMailProcessingStatus,
    RecruitmentMailStore,
    RecruitmentMessageCategory,
    associate_recruitment_email,
)
from packages.repositories.base import RecruitmentRepository
from packages.recruitment_mail.record_view import parsed_record

from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolModel, ToolResponse, ToolStatus


class RecruitmentMailSearchInput(ToolInput):
    on_date: date | None = None
    category: RecruitmentMessageCategory | None = None
    categories: list[RecruitmentMessageCategory] | None = Field(
        default=None,
        min_length=1,
        max_length=7,
    )
    processing_status: RecruitmentMailProcessingStatus | None = None
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class RecruitmentMailSummary(ToolModel):
    id: str
    subject: str
    sender: str | None = None
    received_at: datetime | None = None
    category: RecruitmentMessageCategory
    confidence: float = Field(ge=0.0, le=1.0)
    processing_status: str
    requires_confirmation: bool
    application_id: str | None = None
    company_name: str | None = None
    job_title: str | None = None
    event_times: list[str] = Field(default_factory=list)


class RecruitmentMailSearchData(ToolModel):
    freshness: dict[str, Any] | None = None
    items: list[RecruitmentMailSummary] = Field(default_factory=list)
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    latest_received_at: datetime | None = None


class RecruitmentMailDetailInput(ToolInput):
    record_id: str = Field(min_length=1, max_length=128)


class RecruitmentMailDetailData(ToolModel):
    freshness: dict[str, Any] | None = None
    record_id: str
    message: ParsedRecruitmentEmail
    processing_status: str
    application_id: str | None = None
    job_id: str | None = None
    company_id: str | None = None


class RecruitmentMailReviewInput(RecruitmentMailDetailInput):
    minimum_confidence: float = Field(default=0.65, ge=0.0, le=1.0)
    minimum_margin: float = Field(default=0.15, ge=0.0, le=1.0)


class RecruitmentMailReviewData(ToolModel):
    freshness: dict[str, Any] | None = None
    record_id: str
    association: RecruitmentMailAssociation
    approval_previews: list[ApprovalPreview] = Field(default_factory=list)


class RecruitmentMailSearchResponse(ToolResponse[RecruitmentMailSearchData]):
    freshness: dict[str, Any] | None = None


class RecruitmentMailDetailResponse(ToolResponse[RecruitmentMailDetailData]):
    freshness: dict[str, Any] | None = None


class RecruitmentMailReviewResponse(ToolResponse[RecruitmentMailReviewData]):
    freshness: dict[str, Any] | None = None


class RecruitmentMailSyncInput(ToolInput):
    limit: int = Field(default=100, ge=1, le=500)


class RecruitmentMailSyncData(ToolModel):
    sync: dict[str, Any]
    freshness: dict[str, Any] = Field(default_factory=dict)


class RecruitmentMailSyncResponse(ToolResponse[RecruitmentMailSyncData]):
    read_only: Literal[False] = False


def _elapsed(started: float) -> int:
    return int((perf_counter() - started) * 1000)


def _summary(record) -> RecruitmentMailSummary:
    parsed = parsed_record(record)
    return RecruitmentMailSummary(
        id=record.id,
        subject=record.subject,
        sender=record.sender,
        received_at=record.received_at,
        category=RecruitmentMessageCategory(record.category),
        confidence=record.confidence,
        processing_status=record.processing_status,
        requires_confirmation=record.requires_confirmation,
        application_id=record.application_id,
        company_name=(
            parsed.company_candidates[0].value if parsed.company_candidates else None
        ),
        job_title=parsed.job_candidates[0].value if parsed.job_candidates else None,
        event_times=[
            item.normalized.isoformat()
            for item in parsed.time_candidates
            if item.normalized is not None
        ],
    )


def search_recruitment_mail(request: RecruitmentMailSearchInput, store: RecruitmentMailStore) -> RecruitmentMailSearchResponse:
    started = perf_counter()
    records = store.query(
        start_date=request.on_date,
        end_date=request.on_date,
        category=request.category,
        categories=request.categories,
        processing_status=request.processing_status,
        limit=request.limit,
        offset=request.offset,
    )
    total = store.count(
        start_date=request.on_date,
        end_date=request.on_date,
        category=request.category,
        categories=request.categories,
        processing_status=request.processing_status,
    )
    return RecruitmentMailSearchResponse(
        tool_name="recruitment_mail_search",
        status=ToolStatus.SUCCESS if records else ToolStatus.NO_RESULTS,
        success=bool(records),
        data=RecruitmentMailSearchData(
            items=[_summary(item) for item in records],
            total=total,
            limit=request.limit,
            offset=request.offset,
            latest_received_at=max(
                (item.received_at for item in records if item.received_at is not None),
                default=None,
            ),
        ),
        evidence=[EvidenceSource(source="agent_database", source_ref="recruitment_emails")],
        error_code=None if records else ToolErrorCode.NO_RESULTS,
        error_message=None if records else "No recruitment emails matched the query.",
        timeout_ms=request.timeout_ms,
        elapsed_ms=_elapsed(started),
    )


def get_recruitment_mail(request: RecruitmentMailDetailInput, store: RecruitmentMailStore) -> RecruitmentMailDetailResponse:
    started = perf_counter()
    record = store.get(record_id=request.record_id)
    evidence = [EvidenceSource(source="agent_database", source_ref=request.record_id)]
    if record is None:
        return RecruitmentMailDetailResponse(
            tool_name="recruitment_mail_detail", status=ToolStatus.NO_RESULTS, success=False,
            evidence=evidence, error_code=ToolErrorCode.NOT_FOUND,
            error_message="Recruitment email was not found.", timeout_ms=request.timeout_ms,
            elapsed_ms=_elapsed(started),
        )
    return RecruitmentMailDetailResponse(
        tool_name="recruitment_mail_detail", status=ToolStatus.SUCCESS, success=True,
        data=RecruitmentMailDetailData(
            record_id=record.id,
            message=parsed_record(record),
            processing_status=record.processing_status,
            application_id=record.application_id, job_id=record.job_id, company_id=record.company_id,
        ),
        evidence=evidence, timeout_ms=request.timeout_ms, elapsed_ms=_elapsed(started),
    )


def _approval_previews(record_id: str, association: RecruitmentMailAssociation, applications: list[Application]) -> list[ApprovalPreview]:
    if association.match is None or association.status not in {"matched", "review_required"}:
        return []
    schedule_source_available = "schedule_source_unavailable" not in association.review_reasons
    now = datetime.now(timezone.utc)
    application = next(item for item in applications if item.id == association.match.application_id)
    previews: list[ApprovalPreview] = []
    evidence = tuple(
        EvidenceRef(
            source=item.source,
            source_ref=item.source_ref,
            summary=item.summary,
        )
        for item in association.evidence
    )
    audit_evidence = [item.model_dump(mode="json") for item in association.evidence]
    confirmation_note = (
        f" Human confirmation required: {', '.join(association.review_reasons)}."
        if association.review_reasons
        else ""
    )
    if association.stage_draft is not None:
        draft = association.stage_draft
        previews.append(ApprovalPreview(
            task_id=f"mail-review:{record_id}", operation=OperationName.APPLICATION_STAGE_UPDATE,
            target_id=application.id,
            idempotency_key=f"mail-stage:{record_id}:{application.id}:{draft.target_stage.value}",
            evidence_summary=(
                f"Recruitment email {record_id} indicates {draft.source_status}."
                f"{confirmation_note}"
            ),
            evidence=evidence,
            expires_at=now + timedelta(hours=24), current_stage=draft.current_stage,
            target_stage=draft.target_stage,
            payload={
                "application_id": int(application.id) if application.id.isdecimal() else application.id,
                "current_stage": draft.current_stage.value, "target_stage": draft.target_stage.value,
                "source_status": draft.source_status, "source_status_synced_at": now.isoformat(),
                "confirmation_reasons": association.review_reasons,
                "source": "recruitment_mail",
                "source_ref": record_id,
                "audit_evidence": audit_evidence,
                "note": f"招聘邮件关联：{record_id}",
            },
        ))
    for index, draft in enumerate(association.schedule_drafts if schedule_source_available else []):
        previews.append(ApprovalPreview(
            task_id=f"mail-review:{record_id}", operation=OperationName.SCHEDULE_CREATE,
            target_id=application.id,
            idempotency_key=f"mail-schedule:{record_id}:{application.id}:{index}",
            evidence_summary=(
                f"Recruitment email {record_id} contains a confirmed event time."
                f"{confirmation_note}"
            ),
            evidence=evidence,
            expires_at=now + timedelta(hours=24),
            payload={
                "application_id": int(application.id) if application.id.isdecimal() else application.id,
                "event_type": draft.event_type, "event_date": draft.starts_at.date().isoformat(),
                "event_time": draft.starts_at.time().strftime("%H:%M"),
                "location_or_link": draft.location_or_link,
                "confirmation_reasons": association.review_reasons,
                "source": "recruitment_mail",
                "source_ref": record_id,
                "audit_evidence": audit_evidence,
                "note": draft.location_or_link or "招聘邮件生成的日程草稿",
            },
        ))
    return previews


def _schedule_snapshot(repository: RecruitmentRepository):
    # Fixture repositories may already expose their immutable schedule snapshot.
    cached = getattr(repository, "events", None)
    if isinstance(cached, (list, tuple)):
        return list(cached)
    return repository.list_schedule()


def review_recruitment_mail(request: RecruitmentMailReviewInput, store: RecruitmentMailStore, repository: RecruitmentRepository) -> RecruitmentMailReviewResponse:
    started = perf_counter()
    record = store.get(record_id=request.record_id)
    evidence = [EvidenceSource(source="agent_database", source_ref=request.record_id)]
    if record is None:
        return RecruitmentMailReviewResponse(
            tool_name="recruitment_mail_review", status=ToolStatus.NO_RESULTS, success=False,
            evidence=evidence, error_code=ToolErrorCode.NOT_FOUND,
            error_message="Recruitment email was not found.", timeout_ms=request.timeout_ms,
            elapsed_ms=_elapsed(started),
        )
    from packages.recruitment_mail.analysis_binding import parsed_model_evidence
    from packages.recruitment_mail.model_analysis import MAIL_ANALYSIS_VERSION

    analysis = (record.raw_metadata or {}).get("model_analysis")
    try:
        if (not isinstance(analysis, dict)
                or analysis.get("version", "").split(":", 1)[0] != MAIL_ANALYSIS_VERSION
                or analysis.get("digest") != record.content_digest):
            raise ValueError("missing_or_stale_model_analysis")
        message = parsed_model_evidence(record, analysis["payload"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return RecruitmentMailReviewResponse(
            tool_name="recruitment_mail_review", status=ToolStatus.FAILURE, success=False,
            evidence=evidence, error_code=ToolErrorCode.INVALID_SOURCE,
            error_message="Use recruitment_mail_process first; legacy parsed fields cannot authorize a review preview.",
            timeout_ms=request.timeout_ms, elapsed_ms=_elapsed(started),
        )
    applications = repository.list_applications()
    schedule_source_error = False
    try:
        schedule_events = _schedule_snapshot(repository)
    except Exception:
        schedule_events = []
        schedule_source_error = True
    association = associate_recruitment_email(
        message, applications,
        minimum_confidence=request.minimum_confidence, minimum_margin=request.minimum_margin,
        schedule_events=schedule_events,
        record_id=record.id,
    )
    if schedule_source_error:
        association = association.model_copy(
            update={
                "status": "review_required",
                "requires_confirmation": True,
                "review_reasons": list(dict.fromkeys(
                    [*association.review_reasons, "schedule_source_unavailable"]
                )),
            }
        )
    return RecruitmentMailReviewResponse(
        tool_name="recruitment_mail_review", status=ToolStatus.SUCCESS, success=True,
        data=RecruitmentMailReviewData(
            record_id=record.id, association=association,
            approval_previews=_approval_previews(record.id, association, applications),
        ),
        evidence=evidence, timeout_ms=request.timeout_ms, elapsed_ms=_elapsed(started),
    )


__all__ = [
    "RecruitmentMailDetailData", "RecruitmentMailDetailInput", "RecruitmentMailDetailResponse",
    "RecruitmentMailReviewData", "RecruitmentMailReviewInput", "RecruitmentMailReviewResponse",
    "RecruitmentMailSearchData", "RecruitmentMailSearchInput", "RecruitmentMailSearchResponse",
    "RecruitmentMailSyncData", "RecruitmentMailSyncInput", "RecruitmentMailSyncResponse",
    "RecruitmentMailSummary", "get_recruitment_mail", "review_recruitment_mail",
    "search_recruitment_mail",
]
