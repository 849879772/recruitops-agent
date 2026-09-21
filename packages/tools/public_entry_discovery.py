"""Typed read-only tool for bounded public recruitment-entry discovery."""

from __future__ import annotations

from time import perf_counter

from pydantic import Field, field_validator

from packages.discovery.public_entries import (
    MAX_CANDIDATES_PER_COMPANY,
    MAX_COMPANIES,
    MAX_QUERIES_PER_COMPANY,
    PublicSearchProvider,
    PublicSearchError,
    discover_company_entry_candidates,
    observe_public_entry_identity,
)
from packages.tools.oc_candidates import (
    OcCandidateCrawlBatchInput,
    OcCandidateCrawlItem,
    OcCandidateRunner,
)

from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolModel, ToolResponse, ToolStatus


class PublicEntryDiscoveryInput(ToolInput):
    company_names: list[str] = Field(min_length=1, max_length=MAX_COMPANIES)
    max_queries_per_company: int = Field(default=2, ge=1, le=MAX_QUERIES_PER_COMPANY)
    max_candidates_per_company: int = Field(default=5, ge=1, le=MAX_CANDIDATES_PER_COMPANY)
    cohort_year: int = Field(default=2027, ge=1, le=9999)

    @field_validator("company_names")
    @classmethod
    def normalize_names(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            name = " ".join(value.split())
            if name and name not in result:
                result.append(name)
        if not result:
            raise ValueError("at least one non-empty company name is required")
        return result


class PublicEntryCandidate(ToolModel):
    url: str
    title: str
    snippet: str
    provider: str
    query: str
    score: int
    entry_kind: str
    crawler_key: str | None = None
    company_evidence: str
    verification_status: str = "candidate_only"


class PublicEntryCompanyResult(ToolModel):
    company: str
    status: str
    queries: list[str]
    candidates: list[PublicEntryCandidate]
    error: str | None = None


class PublicEntryDiscoveryData(ToolModel):
    companies: list[PublicEntryCompanyResult]
    searched_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    safety_boundary: str


class PublicEntryDiscoveryResponse(ToolResponse[PublicEntryDiscoveryData]):
    pass


class PublicEntryValidationInput(ToolInput):
    company_name: str = Field(min_length=1, max_length=200)
    candidate_url: str = Field(min_length=8, max_length=2_048)
    expected_cohort: int = Field(default=2027, ge=1, le=9_999)
    require_complete_jd: bool = False
    include_job_evidence: bool = False
    crawl_timeout_seconds: int = Field(default=75, ge=10, le=180)


class PublicEntryValidationData(ToolModel):
    company: str
    candidate_url: str
    identity_verified: bool
    company_evidence: str = ""
    recruitment_evidence: str = ""
    crawl: OcCandidateCrawlItem | None = None


class PublicEntryValidationResponse(ToolResponse[PublicEntryValidationData]):
    pass


def discover_public_recruitment_entries(
    request: PublicEntryDiscoveryInput,
    *,
    provider: PublicSearchProvider | None = None,
) -> PublicEntryDiscoveryResponse:
    started = perf_counter()
    results: list[PublicEntryCompanyResult] = []
    per_company_timeout = max(1.0, request.timeout_ms / 1_000 / len(request.company_names))
    for company in request.company_names:
        try:
            queries, candidates = discover_company_entry_candidates(
                company,
                provider=provider,
                timeout_seconds=per_company_timeout,
                max_queries=request.max_queries_per_company,
                max_candidates=request.max_candidates_per_company,
                cohort_year=request.cohort_year,
            )
            results.append(PublicEntryCompanyResult(
                company=company,
                status="candidates_found" if candidates else "no_candidates",
                queries=queries,
                candidates=[PublicEntryCandidate(
                    url=item.hit.url,
                    title=item.hit.title,
                    snippet=item.hit.snippet,
                    provider=item.hit.provider,
                    query=item.hit.query,
                    score=item.score,
                    entry_kind=item.entry_kind,
                    crawler_key=item.crawler_key,
                    company_evidence=item.company_evidence,
                ) for item in candidates],
            ))
        except PublicSearchError as exc:
            results.append(PublicEntryCompanyResult(
                company=company,
                status="search_failed",
                queries=[],
                candidates=[],
                error=str(exc)[-1_000:],
            ))
    candidate_count = sum(len(item.candidates) for item in results)
    failed_count = sum(item.status == "search_failed" for item in results)
    elapsed_ms = max(0, int((perf_counter() - started) * 1_000))
    data = PublicEntryDiscoveryData(
        companies=results,
        searched_count=len(results),
        candidate_count=candidate_count,
        failed_count=failed_count,
        safety_boundary=(
            "Search results are untrusted candidates only. Validate company identity, public "
            f"{request.cohort_year} recruitment scope, pagination, and jobs through the crawler acceptance path "
            "before changing configuration or database state."
        ),
    )
    if candidate_count:
        status, code, message = ToolStatus.SUCCESS, None, None
    elif failed_count == len(results):
        status, code, message = (
            ToolStatus.FAILURE,
            ToolErrorCode.SOURCE_UNAVAILABLE,
            "Every public search request failed.",
        )
    else:
        status, code, message = (
            ToolStatus.NO_RESULTS,
            ToolErrorCode.NO_RESULTS,
            "No bounded public recruitment-entry candidates passed the deterministic filters.",
        )
    return PublicEntryDiscoveryResponse(
        tool_name="public_recruitment_entry_discovery",
        status=status,
        success=status == ToolStatus.SUCCESS,
        data=data,
        evidence=[EvidenceSource(source="public_search", source_ref="bounded_public_search")],
        error_code=code,
        error_message=message,
        timeout_ms=request.timeout_ms,
        timed_out=elapsed_ms >= request.timeout_ms,
        elapsed_ms=elapsed_ms,
    )


def validate_public_recruitment_entry(
    request: PublicEntryValidationInput,
    runner: OcCandidateRunner,
    *,
    identity_observer=observe_public_entry_identity,
) -> PublicEntryValidationResponse:
    started = perf_counter()
    identity = identity_observer(
        request.company_name,
        request.candidate_url,
        timeout_seconds=min(30.0, request.timeout_ms / 1_000),
    )
    if not identity.valid:
        return PublicEntryValidationResponse(
            tool_name="public_recruitment_entry_validate",
            status=ToolStatus.FAILURE,
            success=False,
            data=PublicEntryValidationData(
                company=request.company_name,
                candidate_url=identity.source_url,
                identity_verified=False,
                company_evidence=identity.company_evidence,
            ),
            evidence=[EvidenceSource(source="public_entry_page", source_ref=identity.source_url)],
            error_code=ToolErrorCode.INVALID_SOURCE,
            error_message=identity.reason,
            timeout_ms=request.timeout_ms,
            elapsed_ms=max(0, int((perf_counter() - started) * 1_000)),
        )
    try:
        crawl = runner.crawl_public_candidate(
            request.company_name,
            identity.source_url,
            OcCandidateCrawlBatchInput(
                company_names=[request.company_name],
                expected_cohort=request.expected_cohort,
                require_complete_jd=request.require_complete_jd,
                include_job_evidence=request.include_job_evidence,
                per_company_timeout_seconds=request.crawl_timeout_seconds,
                timeout_ms=request.timeout_ms,
            ),
            hydrate_details=request.require_complete_jd,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return PublicEntryValidationResponse(
            tool_name="public_recruitment_entry_validate",
            status=ToolStatus.FAILURE,
            success=False,
            data=PublicEntryValidationData(
                company=request.company_name,
                candidate_url=identity.source_url,
                identity_verified=True,
                company_evidence=identity.company_evidence,
                recruitment_evidence=identity.recruitment_evidence,
            ),
            evidence=[EvidenceSource(source="public_entry_page", source_ref=identity.source_url)],
            error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
            error_message=str(exc)[-1_000:],
            timeout_ms=request.timeout_ms,
            elapsed_ms=max(0, int((perf_counter() - started) * 1_000)),
        )
    success = crawl.integration_status == "connected_complete"
    if success:
        status, code, message = ToolStatus.SUCCESS, None, None
    else:
        status = ToolStatus.FAILURE
        code = (
            ToolErrorCode.PAGINATION_INCOMPLETE
            if crawl.raw_job_count and not crawl.pagination_complete
            else ToolErrorCode.INVALID_SOURCE
        )
        message = crawl.error_message or crawl.error_code or "Candidate crawl was not accepted."
    elapsed_ms = max(0, int((perf_counter() - started) * 1_000))
    return PublicEntryValidationResponse(
        tool_name="public_recruitment_entry_validate",
        status=status,
        success=success,
        data=PublicEntryValidationData(
            company=request.company_name,
            candidate_url=identity.source_url,
            identity_verified=True,
            company_evidence=identity.company_evidence,
            recruitment_evidence=identity.recruitment_evidence,
            crawl=crawl,
        ),
        evidence=[
            EvidenceSource(source="public_entry_page", source_ref=identity.source_url),
            EvidenceSource(source="isolated_candidate_crawl", source_ref=identity.source_url),
        ],
        error_code=code,
        error_message=message,
        timeout_ms=request.timeout_ms,
        timed_out=elapsed_ms >= request.timeout_ms,
        elapsed_ms=elapsed_ms,
    )


__all__ = [
    "PublicEntryCandidate",
    "PublicEntryCompanyResult",
    "PublicEntryDiscoveryData",
    "PublicEntryDiscoveryInput",
    "PublicEntryDiscoveryResponse",
    "PublicEntryValidationData",
    "PublicEntryValidationInput",
    "PublicEntryValidationResponse",
    "discover_public_recruitment_entries",
    "validate_public_recruitment_entry",
]
