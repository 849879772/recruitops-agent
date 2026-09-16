from __future__ import annotations

from collections import Counter
from enum import StrEnum
import re
from typing import Any, Literal
from urllib.parse import urlparse, urlsplit

from pydantic import Field, field_validator

from packages.domain.models import RecruitmentBatch
from packages.matching import is_jd_incomplete

from .typed import (
    EvidenceSource,
    ToolErrorCode,
    ToolInput,
    ToolModel,
    ToolResponse,
    ToolStatus,
)


class CrawlerRejectionReason(StrEnum):
    INVALID_JOB_ID = "invalid_job_id"
    MISSING_TITLE = "missing_title"
    INVALID_DETAIL_URL = "invalid_detail_url"
    DETAIL_ORIGIN_NOT_ALLOWED = "detail_origin_not_allowed"
    INCOMPLETE_JD = "incomplete_jd"
    COHORT_NOT_CONFIRMED = "cohort_not_confirmed"
    INELIGIBLE_BATCH = "ineligible_batch"
    DUPLICATE_JOB_ID = "duplicate_job_id"


class ObservedCrawlerJob(ToolModel):
    """The minimum normalized row a crawler adapter may submit for audit."""

    id: str = Field(min_length=1, max_length=300)
    title: str = Field(default="", max_length=500)
    city: str | None = Field(default=None, max_length=300)
    detail_url: str = Field(default="", max_length=2048)
    jd_raw: str | None = Field(default=None, max_length=50_000)
    capture_evidence: dict[str, Any] = Field(default_factory=dict)
    cohort: int | None = Field(default=None, ge=1, le=9_999)
    cohort_status: str = Field(default="unconfirmed", max_length=40)
    cohort_source: str | None = Field(default=None, max_length=300)
    cohort_evidence: str | None = Field(default=None, max_length=1_000)
    batch: RecruitmentBatch = RecruitmentBatch.UNKNOWN


class CrawlerAcceptanceInput(ToolInput):
    """Sanitized crawler output plus pagination evidence for deterministic QA."""

    company: str = Field(min_length=1, max_length=300)
    source_url: str = Field(min_length=1, max_length=2048)
    allowed_origins: list[str] = Field(default_factory=list, max_length=32)
    allowed_detail_urls: list[str] = Field(default_factory=list, max_length=5_000)
    jobs: list[ObservedCrawlerJob] = Field(default_factory=list, max_length=5_000)
    pages_seen: int = Field(default=0, ge=0)
    total_pages: int | None = Field(default=None, ge=0)
    has_more: bool = False
    pagination_complete: bool | None = None
    completeness_known: bool | None = None
    advertised_total: int | None = Field(default=None, ge=0)
    expected_cohort: int = Field(default=2027, ge=1, le=9_999)
    require_confirmed_cohort: bool = True
    require_complete_jd: bool = True

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("crawler audit requires an HTTP(S) source URL")
        return value


class CrawlerAcceptanceData(ToolModel):
    company: str
    source_url: str
    source_origin: str
    accepted_jobs: list[ObservedCrawlerJob]
    accepted_count: int = Field(ge=0)
    rejected_count: int = Field(ge=0)
    rejection_reasons: dict[str, int] = Field(default_factory=dict)
    pagination_complete: bool
    pagination_state: Literal["complete", "incomplete", "unknown"]
    unique_observed_count: int = Field(ge=0)
    pages_seen: int = Field(ge=0)
    total_pages: int | None = Field(default=None, ge=0)
    has_more: bool


class CrawlerAcceptanceResponse(ToolResponse[CrawlerAcceptanceData]):
    pass


def _origin(value: str) -> str | None:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold()
    authority = f"[{host}]" if ":" in host else host
    if port is not None and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def _reject(reasons: Counter[str], reason: CrawlerRejectionReason) -> None:
    reasons[reason.value] += 1


def observed_ats_detail_urls(
    raw_jobs: list[dict[str, Any]], source_url: str, process_result: dict[str, Any],
) -> list[str]:
    """Authorize exact Moka links read in job cards, never the whole ATS origin."""
    source_pages = {source_url, *(process_result.get("effective_source_urls") or [])}
    for run in process_result.get("source_runs") or []:
        source_pages.update(filter(None, (run.get("source_url"), run.get("effective_source_url"))))
    approved: list[str] = []
    for job in raw_jobs:
        if job.get("detail_link_observed") is not True or job.get("detail_link_source_url") not in source_pages:
            continue
        url = str(job.get("jd_url") or "")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or parsed.netloc.casefold() != "app.mokahr.com"
            or not re.fullmatch(r"/(?:campus_apply|campus-recruitment)/[A-Za-z0-9_-]+/\d+/?", parsed.path)
            or not re.fullmatch(r"/job/[A-Za-z0-9-]+", parsed.fragment)
            or parsed.query
        ):
            continue
        if url not in approved:
            approved.append(url)
    return approved


