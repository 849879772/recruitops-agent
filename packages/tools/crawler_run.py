from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import Field
import yaml

from .crawler_audit import (
    CrawlerAcceptanceData,
    CrawlerAcceptanceInput,
    ObservedCrawlerJob,
    accept_crawler_run,
    observed_ats_detail_urls,
)
from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolResponse, ToolStatus


class ConfiguredCrawlerRunInput(ToolInput):
    """Run one company already allowlisted by Agent's companies.yaml."""

    company: str = Field(min_length=1, max_length=300)
    expected_cohort: int = Field(default=2027, ge=1, le=9_999)
    require_complete_jd: bool = True


class ConfiguredCrawlerRunData(CrawlerAcceptanceData):
    crawler_key: str
    configured_urls: list[str]
    raw_job_count: int = Field(ge=0)
    run_reason: str


class ConfiguredCrawlerRunResponse(ToolResponse[ConfiguredCrawlerRunData]):
    pass


class CrawlerProcess(Protocol):
    def __call__(
        self,
        companies_config: Path,
        request: ConfiguredCrawlerRunInput,
    ) -> dict[str, Any]: ...


def _load_company(companies_config: Path, company_name: str) -> CompanyConfig:
    from packages.recruitment_core import CompanyConfig

    path = companies_config.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Agent companies config does not exist: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"unable to read Agent companies config: {path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("companies"), list):
        raise ValueError("Agent companies config must contain a companies list")

    matches = [
        item
        for item in payload["companies"]
        if isinstance(item, dict) and str(item.get("name") or "") == company_name
    ]
    if len(matches) != 1:
        raise ValueError("company must match exactly one Agent companies.yaml entry")

    raw = dict(matches[0])
    campaign_url = str(raw.get("campaign_url") or "").strip()
    if campaign_url and not raw.get("campaign_urls"):
        raw["campaign_urls"] = [campaign_url]
    return CompanyConfig.from_legacy(raw)


