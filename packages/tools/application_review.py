from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any
from urllib.parse import urlparse

from pydantic import Field, field_validator

from packages.approval import ApprovalPreview, OperationName
from packages.domain.models import Application, ApplicationStage
from packages.domain.urls import normalize_http_page_url
from packages.repositories.base import RecruitmentRepository

from .typed import (
    EvidenceSource,
    ToolErrorCode,
    ToolInput,
    ToolModel,
    ToolResponse,
    ToolStatus,
)


class ObservedApplicationStatus(StrEnum):
    INTERESTED = "interested"
    APPLIED = "applied"
    ASSESSMENT = "assessment"
    WRITTEN = "written"
    INTERVIEW = "interview"
    HR = "hr"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class ReviewMode(StrEnum):
    PLAN = "plan"
    RECONCILE = "reconcile"


class ReviewApplication(ToolModel):
    application_id: str
    company_name: str
    job_title: str
    current_stage: ApplicationStage


class ReviewTarget(ToolModel):
    target_id: str
    record_url: str
    normalized_url: str
    origin: str
    action: str = "read_application_status"
    selector_key: str = "application_status"
    applications: list[ReviewApplication] = Field(min_length=1)


class ApplicationStatusEntry(ToolModel):
    status: ObservedApplicationStatus
    label: str = Field(min_length=1, max_length=200)
    context: str = Field(default="", max_length=1_000)
    application_id: str | None = None


class ApplicationStatusObservation(ToolModel):
    target_id: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2_048)
    captured_at: datetime
    entries: list[ApplicationStatusEntry] = Field(min_length=1, max_length=100)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("application status observation requires an HTTP(S) URL")
        return value


class ReviewUnresolved(ToolModel):
    target_id: str
    application_id: str | None = None
    reason: str
    detail: str | None = None


class ReviewUnchanged(ToolModel):
    target_id: str
    application_id: str
    stage: ApplicationStage
    observed_status: ObservedApplicationStatus


class ApplicationStatusProposal(ToolModel):
    target_id: str
    application_id: str
    observed_status: ObservedApplicationStatus
    observed_label: str
    current_stage: ApplicationStage
    target_stage: ApplicationStage
    approval_preview: ApprovalPreview


class ApplicationStatusReviewInput(ToolInput):
    review_id: str = Field(min_length=1, max_length=128)
    application_ids: list[str] = Field(default_factory=list, max_length=500)
    observations: list[ApplicationStatusObservation] = Field(default_factory=list, max_length=500)


class ApplicationStatusReviewData(ToolModel):
    review_id: str
    mode: ReviewMode
    targets: list[ReviewTarget] = Field(default_factory=list)
    proposals: list[ApplicationStatusProposal] = Field(default_factory=list)
    unchanged: list[ReviewUnchanged] = Field(default_factory=list)
    unresolved: list[ReviewUnresolved] = Field(default_factory=list)
    applications_total: int = Field(ge=0)
    pages_total: int = Field(ge=0)


class ApplicationStatusReviewResponse(ToolResponse[ApplicationStatusReviewData]):
    pass


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
_TERMINAL = {ApplicationStage.REJECTED, ApplicationStage.WITHDRAWN}


def normalize_application_page_url(value: str) -> str | None:
    """Strip credentials and query secrets while preserving SPA route fragments."""

    return normalize_http_page_url(value)


def _origin(value: str) -> str:
    parsed = urlparse(value)
    return f"{parsed.scheme}://{parsed.netloc}"


def _target_id(normalized_url: str, application_ids: list[str]) -> str:
    raw = f"{normalized_url}|{'|'.join(sorted(application_ids))}"
    return f"status-{sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def _text_key(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.casefold())


def _build_targets(applications: list[Application]) -> tuple[list[ReviewTarget], list[ReviewUnresolved]]:
    grouped: dict[str, list[Application]] = {}
    first_url: dict[str, str] = {}
    unresolved: list[ReviewUnresolved] = []
    for application in applications:
        record_url = str(application.record_url or "").strip()
        normalized = normalize_application_page_url(record_url) if record_url else None
        if not normalized:
            unresolved.append(
                ReviewUnresolved(
                    target_id="missing-record-url",
                    application_id=application.id,
                    reason="record_url_missing_or_invalid",
                )
            )
            continue
        grouped.setdefault(normalized, []).append(application)
        first_url.setdefault(normalized, record_url)

    targets: list[ReviewTarget] = []
    for normalized, items in sorted(grouped.items()):
        applications_for_target = [
            ReviewApplication(
                application_id=item.id,
                company_name=item.company_name,
                job_title=item.job_title,
                current_stage=item.stage,
            )
            for item in sorted(items, key=lambda value: value.id)
        ]
        targets.append(
            ReviewTarget(
                target_id=_target_id(normalized, [item.id for item in items]),
                record_url=first_url[normalized],
                normalized_url=normalized,
                origin=_origin(first_url[normalized]),
                applications=applications_for_target,
            )
        )
    return targets, unresolved