def accept_crawler_run(
    request: CrawlerAcceptanceInput,
    repository: Any = None,
) -> CrawlerAcceptanceResponse:
    """Audit normalized crawler observations without writing or fetching anything."""

    del repository
    source_origin = _origin(request.source_url)
    evidence = [EvidenceSource(source="crawler_run", source_ref=request.source_url)]
    if source_origin is None:
        return CrawlerAcceptanceResponse(
            tool_name="crawler_acceptance",
            status=ToolStatus.FAILURE,
            success=False,
            data=None,
            evidence=evidence,
            error_code=ToolErrorCode.INVALID_SOURCE,
            error_message="The crawler source URL is not a valid HTTP(S) origin.",
            timeout_ms=request.timeout_ms,
            elapsed_ms=0,
            read_only=True,
        )

    allowed = {_origin(item) for item in request.allowed_origins if _origin(item) is not None}
    if not allowed:
        allowed = {source_origin}
    allowed_details = {url for url in request.allowed_detail_urls if _origin(url) is not None}

    reasons: Counter[str] = Counter()
    accepted: list[ObservedCrawlerJob] = []
    seen_ids: set[str] = set()
    for job in request.jobs:
        if not job.id.strip():
            _reject(reasons, CrawlerRejectionReason.INVALID_JOB_ID)
            continue
        if job.id in seen_ids:
            _reject(reasons, CrawlerRejectionReason.DUPLICATE_JOB_ID)
            continue
        seen_ids.add(job.id)
        if not job.title.strip():
            _reject(reasons, CrawlerRejectionReason.MISSING_TITLE)
            continue
        detail_origin = _origin(job.detail_url)
        if detail_origin is None:
            _reject(reasons, CrawlerRejectionReason.INVALID_DETAIL_URL)
            continue
        if detail_origin not in allowed and job.detail_url not in allowed_details:
            _reject(reasons, CrawlerRejectionReason.DETAIL_ORIGIN_NOT_ALLOWED)
            continue
        if request.require_complete_jd and is_jd_incomplete(job):
            _reject(reasons, CrawlerRejectionReason.INCOMPLETE_JD)
            continue
        if (
            request.require_confirmed_cohort
            and (
                job.cohort != request.expected_cohort
                or job.cohort_status.casefold() != "confirmed"
            )
        ):
            _reject(reasons, CrawlerRejectionReason.COHORT_NOT_CONFIRMED)
            continue
        if job.batch not in {RecruitmentBatch.FORMAL, RecruitmentBatch.EARLY}:
            _reject(reasons, CrawlerRejectionReason.INELIGIBLE_BATCH)
            continue
        accepted.append(job)

    # Count the observed set before business eligibility filtering, not duplicate rows.
    unique_count = len({job.id.strip() for job in request.jobs if job.id.strip()})
    contradicted = (
        request.has_more
        or (request.total_pages is not None and request.pages_seen < request.total_pages)
        or (request.advertised_total is not None and unique_count != request.advertised_total)
    )
    if contradicted or (request.pagination_complete is False and request.completeness_known is not False):
        pagination_state = "incomplete"
    elif request.completeness_known is False:
        pagination_state = "unknown"
    elif request.pages_seen > 0 and (
        request.pagination_complete is True
        or request.total_pages is not None
        or request.advertised_total is not None
    ):
        pagination_state = "complete"
    else:
        pagination_state = "unknown"
    pagination_complete = pagination_state == "complete"
    data = CrawlerAcceptanceData(
        company=request.company,
        source_url=request.source_url,
        source_origin=source_origin,
        accepted_jobs=accepted,
        accepted_count=len(accepted),
        rejected_count=sum(reasons.values()),
        rejection_reasons=dict(reasons),
        pagination_complete=pagination_complete,
        pagination_state=pagination_state,
        unique_observed_count=unique_count,
        pages_seen=request.pages_seen,
        total_pages=request.total_pages,
        has_more=request.has_more,
    )

    if not pagination_complete:
        return CrawlerAcceptanceResponse(
            tool_name="crawler_acceptance",
            status=ToolStatus.FAILURE,
            success=False,
            data=data,
            evidence=evidence,
            error_code=(ToolErrorCode.PAGINATION_EVIDENCE_MISSING if pagination_state == "unknown"
                        else ToolErrorCode.PAGINATION_INCOMPLETE),
            error_message=("The adapter did not provide sufficient pagination evidence; completeness is unknown."
                           if pagination_state == "unknown"
                           else "Crawler pagination evidence is incomplete; no rows may be persisted."),
            timeout_ms=request.timeout_ms,
            elapsed_ms=0,
            read_only=True,
        )
    if not accepted:
        return CrawlerAcceptanceResponse(
            tool_name="crawler_acceptance",
            status=ToolStatus.NO_RESULTS,
            success=False,
            data=data,
            evidence=evidence,
            error_code=ToolErrorCode.NO_RESULTS,
            error_message="No observed job passed the deterministic crawler acceptance rules.",
            timeout_ms=request.timeout_ms,
            elapsed_ms=0,
            read_only=True,
        )
    return CrawlerAcceptanceResponse(
        tool_name="crawler_acceptance",
        status=ToolStatus.SUCCESS,
        success=True,
        data=data,
        evidence=evidence,
        timeout_ms=request.timeout_ms,
        elapsed_ms=0,
        read_only=True,
    )


__all__ = [
    "CrawlerAcceptanceData",
    "CrawlerAcceptanceInput",
    "CrawlerAcceptanceResponse",
    "CrawlerRejectionReason",
    "ObservedCrawlerJob",
    "accept_crawler_run",
]
