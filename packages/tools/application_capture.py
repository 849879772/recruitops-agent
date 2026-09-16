from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from time import perf_counter
from urllib.parse import urlparse

from pydantic import Field, field_validator

from packages.approval import ApprovalPreview, OperationName
from packages.domain.models import Job, RecruitmentBatch
from packages.domain.urls import normalize_http_page_url
from packages.repositories.base import RecruitmentRepository

from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolModel, ToolResponse, ToolStatus


class ApplicationCaptureInput(ToolInput):
    request_id: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2_048)
    title: str = Field(default="", max_length=500)
    page_text: str = Field(default="", max_length=20_000)
    job_id: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=1_000)

    @field_validator("url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("application capture requires an HTTP(S) page")
        if parsed.username or parsed.password:
            raise ValueError("application capture URL cannot contain credentials")
        return value


class ApplicationCaptureCandidate(ToolModel):
    job_id: str
    company_name: str
    job_title: str
    detail_url: str


class ApplicationCaptureData(ToolModel):
    capture_status: str
    message: str
    matched_job: ApplicationCaptureCandidate | None = None
    candidates: list[ApplicationCaptureCandidate] = Field(default_factory=list, max_length=20)
    approval_preview: ApprovalPreview | None = None


class ApplicationCaptureResponse(ToolResponse[ApplicationCaptureData]):
    pass


def _candidate(job: Job) -> ApplicationCaptureCandidate:
    return ApplicationCaptureCandidate(
        job_id=job.id,
        company_name=job.company_id,
        job_title=job.title,
        detail_url=job.detail_url,
    )


def _eligible_jobs(repository: RecruitmentRepository) -> list[Job]:
    jobs: list[Job] = []
    offset = 0
    page_size = 200
    while True:
        page = repository.search_jobs(
            cohort=2027,
            cohort_status="confirmed",
            batches=(RecruitmentBatch.FORMAL, RecruitmentBatch.EARLY),
            limit=page_size,
            offset=offset,
        )
        jobs.extend(page.items)
        offset += len(page.items)
        if not page.items or offset >= page.total:
            return jobs


def _preview(request: ApplicationCaptureInput, job: Job) -> ApprovalPreview:
    captured_at = datetime.now(timezone.utc)
    digest = sha256(f"{job.id}\0{request.url}".encode("utf-8")).hexdigest()[:20]
    return ApprovalPreview(
        task_id=request.request_id,
        operation=OperationName.APPLICATION_CREATE,
        target_id=job.id,
        idempotency_key=f"application-create:{job.id}:{digest}",
        evidence_summary=(
            f"User explicitly requested recording an application from {request.url}; "
            f"the page uniquely matched job {job.id}."
        ),
        evidence=(
            f"browser_page:{request.url}",
            f"jobs.db:{job.source_ref}",
        ),
        expires_at=captured_at + timedelta(hours=24),
        cohort=job.cohort,
        cohort_status=job.cohort_status,
        jd_raw=job.jd_raw,
        payload={
            "job_id": job.id,
            "company": job.company_id,
            "title": job.title,
            "city": job.city or "",
            "record_url": request.url,
            "source_job_url": job.detail_url,
            "current_stage": "applied",
            "note": request.note or "",
            "captured_at": captured_at.isoformat(),
        },
        before=None,
        after={
            "job_id": job.id,
            "company": job.company_id,
            "title": job.title,
            "current_stage": "applied",
            "record_url": request.url,
        },
    )


def prepare_application_capture(
    request: ApplicationCaptureInput,
    repository: RecruitmentRepository,
) -> ApplicationCaptureResponse:
    started = perf_counter()
    evidence = [EvidenceSource(source="browser_page", source_ref=request.url)]
    jobs = _eligible_jobs(repository)
    normalized = normalize_http_page_url(request.url)
    explicit = [job for job in jobs if request.job_id and job.id == request.job_id]

    if request.job_id and not explicit:
        data = ApplicationCaptureData(
            capture_status="not_found",
            message="The supplied job ID is not an eligible confirmed 2027 job.",
        )
        return ApplicationCaptureResponse(
            tool_name="application_capture",
            status=ToolStatus.NO_RESULTS,
            success=False,
            data=data,
            evidence=evidence,
            error_code=ToolErrorCode.NOT_FOUND,
            error_message=data.message,
            timeout_ms=request.timeout_ms,
            elapsed_ms=int((perf_counter() - started) * 1000),
        )

    matches = explicit or [
        job
        for job in jobs
        if normalized is not None and normalize_http_page_url(job.detail_url) == normalized
    ]
    applications = repository.list_applications()
    existing_job_ids = {item.job_id for item in applications if item.job_id}

    if len(matches) == 1:
        job = matches[0]
        evidence.append(EvidenceSource(source=job.source, source_ref=job.source_ref))
        if job.id in existing_job_ids:
            data = ApplicationCaptureData(
                capture_status="already_recorded",
                message="This job already exists in the application tracker.",
                matched_job=_candidate(job),
            )
            return ApplicationCaptureResponse(
                tool_name="application_capture",
                status=ToolStatus.NO_RESULTS,
                success=False,
                data=data,
                evidence=evidence,
                error_code=ToolErrorCode.NO_RESULTS,
                error_message=data.message,
                timeout_ms=request.timeout_ms,
                elapsed_ms=int((perf_counter() - started) * 1000),
            )
        preview = _preview(request, job)
        data = ApplicationCaptureData(
            capture_status="approval_required",
            message="A pending application record preview was created for approval.",
            matched_job=_candidate(job),
            approval_preview=preview,
        )
        return ApplicationCaptureResponse(
            tool_name="application_capture",
            status=ToolStatus.SUCCESS,
            success=True,
            data=data,
            evidence=evidence,
            timeout_ms=request.timeout_ms,
            elapsed_ms=int((perf_counter() - started) * 1000),
        )

    if len(matches) > 1:
        data = ApplicationCaptureData(
            capture_status="ambiguous",
            message="Multiple jobs share this page identity; provide an explicit job ID.",
            candidates=[_candidate(job) for job in matches[:20]],
        )
        return ApplicationCaptureResponse(
            tool_name="application_capture",
            status=ToolStatus.AMBIGUOUS,
            success=False,
            data=data,
            evidence=evidence,
            error_code=ToolErrorCode.AMBIGUOUS_MATCH,
            error_message=data.message,
            timeout_ms=request.timeout_ms,
            elapsed_ms=int((perf_counter() - started) * 1000),
        )

    data = ApplicationCaptureData(
        capture_status="not_found",
        message="The current page could not be matched to an eligible job; provide its job ID.",
    )
    return ApplicationCaptureResponse(
        tool_name="application_capture",
        status=ToolStatus.NO_RESULTS,
        success=False,
        data=data,
        evidence=evidence,
        error_code=ToolErrorCode.NOT_FOUND,
        error_message=data.message,
        timeout_ms=request.timeout_ms,
        elapsed_ms=int((perf_counter() - started) * 1000),
    )


__all__ = [
    "ApplicationCaptureCandidate",
    "ApplicationCaptureData",
    "ApplicationCaptureInput",
    "ApplicationCaptureResponse",
    "prepare_application_capture",
]
