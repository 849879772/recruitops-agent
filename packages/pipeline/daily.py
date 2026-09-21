"""Deterministic, Agent-owned daily recruitment pipeline.

The pipeline deliberately has no scheduler, API client, or legacy-source
dependency.  Crawlers, matchers, and the Agent storage boundary are injected
so a run can be tested without a network or a model call.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import logging
import math
from pathlib import Path
import re
from threading import Event
from typing import Any, Protocol
import unicodedata
from urllib.parse import urlsplit
from uuid import uuid4

import yaml
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from packages.discovery import classify_oc_destination_url, source_identity_for_url
from packages.domain.job_identity import build_job_identity, normalize_job_identity_url, normalize_job_title
from packages.domain.models import Company, Job, JobAnalysis, RecruitmentBatch
from packages.recruitment_core import CompanyConfig
from packages.recruitment_core.jd_capture import assess_jd_capture
from packages.storage.database import Storage
from packages.discovery.company_registry import CompanySourceRegistry
from packages.storage.models import (
    CompanySnapshot,
    JobAnalysisSnapshot,
    JobSnapshot,
)
from packages.storage.sync import (
    upsert_company_snapshot,
    upsert_job_analysis_snapshot,
    upsert_job_snapshot,
)
from packages.tools.crawler_audit import (
    CrawlerAcceptanceInput,
    ObservedCrawlerJob,
    accept_crawler_run,
    observed_ats_detail_urls,
)
from packages.tools.oc_candidates import diagnose_candidate_entry

from .isolation import (
    IsolatedOperationTimeout,
    IsolatedWorkerError,
    crawl_company_result_isolated,
    fetch_job_detail_result_isolated,
)

try:
    from packages.matching import content_fingerprint as _matching_content_fingerprint
    from packages.matching import is_jd_incomplete as _matching_is_jd_incomplete
    from packages.matching import profile_fingerprint as _matching_profile_fingerprint
    from packages.matching import screen_job as _screen_job
except ImportError:  # pragma: no cover - keeps the adapter usable during staged integration
    _matching_content_fingerprint = None
    _matching_is_jd_incomplete = None
    _matching_profile_fingerprint = None
    _screen_job = None

from packages.matching.title_policy import (
    company_title_key,
    normalize_job_title_key,
    screen_title_job,
    stored_detail_retry_required,
)


LOGGER = logging.getLogger(__name__)
PIPELINE_SOURCE = "recruitops-agent.daily_pipeline"
DEFAULT_COMPANIES_PATH = Path(__file__).resolve().parents[2] / "config" / "companies.yaml"
DEFAULT_DETAIL_REUSE_TTL_HOURS = 24.0
_FINGERPRINT_RE = re.compile(r":fp:(?P<fingerprint>[0-9a-f]{64})$")
_TITLE_FIRST_PENDING_ANALYSIS_VERSION = "title-first-pending-v1"
_TITLE_FIRST_CAPTURE_POLICY = "title_first_v2"
_FILTERED_STATUSES = {
    "cohort_unconfirmed",
    "early_batch",
    "direction_out",
    "doctorate_only",
    "filtered",
    "ineligible_batch",
    "internship",
    "jd_incomplete",
    "refused",
}
_CAPTURE_REFRESH_REASONS = frozenset(
    {
        "capture_stale",
        "capture_time_missing",
        "capture_time_future",
        "capture_identity_or_source_invalid",
    }
)


class PipelineError(RuntimeError):
    """Base error for invalid pipeline setup or Agent-owned persistence."""


class CompanyConfigError(PipelineError, ValueError):
    """Raised when the Agent-owned companies YAML has an invalid shape."""


class CrawlerProtocol(Protocol):
    def __call__(self, company: "PipelineCompany") -> Any: ...


class MatcherProtocol(Protocol):
    def match(self, job: Mapping[str, Any], *, existing_analysis: Any = None) -> Any: ...


def _default_jd_hydrator(job: dict[str, Any]) -> Mapping[str, Any]:
    return fetch_job_detail_result_isolated(job, timeout_seconds=120.0)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    return unicodedata.normalize("NFKC", str(value)).strip()


def _compact(value: Any) -> str:
    return " ".join(_text(value).split())


def _raw_detail(value: Any) -> str:
    # Capture hashes bind official text; NFKC normalization changes full-width punctuation.
    return str(value or "").strip()


def _canonical_detail_url(value: Any) -> str:
    raw = _text(value)
    return normalize_job_identity_url(raw) if raw else ""


def _utc_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            result = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _capture_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, Mapping) or "captured_at" not in value:
        return None
    return _utc_datetime(value.get("captured_at"))


def _native_job_identity(value: Any) -> str:
    for field_name in (
        "native_job_id",
        "source_job_id",
        "source_post_id",
        "post_id",
    ):
        candidate = _compact(_field(value, field_name))
        if candidate:
            return candidate
    return ""


def _identity_evidence_values(value: Any, prefix: str) -> tuple[str, ...] | None:
    if isinstance(value, Mapping):
        raw = value.get(prefix)
        if raw is None:
            raw = value.get(prefix.replace("_", ""))
        if raw is None:
            return None
        values = raw if isinstance(raw, (list, tuple, set)) else (raw,)
        return tuple(_compact(item) for item in values if _compact(item))
    if not isinstance(value, (list, tuple, set)):
        return None
    expected = prefix.casefold().replace("-", "_")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or ":" not in item:
            continue
        item_prefix, _, item_value = item.partition(":")
        if item_prefix.casefold().replace("-", "_") == expected and _compact(item_value):
            result.append(_compact(item_value))
    return tuple(result)


def _capture_identity_matches_job(job: Mapping[str, Any], evidence: Mapping[str, Any]) -> bool:
    native_id = _native_job_identity(job)
    title = normalize_job_title(job.get("title"))
    for field_name in ("native_job_id", "native_id", "post_id"):
        observed = _compact(evidence.get(field_name))
        if observed and native_id and observed.casefold() != native_id.casefold():
            return False
    observed_title = _compact(evidence.get("title"))
    if observed_title and title and normalize_job_title(observed_title) != title:
        return False

    identity_evidence = evidence.get("identity_evidence")
    if identity_evidence is None:
        identity_evidence = evidence.get("identityEvidence")
    if identity_evidence is None:
        return True
    native_values = _identity_evidence_values(identity_evidence, "native_id")
    title_values = _identity_evidence_values(identity_evidence, "title")
    if native_values and native_id and (
        len(native_values) != 1 or native_values[0].casefold() != native_id.casefold()
    ):
        return False
    if title_values and title and (
        len(title_values) != 1 or normalize_job_title(title_values[0]) != title
    ):
        return False
    return not observed_title or not title or normalize_job_title(observed_title) == title


def _capture_matches_job(job: Mapping[str, Any]) -> bool:
    assessment = assess_jd_capture(job)
    if not assessment.complete:
        return False
    evidence = job.get("capture_evidence")
    if not isinstance(evidence, Mapping):
        return False
    detail_url = _canonical_detail_url(job.get("detail_url") or job.get("jd_url"))
    source_url = _canonical_detail_url(evidence.get("source_url"))
    return bool(detail_url and source_url and detail_url == source_url) and _capture_identity_matches_job(
        job, evidence
    )


@dataclass(frozen=True, slots=True)
class _StoredDetailDecision:
    reusable: bool
    reason: str
    captured_at: datetime | None = None
    age_seconds: float | None = None


def _capture_freshness_decision(
    evidence: Any,
    *,
    now: datetime,
    ttl_hours: float,
) -> _StoredDetailDecision:
    captured_at = _capture_timestamp(evidence)
    if captured_at is None:
        return _StoredDetailDecision(False, "capture_time_missing")
    age_seconds = (now - captured_at).total_seconds()
    if age_seconds < 0:
        return _StoredDetailDecision(
            False,
            "capture_time_future",
            captured_at=captured_at,
            age_seconds=age_seconds,
        )
    if age_seconds > timedelta(hours=ttl_hours).total_seconds():
        return _StoredDetailDecision(
            False,
            "capture_stale",
            captured_at=captured_at,
            age_seconds=age_seconds,
        )
    return _StoredDetailDecision(
        True,
        "capture_fresh",
        captured_at=captured_at,
        age_seconds=age_seconds,
    )


def _string_list(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, Sequence) or isinstance(values, (bytes, bytearray)):
        raise CompanyConfigError(f"{field_name} must be a list of strings")
    result: list[str] = []
    for item in values:
        normalized = _compact(item)
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _origin(value: str) -> str | None:
    parsed = urlsplit(_text(value))
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


@dataclass(frozen=True, slots=True)
class PipelineCompany(Mapping[str, Any]):
    """A company row read only from the Agent-owned companies YAML."""

    id: str
    name: str
    careers_url: str
    crawler_key: str
    integration_status: str
    aliases: tuple[str, ...] = ()
    campaign_urls: tuple[str, ...] = ()
    campaign_url: str = ""
    campaign_text: str = ""
    link_kind: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def connected(self) -> bool:
        return self.integration_status.casefold() == "connected"

    def __getitem__(self, key: str) -> Any:
        return self.crawler_config()[key]

    def __iter__(self):
        return iter(self.crawler_config())

    def __len__(self) -> int:
        return len(self.crawler_config())

    def crawler_config(self) -> dict[str, Any]:
        result = dict(self.extra)
        result.update(
            {
                "id": self.id,
                "name": self.name,
                "careers_url": self.careers_url,
                "crawler": self.crawler_key,
                "integration_status": self.integration_status,
                "aliases": list(self.aliases),
                "campaign_urls": list(self.campaign_urls),
                "campaign_url": self.campaign_url,
                "campaign_text": self.campaign_text,
                "link_kind": self.link_kind,
            }
        )
        return result

    def to_core_config(self) -> CompanyConfig:
        return CompanyConfig.from_legacy(self.crawler_config())


def _company_from_row(row: Mapping[str, Any], index: int) -> PipelineCompany:
    name = _compact(row.get("name") or row.get("company"))
    if not name:
        raise CompanyConfigError(f"companies[{index}] requires a non-empty name")
    company_id = _compact(row.get("id") or row.get("key") or f"config-{index}")
    if not company_id:
        raise CompanyConfigError(f"companies[{index}] requires a non-empty id")
    crawler = _compact(row.get("crawler") or row.get("crawler_key"))
    status = _compact(
        row.get("integration_status")
        or row.get("status")
        or ("connected" if crawler else "not_connected")
    )
    careers_url = _compact(row.get("careers_url") or row.get("campus_url") or row.get("url"))
    known = {
        "id",
        "key",
        "name",
        "company",
        "careers_url",
        "campus_url",
        "url",
        "crawler",
        "crawler_key",
        "integration_status",
        "status",
        "aliases",
        "campaign_urls",
        "campaign_url",
        "campaign_text",
        "link_kind",
    }
    return PipelineCompany(
        id=company_id,
        name=name,
        careers_url=careers_url,
        crawler_key=crawler,
        integration_status=status or "not_connected",
        aliases=_string_list(row.get("aliases"), field_name=f"companies[{index}].aliases"),
        campaign_urls=_string_list(
            row.get("campaign_urls"), field_name=f"companies[{index}].campaign_urls"
        ),
        campaign_url=_compact(row.get("campaign_url")),
        campaign_text=_compact(row.get("campaign_text")),
        link_kind=_compact(row.get("link_kind")),
        extra={key: value for key, value in row.items() if key not in known},
    )


def load_companies(path: Path | str = DEFAULT_COMPANIES_PATH) -> list[PipelineCompany]:
    """Read the Agent-owned ``companies.yaml`` and normalize its rows."""

    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise CompanyConfigError(f"Agent companies file does not exist: {resolved}")
    try:
        payload = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CompanyConfigError(f"unable to read Agent companies file: {resolved}") from exc

    if isinstance(payload, Mapping):
        rows = payload.get("companies")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise CompanyConfigError("Agent companies YAML must contain a companies list")

    companies: list[PipelineCompany] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, Mapping):
            raise CompanyConfigError(f"companies[{index}] must be a mapping")
        company = _company_from_row(raw_row, index)
        name_key = company.name.casefold()
        if company.id in seen_ids:
            raise CompanyConfigError(f"duplicate company id: {company.id}")
        if name_key in seen_names:
            raise CompanyConfigError(f"duplicate company name: {company.name}")
        seen_ids.add(company.id)
        seen_names.add(name_key)
        companies.append(company)
    return companies


@dataclass(frozen=True, slots=True)
class CrawlResult:
    """Crawler observations and explicit completeness evidence."""

    jobs: Sequence[Any] = ()
    source_url: str = ""
    allowed_origins: Sequence[str] = ()
    pages_seen: int = 0
    total_pages: int | None = None
    has_more: bool = False
    crawler_key: str = ""
    run_reason: str = "completed"
    pagination_complete: bool | None = None
    completeness_known: bool | None = None
    advertised_total: int | None = None
    discovered_entry_url: str | None = None
    crawl_source_url: str | None = None
    effective_source_urls: Sequence[str] = ()
    termination_reasons: Sequence[str] = ()
    source_runs: Sequence[Mapping[str, Any]] = ()
    entry_attempts: Sequence[Mapping[str, Any]] = ()
    scope_key: str | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class CompanyRunResult:
    company_id: str
    company_name: str
    status: str
    raw_job_count: int = 0
    accepted_job_count: int = 0
    new_count: int = 0
    changed_count: int = 0
    reused_count: int = 0
    rejected_count: int = 0
    failed_count: int = 0
    filtered_count: int = 0
    rejection_reasons: Mapping[str, int] = field(default_factory=dict)
    failure_reason: str | None = None
    run_reason: str = ""
    observed_job_ids: tuple[str, ...] = ()
    filtered_reasons: Mapping[str, int] = field(default_factory=dict)
    crawl_evidence: Mapping[str, Any] = field(default_factory=dict)
    jd_results: Sequence[Mapping[str, Any]] = ()
    observed_titles: tuple[str, ...] = ()
    list_complete: bool = False
    detail_success_count: int = 0
    detail_failure_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "company_name": self.company_name,
            "status": self.status,
            "raw_job_count": self.raw_job_count,
            "accepted_job_count": self.accepted_job_count,
            "new_count": self.new_count,
            "changed_count": self.changed_count,
            "reused_count": self.reused_count,
            "rejected_count": self.rejected_count,
            "failed_count": self.failed_count,
            "filtered_count": self.filtered_count,
            "rejection_reasons": dict(self.rejection_reasons),
            "failure_reason": self.failure_reason,
            "run_reason": self.run_reason,
            "observed_job_ids": list(self.observed_job_ids),
            "filtered_reasons": dict(self.filtered_reasons),
            "crawl_evidence": dict(self.crawl_evidence),
            "jd_results": list(self.jd_results),
            "observed_titles": list(self.observed_titles),
            "list_complete": self.list_complete,
            "detail_success_count": self.detail_success_count,
            "detail_failure_count": self.detail_failure_count,
        }


@dataclass(frozen=True, slots=True)
class DailyPipelineResult:
    """Stable summary returned from one daily pipeline invocation."""

    dry_run: bool
    total_companies: int
    selected_companies: int
    crawled_companies: int
    skipped_companies: tuple[str, ...]
    new_count: int
    changed_count: int
    reused_count: int
    rejected_count: int
    failed_company_count: int
    failed_job_count: int
    filtered_count: int
    written: bool
    analysis_enabled: bool = False
    scoring_candidate_count: int = 0
    scored_count: int = 0
    scoring_failed_count: int = 0
    unscored_count: int = 0
    scoped_company_ids: tuple[str, ...] = ()
    resumed_company_ids: tuple[str, ...] = ()
    retried_company_ids: tuple[str, ...] = ()
    company_results: tuple[CompanyRunResult, ...] = ()
    new_job_ids: tuple[str, ...] = ()
    changed_job_ids: tuple[str, ...] = ()
    reused_job_ids: tuple[str, ...] = ()
    rejected_job_ids: tuple[str, ...] = ()
    failed_job_ids: tuple[str, ...] = ()
    rejection_reasons: Mapping[str, int] = field(default_factory=dict)
    failure_reasons: Mapping[str, int] = field(default_factory=dict)

    @property
    def failed_count(self) -> int:
        return self.failed_company_count + self.failed_job_count

    @property
    def new(self) -> int:
        return self.new_count

    @property
    def changed(self) -> int:
        return self.changed_count

    @property
    def reused(self) -> int:
        return self.reused_count

    @property
    def rejected(self) -> int:
        return self.rejected_count

    @property
    def failed(self) -> int:
        return self.failed_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "total_companies": self.total_companies,
            "selected_companies": self.selected_companies,
            "crawled_companies": self.crawled_companies,
            "skipped_companies": list(self.skipped_companies),
            "new": self.new_count,
            "changed": self.changed_count,
            "reused": self.reused_count,
            "rejected": self.rejected_count,
            "failed": self.failed_count,
            "failed_companies": self.failed_company_count,
            "failed_jobs": self.failed_job_count,
            "filtered": self.filtered_count,
            "written": self.written,
            "analysis_enabled": self.analysis_enabled,
            "scoring_candidates": self.scoring_candidate_count,
            "scored": self.scored_count,
            "scoring_failed": self.scoring_failed_count,
            "unscored": self.unscored_count,
            "scoped_company_ids": list(self.scoped_company_ids),
            "resumed_company_ids": list(self.resumed_company_ids),
            "retried_company_ids": list(self.retried_company_ids),
            "new_job_ids": list(self.new_job_ids),
            "changed_job_ids": list(self.changed_job_ids),
            "reused_job_ids": list(self.reused_job_ids),
            "rejected_job_ids": list(self.rejected_job_ids),
            "failed_job_ids": list(self.failed_job_ids),
            "rejection_reasons": dict(self.rejection_reasons),
            "failure_reasons": dict(self.failure_reasons),
            "companies": [item.to_dict() for item in self.company_results],
        }

    as_dict = to_dict


@dataclass(frozen=True, slots=True)
class _ExistingSnapshot:
    job: JobSnapshot
    analysis: JobAnalysisSnapshot | None


@dataclass(slots=True)
class _CompanyWork:
    company: PipelineCompany
    raw_job_count: int = 0
    accepted_jobs: list[dict[str, Any]] = field(default_factory=list)
    rejected_job_ids: list[str] = field(default_factory=list)
    rejection_reasons: Counter[str] = field(default_factory=Counter)
    failure_reason: str | None = None
    run_reason: str = ""
    crawl_evidence: dict[str, Any] = field(default_factory=dict)
    jd_results: list[dict[str, Any]] = field(default_factory=list)
    observed_titles: list[str] = field(default_factory=list)
    list_complete: bool = False
    scope_key: str = ""
    detail_success_count: int = 0
    detail_failure_count: int = 0
    detail_failure_reasons: Counter[str] = field(default_factory=Counter)
    hydration_results: dict[str, dict[str, Any]] = field(default_factory=dict)


_COMPANY_CHECKPOINT_VERSION = 1


def _checkpoint_work_payload(work: _CompanyWork) -> dict[str, Any]:
    """Keep enough normalized crawl output to resume without recrawling it."""

    return {
        "company_id": work.company.id,
        "raw_job_count": work.raw_job_count,
        "accepted_jobs": _dump(work.accepted_jobs),
        "rejected_job_ids": list(work.rejected_job_ids),
        "rejection_reasons": dict(work.rejection_reasons),
        "failure_reason": work.failure_reason,
        "run_reason": work.run_reason,
        "crawl_evidence": _dump(work.crawl_evidence),
        "jd_results": _dump(work.jd_results),
        "observed_titles": list(work.observed_titles),
        "list_complete": work.list_complete,
        "scope_key": work.scope_key,
        "detail_success_count": work.detail_success_count,
        "detail_failure_count": work.detail_failure_count,
        "detail_failure_reasons": dict(work.detail_failure_reasons),
    }


def _checkpoint_counter(value: Any) -> Counter[str]:
    if not isinstance(value, Mapping):
        return Counter()
    result: Counter[str] = Counter()
    for key, count in value.items():
        try:
            result[str(key)] = int(count)
        except (TypeError, ValueError):
            continue
    return result


def _work_from_checkpoint(
    company: PipelineCompany,
    payload: Mapping[str, Any],
) -> _CompanyWork:
    if str(payload.get("company_id") or "") != company.id:
        raise PipelineError(f"company checkpoint does not match {company.id}")
    raw_jobs = payload.get("accepted_jobs") or []
    if not isinstance(raw_jobs, list) or any(not isinstance(item, Mapping) for item in raw_jobs):
        raise PipelineError(f"company checkpoint has invalid jobs: {company.id}")
    raw_evidence = payload.get("crawl_evidence")
    evidence = dict(raw_evidence) if isinstance(raw_evidence, Mapping) else {}
    raw_jd_results = payload.get("jd_results") or []
    jd_results = [dict(item) for item in raw_jd_results if isinstance(item, Mapping)]
    raw_titles = payload.get("observed_titles") or []
    hydration_results = payload.get("hydration_results") or {}
    if not isinstance(hydration_results, Mapping):
        raise PipelineError(f"company checkpoint has invalid hydration results: {company.id}")
    try:
        raw_job_count = int(payload.get("raw_job_count") or 0)
    except (TypeError, ValueError):
        raw_job_count = 0
    return _CompanyWork(
        company=company,
        raw_job_count=max(0, raw_job_count),
        accepted_jobs=[dict(item) for item in raw_jobs],
        rejected_job_ids=[str(item) for item in payload.get("rejected_job_ids") or []],
        rejection_reasons=_checkpoint_counter(payload.get("rejection_reasons")),
        failure_reason=(
            str(payload["failure_reason"])
            if payload.get("failure_reason") is not None
            else None
        ),
        run_reason=str(payload.get("run_reason") or ""),
        crawl_evidence=evidence,
        jd_results=jd_results,
        observed_titles=[str(item) for item in raw_titles],
        list_complete=bool(payload.get("list_complete")),
        scope_key=str(payload.get("scope_key") or ""),
        detail_success_count=max(0, int(payload.get("detail_success_count") or 0)),
        detail_failure_count=max(0, int(payload.get("detail_failure_count") or 0)),
        detail_failure_reasons=_checkpoint_counter(payload.get("detail_failure_reasons")),
        hydration_results={
            str(key): dict(value)
            for key, value in hydration_results.items()
            if isinstance(value, Mapping)
        },
    )


@dataclass(slots=True)
class _StagedWork:
    company: PipelineCompany
    job: dict[str, Any]
    fingerprint: str
    category: str
    analysis: JobAnalysis | None
    existing: _ExistingSnapshot | None


@dataclass(slots=True)
class _JobOutcome:
    work: _CompanyWork
    job_id: str
    staged: _StagedWork | None = None
    category: str = ""
    analysis_status: str = ""
    failed: bool = False
    rejected_reason: str | None = None
    filtered_reasons: tuple[str, ...] = ()
    error_code: str | None = None
    deferred: bool = False


@dataclass(slots=True)
class _TitleFirstCandidate:
    work: _CompanyWork
    job: dict[str, Any]
    title_key: str
    existing: _ExistingSnapshot | None = None
    screening: Any = None
    detail_retry: bool = False
    analysis: JobAnalysis | None = None


_FATAL_MATCH_ERROR_CODES = {"http_401", "http_402", "http_403"}


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_dump(item) for item in value]
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        return _dump(value.value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python")
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return dict(dumped)
    return {}


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _fingerprint_job_payload(job: Mapping[str, Any]) -> dict[str, Any]:
    # The storage model has no job_type column, so the fingerprint is kept in
    # source_ref while the complete source-independent matching payload is used.
    payload = dict(job)
    payload["company"] = ""
    payload["company_id"] = _compact(payload.get("company_id") or payload.get("company"))
    return payload


def job_content_fingerprint(job: Mapping[str, Any]) -> str:
    """Return the stable content fingerprint used for cross-run comparisons."""

    payload = _fingerprint_job_payload(job)
    if _matching_content_fingerprint is not None:
        return _matching_content_fingerprint(payload)
    canonical = {
        "company": payload.get("company_id", ""),
        "title": _compact(payload.get("title")),
        "city": _compact(payload.get("city")),
        "job_type": _compact(payload.get("job_type")),
        "batch": _compact(payload.get("batch")),
        "cohort": payload.get("cohort"),
        "cohort_status": _compact(payload.get("cohort_status")),
        "jd_raw": _compact(payload.get("jd_raw")),
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _fingerprint_from_source_ref(source_ref: Any) -> str | None:
    match = _FINGERPRINT_RE.search(_text(source_ref))
    return match.group("fingerprint") if match else None


def _source_ref(company_id: str, job_id: str, fingerprint: str) -> str:
    value = f"{PIPELINE_SOURCE}:job:{company_id}:{job_id}:fp:{fingerprint}"
    if len(value) <= 500:
        return value
    identity = hashlib.sha256(f"{company_id}\x00{job_id}".encode("utf-8")).hexdigest()
    return f"{PIPELINE_SOURCE}:job:{identity}:fp:{fingerprint}"


def _stable_job_id(company: PipelineCompany, raw: Mapping[str, Any]) -> str:
    explicit = _compact(
        raw.get("id")
        or raw.get("job_id")
        or raw.get("source_id")
        or raw.get("external_id")
    )
    detail_url = _compact(raw.get("detail_url") or raw.get("jd_url"))
    title = _compact(raw.get("title"))
    city = _compact(raw.get("city"))
    value = explicit or hashlib.sha256(
        f"{company.id}\x00{detail_url}\x00{title}\x00{city}".encode("utf-8")
    ).hexdigest()
    if len(value) <= 255:
        return value
    return "derived-" + hashlib.sha256(f"{company.id}\x00{value}".encode("utf-8")).hexdigest()


def _batch_value(raw: Mapping[str, Any]) -> str:
    value = raw.get("batch")
    if value is None or not _compact(value):
        value = raw.get("recruitment_track")
    normalized = _compact(value).casefold()
    return {
        "formal_batch": "formal",
        "early_batch": "early",
        "earlybatch": "early",
        "formalbatch": "formal",
        "校招": "formal",
        "正式": "formal",
        "提前批": "early",
        "实习": "internship",
    }.get(normalized, normalized or RecruitmentBatch.UNKNOWN.value)


def _observed_job(company: PipelineCompany, raw_job: Any) -> tuple[ObservedCrawlerJob, dict[str, Any]]:
    raw = _as_mapping(raw_job)
    from packages.recruitment_core.offerbiu_policy import apply_offerbiu_cohort, is_offerbiu_source

    if is_offerbiu_source(company.extra) or is_offerbiu_source(raw):
        raw = apply_offerbiu_cohort(raw)
    if not raw:
        raise ValueError("crawler job must be a mapping or Pydantic model")
    detail_url = _text(raw.get("detail_url") or raw.get("jd_url"))
    normalized = dict(raw)
    if isinstance(company.extra.get("detail_interaction"), Mapping):
        normalized.setdefault("detail_interaction", dict(company.extra["detail_interaction"]))
    if isinstance(company.extra.get("entry_click_texts"), (list, tuple)):
        normalized.setdefault("entry_click_texts", list(company.extra["entry_click_texts"]))
    normalized.setdefault("careers_url", company.careers_url)
    normalized.update(
        {
            "id": _stable_job_id(company, raw),
            "company": company.name,
            "company_id": company.id,
            "title": _text(raw.get("title")),
            "city": _text(raw.get("city")) or None,
            "detail_url": detail_url,
            "jd_url": detail_url,
            "jd_raw": _raw_detail(raw.get("jd_raw")) or None,
            "cohort_status": _compact(raw.get("cohort_status") or "unconfirmed"),
            "batch": _batch_value(raw),
        }
    )
    observed = ObservedCrawlerJob(
        id=normalized["id"],
        title=normalized["title"],
        city=normalized["city"],
        detail_url=normalized["detail_url"],
        jd_raw=normalized["jd_raw"],
        capture_evidence=normalized.get("capture_evidence") or {},
        cohort=normalized.get("cohort"),
        cohort_status=normalized["cohort_status"],
        cohort_source=_text(normalized.get("cohort_source")) or None,
        cohort_evidence=_text(normalized.get("cohort_evidence")) or None,
        batch=normalized["batch"],
    )
    return observed, normalized


def _configured_origins(company: PipelineCompany) -> list[str]:
    values = [company.careers_url, company.campaign_url, *company.campaign_urls]
    return sorted({origin for value in values for origin in [_origin(value)] if origin})


_STRICT_SOURCE_IDENTITY_KINDS = frozenset(
    {"alibaba", "beisen", "feishu", "hotjob", "moka"}
)


def _source_identity_evidence(
    company: PipelineCompany,
    result: CrawlResult,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return configured and observed identities for registered shared ATS sites."""

    configured = _compact(company.extra.get("source_identity")).casefold()
    identity_kind = configured.partition(":")[0]
    if identity_kind not in _STRICT_SOURCE_IDENTITY_KINDS:
        return (), ()

    expected = {configured}
    for url in (
        company.careers_url,
        company.campaign_url,
        *company.campaign_urls,
    ):
        identity = source_identity_for_url(url, identity_kind).casefold()
        if identity.startswith(f"{identity_kind}:"):
            expected.add(identity)

    observed_urls: list[str] = []
    if result.source_runs:
        for run in result.source_runs:
            observed_urls.append(
                _text(run.get("effective_source_url") or run.get("source_url"))
            )
    elif result.effective_source_urls:
        observed_urls.extend(_text(url) for url in result.effective_source_urls)
    else:
        observed_urls.append(_text(result.discovered_entry_url or result.source_url))
    observed = {
        identity
        for url in observed_urls
        if url
        for identity in [source_identity_for_url(url, identity_kind).casefold()]
        if identity.startswith(f"{identity_kind}:")
    }
    return tuple(sorted(expected)), tuple(sorted(observed))