def _target_stage(current: ApplicationStage, observed: ObservedApplicationStatus) -> ApplicationStage:
    if observed is ObservedApplicationStatus.INTERVIEW:
        if current in {
            ApplicationStage.INTERVIEW1,
            ApplicationStage.INTERVIEW2,
            ApplicationStage.INTERVIEW3,
            ApplicationStage.HR,
            ApplicationStage.OFFER,
        }:
            return current
        return ApplicationStage.INTERVIEW1
    mapping = {
        ObservedApplicationStatus.INTERESTED: ApplicationStage.INTERESTED,
        ObservedApplicationStatus.APPLIED: ApplicationStage.APPLIED,
        ObservedApplicationStatus.ASSESSMENT: ApplicationStage.ASSESSMENT,
        ObservedApplicationStatus.WRITTEN: ApplicationStage.WRITTEN,
        ObservedApplicationStatus.HR: ApplicationStage.HR,
        ObservedApplicationStatus.OFFER: ApplicationStage.OFFER,
        ObservedApplicationStatus.REJECTED: ApplicationStage.REJECTED,
        ObservedApplicationStatus.WITHDRAWN: ApplicationStage.WITHDRAWN,
    }
    return mapping[observed]


def _match_entry(
    application: ReviewApplication,
    entries: list[ApplicationStatusEntry],
    application_count: int,
) -> tuple[ApplicationStatusEntry | None, str | None]:
    explicit = [item for item in entries if item.application_id == application.application_id]
    if len(explicit) == 1:
        return explicit[0], None
    if len(explicit) > 1:
        return None, "multiple_entries_for_application_id"
    if application_count == 1 and len(entries) == 1 and not entries[0].application_id:
        return entries[0], None
    title_key = _text_key(application.job_title)
    contextual = [
        item
        for item in entries
        if title_key and title_key in _text_key(item.context)
    ]
    if len(contextual) == 1:
        return contextual[0], None
    return None, "status_entry_ambiguous" if contextual else "status_entry_not_matched"


def _preview(
    request: ApplicationStatusReviewInput,
    target: ReviewTarget,
    application: ReviewApplication,
    entry: ApplicationStatusEntry,
    target_stage: ApplicationStage,
    captured_at: datetime,
) -> ApprovalPreview:
    evidence = " ".join(entry.label.split())
    digest_source = f"{target.normalized_url}|{entry.status.value}|{evidence}"
    digest = sha256(digest_source.encode("utf-8")).hexdigest()[:16]
    numeric_id: int | str = (
        int(application.application_id)
        if application.application_id.isdecimal()
        else application.application_id
    )
    return ApprovalPreview(
        task_id=request.review_id,
        operation=OperationName.APPLICATION_STAGE_UPDATE,
        idempotency_key=(
            f"application-status:{application.application_id}:"
            f"{application.current_stage.value}:{target_stage.value}:{digest}"
        ),
        evidence_summary=(
            f"Browser extension observed official application status '{evidence}' "
            f"at {target.normalized_url} on {captured_at.astimezone(timezone.utc).isoformat()}."
        ),
        expires_at=captured_at.astimezone(timezone.utc) + timedelta(hours=24),
        target_id=application.application_id,
        current_stage=application.current_stage,
        target_stage=target_stage,
        before={
            "application_id": application.application_id,
            "stage": application.current_stage.value,
        },
        after={
            "application_id": application.application_id,
            "stage": target_stage.value,
            "source_status": evidence,
        },
        payload={
            "application_id": numeric_id,
            "current_stage": application.current_stage.value,
            "target_stage": target_stage.value,
            "source_status": evidence,
            "source_stage": entry.status.value,
            "source_status_synced_at": captured_at.astimezone(timezone.utc).isoformat(),
            "result": "挂" if target_stage is ApplicationStage.REJECTED else "进行中",
            "note": f"官网状态复核：{evidence}",
        },
    )