def _origin(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _job_id(company: str, job: dict[str, Any]) -> str:
    configured = str(job.get("id") or job.get("source_job_id") or "").strip()
    if configured:
        return configured
    material = "\x00".join(
        (
            company,
            str(job.get("jd_url") or job.get("detail_url") or ""),
            str(job.get("title") or ""),
            str(job.get("city") or ""),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _batch(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    return {
        "early_batch": "early",
        "formal": "formal",
        "internship": "internship",
    }.get(normalized, normalized or "unknown")


def _normalized_jobs(company: str, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for job in jobs:
        detail_url = str(job.get("detail_url") or job.get("jd_url") or "")
        cohort = job.get("cohort")
        try:
            cohort = int(cohort) if cohort is not None and str(cohort).strip() else None
        except (TypeError, ValueError):
            cohort = None
        normalized.append(
            {
                "id": _job_id(company, job),
                "title": str(job.get("title") or ""),
                "city": str(job.get("city") or "") or None,
                "detail_url": detail_url,
                "jd_raw": str(job.get("jd_raw") or "") or None,
                "cohort": cohort,
                "cohort_status": str(job.get("cohort_status") or "unconfirmed"),
                "cohort_source": str(job.get("cohort_source") or "") or None,
                "cohort_evidence": str(job.get("cohort_evidence") or "") or None,
                "batch": _batch(job.get("batch") or job.get("recruitment_track")),
            }
        )
    return normalized


class RecruitmentCoreCrawlerProcess:
    """Run the independent crawler kernel against Agent-owned configuration."""

    def __init__(self, companies_config: Path) -> None:
        self.companies_config = companies_config.expanduser().resolve()

    def __call__(
        self, _companies_config: Path, request: ConfiguredCrawlerRunInput
    ) -> dict[str, Any]:
        from packages.recruitment_core import configured_crawl_urls, crawl_company_with_evidence

        company = _load_company(self.companies_config, request.company)
        configured_urls = configured_crawl_urls(company)
        if not configured_urls:
            raise ValueError("company has no configured crawl URL")
        result = crawl_company_with_evidence(company)
        raw_jobs = list(result["jobs"])
        jobs = _normalized_jobs(company.name, raw_jobs)
        origins = {
            origin
            for value in [*configured_urls, *(result.get("effective_source_urls") or [])]
            if (origin := _origin(value)) is not None
        }
        return {
            **result,
            "company": company.name,
            "crawler_key": company.crawler,
            "configured_urls": configured_urls,
            "source_url": company.careers_url,
            "allowed_origins": sorted(origins),
            "allowed_detail_urls": observed_ats_detail_urls(raw_jobs, company.careers_url, result),
            "jobs": jobs,
            "raw_job_count": len(jobs),
            "run_reason": "recruitment_core_crawl",
        }


class SubprocessCrawlerProcess(RecruitmentCoreCrawlerProcess):
    """Compatibility name for callers that used the former process adapter."""

    def __init__(
        self,
        companies_config: Path,
        *,
        python_executable: str | None = None,
    ) -> None:
        del python_executable
        super().__init__(companies_config)


class ConfiguredCrawlerRunner:
    def __init__(self, companies_config: Path, process: CrawlerProcess) -> None:
        self.companies_config = companies_config.resolve()
        self.process = process

    def run(self, request: ConfiguredCrawlerRunInput) -> ConfiguredCrawlerRunResponse:
        started = perf_counter()
        evidence = [
            EvidenceSource(
                source="configured_crawler",
                source_ref=f"companies.yaml:{request.company}",
            )
        ]
        try:
            result = self.process(self.companies_config, request)
        except (subprocess.TimeoutExpired, TimeoutError):
            return self._failure(
                request,
                evidence,
                started,
                ToolErrorCode.TIMEOUT,
                "The configured crawler exceeded its bounded timeout.",
                timed_out=True,
            )
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return self._failure(
                request,
                evidence,
                started,
                ToolErrorCode.SOURCE_UNAVAILABLE,
                f"Configured crawler failed: {exc}",
            )

        try:
            jobs = [ObservedCrawlerJob.model_validate(item) for item in result.get("jobs", [])]
            audit = accept_crawler_run(
                CrawlerAcceptanceInput(
                    company=request.company,
                    source_url=str(result["source_url"]),
                    allowed_origins=list(result.get("allowed_origins") or []),
                    allowed_detail_urls=list(result.get("allowed_detail_urls") or []),
                    jobs=jobs,
                    pages_seen=int(result.get("pages_seen") or 0),
                    total_pages=(
                        int(result["total_pages"])
                        if result.get("total_pages") is not None
                        else None
                    ),
                    has_more=bool(result.get("has_more", False)),
                    pagination_complete=result.get("pagination_complete"),
                    completeness_known=result.get("completeness_known"),
                    advertised_total=result.get("advertised_total"),
                    expected_cohort=request.expected_cohort,
                    require_complete_jd=request.require_complete_jd,
                    timeout_ms=request.timeout_ms,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            return self._failure(
                request,
                evidence,
                started,
                ToolErrorCode.INTERNAL_ERROR,
                f"Configured crawler output failed schema validation: {exc}",
            )

        elapsed_ms = max(0, int((perf_counter() - started) * 1000))
        if audit.data is None:
            return ConfiguredCrawlerRunResponse(
                tool_name="configured_crawler_run",
                status=audit.status,
                success=False,
                data=None,
                evidence=audit.evidence,
                error_code=audit.error_code,
                error_message=audit.error_message,
                timeout_ms=request.timeout_ms,
                timed_out=audit.timed_out,
                elapsed_ms=elapsed_ms,
                read_only=True,
            )
        data = ConfiguredCrawlerRunData(
            **audit.data.model_dump(),
            crawler_key=str(result.get("crawler_key") or ""),
            configured_urls=[str(item) for item in result.get("configured_urls") or []],
            raw_job_count=int(result.get("raw_job_count") or len(jobs)),
            run_reason=str(result.get("run_reason") or "completed"),
        )
        return ConfiguredCrawlerRunResponse(
            tool_name="configured_crawler_run",
            status=audit.status,
            success=audit.success,
            data=data,
            evidence=audit.evidence,
            error_code=audit.error_code,
            error_message=audit.error_message,
            timeout_ms=request.timeout_ms,
            timed_out=audit.timed_out,
            elapsed_ms=elapsed_ms,
            read_only=True,
        )

    @staticmethod
    def _failure(
        request: ConfiguredCrawlerRunInput,
        evidence: list[EvidenceSource],
        started: float,
        code: ToolErrorCode,
        message: str,
        *,
        timed_out: bool = False,
    ) -> ConfiguredCrawlerRunResponse:
        return ConfiguredCrawlerRunResponse(
            tool_name="configured_crawler_run",
            status=ToolStatus.FAILURE,
            success=False,
            data=None,
            evidence=evidence,
            error_code=code,
            error_message=message,
            timeout_ms=request.timeout_ms,
            timed_out=timed_out,
            elapsed_ms=max(0, int((perf_counter() - started) * 1000)),
            read_only=True,
        )


__all__ = [
    "ConfiguredCrawlerRunData",
    "ConfiguredCrawlerRunInput",
    "ConfiguredCrawlerRunResponse",
    "ConfiguredCrawlerRunner",
    "CrawlerProcess",
    "RecruitmentCoreCrawlerProcess",
    "SubprocessCrawlerProcess",
]