def _entry_failure_reason(
    company: PipelineCompany,
    *,
    empty_result: bool,
) -> str | None:
    excluded = classify_oc_destination_url(company.careers_url)
    if excluded is not None:
        kind, _reason = excluded
        return "form_application_only" if kind == "form" else "invalid_entry"

    diagnosis = diagnose_candidate_entry(company.careers_url)
    if diagnosis.entry_kind == "invalid_entry":
        return "invalid_entry"
    if diagnosis.entry_kind == "form_application":
        return "form_application_only"
    if empty_result and diagnosis.entry_kind == "entry_discovery_required":
        return "recruitment_entry_discovery_required"
    if empty_result and company.crawler_key.casefold() == "render":
        return "adapter_required"
    if empty_result:
        return "no_results"
    return None


_PAGINATION_ERROR_CODES = frozenset(
    {
        "pagination_incomplete",
        "pagination_unknown",
        "pagination_evidence_missing",
    }
)


def _pagination_failure_reason(result: CrawlResult, *, completeness_known: bool) -> str | None:
    """Return company coverage failure without changing row-level admission."""

    error_code = _compact(result.error_code).casefold()
    if error_code in {"pagination_evidence_missing", "pagination_unknown"}:
        return "pagination_unknown"
    if error_code == "pagination_incomplete":
        return "pagination_incomplete"
    if not completeness_known:
        return "pagination_unknown"
    if (
        result.pagination_complete is False
        or result.has_more
        or (result.total_pages is not None and result.pages_seen < result.total_pages)
        or (
            result.advertised_total is not None
            and len(result.jobs) < result.advertised_total
        )
    ):
        return "pagination_incomplete"
    return None


def _normalize_crawl_result(raw: Any, company: PipelineCompany) -> CrawlResult:
    if isinstance(raw, CrawlResult):
        return raw
    if isinstance(raw, (list, tuple)):
        return CrawlResult(
            jobs=list(raw),
            source_url=company.careers_url,
            allowed_origins=_configured_origins(company),
        )

    outer = _as_mapping(raw)
    if not outer and raw is not None:
        outer = {
            key: getattr(raw, key)
            for key in (
                "jobs",
                "accepted_jobs",
                "source_url",
                "allowed_origins",
                "pages_seen",
                "total_pages",
                "has_more",
                "pagination_complete",
                 "crawler_key",
                 "run_reason",
                 "error_message",
                 "scope_key",
            )
            if hasattr(raw, key)
        }
    data = _field(raw, "data")
    data_mapping = _as_mapping(data) if data is not None else {}
    if data_mapping:
        combined = {**data_mapping, **outer}
    else:
        combined = outer
    if not combined:
        raise ValueError("crawler returned an unsupported result shape")
    if combined.get("error_message") and not combined.get("error_code") and not combined.get("jobs") and not combined.get("accepted_jobs"):
        raise RuntimeError(_compact(combined["error_message"]))

    jobs = combined.get("jobs")
    if jobs is None:
        jobs = combined.get("accepted_jobs") or []
    if not isinstance(jobs, (list, tuple)):
        raise ValueError("crawler result jobs must be a list")

    source_url = _text(combined.get("source_url") or company.careers_url)
    allowed_origins = combined.get("allowed_origins")
    if not isinstance(allowed_origins, (list, tuple)):
        allowed_origins = _configured_origins(company)
    allowed_origins = list(allowed_origins)
    for value in combined.get("effective_source_urls") or ():
        origin = _origin(_text(value))
        if origin and origin not in allowed_origins:
            allowed_origins.append(origin)

    pagination_complete = combined.get("pagination_complete")
    if pagination_complete is not None:
        complete = bool(pagination_complete)
        has_more = bool(combined.get("has_more", not complete))
    else:
        has_more = bool(combined.get("has_more", False))
    pages_value = combined.get("pages_seen", combined.get("pages_fetched", 0))
    total_value = combined.get("total_pages", combined.get("page_count"))
    try:
        pages_seen = int(pages_value or 0)
    except (TypeError, ValueError):
        pages_seen = 0
    try:
        total_pages = int(total_value) if total_value is not None else None
    except (TypeError, ValueError):
        total_pages = None
    return CrawlResult(
        jobs=list(jobs),
        source_url=source_url,
        allowed_origins=tuple(_text(item) for item in allowed_origins if _text(item)),
        pages_seen=pages_seen,
        total_pages=total_pages,
        has_more=has_more,
        crawler_key=_compact(combined.get("crawler_key") or company.crawler_key),
        run_reason=_compact(combined.get("run_reason") or "completed"),
        pagination_complete=pagination_complete,
        completeness_known=combined.get("completeness_known"),
        advertised_total=combined.get("advertised_total"),
        discovered_entry_url=combined.get("discovered_entry_url"),
        crawl_source_url=combined.get("crawl_source_url"),
        effective_source_urls=tuple(combined.get("effective_source_urls") or ()),
        termination_reasons=tuple(combined.get("termination_reasons") or ()),
        source_runs=tuple(combined.get("source_runs") or ()),
        entry_attempts=tuple(combined.get("entry_attempts") or ()),
        scope_key=_compact(combined.get("scope_key")) or None,
        error_code=_compact(combined.get("error_code")) or None,
    )


_DETAIL_FAILURE_STATUSES = frozenset(
    {
        "failed",
        "failure",
        "error",
        "timeout",
        "content_incomplete",
        "official_unavailable",
        "identity_mismatch",
        "identity_ambiguous",
        "identity_unverified",
        "no_detail_url",
        "detail_link",
        "list_url",
        "unknown",
        "pending",
    }
)


def _title_key(company_id: Any, title: Any) -> str:
    """Use the shared exact title key without importing matching heuristics."""

    _company_key, title_key = company_title_key(company_id, title)
    return _text(title_key)


def _screen_title(job: Mapping[str, Any], profile: Any) -> Any:
    """Run the title-only policy; the fallback exists only during staged integration."""

    return screen_title_job(job, profile)


def _screening_status(screening: Any) -> str:
    return _compact(getattr(screening, "analysis_status", "")).casefold()


def _screening_reasons(screening: Any) -> tuple[str, ...]:
    values = getattr(screening, "reasons", ()) or ()
    result: list[str] = []
    for value in values:
        text = _compact(getattr(value, "value", value))
        if text and text not in result:
            result.append(text)
    status = _screening_status(screening)
    if status and status not in result:
        result.insert(0, status)
    return tuple(result)


def _capture_status(value: Any) -> str:
    status = _compact(value).casefold()
    return status if status in {"unknown", "pending", "complete", "failed"} else "unknown"


def _stored_capture_status(job: Any) -> str:
    value = getattr(job, "capture_status", None)
    if value is None and isinstance(job, Mapping):
        value = job.get("capture_status")
    return _capture_status(value)


def _stored_availability_status(job: Any) -> str:
    value = getattr(job, "availability_status", None)
    if value is None and isinstance(job, Mapping):
        value = job.get("availability_status")
    value = _compact(value).casefold()
    return value if value in {"active", "inactive"} else "active"