def application_status_review(
    request: ApplicationStatusReviewInput,
    repository: RecruitmentRepository,
) -> ApplicationStatusReviewResponse:
    try:
        applications = repository.list_applications()
    except Exception as exc:
        return ApplicationStatusReviewResponse(
            tool_name="application_status_review",
            status=ToolStatus.FAILURE,
            success=False,
            evidence=[EvidenceSource(source="recruitment_repository", source_ref="applications")],
            error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
            error_message=str(exc) or "Application source is unavailable.",
            timeout_ms=request.timeout_ms,
            elapsed_ms=0,
            read_only=True,
        )
    selected = set(request.application_ids)
    if selected:
        applications = [item for item in applications if item.id in selected]
    targets, unresolved = _build_targets(applications)
    evidence = [EvidenceSource(source="applications.json", source_ref=item.id) for item in applications]
    if not evidence:
        evidence = [EvidenceSource(source="recruitment_repository", source_ref="applications")]
    if not targets:
        data = ApplicationStatusReviewData(
            review_id=request.review_id,
            mode=ReviewMode.RECONCILE if request.observations else ReviewMode.PLAN,
            unresolved=unresolved,
            applications_total=len(applications),
            pages_total=0,
        )
        return ApplicationStatusReviewResponse(
            tool_name="application_status_review",
            status=ToolStatus.NO_RESULTS,
            success=False,
            data=data,
            evidence=evidence,
            error_code=ToolErrorCode.NO_RESULTS,
            error_message="No application record URLs are available for browser review.",
            timeout_ms=request.timeout_ms,
            elapsed_ms=0,
            read_only=True,
        )
    if not request.observations:
        return ApplicationStatusReviewResponse(
            tool_name="application_status_review",
            status=ToolStatus.SUCCESS,
            success=True,
            data=ApplicationStatusReviewData(
                review_id=request.review_id,
                mode=ReviewMode.PLAN,
                targets=targets,
                unresolved=unresolved,
                applications_total=len(applications),
                pages_total=len(targets),
            ),
            evidence=evidence,
            timeout_ms=request.timeout_ms,
            elapsed_ms=0,
            read_only=True,
        )

    target_by_id = {item.target_id: item for item in targets}
    observations = {item.target_id: item for item in request.observations}
    proposals: list[ApplicationStatusProposal] = []
    unchanged: list[ReviewUnchanged] = []
    for target in targets:
        observation = observations.get(target.target_id)
        if observation is None:
            unresolved.append(ReviewUnresolved(target_id=target.target_id, reason="observation_missing"))
            continue
        if normalize_application_page_url(observation.url) != target.normalized_url:
            unresolved.append(ReviewUnresolved(target_id=target.target_id, reason="observation_url_mismatch"))
            continue
        ambiguous_application_ids: set[str] = set()
        if len(target.applications) > 1:
            for entry in observation.entries:
                if entry.application_id:
                    continue
                context_key = _text_key(entry.context)
                candidates = [
                    item.application_id
                    for item in target.applications
                    if _text_key(item.job_title)
                    and _text_key(item.job_title) in context_key
                ]
                if len(candidates) > 1:
                    ambiguous_application_ids.update(candidates)
        for application in target.applications:
            if application.application_id in ambiguous_application_ids:
                unresolved.append(
                    ReviewUnresolved(
                        target_id=target.target_id,
                        application_id=application.application_id,
                        reason="status_entry_ambiguous",
                    )
                )
                continue
            entry, reason = _match_entry(application, observation.entries, len(target.applications))
            if entry is None:
                unresolved.append(
                    ReviewUnresolved(
                        target_id=target.target_id,
                        application_id=application.application_id,
                        reason=reason or "status_entry_not_matched",
                    )
                )
                continue
            target_stage = _target_stage(application.current_stage, entry.status)
            if target_stage is application.current_stage:
                unchanged.append(
                    ReviewUnchanged(
                        target_id=target.target_id,
                        application_id=application.application_id,
                        stage=application.current_stage,
                        observed_status=entry.status,
                    )
                )
                continue
            if application.current_stage in _TERMINAL or _STAGE_ORDER[target_stage] < _STAGE_ORDER[application.current_stage]:
                unresolved.append(
                    ReviewUnresolved(
                        target_id=target.target_id,
                        application_id=application.application_id,
                        reason="stage_regression_or_terminal_conflict",
                        detail=f"{application.current_stage.value}->{target_stage.value}",
                    )
                )
                continue
            if not application.application_id.isdecimal():
                unresolved.append(
                    ReviewUnresolved(
                        target_id=target.target_id,
                        application_id=application.application_id,
                        reason="source_application_id_not_writable",
                    )
                )
                continue
            preview = _preview(request, target, application, entry, target_stage, observation.captured_at)
            proposals.append(
                ApplicationStatusProposal(
                    target_id=target.target_id,
                    application_id=application.application_id,
                    observed_status=entry.status,
                    observed_label=entry.label,
                    current_stage=application.current_stage,
                    target_stage=target_stage,
                    approval_preview=preview,
                )
            )
    unknown_targets = set(observations) - set(target_by_id)
    unresolved.extend(
        ReviewUnresolved(target_id=target_id, reason="unknown_review_target")
        for target_id in sorted(unknown_targets)
    )
    return ApplicationStatusReviewResponse(
        tool_name="application_status_review",
        status=ToolStatus.SUCCESS,
        success=True,
        data=ApplicationStatusReviewData(
            review_id=request.review_id,
            mode=ReviewMode.RECONCILE,
            targets=targets,
            proposals=proposals,
            unchanged=unchanged,
            unresolved=unresolved,
            applications_total=len(applications),
            pages_total=len(targets),
        ),
        evidence=evidence,
        timeout_ms=request.timeout_ms,
        elapsed_ms=0,
        read_only=True,
    )


__all__ = [
    "ApplicationStatusEntry",
    "ApplicationStatusObservation",
    "ApplicationStatusProposal",
    "ApplicationStatusReviewData",
    "ApplicationStatusReviewInput",
    "ApplicationStatusReviewResponse",
    "ObservedApplicationStatus",
    "ReviewApplication",
    "ReviewMode",
    "ReviewTarget",
    "ReviewUnchanged",
    "ReviewUnresolved",
    "application_status_review",
    "normalize_application_page_url",
]