def _is_title_first_pending_analysis(analysis: JobAnalysisSnapshot | None) -> bool:
    return bool(
        analysis is not None
        and _analysis_status(getattr(analysis, "analysis_status", None), default="")
        == "pending"
        and _compact(getattr(analysis, "analysis_version", ""))
        == _TITLE_FIRST_PENDING_ANALYSIS_VERSION
    )


def _existing_score_is_valid(existing: _ExistingSnapshot) -> bool:
    """Treat a persisted row score as authoritative even without its analysis row."""

    def valid_score(value: Any) -> bool:
        if value is None or isinstance(value, bool):
            return False
        try:
            score = float(value)
        except (TypeError, ValueError, OverflowError):
            return False
        return math.isfinite(score) and 0 <= score <= 100

    if valid_score(_field(existing.job, "match_score")):
        return True
    analysis = existing.analysis
    return bool(
        analysis is not None
        and _analysis_status(_field(analysis, "analysis_status"), default="") == "complete"
        and valid_score(_field(analysis, "match_score"))
    )


def _scope_key(company: PipelineCompany, evidence: Mapping[str, Any]) -> str:
    explicit = _compact(evidence.get("scope_key"))
    if explicit:
        return explicit
    source = _text(
        evidence.get("crawl_source_url")
        or evidence.get("source_url")
        or company.careers_url
    )
    tenant = _compact(company.extra.get("source_identity"))
    platform = _compact(evidence.get("crawler_key") or company.crawler_key)
    return "|".join((platform.casefold(), tenant.casefold(), source))


def _scope_marker(value: Any) -> str | None:
    source_ref = _text(value)
    match = re.search(r":scope:(?P<scope>[0-9a-f]{64})(?=:|$)", source_ref)
    return match.group("scope") if match else None


def _scope_digest(scope: str) -> str:
    return hashlib.sha256(scope.encode("utf-8")).hexdigest()


def _title_first_source_ref(company_id: str, job_id: str, fingerprint: str, scope: str) -> str:
    value = (
        f"{PIPELINE_SOURCE}:job:{company_id}:{job_id}:scope:{_scope_digest(scope)}:fp:{fingerprint}"
    )
    if len(value) <= 500:
        return value
    identity = hashlib.sha256(f"{company_id}\x00{job_id}".encode("utf-8")).hexdigest()
    return f"{PIPELINE_SOURCE}:job:{identity}:scope:{_scope_digest(scope)}:fp:{fingerprint}"


def _existing_scope_matches(
    existing: JobSnapshot,
    company: PipelineCompany,
    work: _CompanyWork,
) -> bool:
    current_scope = _scope_digest(work.scope_key)
    marker = _scope_marker(getattr(existing, "source_ref", None))
    if marker is not None:
        return marker == current_scope

    stored_platform = _compact(getattr(existing, "source_platform", None)).casefold()
    if stored_platform and stored_platform != company.crawler_key.casefold():
        return False
    configured_tenant = _compact(company.extra.get("source_identity")).casefold()
    stored_tenant = _compact(getattr(existing, "source_tenant", None)).casefold()
    if configured_tenant and stored_tenant and configured_tenant != stored_tenant:
        return False
    current_origin = _origin(
        work.crawl_evidence.get("crawl_source_url")
        or work.crawl_evidence.get("source_url")
        or company.careers_url
    )
    stored_origin = _origin(getattr(existing, "detail_url", None))
    # A legacy row has no persisted scope marker.  Without both comparable
    # origins, it is safer to leave the row active than to infer an absence
    # from incomplete historical evidence.
    return bool(current_origin and stored_origin and current_origin == stored_origin)


def _snapshot_job_payload(row: JobSnapshot) -> dict[str, Any]:
    """Rehydrate only persisted job data for a pending title-first score."""

    detail_url = _text(getattr(row, "detail_url", None))
    return {
        "id": row.id,
        "company_id": row.company_id,
        "title": row.title,
        "city": row.city,
        "detail_url": detail_url,
        "jd_url": detail_url,
        "jd_raw": row.jd_raw,
        "cohort": row.cohort,
        "cohort_status": row.cohort_status,
        "batch": row.batch,
        "match_score": row.match_score,
        "capture_status": getattr(row, "capture_status", "unknown"),
        "capture_failure_reason": getattr(row, "capture_failure_reason", ""),
        "availability_status": getattr(row, "availability_status", "active"),
        "title_key": getattr(row, "title_key", None),
        "capture_evidence": dict(getattr(row, "capture_evidence", {}) or {}),
        "native_job_id": getattr(row, "native_job_id", None),
        "normalized_detail_url": getattr(row, "normalized_detail_url", None),
        "business_key": getattr(row, "business_key", None),
        "recruitment_campaign_id": getattr(row, "recruitment_campaign_id", None),
    }


def _detail_capture_result(
    job: dict[str, Any],
    raw_result: Any,
) -> tuple[bool, str, str, dict[str, Any], dict[str, Any]]:
    """Normalize one injected detail result without applying a JD length rule."""

    payload = _as_mapping(raw_result)
    if not payload and raw_result is not None:
        payload = {
            key: getattr(raw_result, key)
            for key in (
                "detail",
                "jd_raw",
                "status",
                "detail_url",
                "source",
                "attempts",
                "error_type",
                "error_code",
                "error_detail",
                "identity_status",
                "identity_evidence",
                "identity_diagnostic",
                "capture_evidence",
            )
            if hasattr(raw_result, key)
        }
    if payload:
        detail = _raw_detail(payload.get("detail") or payload.get("jd_raw"))
        status = _compact(payload.get("status")).casefold()
        response_url = _text(payload.get("detail_url")) or _text(job.get("detail_url"))
        evidence = payload.get("capture_evidence")
        capture = dict(evidence) if isinstance(evidence, Mapping) else {}
        diagnostic = {
            key: _dump(payload.get(key))
            for key in (
                "status",
                "source",
                "detail_url",
                "attempts",
                "error_type",
                "error_code",
                "error_detail",
                "identity_status",
                "identity_evidence",
                "identity_diagnostic",
                "capture_evidence",
            )
            if key in payload
        }
    else:
        detail = _raw_detail(raw_result)
        status = "complete" if detail else "failed"
        response_url = _text(job.get("detail_url"))
        capture = {}
        diagnostic = {"status": status, "detail_url": response_url}

    identity_status = _compact(diagnostic.get("identity_status")).casefold()
    if identity_status in {"mismatch", "ambiguous", "unmatched", "identity_mismatch"}:
        status = "identity_mismatch"
    if status in _DETAIL_FAILURE_STATUSES or not detail:
        reason = (
            _compact(diagnostic.get("error_code"))
            or _compact(diagnostic.get("error_type"))
            or _compact(diagnostic.get("error_detail"))
            or (status if status not in {"", "complete", "success", "official_sparse"} else "detail_empty")
            or "detail_empty"
        )
        return False, detail, reason, capture, diagnostic

    candidate = {**job, "jd_raw": detail, "detail_url": response_url}
    if response_url and _canonical_detail_url(response_url) != _canonical_detail_url(job.get("detail_url")):
        if not capture:
            return False, detail, "detail_url_changed_unverified", capture, diagnostic
    if capture and not _capture_matches_job({**candidate, "capture_evidence": capture}):
        return False, detail, "capture_identity_or_source_invalid", capture, diagnostic
    return True, detail, "", capture, diagnostic


def _stored_analysis_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value.strip() else []
        if isinstance(value, str):
            return [value] if value.strip() else []
    return _safe_strings(value)


def _existing_analysis_mapping(row: JobAnalysisSnapshot | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "job_id": row.job_id,
        "match_score": row.match_score,
        "advantages": _stored_analysis_strings(row.advantages),
        "gaps": _stored_analysis_strings(row.gaps),
        "summary": row.summary,
        "recommendation": row.recommendation,
        "score_breakdown": dict(row.score_breakdown or {}),
        "evidence": list(row.evidence or []),
        "evidence_level": row.evidence_level,
        "matched_directions": list(row.matched_directions or []),
        "primary_match_direction": row.primary_match_direction,
        "analysis_status": row.analysis_status,
        "model": row.model,
        "analysis_version": row.analysis_version,
        "prompt_version": row.prompt_version,
        "content_fingerprint": row.content_fingerprint,
        "profile_fingerprint": row.profile_fingerprint,
        "input_tokens": row.input_tokens,
        "output_tokens": row.output_tokens,
        "filter_reasons": list(row.filter_reasons or []),
        "refusal_reason": row.refusal_reason,
        "error_code": row.error_code,
        "analyzed_at": row.analyzed_at,
    }


def _analysis_status(value: Any, *, default: str = "complete") -> str:
    if value is None:
        return default
    raw = getattr(value, "value", value)
    normalized = _compact(raw).casefold()
    return normalized or default


def _safe_score(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def _safe_strings(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        normalized = _compact(item)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _safe_mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        dumped = _dump(item)
        if isinstance(dumped, Mapping):
            result.append(dict(dumped))
    return result


def _analysis_model(
    raw_result: Any,
    job: Mapping[str, Any],
    clock: Callable[[], datetime],
) -> JobAnalysis:
    if isinstance(raw_result, (int, float)):
        payload: dict[str, Any] = {"match_score": raw_result}
    else:
        payload = _as_mapping(raw_result)
    if not payload:
        nested = _field(raw_result, "result") or _field(raw_result, "analysis")
        if nested is not None:
            payload = _as_mapping(nested)
    if not payload:
        raise ValueError("matcher returned an unsupported result shape")
    nested_result = payload.get("result") or payload.get("analysis")
    if isinstance(nested_result, (Mapping, BaseModel)):
        payload = _as_mapping(nested_result)
    status = _analysis_status(payload.get("analysis_status") or payload.get("status"))
    if payload.get("eligible") is False and status == "complete":
        status = "filtered"
    score = _safe_score(payload.get("match_score", payload.get("score")))
    analyzed_at = payload.get("analyzed_at")
    if not isinstance(analyzed_at, datetime):
        analyzed_at = _timestamp(clock)
    return JobAnalysis(
        match_score=score,
        advantages=_safe_strings(payload.get("advantages")),
        gaps=_safe_strings(payload.get("gaps")),
        summary=_compact(payload.get("summary")) or None,
        recommendation=_compact(payload.get("recommendation")) or None,
        score_breakdown=dict(_dump(payload.get("score_breakdown") or {})),
        evidence=_safe_mapping_list(payload.get("evidence")),
        evidence_level=_compact(payload.get("evidence_level")) or None,
        matched_directions=[
            _compact(getattr(item, "value", item))
            for item in payload.get("matched_directions") or []
            if _compact(getattr(item, "value", item))
        ],
        primary_match_direction=(
            _compact(getattr(payload.get("primary_match_direction"), "value", payload.get("primary_match_direction")))
            or None
        ),
        analysis_status=status,
        model=_compact(payload.get("model")) or None,
        analysis_version=_compact(payload.get("analysis_version")) or None,
        prompt_version=_compact(payload.get("prompt_version")) or None,
        content_fingerprint=_compact(payload.get("content_fingerprint")) or None,
        profile_fingerprint=_compact(payload.get("profile_fingerprint")) or None,
        input_tokens=payload.get("input_tokens"),
        output_tokens=payload.get("output_tokens"),
        filter_reasons=_safe_strings(payload.get("filter_reasons")),
        refusal_reason=_compact(payload.get("refusal_reason")) or None,
        error_code=_compact(payload.get("error_code")) or None,
        analyzed_at=analyzed_at,
    )


def _decision_action(raw_result: Any) -> str:
    """Extract a model service reuse decision without depending on its class."""

    decision = _field(raw_result, "decision")
    action = _field(decision, "action") if decision is not None else None
    return _compact(getattr(action, "value", action)).casefold()


class DeterministicMatcher:
    """Local screening adapter used when no model-backed matcher is injected."""

    def __init__(self, profile: Any = None):
        self.profile = profile

    def match(self, job: Mapping[str, Any], *, existing_analysis: Any = None) -> dict[str, Any]:
        del existing_analysis
        if _screen_job is None:
            return {"analysis_status": "eligible"}
        screening = _screen_job(job, self.profile)
        result = {
            "eligible": screening.eligible,
            "analysis_status": getattr(screening.analysis_status, "value", screening.analysis_status),
            "reasons": list(screening.reasons),
            "matched_directions": [
                getattr(item, "value", item) for item in screening.matched_directions
            ],
            "primary_match_direction": getattr(
                screening.primary_match_direction,
                "value",
                screening.primary_match_direction,
            ),
            "evidence": [_dump(item) for item in screening.evidence],
            "summary": "确定性筛选通过" if screening.eligible else "确定性筛选拒绝",
            "analysis_version": "deterministic-v1",
            "prompt_version": "none",
            "content_fingerprint": job_content_fingerprint(job),
        }
        if _matching_profile_fingerprint is not None:
            result["profile_fingerprint"] = _matching_profile_fingerprint(self.profile)
        return result

    def can_reuse(self, analysis: JobAnalysisSnapshot | None) -> bool:
        if analysis is None or analysis.analysis_version != "deterministic-v1":
            return False
        if _matching_profile_fingerprint is None:
            return False
        return analysis.profile_fingerprint == _matching_profile_fingerprint(self.profile)


class MatchingServiceAdapter:
    """Adapt the existing ``MatchingService.analyze`` shape to ``match``."""

    def __init__(self, service: Any, profile: Any = None):
        if not callable(getattr(service, "analyze", None)):
            raise TypeError("matching service must provide analyze()")
        self.service = service
        self.profile = profile

    def match(self, job: Mapping[str, Any], *, existing_analysis: Any = None) -> Any:
        return self.service.analyze(
            job,
            self.profile,
            existing_analysis=existing_analysis,
        )


def _matcher_callable(matcher: Any) -> Callable[..., Any]:
    target = getattr(matcher, "match", None)
    if callable(target):
        return target
    target = getattr(matcher, "analyze", None)
    if callable(target):
        return target
    if callable(matcher):
        return matcher
    raise TypeError("matcher must be callable or provide match()/analyze()")


def _call_matcher(
    matcher: Any,
    job: Mapping[str, Any],
    profile: Any,
    existing_analysis: Any,
) -> Any:
    target = _matcher_callable(matcher)
    target_name = getattr(target, "__name__", "")
    if target_name == "analyze" or getattr(matcher, "analyze", None) is target:
        return target(job, profile, existing_analysis=existing_analysis)

    try:
        parameters = inspect.signature(target).parameters
    except (TypeError, ValueError):
        parameters = {}
    kwargs: dict[str, Any] = {}
    accepts_kwargs = any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values())
    if "existing_analysis" in parameters or accepts_kwargs:
        kwargs["existing_analysis"] = existing_analysis
    if "profile" in parameters or accepts_kwargs:
        kwargs["profile"] = profile
    if kwargs:
        return target(job, **kwargs)
    positional = [
        item
        for item in parameters.values()
        if item.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    if len(positional) >= 2:
        return target(job, profile)
    return target(job)


class DailyRecruitmentPipeline:
    """Run one isolated, bounded daily crawl over the Agent company catalog.

    Persisted detail receipts are reusable for 24 hours by default.  Callers
    can set ``detail_reuse_ttl_hours`` explicitly when a runtime needs a
    different, predictable freshness window.
    """

    def __init__(
        self,
        *,
        companies_path: Path | str = DEFAULT_COMPANIES_PATH,
        storage: Storage | None = None,
        crawler: CrawlerProtocol | Mapping[str, Any] | None = None,
        crawler_map: Mapping[str, type] | None = None,
        matcher: MatcherProtocol | Callable[..., Any] | None = None,
        jd_hydrator: Callable[[dict[str, Any]], Any] | None = _default_jd_hydrator,
        profile: Any = None,
        max_concurrency: int = 4,
        match_max_concurrency: int = 4,
        checkpoint_batch_size: int = 25,
        company_timeout_seconds: float = 300.0,
        detail_reuse_ttl_hours: float = DEFAULT_DETAIL_REUSE_TTL_HOURS,
        company_ids: Sequence[str] = (),
        checkpoint_path: Path | str | None = None,
        resume_from_checkpoint: bool = False,
        progress_callback: Callable[[str, int, int], None] | None = None,
        clock: Callable[[], datetime] = _now,
    ):
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if company_timeout_seconds <= 0:
            raise ValueError("company_timeout_seconds must be positive")
        if match_max_concurrency < 1:
            raise ValueError("match_max_concurrency must be positive")
        if checkpoint_batch_size < 1:
            raise ValueError("checkpoint_batch_size must be positive")
        try:
            detail_reuse_ttl_hours = float(detail_reuse_ttl_hours)
        except (TypeError, ValueError) as exc:
            raise ValueError("detail_reuse_ttl_hours must be positive") from exc
        if not math.isfinite(detail_reuse_ttl_hours) or detail_reuse_ttl_hours <= 0:
            raise ValueError("detail_reuse_ttl_hours must be positive")
        self.companies_path = Path(companies_path)
        self.storage = storage
        self.crawler = crawler
        self.crawler_map = crawler_map
        self.profile = profile
        self.matcher = matcher if matcher is not None else DeterministicMatcher(profile)
        self._deterministic_only = matcher is None or isinstance(matcher, DeterministicMatcher)
        self.jd_hydrator = jd_hydrator
        self.max_concurrency = max_concurrency
        self.match_max_concurrency = match_max_concurrency
        self.checkpoint_batch_size = checkpoint_batch_size
        self.company_timeout_seconds = company_timeout_seconds
        self.detail_reuse_ttl_hours = detail_reuse_ttl_hours
        self.company_ids = tuple(dict.fromkeys(_compact(value) for value in company_ids if _compact(value)))
        self.checkpoint_path = (
            Path(checkpoint_path).expanduser()
            if checkpoint_path is not None
            else None
        )
        self.resume_from_checkpoint = bool(resume_from_checkpoint)
        self._checkpoint_company_ids: tuple[str, ...] = ()
        self._hydration_checkpoint_id = uuid4().hex
        self.progress_callback = progress_callback
        self.clock = clock

    def _load_company_checkpoint(
        self,
        selected: Sequence[PipelineCompany],
        *,
        dry_run: bool,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, _CompanyWork]]:
        """Load one run's immutable company set and completed crawl receipts."""

        if self.checkpoint_path is None:
            if self.resume_from_checkpoint:
                raise PipelineError("resume checkpoint reference is missing")
            return {}, {}
        if self.resume_from_checkpoint:
            if not self.checkpoint_path.is_file():
                raise PipelineError(
                    f"resume checkpoint does not exist: {self.checkpoint_path}"
                )
            try:
                payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise PipelineError(
                    f"resume checkpoint is unreadable: {self.checkpoint_path}"
                ) from exc
            if not isinstance(payload, Mapping) or payload.get("version") != _COMPANY_CHECKPOINT_VERSION:
                raise PipelineError("resume checkpoint has an unsupported format")
            expected_ids = tuple(item.id for item in selected)
            self._checkpoint_company_ids = expected_ids
            stored_ids = payload.get("company_ids")
            if not isinstance(stored_ids, list) or tuple(str(item) for item in stored_ids) != expected_ids:
                raise PipelineError("resume checkpoint company scope does not match frozen scope")
            entries = payload.get("companies")
            if not isinstance(entries, Mapping):
                raise PipelineError("resume checkpoint has no company entries")
            unknown_ids = set(str(key) for key in entries) - set(expected_ids)
            if unknown_ids:
                raise PipelineError(
                    "resume checkpoint contains companies outside frozen scope: "
                    + ", ".join(sorted(unknown_ids))
                )
            parsed: dict[str, _CompanyWork] = {}
            for company in selected:
                entry = entries.get(company.id)
                if not isinstance(entry, Mapping):
                    continue
                status = str(entry.get("status") or "")
                work_payload = entry.get("work")
                if status in {"complete", "partial", "failed"}:
                    if not isinstance(work_payload, Mapping):
                        raise PipelineError(
                            f"resume checkpoint has invalid work: {company.id}"
                        )
                    parsed[company.id] = _work_from_checkpoint(company, work_payload)
            hydration_id = payload.get("hydration_checkpoint_id")
            if hydration_id is not None:
                if not isinstance(hydration_id, str) or not re.fullmatch(r"[0-9a-f]{32}", hydration_id):
                    raise PipelineError("resume checkpoint has an invalid hydration reference")
                self._hydration_checkpoint_id = hydration_id
                for company_id, work in parsed.items():
                    sidecar = self._hydration_checkpoint_path(company_id)
                    if not sidecar.is_file():
                        sidecar = self._legacy_hydration_checkpoint_path(company_id)
                    if not sidecar.is_file():
                        continue
                    try:
                        details = json.loads(sidecar.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                        raise PipelineError(f"unreadable hydration checkpoint: {company_id}") from exc
                    if not isinstance(details, Mapping) or details.get("company_id") != company_id:
                        raise PipelineError(f"invalid hydration checkpoint: {company_id}")
                    restored = _work_from_checkpoint(work.company, details)
                    work.hydration_results = restored.hydration_results
                    work.jd_results = restored.jd_results
                    work.detail_success_count = restored.detail_success_count
                    work.detail_failure_count = restored.detail_failure_count
                    work.detail_failure_reasons = restored.detail_failure_reasons
            elif not dry_run:
                # Upgrade an old list-only checkpoint once, without changing its
                # version or receipts. Subsequent JD batches only write sidecars.
                payload["hydration_checkpoint_id"] = self._hydration_checkpoint_id
                temporary = self.checkpoint_path.with_name(self.checkpoint_path.name + ".tmp")
                temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
                temporary.replace(self.checkpoint_path)
            return {str(key): dict(value) for key, value in entries.items() if isinstance(value, Mapping)}, parsed

        if not dry_run:
            self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            self._checkpoint_company_ids = tuple(item.id for item in selected)
            payload = {
                "version": _COMPANY_CHECKPOINT_VERSION,
                "hydration_checkpoint_id": self._hydration_checkpoint_id,
                "company_ids": [item.id for item in selected],
                "companies": {},
            }
            temporary = self.checkpoint_path.with_name(self.checkpoint_path.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(self.checkpoint_path)
        return {}, {}

    def _write_company_checkpoint(
        self,
        entries: dict[str, dict[str, Any]],
        work: _CompanyWork,
        *,
        attempts: int,
        dry_run: bool,
    ) -> None:
        if self.checkpoint_path is None or dry_run:
            return
        status = (
            # This is list coverage only, not hydration or run completion.
            "complete"
            if not work.failure_reason and work.list_complete
            else "partial"
            if work.raw_job_count or work.accepted_jobs
            else "failed"
        )
        entries[work.company.id] = {
            "status": status,
            "attempts": max(1, int(attempts)),
            "work": _checkpoint_work_payload(work),
        }
        payload = {
            "version": _COMPANY_CHECKPOINT_VERSION,
            "hydration_checkpoint_id": self._hydration_checkpoint_id,
            "company_ids": list(self._checkpoint_company_ids),
            "companies": dict(entries),
        }
        self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.checkpoint_path.with_name(self.checkpoint_path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.checkpoint_path)

    def _hydration_checkpoint_path(self, company_id: str) -> Path:
        assert self.checkpoint_path is not None
        # Keep the complete path comfortably below Windows MAX_PATH. Desktop
        # instance roots are already long, so repeating the checkpoint name and
        # two full SHA-256 values can make an otherwise valid write fail with a
        # misleading FileNotFoundError.
        directory = self.checkpoint_path.parent / f".jd-{self._hydration_checkpoint_id[:12]}"
        digest = hashlib.sha256(company_id.encode("utf-8")).hexdigest()[:32]
        return directory / f"{digest}.json"

    def _legacy_hydration_checkpoint_path(self, company_id: str) -> Path:
        assert self.checkpoint_path is not None
        directory = self.checkpoint_path.with_name(
            f"{self.checkpoint_path.name}.hydration-{self._hydration_checkpoint_id}"
        )
        return directory / (hashlib.sha256(company_id.encode("utf-8")).hexdigest() + ".json")

    def _write_hydration_checkpoint(self, work: _CompanyWork, *, dry_run: bool) -> None:
        if self.checkpoint_path is None or dry_run:
            return
        path = self._hydration_checkpoint_path(work.company.id)
        payload = {
            "company_id": work.company.id,
            "hydration_results": _dump(work.hydration_results),
            "jd_results": _dump(work.jd_results),
            "detail_success_count": work.detail_success_count,
            "detail_failure_count": work.detail_failure_count,
            "detail_failure_reasons": dict(work.detail_failure_reasons),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _merge_partial_checkpoint_work(
        previous: _CompanyWork | None,
        current: _CompanyWork,
    ) -> _CompanyWork:
        """Keep a previously captured partial queue when the bounded retry fails."""

        if previous is None or not previous.accepted_jobs:
            return current
        if not current.failure_reason and current.list_complete:
            return current
        current.raw_job_count = max(current.raw_job_count, previous.raw_job_count)
        merged_jobs: list[dict[str, Any]] = []
        seen_job_keys: set[str] = set()
        for item in [*previous.accepted_jobs, *current.accepted_jobs]:
            key = _compact(item.get("id")) or _compact(item.get("business_key"))
            if key and key in seen_job_keys:
                continue
            if key:
                seen_job_keys.add(key)
            merged_jobs.append(dict(item))
        current.accepted_jobs = merged_jobs
        current.rejected_job_ids = list(dict.fromkeys([
            *previous.rejected_job_ids,
            *current.rejected_job_ids,
        ]))
        merged_reasons = Counter(previous.rejection_reasons)
        merged_reasons.update(current.rejection_reasons)
        current.rejection_reasons = merged_reasons
        current.crawl_evidence = {
            **previous.crawl_evidence,
            **current.crawl_evidence,
            "previous_partial_preserved": True,
        }
        current.observed_titles = list(
            dict.fromkeys([*previous.observed_titles, *current.observed_titles])
        )
        current.list_complete = False
        current.failure_reason = current.failure_reason or previous.failure_reason
        current.run_reason = current.run_reason or previous.run_reason
        return current

    def run(
        self,
        *,
        dry_run: bool = False,
        legacy: bool = False,
    ) -> DailyPipelineResult:
        if not legacy:
            return self._run_title_first(dry_run=dry_run)

        # Explicit compatibility path for callers migrating old tests. The
        # scheduler and run_daily_pipeline leave legacy=False by default.
        if not dry_run and self.storage is None:
            raise PipelineError("storage is required unless dry_run=True")

        companies = load_companies(self.companies_path)
        if self.company_ids:
            available = {item.id for item in companies}
            missing = sorted(set(self.company_ids) - available)
            if missing:
                raise PipelineError(
                    "requested company IDs are not configured: " + ", ".join(missing)
                )
        scope = set(self.company_ids)
        requested = [
            item for item in companies
            if not scope or item.id in scope
        ]
        selected = [
            item for item in requested
            if item.connected and bool(item.crawler_key)
        ]
        skipped = tuple(
            item.name
            for item in requested
            if item not in selected
        )

        source_registry = CompanySourceRegistry(self.storage) if not dry_run and self.storage else None
        source_records: dict[str, str] = {}
        if source_registry is not None:
            for company in requested:
                record = source_registry.upsert_source(
                    source=_compact(company.extra.get("discovery_source")) or "configured",
                    source_record_id=_compact(company.extra.get("source_record_id")) or company.id,
                    company_name=company.name,
                    source_url=_text(company.extra.get("source_url")),
                    entry_url=company.careers_url,
                )
                source_records[company.id] = record["id"]
                if company not in selected:
                    source_registry.record_attempt(
                        record["id"], status="pending", attempted_url=company.careers_url,
                        failure_stage="entry", reason_code="not_connected",
                        reason="Source retained; no verified crawler is configured.",
                    )
        works = self._crawl_companies(selected)
        for work in works:
            company_identity = work.company.crawler_config()
            for job in work.accepted_jobs:
                # Crawlers may already know the official source identity while
                # ``id`` is only the Agent row key. Preserve that identity
                # before the shared identity builder sees the normalized row.
                if not _compact(job.get("native_job_id")):
                    for field_name in ("source_job_id", "source_post_id", "post_id"):
                        source_identity = _compact(job.get(field_name))
                        if source_identity:
                            job["native_job_id"] = source_identity
                            break
                identity = build_job_identity(company_identity, job)
                job["native_job_id"] = identity.native_job_id
                job["normalized_detail_url"] = identity.normalized_detail_url
                job["business_key"] = identity.business_key
        existing = self._read_existing(
            job_id
            for work in works
            for job in work.accepted_jobs
            for job_id in [_compact(job.get("id"))]
        )
        existing_by_key = self._read_existing_by_business_keys(
            job.get("business_key")
            for work in works
            for job in work.accepted_jobs
        )
        seen_job_ids: set[str] = set()
        seen_business_keys: set[str] = set()
        for work in sorted(works, key=lambda item: item.company.id):
            unique_jobs: list[dict[str, Any]] = []
            for job in sorted(work.accepted_jobs, key=lambda item: _compact(item.get("id"))):
                original_id = _compact(job.get("id"))
                known = existing.get(original_id) or existing_by_key.get(
                    _compact(job.get("business_key"))
                )
                if known is not None:
                    job["id"] = known.job.id
                canonical_id = _compact(job.get("id"))
                business_key = _compact(job.get("business_key"))
                if canonical_id in seen_job_ids or (
                    business_key and business_key in seen_business_keys
                ):
                    continue
                seen_job_ids.add(canonical_id)
                if business_key:
                    seen_business_keys.add(business_key)
                unique_jobs.append(job)
            work.accepted_jobs[:] = unique_jobs
        existing.update(self._read_existing(seen_job_ids))
        self._prepare_job_details(works, existing)
        if source_registry is not None:
            for work in works:
                evidence = work.crawl_evidence
                pending_details = sum(
                    bool(_matching_is_jd_incomplete(job)) for job in work.accepted_jobs
                ) if _matching_is_jd_incomplete else len(work.accepted_jobs)
                pagination = evidence.get("pagination_complete")
                reason = work.failure_reason or ""
                if reason:
                    status = "partial" if work.raw_job_count else "failed"
                elif any(work.rejection_reasons.get(code) for code in (
                    "invalid_job_id", "missing_title", "invalid_detail_url", "detail_origin_not_allowed",
                )):
                    status, reason = "partial", "job_validation_failed"
                elif pagination is not True or pending_details:
                    status = "partial"
                    reason = "jd_capture_pending" if pending_details else "pagination_unverified"
                else:
                    status = "complete"
                source_registry.record_attempt(
                    source_records[work.company.id], status=status,
                    attempted_url=work.company.careers_url,
                    final_url=evidence.get("crawl_source_url") or evidence.get("source_url") or "",
                    failure_stage=("detail" if reason == "jd_capture_pending" else "crawl") if reason else "",
                    reason_code=reason, reason=reason or work.run_reason,
                    job_count=work.raw_job_count, jd_pending_count=pending_details,
                    pagination_complete=pagination,
                )

        staged: list[_StagedWork] = []
        company_results: list[CompanyRunResult] = []
        new_ids: list[str] = []
        changed_ids: list[str] = []
        reused_ids: list[str] = []
        rejected_ids: list[str] = []
        failed_ids: list[str] = []
        rejection_reasons: Counter[str] = Counter()
        failure_reasons: Counter[str] = Counter()
        failed_company_count = 0
        failed_job_count = 0
        filtered_count = 0

        company_metrics: dict[str, Counter[str]] = {
            work.company.id: Counter() for work in works
        }
        company_filtered_reasons: dict[str, Counter[str]] = {
            work.company.id: Counter() for work in works
        }
        for work in works:
            rejection_reasons.update(work.rejection_reasons)
            rejected_ids.extend(work.rejected_job_ids)
            if work.failure_reason:
                failed_company_count += 1
                failure_reasons[work.failure_reason] += 1

        job_inputs = [
            (work, job, existing.get(_compact(job.get("id"))))
            for work in sorted(works, key=lambda item: item.company.id)
            for job in sorted(work.accepted_jobs, key=lambda item: _compact(item.get("id")))
        ]
        total_jobs = len(job_inputs)
        completed_jobs = 0
        checkpoint: list[_StagedWork] = []
        written = False
        companies_pending = True
        matching_abort = Event()
        matching_abort_code: str | None = None

        def record_outcome(outcome: _JobOutcome) -> None:
            nonlocal completed_jobs, failed_job_count, filtered_count, written, companies_pending
            nonlocal matching_abort_code
            metrics = company_metrics[outcome.work.company.id]
            filtered_reasons_for_company = company_filtered_reasons[
                outcome.work.company.id
            ]
            completed_jobs += 1
            if outcome.deferred:
                pass
            elif outcome.failed:
                metrics["failed"] += 1
                failed_job_count += 1
                failed_ids.append(outcome.job_id)
                reason = outcome.error_code or "matcher_failed"
                failure_reasons[reason] += 1
                if reason in _FATAL_MATCH_ERROR_CODES:
                    matching_abort_code = reason
                    matching_abort.set()
            elif outcome.category == "reused":
                metrics["reused"] += 1
                reused_ids.append(outcome.job_id)
            elif outcome.rejected_reason:
                metrics["filtered"] += 1
                filtered_count += 1
                rejected_ids.append(outcome.job_id)
                rejection_reasons[outcome.rejected_reason] += 1
                filtered_reasons_for_company.update(outcome.filtered_reasons)
            else:
                if outcome.analysis_status in _FILTERED_STATUSES:
                    metrics["filtered"] += 1
                    filtered_count += 1
                    filtered_reasons_for_company.update(outcome.filtered_reasons)
                if outcome.category == "new":
                    metrics["new"] += 1
                    new_ids.append(outcome.job_id)
                elif outcome.category == "changed":
                    metrics["changed"] += 1
                    changed_ids.append(outcome.job_id)
            if outcome.staged is not None:
                checkpoint.append(outcome.staged)
            if len(checkpoint) >= self.checkpoint_batch_size:
                written = self._persist(
                    companies=selected if companies_pending else (),
                    staged=tuple(checkpoint),
                    dry_run=dry_run,
                ) or written
                companies_pending = False
                staged.extend(checkpoint)
                checkpoint.clear()
            progress_due = (
                completed_jobs % self.checkpoint_batch_size == 0
                or completed_jobs == total_jobs
            )
            if progress_due and self.progress_callback is not None:
                self.progress_callback("matching", completed_jobs, total_jobs)
            elif progress_due:
                LOGGER.info("matching progress %s/%s", completed_jobs, total_jobs)

        if job_inputs:
            with ThreadPoolExecutor(
                max_workers=min(self.match_max_concurrency, total_jobs)
            ) as executor:
                futures = {
                    executor.submit(
                        self._process_job,
                        work,
                        job,
                        previous,
                        matching_abort,
                    ): (work, job)
                    for work, job, previous in job_inputs
                }
                for future in as_completed(futures):
                    work, job = futures[future]
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        job_id = _compact(job.get("id"))
                        LOGGER.warning(
                            "[%s] matcher worker failed for %s: %s",
                            work.company.name,
                            job_id,
                            exc,
                        )
                        outcome = _JobOutcome(
                            work=work,
                            job_id=job_id,
                            category="failed",
                            failed=True,
                        )
                    record_outcome(outcome)

        if checkpoint or companies_pending:
            written = self._persist(
                companies=selected if companies_pending else (),
                staged=tuple(checkpoint),
                dry_run=dry_run,
            ) or written
            companies_pending = False
            staged.extend(checkpoint)

        if matching_abort_code is not None:
            raise PipelineError(
                f"matching aborted after provider authorization failure: {matching_abort_code}"
            )

        for work in sorted(works, key=lambda item: item.company.id):
            metrics = company_metrics[work.company.id]
            company_results.append(
                CompanyRunResult(
                    company_id=work.company.id,
                    company_name=work.company.name,
                    status="failed" if work.failure_reason else "completed",
                    raw_job_count=work.raw_job_count,
                    accepted_job_count=len(work.accepted_jobs),
                    new_count=metrics["new"],
                    changed_count=metrics["changed"],
                    reused_count=metrics["reused"],
                    rejected_count=sum(work.rejection_reasons.values()),
                    failed_count=metrics["failed"],
                    filtered_count=metrics["filtered"],
                    rejection_reasons=dict(work.rejection_reasons),
                    failure_reason=work.failure_reason,
                    run_reason=work.run_reason,
                    observed_job_ids=tuple(
                        sorted(
                            job_id
                            for job in work.accepted_jobs
                            if (job_id := _compact(job.get("id")))
                        )
                    ),
                    filtered_reasons=dict(company_filtered_reasons[work.company.id]),
                    crawl_evidence=work.crawl_evidence,
                    jd_results=tuple(sorted(work.jd_results, key=lambda item: item["job_id"])),
                )
            )

        return DailyPipelineResult(
            dry_run=dry_run,
            total_companies=len(requested),
            selected_companies=len(selected),
            crawled_companies=sum(not bool(work.failure_reason) for work in works),
            skipped_companies=skipped,
            new_count=len(new_ids),
            changed_count=len(changed_ids),
            reused_count=len(reused_ids),
            rejected_count=sum(rejection_reasons.values()),
            failed_company_count=failed_company_count,
            failed_job_count=failed_job_count,
            filtered_count=filtered_count,
            written=written,
            scoped_company_ids=self.company_ids,
            company_results=tuple(company_results),
            new_job_ids=tuple(sorted(new_ids)),
            changed_job_ids=tuple(sorted(changed_ids)),
            reused_job_ids=tuple(sorted(reused_ids)),
            rejected_job_ids=tuple(sorted(rejected_ids)),
            failed_job_ids=tuple(sorted(failed_ids)),
            rejection_reasons=dict(rejection_reasons),
            failure_reasons=dict(failure_reasons),
        )

    def _run_title_first(self, *, dry_run: bool) -> DailyPipelineResult:
        """Run the shared title-first capture policy for one daily pass."""

        if not dry_run and self.storage is None:
            raise PipelineError("storage is required unless dry_run=True")

        companies = load_companies(self.companies_path)
        if self.company_ids:
            available = {item.id for item in companies}
            missing = sorted(set(self.company_ids) - available)
            if missing:
                raise PipelineError(
                    "requested company IDs are not configured: " + ", ".join(missing)
                )
        scope = set(self.company_ids)
        requested = [item for item in companies if not scope or item.id in scope]
        selected = [item for item in requested if item.connected and bool(item.crawler_key)]
        skipped = tuple(item.name for item in requested if item not in selected)

        source_registry = CompanySourceRegistry(self.storage) if not dry_run and self.storage else None
        source_records: dict[str, str] = {}
        unusable_source_records: set[str] = set()
        for company in requested:
            if source_registry is None:
                continue
            record = self._register_title_first_source(source_registry, company)
            source_records[company.id] = str(record["id"])
            if record.get("status") == "unusable":
                unusable_source_records.add(str(record["id"]))
            if company not in selected and record.get("status") != "unusable":
                self._record_title_first_source_attempt(
                    source_registry,
                    record["id"],
                    status="pending",
                    company=company,
                    reason_code="not_connected",
                    reason="Source retained; no verified crawler is configured.",
                )

        checkpoint_entries, checkpoint_works = self._load_company_checkpoint(
            selected,
            dry_run=dry_run,
        )
        works: list[_CompanyWork] = []
        resumed_company_ids: list[str] = []
        retry_companies: list[PipelineCompany] = []
        retried_company_ids: list[str] = []
        if self.resume_from_checkpoint:
            for company in selected:
                entry = checkpoint_entries.get(company.id)
                if isinstance(entry, Mapping) and entry.get("status") == "complete":
                    work = checkpoint_works.get(company.id)
                    if work is None:
                        raise PipelineError(
                            f"resume checkpoint is missing completed company: {company.id}"
                        )
                    works.append(work)
                    resumed_company_ids.append(company.id)
                    continue
                retry_companies.append(company)
                retried_company_ids.append(company.id)
            if self.progress_callback is not None:
                self.progress_callback("companies", len(works), len(selected))
        else:
            retry_companies = list(selected)

        def checkpoint_company(work: _CompanyWork) -> None:
            previous = checkpoint_works.get(work.company.id)
            if previous is not None:
                work.hydration_results = dict(previous.hydration_results)
            merged = self._merge_partial_checkpoint_work(previous, work)
            checkpoint_works[work.company.id] = merged
            previous_entry = checkpoint_entries.get(work.company.id)
            previous_attempts = (
                int(previous_entry.get("attempts") or 0)
                if isinstance(previous_entry, Mapping)
                else 0
            )
            self._write_company_checkpoint(
                checkpoint_entries,
                merged,
                attempts=previous_attempts + 1,
                dry_run=dry_run,
            )

        def checkpoint_details(work: _CompanyWork) -> None:
            self._write_hydration_checkpoint(work, dry_run=dry_run)

        works.extend(
            self._crawl_companies(
                retry_companies,
                progress_offset=len(works) if self.resume_from_checkpoint else 0,
                progress_total=len(selected) if self.resume_from_checkpoint else None,
                checkpoint_callback=checkpoint_company
                if self.checkpoint_path is not None
                else None,
            )
        )
        works = sorted(works, key=lambda item: item.company.id)
        existing_by_company = self._read_existing_for_companies(item.id for item in selected)
        existing_by_title: dict[tuple[str, str], list[_ExistingSnapshot]] = {}
        existing_by_key: dict[tuple[str, str], _ExistingSnapshot] = {}
        for company_id, rows in existing_by_company.items():
            for row in rows:
                title_key = _title_key(company_id, row.job.title)
                if title_key:
                    existing_by_title.setdefault((company_id, title_key), []).append(row)
        for title_key, rows in existing_by_title.items():
            existing_by_key[title_key] = next(
                (
                    row
                    for row in rows
                    if not stored_detail_retry_required(row.job)
                ),
                rows[0],
            )

        metrics: dict[str, Counter[str]] = {
            work.company.id: Counter() for work in works
        }
        filtered_reasons: dict[str, Counter[str]] = {
            work.company.id: Counter() for work in works
        }
        rejection_reasons: Counter[str] = Counter()
        failure_reasons: Counter[str] = Counter()
        existing_updates: list[_ExistingSnapshot] = []
        existing_update_ids: set[str] = set()
        candidates: list[_TitleFirstCandidate] = []
        resume_candidates: list[_TitleFirstCandidate] = []
        rejected_ids: list[str] = []
        reused_ids: list[str] = []
        new_ids: list[str] = []
        failed_ids: list[str] = []
        inactive_ids: list[str] = []
        resolved_detail_titles: dict[str, set[str]] = {
            work.company.id: set() for work in works
        }

        for work in works:
            company_id = work.company.id
            seen_title_keys: set[str] = set()
            for job in sorted(
                work.accepted_jobs,
                key=lambda item: (_title_key(company_id, item.get("title")), _compact(item.get("id"))),
            ):
                title_key = _title_key(company_id, job.get("title"))
                if not title_key or title_key in seen_title_keys:
                    continue
                seen_title_keys.add(title_key)
                existing = existing_by_key.get((company_id, title_key))
                if existing is not None:
                    metrics[company_id]["reused"] += 1
                    reused_ids.append(existing.job.id)
                    if existing.job.id not in existing_update_ids:
                        existing_updates.append(existing)
                        existing_update_ids.add(existing.job.id)
                    if stored_detail_retry_required(existing.job):
                        screening = _screen_title(job, self.profile)
                        if bool(getattr(screening, "eligible", False)):
                            retry_job = dict(job)
                            retry_job["id"] = existing.job.id
                            retry_job["company_id"] = company_id
                            retry_job["company"] = work.company.name
                            retry_job["capture_status"] = "pending"
                            retry_job["capture_failure_reason"] = ""
                            retry_job["availability_status"] = "active"
                            candidates.append(
                                _TitleFirstCandidate(
                                    work=work,
                                    job=retry_job,
                                    title_key=title_key,
                                    existing=existing,
                                    screening=screening,
                                    detail_retry=True,
                                )
                            )
                    else:
                        resolved_detail_titles[company_id].add(title_key)
                        if (
                            not self._deterministic_only
                            and (
                                _is_title_first_pending_analysis(existing.analysis)
                                or (
                                    bool(self.company_ids)
                                    and not _existing_score_is_valid(existing)
                                )
                            )
                            and _stored_capture_status(existing.job) == "complete"
                        ):
                            screening = _screen_title(job, self.profile)
                            if bool(getattr(screening, "eligible", False)):
                                resume_candidates.append(
                                    _TitleFirstCandidate(
                                        work=work,
                                        job=_snapshot_job_payload(existing.job),
                                        title_key=title_key,
                                        existing=existing,
                                        screening=screening,
                                    )
                                )
                    continue

                screening = _screen_title(job, self.profile)
                if not bool(getattr(screening, "eligible", False)):
                    reasons = _screening_reasons(screening) or ("title_not_matched",)
                    metrics[company_id]["filtered"] += 1
                    filtered_reasons[company_id].update(reasons)
                    rejection_reasons.update(reasons)
                    rejected_ids.append(_compact(job.get("id")))
                    continue

                job = dict(job)
                job["title_key"] = title_key
                job["capture_status"] = "pending"
                job["capture_failure_reason"] = ""
                job["availability_status"] = "active"
                candidate = _TitleFirstCandidate(
                    work=work,
                    job=job,
                    title_key=title_key,
                    screening=screening,
                )
                candidates.append(candidate)
                new_ids.append(_compact(job.get("id")))
                metrics[company_id]["new"] += 1

            if work.list_complete and not work.failure_reason:
                observed_keys = {
                    _title_key(company_id, title) for title in work.observed_titles
                }
                for existing in existing_by_company.get(company_id, ()):
                    title_key = _title_key(company_id, existing.job.title)
                    if (
                        title_key
                        and title_key not in observed_keys
                        and _existing_scope_matches(existing.job, work.company, work)
                    ):
                        inactive_ids.append(existing.job.id)

        self._hydrate_title_first_candidates(
            candidates,
            checkpoint_callback=checkpoint_details if self.checkpoint_path is not None else None,
        )
        # Details can reveal internship evidence absent from the listing title.
        # Recheck before both persistence and scoring; never store new rejected rows.
        retained_candidates = []
        for candidate in candidates:
            screening = screen_title_job(candidate.job, self.profile)
            if screening.eligible:
                candidate.screening = screening
                retained_candidates.append(candidate)
                continue
            company_id = candidate.work.company.id
            reason = (screening.reasons or [screening.analysis_status.value])[0]
            filtered_reasons[company_id][reason] += 1
            rejection_reasons[reason] += 1
            job_id = _compact(candidate.job.get("id"))
            rejected_ids.append(job_id)
            if job_id in new_ids:
                new_ids.remove(job_id)
                metrics[company_id]["new"] -= 1
        candidates = retained_candidates
        for work in works:
            work.detail_success_count = 0
            work.detail_failure_count = 0
            work.detail_failure_reasons.clear()
        for candidate in candidates:
            if _capture_status(candidate.job.get("capture_status")) == "failed":
                failed_ids.append(_compact(candidate.job.get("id")))
                candidate.work.detail_failure_count += 1
                reason = _compact(candidate.job.get("capture_failure_reason")) or "detail_capture_failed"
                candidate.work.detail_failure_reasons[reason] += 1
                failure_reasons[reason] += 1
            else:
                candidate.work.detail_success_count += 1
                if candidate.detail_retry:
                    resolved_detail_titles[candidate.work.company.id].add(
                        candidate.title_key
                    )

        unresolved_detail_titles: dict[str, set[str]] = {}
        for work in works:
            company_id = work.company.id
            resolved_titles = resolved_detail_titles[company_id]
            unresolved_detail_titles[company_id] = {
                title_key
                for (group_company_id, title_key), rows in existing_by_title.items()
                if group_company_id == company_id
                and title_key not in resolved_titles
                and any(stored_detail_retry_required(row.job) for row in rows)
            }
            new_failed_titles = {
                candidate.title_key
                for candidate in candidates
                if candidate.work.company.id == company_id
                and candidate.existing is None
                and _capture_status(candidate.job.get("capture_status")) == "failed"
            }
            unresolved_detail_titles[company_id].update(new_failed_titles)

        # Persist a durable marker for successful new captures before calling
        # the matcher.  A restart can resume only these explicit pending rows;
        # historical rows with no marker remain outside the scoring queue.
        score_candidates = [
            candidate
            for candidate in [*resume_candidates, *candidates]
            if (
                _capture_status(candidate.job.get("capture_status")) == "complete"
                and (
                    candidate.existing is None
                    or not _existing_score_is_valid(candidate.existing)
                )
            )
        ]
        scoring_candidate_count = len(score_candidates)
        scored_count = 0

        if not self._deterministic_only:
            for candidate in candidates:
                if (
                    _capture_status(candidate.job.get("capture_status")) == "complete"
                    and (
                        candidate.existing is None
                        or not _existing_score_is_valid(candidate.existing)
                    )
                ):
                    candidate.analysis = self._pending_title_first_analysis(candidate)

        written = self._persist_title_first(
            companies=requested,
            existing_updates=existing_updates,
            new_candidates=candidates,
            scored_candidates=(),
            inactive_ids=inactive_ids,
            dry_run=dry_run,
        )

        # Source bookkeeping must not gate durable jobs or pending score markers.
        # Let errors propagate after persistence, never report a successful run.
        for work in works:
            company_id = work.company.id
            detail_pending = len(unresolved_detail_titles[company_id])
            if source_registry is not None:
                if source_records[company_id] in unusable_source_records:
                    continue
                if work.failure_reason:
                    status = "partial" if work.raw_job_count else "failed"
                    reason = work.failure_reason
                elif not work.list_complete:
                    status = "partial" if work.raw_job_count else "failed"
                    reason = work.run_reason or "crawl_incomplete"
                elif detail_pending or work.detail_failure_count:
                    status = "partial"
                    reason = "detail_capture_failed"
                else:
                    status = "complete"
                    reason = ""
                self._record_title_first_source_attempt(
                    source_registry,
                    source_records[company_id],
                    status=status,
                    company=work.company,
                    reason_code=reason,
                    reason=reason or work.run_reason,
                    job_count=metrics[company_id]["new"] + metrics[company_id]["reused"],
                    jd_pending_count=detail_pending,
                    pagination_complete=work.list_complete,
                    final_url=_text(
                        work.crawl_evidence.get("crawl_source_url")
                        or work.crawl_evidence.get("source_url")
                    ),
                )

        for candidate in candidates:
            candidate.analysis = None

        if not self._deterministic_only:
            scored_checkpoint: list[_TitleFirstCandidate] = []
            matching_abort = Event()
            matching_abort_code: str | None = None
            completed = 0
            if score_candidates:
                with ThreadPoolExecutor(
                    max_workers=min(self.match_max_concurrency, len(score_candidates))
                ) as executor:
                    futures = {
                        executor.submit(
                            self._score_title_first_candidate,
                            candidate,
                            matching_abort,
                        ): candidate
                        for candidate in score_candidates
                    }
                    for future in as_completed(futures):
                        candidate = futures[future]
                        try:
                            analysis = future.result()
                        except Exception as exc:
                            LOGGER.warning(
                                "[%s] title-first matcher failed for %s: %s",
                                candidate.work.company.name,
                                candidate.job.get("id"),
                                exc,
                            )
                            metrics[candidate.work.company.id]["failed"] += 1
                            failed_ids.append(_compact(candidate.job.get("id")))
                            failure_reasons["matcher_failed"] += 1
                        else:
                            if analysis is not None:
                                candidate.analysis = analysis
                                scored_checkpoint.append(candidate)
                                status = _analysis_status(analysis.analysis_status)
                                if status == "failed":
                                    metrics[candidate.work.company.id]["failed"] += 1
                                    failed_ids.append(_compact(candidate.job.get("id")))
                                    reason = _compact(analysis.error_code) or "matcher_failed"
                                    failure_reasons[reason] += 1
                                    if reason in _FATAL_MATCH_ERROR_CODES:
                                        matching_abort_code = matching_abort_code or reason
                                        matching_abort.set()
                                else:
                                    scored_count += 1
                        completed += 1
                        if len(scored_checkpoint) >= self.checkpoint_batch_size:
                            written = self._persist_title_first(
                                companies=(),
                                existing_updates=(),
                                new_candidates=(),
                                scored_candidates=tuple(scored_checkpoint),
                                inactive_ids=(),
                                dry_run=dry_run,
                            ) or written
                            scored_checkpoint.clear()
                        progress_due = (
                            completed % self.checkpoint_batch_size == 0
                            or completed == len(score_candidates)
                        )
                        if progress_due and self.progress_callback is not None:
                            self.progress_callback(
                                "matching", completed, len(score_candidates)
                            )
            if scored_checkpoint:
                written = self._persist_title_first(
                    companies=(),
                    existing_updates=(),
                    new_candidates=(),
                    scored_candidates=tuple(scored_checkpoint),
                    inactive_ids=(),
                    dry_run=dry_run,
                ) or written
            if matching_abort_code is not None:
                raise PipelineError(
                    "matching aborted after provider authorization failure: "
                    f"{matching_abort_code}"
                )

        for work in works:
            rejection_reasons.update(work.rejection_reasons)
            for job_id in work.rejected_job_ids:
                if job_id not in rejected_ids:
                    rejected_ids.append(job_id)
            if work.failure_reason:
                failure_reasons[work.failure_reason] += 1

        company_results: list[CompanyRunResult] = []
        failed_company_count = 0
        failed_job_count = len(failed_ids)
        filtered_count = sum(item["filtered"] for item in metrics.values())
        for work in works:
            company_id = work.company.id
            history_has_failure = bool(unresolved_detail_titles[company_id])
            if work.failure_reason:
                failed_company_count += 1
                status = "failed" if not work.raw_job_count else "partial"
                company_reason = work.failure_reason
            elif work.detail_failure_count or history_has_failure:
                status = "partial"
                company_reason = "detail_capture_failed"
            elif not work.list_complete:
                status = "partial"
                company_reason = work.run_reason or "crawl_incomplete"
            else:
                status = "complete"
                company_reason = None
            company_results.append(
                CompanyRunResult(
                    company_id=company_id,
                    company_name=work.company.name,
                    status=status,
                    raw_job_count=work.raw_job_count,
                    accepted_job_count=len(work.accepted_jobs),
                    new_count=metrics[company_id]["new"],
                    changed_count=0,
                    reused_count=metrics[company_id]["reused"],
                    rejected_count=sum(work.rejection_reasons.values())
                    + metrics[company_id]["filtered"],
                    failed_count=metrics[company_id]["failed"] + work.detail_failure_count,
                    filtered_count=metrics[company_id]["filtered"],
                    rejection_reasons=dict(work.rejection_reasons),
                    failure_reason=company_reason,
                    run_reason=work.run_reason,
                    observed_job_ids=tuple(
                        sorted(
                            _compact(job.get("id"))
                            for job in work.accepted_jobs
                            if _compact(job.get("id"))
                        )
                    ),
                    filtered_reasons=dict(filtered_reasons[company_id]),
                    crawl_evidence=work.crawl_evidence,
                    jd_results=tuple(
                        sorted(work.jd_results, key=lambda item: _compact(item.get("job_id")))
                    ),
                    observed_titles=tuple(work.observed_titles),
                    list_complete=work.list_complete,
                    detail_success_count=work.detail_success_count,
                    detail_failure_count=work.detail_failure_count,
                )
            )

        return DailyPipelineResult(
            dry_run=dry_run,
            total_companies=len(requested),
            selected_companies=len(selected),
            crawled_companies=sum(not bool(work.failure_reason) for work in works),
            skipped_companies=skipped,
            new_count=len(new_ids),
            changed_count=0,
            reused_count=len(set(reused_ids)),
            rejected_count=len(rejected_ids),
            failed_company_count=failed_company_count,
            failed_job_count=failed_job_count,
            filtered_count=filtered_count,
            written=written,
            analysis_enabled=not self._deterministic_only,
            scoring_candidate_count=scoring_candidate_count,
            scored_count=scored_count,
            scoring_failed_count=max(0, scoring_candidate_count - scored_count)
            if not self._deterministic_only
            else 0,
            unscored_count=max(0, scoring_candidate_count - scored_count),
            scoped_company_ids=self.company_ids,
            resumed_company_ids=tuple(resumed_company_ids),
            retried_company_ids=tuple(retried_company_ids),
            company_results=tuple(company_results),
            new_job_ids=tuple(sorted(set(new_ids))),
            changed_job_ids=(),
            reused_job_ids=tuple(sorted(set(reused_ids))),
            rejected_job_ids=tuple(sorted(set(rejected_ids))),
            failed_job_ids=tuple(sorted(set(failed_ids))),
            rejection_reasons=dict(rejection_reasons),
            failure_reasons=dict(failure_reasons),
        )

    def _register_title_first_source(
        self,
        registry: CompanySourceRegistry,
        company: PipelineCompany,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "source": _compact(company.extra.get("discovery_source")) or "configured",
            "source_record_id": _compact(company.extra.get("source_record_id")) or company.id,
            "company_name": company.name,
            "source_url": _text(company.extra.get("source_url")),
            "entry_url": company.careers_url,
        }
        try:
            parameters = inspect.signature(registry.upsert_source).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "company_id" in parameters:
            kwargs["company_id"] = company.id
        return registry.upsert_source(**kwargs)

    def _record_title_first_source_attempt(
        self,
        registry: CompanySourceRegistry,
        record_id: str,
        *,
        status: str,
        company: PipelineCompany,
        reason_code: str = "",
        reason: str = "",
        job_count: int = 0,
        jd_pending_count: int = 0,
        pagination_complete: bool | None = None,
        final_url: str = "",
    ) -> None:
        registry.record_attempt(
            record_id,
            status=status,
            attempted_url=company.careers_url,
            final_url=final_url,
            failure_stage="crawl" if status in {"failed", "partial"} else "",
            reason_code=reason_code,
            reason=reason,
            job_count=job_count,
            jd_pending_count=jd_pending_count,
            pagination_complete=pagination_complete,
        )

    def _read_existing_for_companies(
        self,
        company_ids: Sequence[str] | Any,
    ) -> dict[str, list[_ExistingSnapshot]]:
        ids = sorted({_compact(item) for item in company_ids if _compact(item)})
        if not ids or self.storage is None:
            return {}
        with self.storage.session() as session:
            jobs = list(
                session.scalars(select(JobSnapshot).where(JobSnapshot.company_id.in_(ids)))
            )
            analyses = list(
                session.scalars(
                    select(JobAnalysisSnapshot).where(
                        JobAnalysisSnapshot.job_id.in_([row.id for row in jobs])
                    )
                )
            )
        analyses_by_id = {row.job_id: row for row in analyses}
        result: dict[str, list[_ExistingSnapshot]] = {company_id: [] for company_id in ids}
        for row in jobs:
            result.setdefault(row.company_id, []).append(
                _ExistingSnapshot(job=row, analysis=analyses_by_id.get(row.id))
            )
        for rows in result.values():
            rows.sort(
                key=lambda item: (
                    _stored_availability_status(item.job) == "active",
                    getattr(item.job, "last_seen_at", None) or datetime.min.replace(tzinfo=timezone.utc),
                    item.job.id,
                ),
                reverse=True,
            )
        return result

    def _hydrate_title_first(
        self,
        candidate: _TitleFirstCandidate,
        checkpoint_result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        job = candidate.job
        job["detail_capture_policy"] = _TITLE_FIRST_CAPTURE_POLICY
        job_id = _compact(job.get("id"))
        diagnostic: dict[str, Any] = {"job_id": job_id}
        if checkpoint_result is not None:
            ok, detail, reason, capture, response = _detail_capture_result(job, checkpoint_result)
        elif self.jd_hydrator is None:
            ok, detail, reason, capture, response = (
                False,
                "",
                "hydrator_unavailable",
                {},
                {"status": "failed", "detail_url": job.get("detail_url")},
            )
        else:
            try:
                raw_result = self.jd_hydrator(job)
                ok, detail, reason, capture, response = _detail_capture_result(job, raw_result)
            except IsolatedOperationTimeout as exc:
                ok, detail, reason, capture, response = (
                    False,
                    "",
                    "timeout",
                    {},
                    {"status": "timeout", "error_type": type(exc).__name__},
                )
            except Exception as exc:
                LOGGER.warning(
                    "[%s] title-first JD hydration failed for %s: %s",
                    candidate.work.company.name,
                    job_id,
                    exc,
                )
                ok, detail, reason, capture, response = (
                    False,
                    "",
                    type(exc).__name__.casefold(),
                    {},
                    {"status": "failed", "error_type": type(exc).__name__},
                )

        diagnostic.update(response)
        diagnostic["detail_url"] = _text(response.get("detail_url")) or job.get("detail_url")
        diagnostic["detail_chars"] = len(detail)
        diagnostic["detail_sha256"] = hashlib.sha256(detail.encode("utf-8")).hexdigest() if detail else None
        if ok:
            job["jd_raw"] = detail
            job["capture_evidence"] = capture
            job["capture_status"] = "complete"
            job["capture_failure_reason"] = ""
            response_url = _text(response.get("detail_url"))
            if response_url and _origin(response_url):
                job["detail_url"] = response_url
                job["jd_url"] = response_url
            diagnostic["status"] = "complete"
        else:
            job["jd_raw"] = None
            job["capture_evidence"] = capture
            job["capture_status"] = "failed"
            job["capture_failure_reason"] = reason or "detail_capture_failed"
            diagnostic["status"] = "failed"
            diagnostic["failure_reason"] = job["capture_failure_reason"]
        return diagnostic

    def _hydrate_title_first_candidates(
        self,
        candidates: Sequence[_TitleFirstCandidate],
        *,
        checkpoint_callback: Callable[[_CompanyWork], None] | None = None,
    ) -> None:
        """Hydrate new candidates in the same bounded pool as legacy details."""

        if not candidates:
            return
        dirty: dict[str, _CompanyWork] = {}
        inputs: dict[int, tuple[str, Mapping[str, Any] | None]] = {}
        for candidate in candidates:
            # Bind receipts to the exact listing input, not just a shared title.
            candidate.job["detail_capture_policy"] = _TITLE_FIRST_CAPTURE_POLICY
            key = hashlib.sha256(
                json.dumps(_dump(candidate.job), sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            receipt = candidate.work.hydration_results.get(key)
            if receipt is not None and (
                receipt.get("status") != "complete"
                or receipt.get("job_id") != _compact(candidate.job.get("id"))
                or receipt.get("detail_sha256") != hashlib.sha256(
                    _raw_detail(receipt.get("detail")).encode("utf-8")
                ).hexdigest()
                or not _detail_capture_result(candidate.job, receipt)[0]
            ):
                receipt = None
            inputs[id(candidate)] = (key, receipt)
        for work in {item.work.company.id: item.work for item in candidates}.values():
            active_keys = {
                inputs[id(item)][0] for item in candidates if item.work is work
            }
            work.hydration_results = {
                key: value for key, value in work.hydration_results.items() if key in active_keys
            }
            work.jd_results = []
            work.detail_success_count = 0
            work.detail_failure_count = 0
            work.detail_failure_reasons.clear()
        total = len(candidates)
        if self.progress_callback is not None:
            self.progress_callback("jd", 0, total)
        completed = 0
        with ThreadPoolExecutor(max_workers=min(self.max_concurrency, total)) as executor:
            futures = {
                executor.submit(self._hydrate_title_first, candidate, inputs[id(candidate)][1]): candidate
                for candidate in candidates
            }
            for future in as_completed(futures):
                candidate = futures[future]
                try:
                    diagnostic = future.result()
                except Exception as exc:
                    # Keep the row visible even if the injected worker itself
                    # fails outside the hydrator's normal result contract.
                    reason = type(exc).__name__.casefold()
                    candidate.job.update(
                        {
                            "jd_raw": None,
                            "capture_status": "failed",
                            "capture_failure_reason": reason,
                        }
                    )
                    diagnostic = {
                        "job_id": _compact(candidate.job.get("id")),
                        "status": "failed",
                        "detail_url": candidate.job.get("detail_url"),
                        "failure_reason": reason,
                    }
                # Only the coordinator mutates checkpoint work; other workers
                # may still be updating their private candidate copies.
                work = candidate.work
                work.jd_results.append(diagnostic)
                work.hydration_results[inputs[id(candidate)][0]] = {
                    **diagnostic,
                    "detail": candidate.job.get("jd_raw"),
                    "capture_evidence": candidate.job.get("capture_evidence") or {},
                }
                if diagnostic.get("status") == "complete":
                    work.detail_success_count += 1
                else:
                    work.detail_failure_count += 1
                    work.detail_failure_reasons[diagnostic.get("failure_reason") or "detail_capture_failed"] += 1
                dirty[work.company.id] = work
                completed += 1
                if completed % self.checkpoint_batch_size == 0 or completed == total:
                    if checkpoint_callback is not None:
                        for work in dirty.values():
                            checkpoint_callback(work)
                    dirty.clear()
                if (
                    self.progress_callback is not None
                    and (
                        completed % self.checkpoint_batch_size == 0
                        or completed == total
                    )
                ):
                    self.progress_callback("jd", completed, total)

    def _pending_title_first_analysis(
        self,
        candidate: _TitleFirstCandidate,
    ) -> JobAnalysis:
        profile_fingerprint = (
            _matching_profile_fingerprint(self.profile)
            if _matching_profile_fingerprint is not None
            else None
        )
        return JobAnalysis(
            analysis_status="pending",
            analysis_version=_TITLE_FIRST_PENDING_ANALYSIS_VERSION,
            prompt_version="pending",
            content_fingerprint=job_content_fingerprint(candidate.job),
            profile_fingerprint=profile_fingerprint,
        )

    def _score_title_first_candidate(
        self,
        candidate: _TitleFirstCandidate,
        abort_event: Event,
    ) -> JobAnalysis | None:
        """Score one candidate unless a fatal provider error already stopped the batch."""

        if abort_event.is_set():
            return None
        raw_result = self._call_title_first_matcher(candidate)
        return _analysis_model(raw_result, candidate.job, self.clock)

    def _call_title_first_matcher(self, candidate: _TitleFirstCandidate) -> Any:
        service = getattr(self.matcher, "service", None)
        target = getattr(service, "analyze_title_first", None)
        if callable(target):
            return target(
                candidate.job,
                self.profile,
                existing_analysis=None,
                screening=candidate.screening,
            )
        target = getattr(self.matcher, "analyze_title_first", None)
        if callable(target):
            return target(
                candidate.job,
                self.profile,
                existing_analysis=None,
                screening=candidate.screening,
            )
        return _call_matcher(self.matcher, candidate.job, self.profile, None)

    @staticmethod
    def _title_first_job_model(
        candidate: _TitleFirstCandidate,
        *,
        now: datetime,
    ) -> Job:
        job = candidate.job
        previous = candidate.existing.job if candidate.existing is not None else None
        analysis = candidate.analysis
        match_score = analysis.match_score if analysis is not None else None
        if match_score is None and previous is not None:
            match_score = previous.match_score
        scope = candidate.work.scope_key
        fingerprint = job_content_fingerprint(job)
        retry_succeeded = (
            previous is not None
            and candidate.detail_retry
            and _capture_status(job.get("capture_status")) == "complete"
        )
        if previous is None:
            detail_url = _text(job.get("detail_url"))
            jd_raw = _raw_detail(job.get("jd_raw")) or None
            capture_evidence = dict(job.get("capture_evidence") or {})
        elif retry_succeeded:
            detail_url = _text(job.get("detail_url")) or previous.detail_url
            jd_raw = _raw_detail(job.get("jd_raw")) or previous.jd_raw
            capture_evidence = dict(job.get("capture_evidence") or {})
        else:
            detail_url = previous.detail_url
            jd_raw = previous.jd_raw
            capture_evidence = dict(getattr(previous, "capture_evidence", {}) or {})
        return Job(
            id=previous.id if previous is not None else _compact(job.get("id")),
            company_id=candidate.work.company.id,
            title=_text(previous.title if previous is not None else job.get("title")),
            city=(previous.city if previous is not None else _text(job.get("city")) or None),
            detail_url=detail_url,
            jd_raw=jd_raw,
            cohort=(2027 if job.get("cohort_policy") == "offerbiu_force_2027"
                    else previous.cohort if previous is not None else job.get("cohort")),
            cohort_status=(
                "confirmed" if job.get("cohort_policy") == "offerbiu_force_2027"
                else previous.cohort_status
                if previous is not None
                else _compact(job.get("cohort_status")) or "unconfirmed"
            ),
            batch=previous.batch if previous is not None else _batch_value(job),
            match_score=match_score,
            first_seen_at=previous.first_seen_at if previous is not None else now,
            last_seen_at=now,
            organization_id=(
                previous.organization_id
                if previous is not None
                else _compact(candidate.work.company.extra.get("organization_id"))
                or candidate.work.company.id
            ),
            recruitment_unit_id=(
                previous.recruitment_unit_id
                if previous is not None
                else _compact(candidate.work.company.extra.get("recruitment_unit_id"))
                or candidate.work.company.id
            ),
            recruitment_campaign_id=(
                previous.recruitment_campaign_id
                if previous is not None
                else _compact(
                    candidate.work.company.extra.get("recruitment_campaign_id")
                    or job.get("recruitment_campaign_id")
                )
                or None
            ),
            source_platform=(
                previous.source_platform
                if previous is not None
                else candidate.work.company.crawler_key or None
            ),
            source_tenant=(
                previous.source_tenant
                if previous is not None
                else _compact(candidate.work.company.extra.get("source_identity")) or None
            ),
            native_job_id=(
                previous.native_job_id
                if previous is not None
                else _compact(
                    job.get("native_job_id")
                    or job.get("source_job_id")
                    or job.get("native_id")
                    or job.get("post_id")
                )
                or None
            ),
            normalized_detail_url=(
                previous.normalized_detail_url
                if previous is not None
                else _compact(job.get("normalized_detail_url")) or None
            ),
            business_key=(
                previous.business_key
                if previous is not None
                else _compact(job.get("business_key")) or None
            ),
            created_at=previous.created_at if previous is not None else now,
            updated_at=(
                previous.updated_at
                if previous is not None and not candidate.detail_retry
                else now
            ),
            source=PIPELINE_SOURCE,
            source_ref=(
                previous.source_ref
                if previous is not None
                else _title_first_source_ref(
                    candidate.work.company.id,
                    _compact(job.get("id")),
                    fingerprint,
                    scope,
                )
            ),
            capture_evidence=capture_evidence,
        )

    @staticmethod
    def _upsert_title_first_job(
        session: Any,
        model: Job,
        *,
        capture_status: str,
        capture_failure_reason: str,
        availability_status: str,
        title_key: str,
    ) -> None:
        upsert_job_snapshot(
            session,
            model,
            capture_status=capture_status,
            capture_failure_reason=capture_failure_reason,
            availability_status=availability_status,
            title_key=title_key,
        )

    def _persist_title_first(
        self,
        *,
        companies: Sequence[PipelineCompany],
        existing_updates: Sequence[_ExistingSnapshot],
        new_candidates: Sequence[_TitleFirstCandidate],
        scored_candidates: Sequence[_TitleFirstCandidate],
        inactive_ids: Sequence[str],
        dry_run: bool,
    ) -> bool:
        if dry_run or self.storage is None:
            return False
        now = _timestamp(self.clock)
        company_models = [
            Company(
                id=company.id,
                name=company.name,
                aliases=list(company.aliases),
                campus_url=company.careers_url or None,
                crawler_key=company.crawler_key or None,
                integration_status=company.integration_status,
                organization_id=_compact(company.extra.get("organization_id")) or company.id,
                recruitment_unit_name=_compact(company.extra.get("recruitment_unit_name"))
                or company.name,
                source_identity=_compact(company.extra.get("source_identity")) or None,
                created_at=now,
                updated_at=now,
                source=PIPELINE_SOURCE,
                source_ref=f"companies.yaml:{company.id}",
            )
            for company in companies
        ]
        candidates = list(new_candidates) + list(scored_candidates)
        # A scored candidate is also a new candidate only when the second call
        # is intentionally updating its score; keep one row per write scope.
        if scored_candidates:
            candidates = list(scored_candidates)
        with self.storage.write_transaction() as session:
            for company in company_models:
                upsert_company_snapshot(session, company)
            for existing in existing_updates:
                row = session.get(JobSnapshot, existing.job.id)
                if row is None:
                    continue
                row.last_seen_at = now
                if hasattr(row, "availability_status"):
                    row.availability_status = "active"
            for job_id in dict.fromkeys(_compact(item) for item in inactive_ids if _compact(item)):
                row = session.get(JobSnapshot, job_id)
                if row is not None and hasattr(row, "availability_status"):
                    row.availability_status = "inactive"
            # Flush ORM metadata before SQL upserts so onupdate cannot overwrite
            # the repaired job's timestamp during the final transaction flush.
            session.flush()
            for candidate in candidates:
                model = self._title_first_job_model(candidate, now=now)
                _upsert_status = _capture_status(candidate.job.get("capture_status"))
                self._upsert_title_first_job(
                    session,
                    model,
                    capture_status=_upsert_status,
                    capture_failure_reason=_compact(
                        candidate.job.get("capture_failure_reason")
                    ),
                    availability_status=_stored_availability_status(candidate.job),
                    title_key=candidate.title_key,
                )
                if candidate.analysis is not None:
                    upsert_job_analysis_snapshot(session, model, candidate.analysis)
        return True

    def _process_job(
        self,
        work: _CompanyWork,
        job: dict[str, Any],
        previous: _ExistingSnapshot | None,
        abort_event: Event | None = None,
    ) -> _JobOutcome:
        job_id = _compact(job.get("id"))
        if abort_event is not None and abort_event.is_set():
            return _JobOutcome(work=work, job_id=job_id, deferred=True)
        if job.get("_detail_refresh_failed"):
            return _JobOutcome(
                work=work,
                job_id=job_id,
                category="failed",
                failed=True,
                error_code="detail_refresh_failed",
            )
        fingerprint = job_content_fingerprint(job)
        old_fingerprint = self._existing_fingerprint(previous)
        old_status = _analysis_status(
            previous.analysis.analysis_status if previous and previous.analysis else None,
            default="",
        )
        matcher_owns_reuse = isinstance(self.matcher, MatchingServiceAdapter)
        deterministic_reusable = (
            not isinstance(self.matcher, DeterministicMatcher)
            or self.matcher.can_reuse(previous.analysis if previous else None)
        )
        if (
            previous is not None
            and previous.analysis is not None
            and old_fingerprint == fingerprint
            and old_status not in {"failed", "jd_incomplete"}
            and not matcher_owns_reuse
            and deterministic_reusable
        ):
            return _JobOutcome(
                work=work,
                job_id=job_id,
                category="reused",
                staged=_StagedWork(
                    company=work.company,
                    job=job,
                    fingerprint=fingerprint,
                    category="reused",
                    analysis=None,
                    existing=previous,
                ),
            )

        category = "new" if previous is None else "changed"
        try:
            screening = _screen_job(job, self.profile) if _screen_job is not None else None
            screening_status = _analysis_status(
                screening.analysis_status if screening is not None else None,
                default="",
            )
            if (
                screening is not None
                and not screening.eligible
                and screening_status != "direction_out"
            ):
                result = DeterministicMatcher(self.profile).match(job)
            else:
                result = _call_matcher(
                    self.matcher,
                    job,
                    self.profile,
                    _existing_analysis_mapping(previous.analysis) if previous else None,
                )
            decision_action = _decision_action(result)
            analysis = _analysis_model(result, job, self.clock)
        except Exception as exc:
            LOGGER.warning(
                "[%s] matcher failed for %s: %s",
                work.company.name,
                job_id,
                exc,
            )
            return _JobOutcome(
                work=work,
                job_id=job_id,
                category="failed",
                failed=True,
            )

        if decision_action == "reuse":
            return _JobOutcome(
                work=work,
                job_id=job_id,
                category="reused",
                staged=_StagedWork(
                    company=work.company,
                    job=job,
                    fingerprint=fingerprint,
                    category="reused",
                    analysis=None,
                    existing=previous,
                ),
            )

        status = _analysis_status(analysis.analysis_status)
        filtered_reasons = _safe_strings(analysis.filter_reasons)
        if status in _FILTERED_STATUSES and status not in filtered_reasons:
            filtered_reasons.insert(0, status)
        staged = _StagedWork(
            company=work.company,
            job=job,
            fingerprint=fingerprint,
            category=category,
            analysis=analysis,
            existing=previous,
        )
        if status == "failed":
            return _JobOutcome(
                work=work,
                job_id=job_id,
                category=category,
                analysis_status=status,
                failed=True,
                staged=staged,
                error_code=_compact(analysis.error_code) or None,
            )
        if status in {"direction_out", "doctorate_only", "internship"}:
            return _JobOutcome(
                work=work,
                job_id=job_id,
                category=category,
                analysis_status=status,
                rejected_reason=status,
                filtered_reasons=tuple(filtered_reasons),
            )
        return _JobOutcome(
            work=work,
            job_id=job_id,
            category=category,
            analysis_status=status,
            staged=staged,
            filtered_reasons=tuple(filtered_reasons),
        )

    def _stored_detail_decision(
        self,
        job: Mapping[str, Any],
        previous: _ExistingSnapshot | None,
        now: datetime,
    ) -> _StoredDetailDecision:
        """Decide whether a persisted complete JD can satisfy this sparse row."""

        if previous is None or not _raw_detail(previous.job.jd_raw):
            return _StoredDetailDecision(False, "stored_detail_missing")

        stored_job = previous.job
        capture = getattr(stored_job, "capture_evidence", {})
        if not isinstance(capture, Mapping) or not capture:
            return _StoredDetailDecision(False, "capture_unverified")

        current_title = normalize_job_title(job.get("title"))
        stored_title = normalize_job_title(stored_job.title)
        if not current_title or not stored_title or current_title != stored_title:
            return _StoredDetailDecision(False, "title_changed")

        current_native_id = _native_job_identity(job)
        stored_native_id = _native_job_identity(stored_job)
        if not current_native_id or not stored_native_id:
            return _StoredDetailDecision(False, "identity_missing")
        if current_native_id.casefold() != stored_native_id.casefold():
            return _StoredDetailDecision(False, "native_id_changed")

        current_url = _canonical_detail_url(job.get("detail_url") or job.get("jd_url"))
        stored_url = _canonical_detail_url(stored_job.detail_url)
        if not current_url or not stored_url:
            return _StoredDetailDecision(False, "detail_url_invalid")
        if current_url != stored_url:
            return _StoredDetailDecision(False, "detail_url_changed")

        for field_name in ("source_platform", "source_tenant"):
            current_value = _compact(job.get(field_name))
            stored_value = _compact(getattr(stored_job, field_name, None))
            if current_value and stored_value and current_value.casefold() != stored_value.casefold():
                return _StoredDetailDecision(False, "source_identity_mismatch")

        capture_url = _canonical_detail_url(capture.get("source_url"))
        if not capture_url:
            return _StoredDetailDecision(False, "capture_source_missing")
        if capture_url != current_url:
            return _StoredDetailDecision(False, "capture_source_url_mismatch")
        if not _capture_identity_matches_job(job, capture):
            return _StoredDetailDecision(False, "capture_identity_mismatch")

        candidate = dict(job)
        candidate["detail_url"] = current_url
        candidate["jd_raw"] = stored_job.jd_raw
        candidate["capture_evidence"] = dict(capture)
        assessment = assess_jd_capture(candidate)
        if not assessment.complete:
            return _StoredDetailDecision(False, f"capture_{assessment.reason_code}")

        freshness = _capture_freshness_decision(
            capture,
            now=now,
            ttl_hours=self.detail_reuse_ttl_hours,
        )
        if not freshness.reusable:
            return freshness
        return _StoredDetailDecision(
            True,
            "stored_fresh_verified",
            captured_at=freshness.captured_at,
            age_seconds=freshness.age_seconds,
        )

    def _prepare_job_details(
        self,
        works: Sequence[_CompanyWork],
        existing: Mapping[str, _ExistingSnapshot],
    ) -> None:
        """Hydrate eligible sparse jobs before deciding uncertain directions."""

        if _matching_is_jd_incomplete is None or _screen_job is None:
            return
        now = _timestamp(self.clock)
        pending: list[dict[str, Any]] = []
        refresh_reasons: dict[str, str] = {}
        for work in works:
            for job in work.accepted_jobs:
                source_platform = _compact(_field(work.company, "crawler_key"))
                if not source_platform:
                    crawler_config = getattr(work.company, "crawler_config", None)
                    if callable(crawler_config):
                        source_config = crawler_config()
                        source_platform = _compact(
                            _field(source_config, "crawler_key")
                            or _field(source_config, "crawler")
                        )
                job.setdefault("source_platform", source_platform)
                configured_source_identity = _compact(
                    _field(_field(work.company, "extra", {}), "source_identity")
                )
                if configured_source_identity:
                    job.setdefault("source_tenant", configured_source_identity)
                previous = existing.get(_compact(job.get("id")))
                stored = previous.job if previous is not None else None
                current_capture = job.get("capture_evidence")
                current_capture_claim = (
                    isinstance(current_capture, Mapping)
                    and current_capture.get("status") == "complete"
                    and bool(_raw_detail(job.get("jd_raw")))
                )

                # A bound capture observed in this crawl can win over the
                # database only after its own receipt passes the freshness TTL.
                if _capture_matches_job(job):
                    current_freshness = _capture_freshness_decision(
                        current_capture,
                        now=now,
                        ttl_hours=self.detail_reuse_ttl_hours,
                    )
                    if not current_freshness.reusable:
                        screening = _screen_job(job, self.profile)
                        status = _analysis_status(screening.analysis_status, default="")
                        if bool(getattr(screening, "eligible", False)) or status == "jd_incomplete":
                            job_id = _compact(job.get("id"))
                            refresh_reasons[job_id] = current_freshness.reason
                            pending.append(job)
                        continue
                    stored_capture = getattr(stored, "capture_evidence", {}) if stored else {}
                    current_capture_time = current_freshness.captured_at
                    stored_capture_time = _capture_timestamp(stored_capture)
                    if (
                        stored is None
                        or _raw_detail(stored.jd_raw) != _raw_detail(job.get("jd_raw"))
                        or not isinstance(stored_capture, Mapping)
                        or not stored_capture
                        or stored_capture_time is None
                        or (
                            current_capture_time is not None
                            and current_capture_time
                            > stored_capture_time
                        )
                    ):
                        # This is a reliable receipt observed in this run, so
                        # its persistence timestamp may advance.  Identical
                        # list-side receipts keep the prior timestamp.
                        job["_detail_capture_observed"] = True
                    if (
                        stored is not None
                        and _raw_detail(stored.jd_raw) != _raw_detail(job.get("jd_raw"))
                    ):
                        work.jd_results.append(
                            {
                                "job_id": _compact(job.get("id")),
                                "status": "current_capture_used",
                                "detail_url": job.get("detail_url"),
                                "detail_chars": len(_raw_detail(job.get("jd_raw"))),
                                "detail_sha256": hashlib.sha256(
                                    _raw_detail(job.get("jd_raw")).encode("utf-8")
                                ).hexdigest(),
                                "detail_reuse": {
                                    "reused": False,
                                    "request_made": False,
                                    "mode": "storage",
                                    "reason": "new_official_content",
                                },
                            }
                        )
                    continue
                if current_capture_claim:
                    screening = _screen_job(job, self.profile)
                    status = _analysis_status(screening.analysis_status, default="")
                    if bool(getattr(screening, "eligible", False)) or status == "jd_incomplete":
                        job_id = _compact(job.get("id"))
                        refresh_reasons[job_id] = "capture_identity_or_source_invalid"
                        pending.append(job)
                    continue
                if not _matching_is_jd_incomplete(job):
                    continue

                screening = _screen_job(job, self.profile)
                status = _analysis_status(screening.analysis_status, default="")
                if status != "jd_incomplete":
                    continue

                decision = self._stored_detail_decision(job, previous, now)
                if decision.reusable and stored is not None:
                    job["jd_raw"] = stored.jd_raw
                    job["capture_evidence"] = dict(getattr(stored, "capture_evidence", {}))
                    # Reusing a stored detail must not make its audit timestamp
                    # look freshly captured when the row is written again.
                    job["_detail_storage_reused"] = True
                    work.jd_results.append(
                        {
                            "job_id": _compact(job.get("id")),
                            "status": "stored_reused",
                            "detail_url": job.get("detail_url"),
                            "detail_chars": len(_raw_detail(stored.jd_raw)),
                            "detail_sha256": hashlib.sha256(
                                _raw_detail(stored.jd_raw).encode("utf-8")
                            ).hexdigest(),
                            "detail_reuse": {
                                "reused": True,
                                "request_made": False,
                                "mode": "storage",
                                "reason": decision.reason,
                                "age_seconds": decision.age_seconds,
                                "ttl_hours": self.detail_reuse_ttl_hours,
                            },
                        }
                    )
                    continue

                refresh_reasons[_compact(job.get("id"))] = decision.reason
                pending.append(job)

        if not pending or self.jd_hydrator is None:
            for job in pending:
                previous = existing.get(_compact(job.get("id")))
                refresh_reason = refresh_reasons.get(_compact(job.get("id")), "")
                if (
                    (previous is not None and _raw_detail(previous.job.jd_raw))
                    or refresh_reason in _CAPTURE_REFRESH_REASONS
                ):
                    job["_detail_refresh_failed"] = True
                owner = next(
                    (
                        work
                        for work in works
                        if work.company.id == _compact(job.get("company_id"))
                    ),
                    None,
                )
                if owner is not None:
                    owner.jd_results.append(
                        {
                            "job_id": _compact(job.get("id")),
                            "status": "refresh_required",
                            "detail_url": job.get("detail_url"),
                            "detail_reuse": {
                                "reused": False,
                                "request_made": False,
                                "mode": "storage",
                                "reason": refresh_reasons.get(
                                    _compact(job.get("id")), "hydrator_unavailable"
                                ),
                            },
                        }
                    )
            return
        if self.progress_callback is not None:
            self.progress_callback("jd", 0, len(pending))
        works_by_company_id = {item.company.id: item for item in works}
        previous_by_job_id = {
            _compact(job.get("id")): existing.get(_compact(job.get("id")))
            for job in pending
        }
        completed = 0
        from packages.recruitment_core.detail_reuse import ReusingDetailHydrator

        def fetch_detail(job: dict[str, Any], *, timeout_seconds: float) -> Any:
            # Injected hydrators retain their existing timeout and return contracts.
            del timeout_seconds
            return self.jd_hydrator(job)

        def hydration_input(job: dict[str, Any]) -> dict[str, Any]:
            refresh_reason = refresh_reasons.get(_compact(job.get("id")), "")
            request = dict(job)
            if refresh_reason in _CAPTURE_REFRESH_REASONS:
                # A complete stale/missing receipt makes job_details return the
                # stored value.  Isolate the request input so the original
                # receipt remains available for failure protection/provenance.
                request["capture_evidence"] = {}
            return request

        reusable_hydrator = ReusingDetailHydrator(fetch_detail)
        with reusable_hydrator, ThreadPoolExecutor(
            max_workers=min(self.max_concurrency, len(pending))
        ) as executor:
            futures = {
                executor.submit(
                    reusable_hydrator,
                    hydration_input(job),
                    timeout_seconds=120.0,
                ): job
                for job in pending
            }
            for future in as_completed(futures):
                job = futures[future]
                owner = works_by_company_id.get(_compact(job.get("company_id")))
                job_id = _compact(job.get("id"))
                refresh_reason = refresh_reasons.get(job_id, "refresh_required")
                previous = previous_by_job_id.get(job_id)
                diagnostic: dict[str, Any] = {
                    "job_id": job_id,
                    "detail_reuse": {
                        "reused": False,
                        "request_made": True,
                        "mode": "storage_refresh",
                        "reason": refresh_reason,
                    },
                }
                try:
                    result = future.result()
                    if isinstance(result, Mapping) or hasattr(result, "detail"):
                        detail = _raw_detail(_field(result, "detail"))
                        for key in (
                            "status",
                            "source",
                            "detail_url",
                            "attempts",
                            "error_type",
                            "identity_status",
                            "identity_evidence",
                            "identity_diagnostic",
                            "error_detail",
                            "capture_evidence",
                        ):
                            diagnostic[key] = _dump(_field(result, key))
                        reuse = _field(result, "_detail_reuse")
                        if isinstance(reuse, Mapping):
                            diagnostic["detail_reuse"].update(dict(reuse))
                            diagnostic["detail_reuse"]["refresh_reason"] = refresh_reason
                    else:
                        detail = _raw_detail(result)
                except IsolatedOperationTimeout as exc:
                    diagnostic.update(status="timeout", error_type=type(exc).__name__)
                    LOGGER.warning(
                        "[%s] JD hydration timed out for %s: %s",
                        _text(job.get("company")),
                        _text(job.get("id")),
                        exc,
                    )
                    detail = ""
                except Exception as exc:  # one detail page must not stop the run
                    diagnostic.update(status="failed", error_type=type(exc).__name__)
                    LOGGER.warning(
                        "[%s] JD hydration failed for %s: %s",
                        _text(job.get("company")),
                        _text(job.get("id")),
                        exc,
                    )
                    detail = ""
                hydrated = {**job, "jd_raw": detail,
                            "capture_evidence": diagnostic.get("capture_evidence") or {}}
                for identity_key in ("identity_status", "identity_evidence"):
                    if identity_key in diagnostic:
                        hydrated[identity_key] = diagnostic[identity_key]
                response_detail_url = _text(diagnostic.get("detail_url")) or _text(
                    job.get("detail_url")
                )
                hydrated["detail_url"] = response_detail_url
                hydration_status = re.sub(
                    r"[^a-z0-9_]+", "_", _compact(diagnostic.get("status")).casefold()
                ).strip("_")
                valid_detail = bool(detail) and not _matching_is_jd_incomplete(hydrated)
                if hydration_status not in {"", "complete", "content_incomplete"}:
                    valid_detail = False
                if valid_detail and not _capture_matches_job(hydrated):
                    valid_detail = False
                    diagnostic["capture_failure_reason"] = "capture_identity_or_source_invalid"
                if valid_detail:
                    hydration_freshness = _capture_freshness_decision(
                        hydrated.get("capture_evidence"),
                        now=_timestamp(self.clock),
                        ttl_hours=self.detail_reuse_ttl_hours,
                    )
                    if not hydration_freshness.reusable:
                        valid_detail = False
                        diagnostic["capture_failure_reason"] = hydration_freshness.reason
                if valid_detail:
                    job["jd_raw"] = detail
                    job["capture_evidence"] = hydrated["capture_evidence"]
                    resolved_url = _text(diagnostic.get("detail_url"))
                    if resolved_url and _origin(resolved_url):
                        job["detail_url"] = job["jd_url"] = resolved_url
                        job["link_kind"] = "detail"
                    job["_detail_capture_observed"] = True
                    job.pop("_detail_refresh_failed", None)
                    hydration_status = "complete"
                else:
                    if hydration_status in {"", "complete"}:
                        hydration_status = "content_incomplete"
                    if (
                        (previous is not None and _raw_detail(previous.job.jd_raw))
                        or refresh_reason in _CAPTURE_REFRESH_REASONS
                    ):
                        # Leave the old snapshot untouched.  In particular, do
                        # not stage an empty/invalid replacement after a
                        # failed refresh of a previously captured JD.
                        job["_detail_refresh_failed"] = True
                        diagnostic["detail_reuse"]["preserved_old_value"] = True
                    if owner is not None:
                        owner.rejection_reasons[f"jd_hydration_{hydration_status}"] += 1
                diagnostic["status"] = hydration_status
                diagnostic["detail_url"] = diagnostic.get("detail_url") or job.get("detail_url")
                diagnostic["detail_chars"] = len(detail)
                diagnostic["detail_sha256"] = hashlib.sha256(detail.encode("utf-8")).hexdigest() if detail else None
                if owner is not None:
                    owner.jd_results.append(diagnostic)
                    if valid_detail:
                        identity = build_job_identity(owner.company.crawler_config(), job)
                        job["normalized_detail_url"] = identity.normalized_detail_url
                        job["business_key"] = identity.business_key
                completed += 1
                if (
                    self.progress_callback is not None
                    and (
                        completed % self.checkpoint_batch_size == 0
                        or completed == len(pending)
                    )
                ):
                    self.progress_callback("jd", completed, len(pending))

    def _crawl_companies(
        self,
        companies: Sequence[PipelineCompany],
        *,
        progress_offset: int = 0,
        progress_total: int | None = None,
        checkpoint_callback: Callable[[_CompanyWork], None] | None = None,
    ) -> list[_CompanyWork]:
        if not companies:
            return []
        works: list[_CompanyWork] = []
        completed = 0
        total = progress_total if progress_total is not None else len(companies)
        if self.progress_callback is not None:
            self.progress_callback("companies", progress_offset, total)
        with ThreadPoolExecutor(max_workers=min(self.max_concurrency, len(companies))) as executor:
            futures = {executor.submit(self._crawl_one, company): company for company in companies}
            for future in as_completed(futures):
                company = futures[future]
                try:
                    works.append(future.result())
                except Exception as exc:  # defensive isolation around injected crawlers
                    LOGGER.warning("[%s] crawler failed: %s", company.name, exc)
                    works.append(
                        _CompanyWork(
                            company=company,
                            failure_reason="crawler_failed",
                        )
                    )
                if checkpoint_callback is not None:
                    checkpoint_callback(works[-1])
                completed += 1
                if (
                    self.progress_callback is not None
                    and (
                        completed % self.checkpoint_batch_size == 0
                        or completed == len(companies)
                    )
                ):
                    self.progress_callback("companies", progress_offset + completed, total)
        return works

    def _crawl_one(self, company: PipelineCompany) -> _CompanyWork:
        work = _CompanyWork(company=company)
        work.failure_reason = _entry_failure_reason(company, empty_result=False)
        if work.failure_reason:
            return work
        try:
            raw_result = self._invoke_crawler(company)
            result = _normalize_crawl_result(raw_result, company)
            work.raw_job_count = len(result.jobs)
            work.run_reason = result.run_reason
            known = result.completeness_known
            if known is None:
                known = result.pagination_complete is not None or result.total_pages is not None
            work.crawl_evidence = {
                "source_url": result.source_url,
                "discovered_entry_url": result.discovered_entry_url,
                "crawl_source_url": result.crawl_source_url,
                "effective_source_urls": list(result.effective_source_urls),
                "pagination_complete": result.pagination_complete,
                "completeness_known": known,
                "pages_seen": result.pages_seen,
                "total_pages": result.total_pages,
                "has_more": result.has_more,
                "advertised_total": result.advertised_total,
                "termination_reasons": list(result.termination_reasons),
                "source_runs": list(result.source_runs),
                "entry_attempts": list(result.entry_attempts),
                "observed_job_ids": [_stable_job_id(company, _as_mapping(job)) for job in result.jobs],
                "observed_titles": [
                    _text(_field(job, "title"))
                    for job in result.jobs
                    if _text(_field(job, "title"))
                ],
            }
            work.observed_titles = list(work.crawl_evidence["observed_titles"])
            work.crawl_evidence["scope_key"] = result.scope_key or ""
            work.scope_key = _scope_key(company, work.crawl_evidence)
            work.crawl_evidence["scope_key"] = work.scope_key
            expected_identities, observed_identities = _source_identity_evidence(
                company, result
            )
            if expected_identities:
                work.crawl_evidence["expected_source_identities"] = list(
                    expected_identities
                )
                work.crawl_evidence["observed_source_identities"] = list(
                    observed_identities
                )
                if observed_identities and not set(observed_identities).issubset(
                    expected_identities
                ):
                    work.failure_reason = "identity_mismatch"
                    return work
            error_code = _compact(result.error_code).casefold()
            if error_code and error_code not in _PAGINATION_ERROR_CODES:
                # Authentication, challenge, adapter, source and identity errors
                # never become a broad row-admission escape hatch.
                work.failure_reason = result.error_code
                return work
            coverage_failure = _pagination_failure_reason(
                result,
                completeness_known=known,
            )
            if not result.jobs:
                work.failure_reason = coverage_failure
                work.list_complete = coverage_failure is None
                work.run_reason = "activity_empty" if coverage_failure is None else work.run_reason
                return work
            observed_jobs: list[ObservedCrawlerJob] = []
            normalized_jobs: dict[str, dict[str, Any]] = {}
            for raw_job in result.jobs:
                try:
                    observed, normalized = _observed_job(company, raw_job)
                except (TypeError, ValueError, ValidationError):
                    work.rejection_reasons["invalid_job"] += 1
                    continue
                observed_jobs.append(observed)
                normalized_jobs.setdefault(observed.id, normalized)

            request = CrawlerAcceptanceInput(
                company=company.name,
                source_url=result.source_url,
                allowed_origins=list(result.allowed_origins) or _configured_origins(company),
                allowed_detail_urls=observed_ats_detail_urls(
                    [_as_mapping(job) for job in result.jobs], result.source_url,
                    {"effective_source_urls": result.effective_source_urls, "source_runs": result.source_runs},
                ),
                jobs=observed_jobs,
                pages_seen=result.pages_seen,
                total_pages=result.total_pages,
                has_more=result.has_more,
                pagination_complete=result.pagination_complete,
                completeness_known=known,
                # Pagination coverage is established against the raw crawler
                # rows above. Do not let one malformed row turn a complete
                # page into a false pagination failure during row audit.
                advertised_total=(
                    result.advertised_total
                    if len(observed_jobs) == len(result.jobs)
                    else None
                ),
                expected_cohort=2027,
                require_confirmed_cohort=False,
                require_complete_jd=False,
            )
            response = accept_crawler_run(request)
            if response.data is not None:
                work.crawl_evidence["pagination_state"] = response.data.pagination_state
            if response.data is None:
                work.failure_reason = _text(response.error_code or "crawler_audit_failed")
                return work

            work.rejection_reasons.update(response.data.rejection_reasons)
            accepted_ids = {item.id for item in response.data.accepted_jobs}
            work.rejected_job_ids.extend(
                item.id for item in observed_jobs if item.id not in accepted_ids
            )
            if not response.data.pagination_complete:
                # Coverage remains failed, but the audit has already admitted
                # these rows independently of the company-level pagination state.
                coverage_failure = (
                    "pagination_unknown"
                    if response.data.pagination_state == "unknown"
                    else "pagination_incomplete"
                )
            work.failure_reason = coverage_failure
            work.list_complete = coverage_failure is None and response.data.pagination_complete

            for accepted in response.data.accepted_jobs:
                normalized = normalized_jobs.get(accepted.id)
                if normalized is None:
                    continue
                normalized.update(
                    {
                        "id": accepted.id,
                        "title": accepted.title,
                        "city": accepted.city,
                        "detail_url": accepted.detail_url,
                        "jd_url": accepted.detail_url,
                        "jd_raw": accepted.jd_raw,
                        "cohort": accepted.cohort,
                        "cohort_status": accepted.cohort_status,
                        "cohort_source": accepted.cohort_source,
                        "cohort_evidence": accepted.cohort_evidence,
                        "batch": getattr(accepted.batch, "value", accepted.batch),
                        "company": company.name,
                        "company_id": company.id,
                        "careers_url": result.discovered_entry_url or result.source_url,
                        "source_url": result.source_url,
                        "campaign_url": company.campaign_url,
                        "campaign_urls": list(company.campaign_urls),
                    }
                )
                work.accepted_jobs.append(normalized)
            if not work.accepted_jobs and not observed_jobs:
                work.failure_reason = "no_results"
            return work
        except IsolatedOperationTimeout as exc:
            LOGGER.warning("[%s] crawler timed out: %s", company.name, exc)
            work.failure_reason = "crawler_timeout"
            return work
        except IsolatedWorkerError as exc:
            LOGGER.warning("[%s] crawler worker failed: %s", company.name, exc)
            work.failure_reason = "crawler_worker_failed"
            if exc.error_type:
                work.run_reason = f"worker_error:{exc.error_type}"
            return work
        except Exception as exc:
            LOGGER.warning("[%s] crawler/audit failed: %s", company.name, exc)
            work.failure_reason = "crawler_failed"
            return work

    def _invoke_crawler(self, company: PipelineCompany) -> Any:
        if self.crawler is None:
            if self.crawler_map is None:
                return crawl_company_result_isolated(
                    company.crawler_config(),
                    timeout_seconds=float(
                        company.extra.get(
                            "crawl_timeout_seconds", self.company_timeout_seconds
                        )
                    ),
                )
            else:
                # Explicit maps are a test/custom injection boundary and may not
                # be importable inside a fresh worker process.
                from packages.recruitment_core.entry_crawl import crawl_company_with_entry_discovery

                return crawl_company_with_entry_discovery(
                    company.to_core_config(),
                    crawler_map=self.crawler_map,
                )
        crawler = self.crawler
        if isinstance(crawler, Mapping):
            target = crawler.get(company.crawler_key)
            if target is None:
                raise KeyError(f"no injected crawler for key: {company.crawler_key}")
        else:
            target = crawler
        if not callable(target):
            raise TypeError("injected crawler must be callable")
        try:
            parameters = inspect.signature(target).parameters
        except (TypeError, ValueError):
            parameters = {}
        if len(parameters) >= 2:
            return target(company.name, company.careers_url)
        return target(company)

    def _read_existing(self, job_ids: Sequence[str] | Any) -> dict[str, _ExistingSnapshot]:
        ids = sorted({_compact(item) for item in job_ids if _compact(item)})
        if not ids or self.storage is None:
            return {}
        with self.storage.session() as session:
            jobs = list(session.scalars(select(JobSnapshot).where(JobSnapshot.id.in_(ids))))
            analyses = list(
                session.scalars(
                    select(JobAnalysisSnapshot).where(JobAnalysisSnapshot.job_id.in_(ids))
                )
            )
        analyses_by_id = {row.job_id: row for row in analyses}
        return {
            row.id: _ExistingSnapshot(job=row, analysis=analyses_by_id.get(row.id))
            for row in jobs
        }

    def _read_existing_by_business_keys(
        self, business_keys: Sequence[str] | Any
    ) -> dict[str, _ExistingSnapshot]:
        keys = sorted({_compact(item) for item in business_keys if _compact(item)})
        if not keys or self.storage is None:
            return {}
        with self.storage.session() as session:
            jobs = list(
                session.scalars(
                    select(JobSnapshot).where(JobSnapshot.business_key.in_(keys))
                )
            )
            analyses = list(
                session.scalars(
                    select(JobAnalysisSnapshot).where(
                        JobAnalysisSnapshot.job_id.in_([job.id for job in jobs])
                    )
                )
            )
        analyses_by_id = {row.job_id: row for row in analyses}
        return {
            row.business_key: _ExistingSnapshot(
                job=row, analysis=analyses_by_id.get(row.id)
            )
            for row in jobs
            if row.business_key
        }

    @staticmethod
    def _existing_fingerprint(existing: _ExistingSnapshot | None) -> str | None:
        if existing is None:
            return None
        stored = _fingerprint_from_source_ref(existing.job.source_ref)
        if stored:
            return stored
        return job_content_fingerprint(
            {
                "company_id": existing.job.company_id,
                "title": existing.job.title,
                "city": existing.job.city,
                "detail_url": existing.job.detail_url,
                "jd_raw": existing.job.jd_raw,
                "cohort": existing.job.cohort,
                "cohort_status": existing.job.cohort_status,
                "batch": existing.job.batch,
            }
        )

    def _persist(
        self,
        *,
        companies: Sequence[PipelineCompany],
        staged: Sequence[_StagedWork],
        dry_run: bool,
    ) -> bool:
        if dry_run or self.storage is None or (not companies and not staged):
            return False
        now = _timestamp(self.clock)
        company_models = [
            Company(
                id=company.id,
                name=company.name,
                aliases=list(company.aliases),
                campus_url=company.careers_url or None,
                crawler_key=company.crawler_key or None,
                integration_status=company.integration_status,
                organization_id=_compact(company.extra.get("organization_id")) or company.id,
                recruitment_unit_name=_compact(
                    company.extra.get("recruitment_unit_name")
                ) or company.name,
                source_identity=_compact(company.extra.get("source_identity")) or None,
                created_at=now,
                updated_at=now,
                source=PIPELINE_SOURCE,
                source_ref=f"companies.yaml:{company.id}",
            )
            for company in companies
        ]
        job_models: list[tuple[Job, JobAnalysis | None]] = []
        for item in staged:
            existing_job = item.existing.job if item.existing else None
            updated_at = (
                now
                if item.job.get("_detail_capture_observed")
                else existing_job.updated_at
                if existing_job is not None
                else now
            )
            if item.analysis is not None:
                match_score = item.analysis.match_score
            elif existing_job is not None:
                match_score = existing_job.match_score
            else:
                match_score = None
            job_models.append(
                (
                    Job(
                        id=item.job["id"],
                        company_id=item.company.id,
                        title=_text(item.job.get("title")),
                        city=_text(item.job.get("city")) or None,
                        detail_url=_text(item.job.get("detail_url")),
                        jd_raw=_raw_detail(item.job.get("jd_raw")) or None,
                        capture_evidence=item.job.get("capture_evidence") or {},
                        cohort=item.job.get("cohort"),
                        cohort_status=_compact(item.job.get("cohort_status")) or "unconfirmed",
                        batch=_batch_value(item.job),
                        match_score=match_score,
                        first_seen_at=existing_job.first_seen_at if existing_job else now,
                        last_seen_at=now,
                        organization_id=_compact(
                            item.company.extra.get("organization_id")
                        ) or item.company.id,
                        recruitment_unit_id=_compact(
                            item.company.extra.get("recruitment_unit_id")
                        ) or item.company.id,
                        recruitment_campaign_id=_compact(
                            item.company.extra.get("recruitment_campaign_id")
                            or item.job.get("recruitment_campaign_id")
                        ) or None,
                        source_platform=item.company.crawler_key or None,
                        source_tenant=_compact(
                            item.company.extra.get("source_identity")
                        ) or None,
                        native_job_id=_compact(item.job.get("native_job_id")) or None,
                        normalized_detail_url=_compact(
                            item.job.get("normalized_detail_url")
                        ) or None,
                        business_key=_compact(item.job.get("business_key")) or None,
                        created_at=existing_job.created_at if existing_job else now,
                        updated_at=updated_at,
                        source=PIPELINE_SOURCE,
                        source_ref=_source_ref(item.company.id, item.job["id"], item.fingerprint),
                    ),
                    item.analysis,
                )
            )

        # The whole Agent snapshot is committed through exactly one write scope.
        with self.storage.write_transaction() as session:
            for company in company_models:
                upsert_company_snapshot(session, company)
            for job, analysis in job_models:
                upsert_job_snapshot(session, job)
                if analysis is not None:
                    upsert_job_analysis_snapshot(session, job, analysis)
        return True


def run_daily_pipeline(
    *,
    companies_path: Path | str = DEFAULT_COMPANIES_PATH,
    storage: Storage | None = None,
    crawler: CrawlerProtocol | Mapping[str, Any] | None = None,
    crawler_map: Mapping[str, type] | None = None,
    matcher: MatcherProtocol | Callable[..., Any] | None = None,
    jd_hydrator: Callable[[dict[str, Any]], Any] | None = _default_jd_hydrator,
    profile: Any = None,
    max_concurrency: int = 4,
    match_max_concurrency: int = 4,
    checkpoint_batch_size: int = 25,
    detail_reuse_ttl_hours: float = DEFAULT_DETAIL_REUSE_TTL_HOURS,
    company_ids: Sequence[str] = (),
    checkpoint_path: Path | str | None = None,
    resume_from_checkpoint: bool = False,
    progress_callback: Callable[[str, int, int], None] | None = None,
    dry_run: bool = False,
    legacy: bool = False,
    clock: Callable[[], datetime] = _now,
) -> DailyPipelineResult:
    """Run one non-scheduled daily crawl; detail receipts default to 24 hours."""

    return DailyRecruitmentPipeline(
        companies_path=companies_path,
        storage=storage,
        crawler=crawler,
        crawler_map=crawler_map,
        matcher=matcher,
        jd_hydrator=jd_hydrator,
        profile=profile,
        max_concurrency=max_concurrency,
        match_max_concurrency=match_max_concurrency,
        checkpoint_batch_size=checkpoint_batch_size,
        detail_reuse_ttl_hours=detail_reuse_ttl_hours,
        company_ids=company_ids,
        checkpoint_path=checkpoint_path,
        resume_from_checkpoint=resume_from_checkpoint,
        progress_callback=progress_callback,
        clock=clock,
    ).run(dry_run=dry_run, legacy=legacy)


__all__ = [
    "DEFAULT_COMPANIES_PATH",
    "DEFAULT_DETAIL_REUSE_TTL_HOURS",
    "PIPELINE_SOURCE",
    "CompanyConfigError",
    "CompanyRunResult",
    "CrawlResult",
    "CrawlerProtocol",
    "DailyPipelineResult",
    "DailyRecruitmentPipeline",
    "DeterministicMatcher",
    "MatcherProtocol",
    "MatchingServiceAdapter",
    "PipelineCompany",
    "PipelineError",
    "job_content_fingerprint",
    "load_companies",
    "run_daily_pipeline",
]
