"""Offline replay for the title-first recruitment capture policy.

The replay consumes frozen source/checkpoint, hydration, and catalog files.  It
does not import the legacy capture screener, access a repository, call a model,
or perform network work.  The catalog and all input records are treated as
immutable evidence; output rows are plans and evidence for a later, separately
approved integration.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from packages.recruitment_core.jd_capture import assess_jd_capture


SCHEMA_VERSION = "title-first-capture-replay.v3"
POLICY_SCOPE = "2027-autumn-three-industry-title-first"

OUTPUT_FILES: dict[str, str] = {
    "manifest": "replay-manifest.json",
    "summary": "summary.json",
    "company_status": "company-status.jsonl",
    "observed_titles": "observed-list-jobs.jsonl",
    "filtered_jobs": "filtered-jobs.jsonl",
    "existing_skipped": "existing-skipped.jsonl",
    "existing_repairs": "existing-repairs.jsonl",
    "successful_jd_reuse": "successful-jd-reuse.jsonl",
    "new_failures": "new-failures.jsonl",
    "missing_jd": "missing-jd.jsonl",
    "retry_candidates": "retry-candidates.jsonl",
    "deactivation_plan": "deactivation-plan.json",
    "applications": "applications-preserved.jsonl",
    "application_preservation": "application-preservation.json",
}

_SUCCESS_OUTCOMES = {
    "complete",
    "completed",
    "hydrated",
    "success",
    "succeeded",
    "reused",
    "reuse",
}
_FAILURE_OUTCOMES = {
    "failed",
    "failure",
    "fetch_failed",
    "detail_failed",
    "jd_hydration_fetch_failed",
    "capture_failed",
    "capture_incomplete",
    "incomplete",
    "error",
    "exception",
}
_PENDING_OUTCOMES = {
    "",
    "pending",
    "not_attempted",
    "unattempted",
    "queued",
    "running",
    "cohort_unconfirmed_not_hydrated",
    "capture_already_complete",
}
_UNUSABLE_REASON_CODES = {
    "missing_entry",
    "invalid_entry",
    "preflight_skip",
    "personal_center",
    "delivery_record",
    "success_page",
    "form_only",
    "wechat_only",
    "unusable",
}
_STATUS_VALUES = {"pending", "running", "complete", "partial", "failed", "unusable"}
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class _TitlePolicy:
    normalize_job_title_key: Callable[[Any], str]
    company_title_key: Callable[[Any, Any], tuple[str, str]]
    screen_title_job: Callable[[Any, Any | None], Any]
    stored_detail_retry_required: Callable[[Any], bool] | None = None


@dataclass
class _CompanyResolver:
    """Resolve exact IDs/names/aliases without fuzzy company matching."""

    aliases: dict[str, str] = field(default_factory=dict)
    display_names: dict[str, str] = field(default_factory=dict)

    def register(
        self,
        company_id: Any,
        name: Any = None,
        aliases: Iterable[Any] = (),
    ) -> str:
        raw_id = _text(company_id)
        raw_name = _text(name)
        tokens = [raw_id, raw_name, *(_text(item) for item in aliases)]
        existing = next(
            (self.aliases[_company_key(token)] for token in tokens if _company_key(token) in self.aliases),
            None,
        )
        canonical = existing or raw_id or raw_name or _company_key(raw_name)
        if not canonical:
            canonical = "unknown-company"
        self.display_names.setdefault(canonical, raw_name or canonical)
        for token in tokens:
            key = _company_key(token)
            if key:
                self.aliases[key] = canonical
        return canonical

    def resolve(self, company_id: Any = None, name: Any = None) -> str:
        raw_id = _text(company_id)
        raw_name = _text(name)
        for token in (raw_id, raw_name):
            key = _company_key(token)
            if key and key in self.aliases:
                canonical = self.aliases[key]
                if raw_name:
                    self.display_names.setdefault(canonical, raw_name)
                return canonical
        return self.register(raw_id or raw_name, raw_name)

    def name(self, company_id: str, fallback: Any = None) -> str:
        return self.display_names.get(company_id) or _text(fallback) or company_id


@dataclass
class _Source:
    ordinal: int
    source_key: str
    company_id: str
    company_name: str
    source_url: str
    source_urls: list[str]
    raw: dict[str, Any]
    scope: Any = None


@dataclass
class _Checkpoint:
    source_key: str
    path: Path
    raw: dict[str, Any]
    jobs: list[dict[str, Any]]
    raw_status: str
    status: str
    reason_code: str | None
    reason: str
    pagination_complete: bool | None
    completeness_known: bool | None
    has_more: bool | None
    list_complete: bool
    scope: Any = None
    pages_seen: int | None = None
    total_pages: int | None = None
    advertised_total: int | None = None
    source_runs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _HydrationEntry:
    raw: dict[str, Any]
    line_number: int
    state: str
    reason: str
    title: str
    company_id: str
    detail_url: str
    rank: tuple[int, int, int, int, int]
    attempted: bool


@dataclass
class _HydrationBucket:
    total: int = 0
    successful: _HydrationEntry | None = None
    failed: _HydrationEntry | None = None
    pending: _HydrationEntry | None = None


@dataclass
class _Catalog:
    raw: Any
    companies: list[dict[str, Any]]
    jobs: list[dict[str, Any]]
    analyses: list[dict[str, Any]]
    applications: list[dict[str, Any]]
    application_audit: dict[str, Any] | None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _casefold_text(value: Any) -> str:
    return _text(value).casefold()


def _company_key(value: Any) -> str:
    return _WHITESPACE_RE.sub(" ", _text(value)).strip().casefold()


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _first(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_text(value)] if _text(value) else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return list(dict.fromkeys(_text(item) for item in value if _text(item)))


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        value = value.strip().casefold()
        if value in {"true", "1", "yes", "complete", "completed"}:
            return True
        if value in {"false", "0", "no", "incomplete", "partial"}:
            return False
    return None


def _int_list(value: Any) -> list[int]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[int] = []
    for item in values:
        try:
            result.append(int(str(item).strip()))
        except (TypeError, ValueError):
            continue
    return result


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _dump_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


class _JsonlWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = path.open("w", encoding="utf-8", newline="\n")

    def write(self, value: Any) -> None:
        self.handle.write(_dump_json(value))
        self.handle.write("\n")

    def close(self) -> None:
        self.handle.close()

    def __enter__(self) -> _JsonlWriter:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()


def _load_title_policy() -> _TitlePolicy:
    """Load only the shared title policy; no legacy screener fallback is allowed."""

    from packages.matching.title_policy import (
        company_title_key,
        normalize_job_title_key,
        screen_title_job,
        stored_detail_retry_required,
    )

    return _TitlePolicy(
        normalize_job_title_key,
        company_title_key,
        screen_title_job,
        stored_detail_retry_required,
    )


def _title_key(policy: _TitlePolicy, title: Any) -> str:
    return _text(policy.normalize_job_title_key(title))


def _company_title_key(policy: _TitlePolicy, company_id: str, title: Any) -> tuple[str, str] | None:
    normalized_title = _title_key(policy, title)
    if not normalized_title:
        return None
    raw_key = policy.company_title_key(company_id, title)
    if not isinstance(raw_key, tuple) or len(raw_key) != 2:
        raise ValueError("company_title_key must return a two-item tuple")
    return (_text(raw_key[0]), _text(raw_key[1]))


def _screen_title(policy: _TitlePolicy, title: Any, profile: Any) -> tuple[dict[str, Any], bool, str | None]:
    # Passing only the title prevents legacy JD/cohort/direction gates from entering this replay.
    try:
        result = policy.screen_title_job({"title": _text(title)}, profile)
        dumped = _jsonable(result)
        if not isinstance(dumped, Mapping):
            raise TypeError("screen_title_job must return ScreeningResult-like data")
        payload = dict(dumped)
        eligible = bool(payload.get("eligible"))
        return payload, eligible, None
    except Exception as exc:  # A malformed title is recorded, never silently admitted.
        return {
            "eligible": False,
            "analysis_status": "screening_error",
            "reasons": [f"screening_error:{type(exc).__name__}"],
            "evidence": [],
        }, False, f"{type(exc).__name__}: {_text(exc)}"


def _source_items(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("sources", "items", "records", "companies"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
    raise ValueError("sources.json must contain a list or an object with a list")


def _urls_from(value: Any) -> list[str]:
    if isinstance(value, str):
        return [_text(value)] if _text(value) else []
    if isinstance(value, (list, tuple, set)):
        return list(dict.fromkeys(_text(item) for item in value if _text(item)))
    return []


def _load_sources(path: Path, resolver: _CompanyResolver) -> list[_Source]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid sources JSON: {path}") from exc

    sources: list[_Source] = []
    used_keys: Counter[str] = Counter()
    for ordinal, item in enumerate(_source_items(payload), 1):
        nested = item.get("row") if isinstance(item.get("row"), Mapping) else {}
        merged: dict[str, Any] = dict(item)
        merged.update(dict(nested))
        company_id_raw = _first(merged, ("company_id", "companyId", "companyID", "employer_id"))
        company_name = _text(_first(merged, ("company", "company_name", "companyName", "employer")))
        company_id = resolver.resolve(company_id_raw, company_name)
        if not company_name:
            company_name = resolver.name(company_id, company_id_raw)
        url_value = _first(
            merged,
            ("source_url", "sourceUrl", "crawl_url", "crawlUrl", "apply_url", "applyUrl", "entry_url", "url"),
        )
        urls = _urls_from(url_value)
        if not urls:
            urls = _urls_from(merged.get("source_urls"))
        if not urls:
            urls = [""]
        base_key = _text(_first(merged, ("source_key", "sourceKey", "task_key", "taskKey", "key")))
        base_key = base_key or f"source-{ordinal}"
        for url_index, source_url in enumerate(urls):
            source_key = base_key if len(urls) == 1 else f"{base_key}:{url_index + 1}"
            used_keys[source_key] += 1
            if used_keys[source_key] > 1:
                source_key = f"{source_key}#{used_keys[source_key]}"
            source_raw = deepcopy(dict(item))
            sources.append(
                _Source(
                    ordinal=ordinal,
                    source_key=source_key,
                    company_id=company_id,
                    company_name=company_name,
                    source_url=source_url,
                    source_urls=urls,
                    raw=source_raw,
                    scope=_scope_value(merged),
                )
            )
    if not sources:
        raise ValueError(f"sources.json contains no source records: {path}")
    return sources


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid JSON: {path}") from exc


def _as_mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]


def _load_catalog(path: Path) -> _Catalog:
    if path.suffix.casefold() in {".jsonl", ".ndjson"}:
        jobs: list[dict[str, Any]] = []
        malformed = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    malformed += 1
                    continue
                if isinstance(value, Mapping):
                    jobs.append(deepcopy(dict(value)))
        if malformed:
            raise ValueError(f"Catalog JSONL contains malformed lines: {path}")
        return _Catalog(path.name, [], jobs, [], [], None)

    payload = _read_json(path)
    if isinstance(payload, list):
        return _Catalog(payload, [], _as_mapping_list(payload), [], [], None)
    if not isinstance(payload, Mapping):
        raise ValueError("catalog snapshot must contain an object or a list")
    jobs_value = payload.get("jobs")
    if jobs_value is None and isinstance(payload.get("items"), list):
        jobs_value = payload.get("items")
    analyses_value = payload.get("analyses") or payload.get("job_analyses")
    applications_value = payload.get("applications") or payload.get("application_records")
    return _Catalog(
        deepcopy(dict(payload)),
        _as_mapping_list(payload.get("companies")),
        _as_mapping_list(jobs_value),
        _as_mapping_list(analyses_value),
        _as_mapping_list(applications_value),
        deepcopy(dict(payload["application_audit"]))
        if isinstance(payload.get("application_audit"), Mapping)
        else None,
    )


def _catalog_resolver(catalog: _Catalog) -> _CompanyResolver:
    resolver = _CompanyResolver()
    for company in catalog.companies:
        resolver.register(
            _first(company, ("id", "company_id", "companyId")),
            _first(company, ("name", "company_name", "companyName")),
            company.get("aliases", []),
        )
    for job in catalog.jobs:
        resolver.register(
            _first(job, ("company_id", "companyId")),
            _first(job, ("company", "company_name", "companyName")),
        )
    return resolver


def _job_company_id(job: Mapping[str, Any], resolver: _CompanyResolver) -> str:
    return resolver.resolve(
        _first(job, ("company_id", "companyId", "companyID")),
        _first(job, ("company", "company_name", "companyName", "employer")),
    )


def _job_title(job: Mapping[str, Any]) -> str:
    return _text(_first(job, ("title", "job_title", "position_name", "positionName")))


def _job_detail_url(job: Mapping[str, Any]) -> str:
    return _text(_first(job, ("detail_url", "detailUrl", "jd_url", "jdUrl", "url")))


def _analysis_by_job(catalog: _Catalog) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for analysis in catalog.analyses:
        job_id = _text(_first(analysis, ("job_id", "jobId", "id")))
        if job_id and job_id not in result:
            result[job_id] = deepcopy(analysis)
    return result


def _build_catalog_index(
    catalog: _Catalog,
    resolver: _CompanyResolver,
    policy: _TitlePolicy,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    analyses = _analysis_by_job(catalog)
    index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for ordinal, job in enumerate(catalog.jobs, 1):
        company_id = _job_company_id(job, resolver)
        key = _company_title_key(policy, company_id, _job_title(job))
        if key is None:
            continue
        job_id = _text(_first(job, ("id", "job_id", "jobId"))) or f"catalog-row-{ordinal}"
        analysis = job.get("previous_analysis")
        if not isinstance(analysis, Mapping):
            analysis = analyses.get(job_id)
        index[key].append(
            {
                "catalog_ordinal": ordinal,
                "job_id": job_id,
                "company_id": company_id,
                "job": deepcopy(job),
                "analysis": deepcopy(dict(analysis)) if isinstance(analysis, Mapping) else None,
            }
        )
    return index


def _extract_checkpoint_jobs(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    crawl = payload.get("crawl")
    if not isinstance(crawl, Mapping):
        crawl = payload.get("result") if isinstance(payload.get("result"), Mapping) else {}
    for container in (crawl, payload):
        for key in ("raw_jobs", "jobs", "items"):
            value = container.get(key)
            if isinstance(value, list):
                return [deepcopy(dict(item)) for item in value if isinstance(item, Mapping)]
    return []


def _optional_int(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _checkpoint_metadata(
    payload: Mapping[str, Any],
) -> tuple[
    bool | None,
    bool | None,
    bool | None,
    int | None,
    int | None,
    int | None,
    list[dict[str, Any]],
]:
    crawl = payload.get("crawl") if isinstance(payload.get("crawl"), Mapping) else {}
    if not crawl and isinstance(payload.get("result"), Mapping):
        crawl = payload["result"]
    evidence = crawl.get("pagination_evidence")
    if not isinstance(evidence, Mapping):
        evidence = payload.get("pagination_evidence") if isinstance(payload.get("pagination_evidence"), Mapping) else {}

    def first_metadata(keys: Sequence[str]) -> Any:
        for container in (evidence, crawl, payload):
            value = _first(container, keys)
            if value not in (None, ""):
                return value
        return None

    complete = _bool_or_none(first_metadata(("pagination_complete", "complete")))
    known = _bool_or_none(first_metadata(("completeness_known", "known")))
    has_more = _bool_or_none(first_metadata(("has_more", "hasMore")))
    if complete is None and _casefold_text(evidence.get("pagination_state")) == "complete":
        complete = True
    source_runs_value = crawl.get("source_runs")
    if not isinstance(source_runs_value, list):
        source_runs_value = payload.get("source_runs")
    source_runs = _as_mapping_list(source_runs_value)
    return (
        complete,
        known,
        has_more,
        _optional_int(first_metadata(("pages_seen", "pagesSeen"))),
        _optional_int(first_metadata(("total_pages", "totalPages"))),
        _optional_int(first_metadata(("advertised_total", "advertisedTotal"))),
        source_runs,
    )


def _checkpoint_pagination(payload: Mapping[str, Any]) -> tuple[bool | None, bool | None, bool | None]:
    return _checkpoint_metadata(payload)[:3]


def _scope_value(value: Mapping[str, Any]) -> Any:
    for key in ("scope_id", "scopeId", "capture_scope", "source_scope", "filter_scope", "scope"):
        if value.get(key) not in (None, ""):
            return deepcopy(value[key])
    relevant = {
        key: deepcopy(value[key])
        for key in ("targetYears", "target_years", "recruitType", "recruit_type", "industryGroupCodes", "industry_groups")
        if key in value
    }
    return relevant or None


def _scope_json(value: Any) -> str:
    return _dump_json(value) if value is not None else ""


def _normalise_status(raw_status: str, reason_code: str | None, list_complete: bool, jobs: list[Any]) -> str:
    status = raw_status.casefold()
    reason = (reason_code or "").casefold()
    if status in {"skip", "skipped", "unusable"} or reason in _UNUSABLE_REASON_CODES:
        return "unusable"
    if status in {"failed", "error", "exception"}:
        return "failed"
    if status in {"pending", "queued"}:
        return "pending"
    if status in {"running", "in_progress"}:
        return "running"
    if status == "partial":
        return "partial"
    if list_complete and status in {
        "",
        "complete",
        "completed",
        "success",
        "succeeded",
        "raw_jobs_observed",
        "empty_raw_result_not_proof_of_no_jobs",
    }:
        return "complete"
    if status in {"complete", "completed", "success", "succeeded"} or jobs:
        return "partial"
    return "partial"


def _checkpoint_from_payload(path: Path, payload: Mapping[str, Any], source_key: str) -> _Checkpoint:
    jobs = _extract_checkpoint_jobs(payload)
    crawl = payload.get("crawl") if isinstance(payload.get("crawl"), Mapping) else {}
    raw_status = _text(_first(payload, ("crawler_status", "status", "capture_status")))
    if not raw_status:
        raw_status = _text(_first(crawl, ("crawler_status", "status")))
    reason_code = _text(_first(payload, ("reason_code", "error_code", "failure_code"))) or None
    reason = _text(_first(payload, ("reason", "error", "error_message", "failure_reason")))
    error_marker = _first(payload, ("error", "error_code", "error_message"))
    if error_marker in (None, ""):
        error_marker = _first(crawl, ("error", "error_code", "error_message"))
    if not raw_status and error_marker not in (None, ""):
        raw_status = "failed"
    metadata = _checkpoint_metadata(payload)
    complete, known, has_more, pages_seen, total_pages, advertised_total, source_runs = metadata
    if not reason:
        termination = payload.get("termination_reasons") or crawl.get("termination_reasons")
        if isinstance(termination, list):
            reason = "; ".join(_text(item) for item in termination if _text(item))
    if not reason:
        termination = [
            _text(run.get("termination_reason"))
            for run in source_runs
            if _text(run.get("termination_reason"))
        ]
        if termination:
            reason = "; ".join(termination)
    if not reason:
        reason = "No checkpoint reason recorded."
    list_complete = complete is True and known is True and has_more is False
    status = _normalise_status(raw_status, reason_code, list_complete, jobs)
    if status == "complete" and not list_complete:
        status = "partial"
    scope = _scope_value(payload)
    if scope is None:
        scope = _scope_value(crawl)
    return _Checkpoint(
        source_key=source_key,
        path=path,
        raw=deepcopy(dict(payload)),
        jobs=jobs,
        raw_status=raw_status or "unknown",
        status=status,
        reason_code=reason_code,
        reason=reason,
        pagination_complete=complete,
        completeness_known=known,
        has_more=has_more,
        list_complete=list_complete,
        scope=scope,
        pages_seen=pages_seen,
        total_pages=total_pages,
        advertised_total=advertised_total,
        source_runs=source_runs,
    )


def _checkpoint_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise ValueError(f"Checkpoint path does not exist: {path}")
    return sorted(item for item in path.glob("*.json") if item.is_file())


def _failed_checkpoint(path: Path, source_key: str, reason: str) -> _Checkpoint:
    return _Checkpoint(
        source_key=source_key,
        path=path,
        raw={},
        jobs=[],
        raw_status="failed",
        status="failed",
        reason_code="invalid_checkpoint",
        reason=reason,
        pagination_complete=False,
        completeness_known=False,
        has_more=None,
        list_complete=False,
        scope=None,
    )


def _load_checkpoints(path: Path, sources: Sequence[_Source]) -> tuple[dict[str, _Checkpoint], list[dict[str, Any]]]:
    source_keys = {source.source_key for source in sources}
    result: dict[str, _Checkpoint] = {}
    extras: list[dict[str, Any]] = []
    for checkpoint_path in _checkpoint_files(path):
        try:
            payload = _read_json(checkpoint_path)
        except ValueError as exc:
            source_key = checkpoint_path.stem
            if source_key in source_keys and source_key not in result:
                result[source_key] = _failed_checkpoint(checkpoint_path, source_key, str(exc))
            extras.append({"path": str(checkpoint_path), "reason": str(exc), "kind": "invalid_json"})
            continue
        if not isinstance(payload, Mapping):
            source_key = checkpoint_path.stem
            if source_key in source_keys and source_key not in result:
                result[source_key] = _failed_checkpoint(
                    checkpoint_path,
                    source_key,
                    "checkpoint is not an object",
                )
            extras.append({"path": str(checkpoint_path), "reason": "checkpoint is not an object", "kind": "invalid_shape"})
            continue
        source_key = _text(_first(payload, ("task_key", "source_key", "sourceKey", "key"))) or checkpoint_path.stem
        checkpoint = _checkpoint_from_payload(checkpoint_path, payload, source_key)
        if source_key not in source_keys:
            extras.append({"path": str(checkpoint_path), "source_key": source_key, "kind": "unmatched"})
            continue
        if source_key in result:
            extras.append({"path": str(checkpoint_path), "source_key": source_key, "kind": "duplicate"})
            continue
        result[source_key] = checkpoint
    return result, extras


def _resolve_hydration_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    if path.is_dir():
        path = path / "jobs.jsonl"
    if not path.is_file():
        raise ValueError(f"Hydration jobs JSONL does not exist: {path}")
    return path


def _iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except (ValueError, TypeError) as exc:
                yield line_number, None, f"invalid_json:{type(exc).__name__}"
                continue
            if not isinstance(value, Mapping):
                yield line_number, None, "record_is_not_an_object"
                continue
            yield line_number, deepcopy(dict(value)), None


def _hydration_job(record: Mapping[str, Any]) -> dict[str, Any]:
    raw_job = record.get("job")
    if isinstance(raw_job, Mapping):
        return deepcopy(dict(raw_job))
    return deepcopy(dict(record))


def _raw_detail(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _chosen_detail_and_evidence(
    record: Mapping[str, Any],
    job: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any] | None, str]:
    """Keep a hydration detail paired with evidence from the same capture."""

    pairs = (
        (record, "candidate_jd_raw", "candidate_capture_evidence", "candidate"),
        (record, "detail", "capture_evidence", "hydration"),
        (record, "jd_raw", "capture_evidence", "record"),
        (job, "jd_raw", "capture_evidence", "job"),
    )
    for container, detail_field, evidence_field, origin in pairs:
        # Evidence cannot identify a body by itself. A present candidate body,
        # including an empty one, is authoritative and must not fall through to
        # an older inline value that could make a failed candidate look valid.
        if detail_field not in container:
            continue
        detail = _raw_detail(container.get(detail_field))
        evidence = container.get(evidence_field)
        return detail, evidence if isinstance(evidence, Mapping) else None, origin
    return "", None, "missing"


def _capture_evidence(record: Mapping[str, Any], job: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return _chosen_detail_and_evidence(record, job)[1]


def _effective_jd(record: Mapping[str, Any], job: Mapping[str, Any]) -> str:
    return _chosen_detail_and_evidence(record, job)[0]


def _url_key(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw.casefold().rstrip("/")
    if not parsed.scheme or not parsed.netloc:
        return raw.casefold().rstrip("/")
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path.rstrip("/") or "/",
            parsed.query,
            "",
        )
    )


def _evidence_title_values(evidence: Mapping[str, Any]) -> set[str]:
    values = {
        _text(evidence.get(key))
        for key in ("title", "job_title", "position_name", "positionName")
        if _text(evidence.get(key))
    }
    identity = evidence.get("identity_evidence")
    if isinstance(identity, (list, tuple)):
        for item in identity:
            text = _text(item)
            prefix, separator, value = text.partition(":")
            if separator and prefix.casefold().replace("-", "_") in {"title", "job_title", "position_name"}:
                if _text(value):
                    values.add(_text(value))
    return values


def _capture_binding(
    record: Mapping[str, Any],
    job: Mapping[str, Any],
    title: str,
    policy: _TitlePolicy,
) -> tuple[bool, dict[str, Any]]:
    detail, evidence, evidence_origin = _chosen_detail_and_evidence(record, job)
    assessment = assess_jd_capture(
        {
            "jd_raw": detail,
            "capture_evidence": evidence or {},
        }
    )
    detail_url = _job_detail_url(job) or _text(_first(record, ("detail_url", "detailUrl", "jd_url", "jdUrl")))
    source_url = _text(evidence.get("source_url")) if evidence else ""
    evidence_status = _casefold_text(evidence.get("status")) if evidence else ""
    url_bound = bool(detail_url and source_url and _url_key(detail_url) == _url_key(source_url))
    evidence_titles = _evidence_title_values(evidence) if evidence else set()
    expected_title_key = _title_key(policy, title)
    title_keys = {_title_key(policy, item) for item in evidence_titles if _title_key(policy, item)}
    title_bound = not title_keys or title_keys == {expected_title_key}
    complete = assessment.complete
    binding = {
        "evidence_present": evidence is not None,
        "chosen_evidence_origin": evidence_origin,
        "evidence_status": evidence_status or None,
        "detail_url": detail_url or None,
        "source_url": source_url or None,
        "url_bound": url_bound,
        "evidence_titles": sorted(evidence_titles),
        "title_bound": title_bound,
        "complete": complete,
        "assessment_complete": assessment.complete,
        "assessment_reason_code": assessment.reason_code,
        "assessment_reason": assessment.reason,
        "chosen_jd_sha256": hashlib.sha256(detail.encode("utf-8")).hexdigest()
        if detail
        else None,
    }
    return complete and url_bound and title_bound, binding


def _hydration_state(
    record: Mapping[str, Any],
    policy: _TitlePolicy,
    title: str,
    job: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any], tuple[int, int, int, int, int], bool]:
    outcome = _casefold_text(_first(record, ("hydration_outcome", "outcome", "result")))
    status = _casefold_text(_first(record, ("hydration_status", "status", "capture_status")))
    effective_jd = _effective_jd(record, job)
    bound, binding = _capture_binding(record, job, title, policy)
    explicit_failure = (
        outcome in _FAILURE_OUTCOMES
        or status in _FAILURE_OUTCOMES
        or outcome.endswith("_failed")
        or status.endswith("_failed")
    )
    explicit_success = outcome in _SUCCESS_OUTCOMES or status in _SUCCESS_OUTCOMES
    request_value = _first(record, ("request_made", "detail_attempted", "attempted", "fetch_attempted"))
    if isinstance(request_value, bool):
        attempted = request_value
    elif explicit_failure or explicit_success:
        attempted = True
    elif outcome in _PENDING_OUTCOMES or status in _PENDING_OUTCOMES:
        attempted = False
    else:
        attempted = bool(effective_jd or binding["evidence_present"])

    explicit_pending = (
        (outcome in _PENDING_OUTCOMES or status in _PENDING_OUTCOMES)
        and not explicit_failure
        and not explicit_success
    )
    if not explicit_failure and effective_jd and bound and (explicit_success or binding["complete"]):
        state = "success"
        reason = "Historical official detail is complete and source-bound."
    elif explicit_pending:
        state = "pending"
        reason = (
            "Historical detail attempt is still pending."
            if attempted
            else "Historical detail was not attempted or remains pending."
        )
    elif explicit_failure or attempted:
        state = "failure"
        reason = _hydration_failure_reason(record, binding)
    else:
        state = "pending"
        reason = "Historical detail was not attempted or remains pending."
    rank = (
        int(state == "success"),
        int(explicit_success),
        int(binding["title_bound"]),
        int(binding["url_bound"]),
        len(effective_jd),
    )
    return state, reason, binding, rank, attempted


def _hydration_failure_reason(record: Mapping[str, Any], binding: Mapping[str, Any]) -> str:
    value = _first(
        record,
        ("capture_failure_reason", "failure_reason", "error", "error_message", "reason", "error_code"),
    )
    if _text(value):
        return _text(value)
    if not binding.get("evidence_present"):
        return "Detail attempt has no capture evidence."
    if binding.get("assessment_complete") is not True:
        return _text(binding.get("assessment_reason")) or "Official detail capture failed validation."
    if binding.get("evidence_status") != "complete":
        return "Detail capture evidence is not complete."
    if not binding.get("url_bound"):
        return "Detail capture evidence is not bound to the original detail URL."
    if not binding.get("title_bound"):
        return "Detail capture evidence title does not match the list title."
    return "Historical detail capture failed without a recorded reason."


def _add_hydration_entry(
    buckets: dict[tuple[str, str], _HydrationBucket],
    key: tuple[str, str],
    entry: _HydrationEntry,
) -> None:
    bucket = buckets.setdefault(key, _HydrationBucket())
    bucket.total += 1
    if entry.state == "success":
        if bucket.successful is None or entry.rank > bucket.successful.rank:
            bucket.successful = entry
    elif entry.state == "failure":
        if bucket.failed is None or entry.line_number < bucket.failed.line_number:
            bucket.failed = entry
    elif bucket.pending is None or entry.line_number < bucket.pending.line_number:
        bucket.pending = entry


def _load_hydration(
    path: Path | None,
    resolver: _CompanyResolver,
    policy: _TitlePolicy,
) -> tuple[dict[tuple[str, str], _HydrationBucket], dict[str, int]]:
    buckets: dict[tuple[str, str], _HydrationBucket] = {}
    counts: Counter[str] = Counter()
    if path is None:
        return buckets, {"records": 0, "malformed": 0, "unmatched": 0}
    for line_number, record, error in _iter_jsonl(path):
        if error:
            counts["malformed"] += 1
            continue
        assert record is not None
        counts["records"] += 1
        job = _hydration_job(record)
        title = _job_title(job) or _text(_first(record, ("title", "job_title", "position_name")))
        company_id = resolver.resolve(
            _first(record, ("company_id", "companyId"))
            or _first(job, ("company_id", "companyId")),
            _first(record, ("company", "company_name", "companyName"))
            or _first(job, ("company", "company_name", "companyName")),
        )
        key = _company_title_key(policy, company_id, title)
        if key is None:
            counts["unmatched"] += 1
            continue
        state, reason, _binding, rank, attempted = _hydration_state(record, policy, title, job)
        _add_hydration_entry(
            buckets,
            key,
            _HydrationEntry(
                raw=record,
                line_number=line_number,
                state=state,
                reason=reason,
                title=title,
                company_id=company_id,
                detail_url=_job_detail_url(job),
                rank=rank,
                attempted=attempted,
            ),
        )
        counts[f"{state}_records"] += 1
    counts.setdefault("records", 0)
    counts.setdefault("malformed", 0)
    counts.setdefault("unmatched", 0)
    return buckets, dict(counts)


def _catalog_cohort_matches(job: Mapping[str, Any]) -> bool:
    value = _first(job, ("cohort", "cohort_year"))
    if value in (None, ""):
        return False
    try:
        if int(str(value).strip()) != 2027:
            return False
    except (TypeError, ValueError):
        return False
    status = _casefold_text(job.get("cohort_status"))
    if status != "confirmed":
        return False
    return True


def _source_scope_matches(source: _Source, checkpoint: _Checkpoint) -> bool:
    source_raw = source.raw
    nested = source_raw.get("row") if isinstance(source_raw.get("row"), Mapping) else {}
    combined = dict(source_raw)
    combined.update(dict(nested))
    target_years = _first(combined, ("targetYears", "target_years"))
    if target_years not in (None, "") and 2027 not in _int_list(target_years):
        return False
    recruit_type = _casefold_text(_first(combined, ("recruitType", "recruit_type")))
    if recruit_type and recruit_type not in {"秋招", "autumn", "autumn_recruitment", "campus_autumn"}:
        return False
    source_scope = source.scope
    checkpoint_scope = checkpoint.scope
    if source_scope is not None and checkpoint_scope is not None:
        return _scope_json(source_scope) == _scope_json(checkpoint_scope)
    return True


def _catalog_scope_matches(source: _Source, checkpoint: _Checkpoint, job: Mapping[str, Any]) -> bool:
    if not _catalog_cohort_matches(job) or not _source_scope_matches(source, checkpoint):
        return False
    source_scope = source.scope if source.scope is not None else checkpoint.scope
    job_scope = _scope_value(job)
    if source_scope is not None and job_scope is not None:
        return _scope_json(source_scope) == _scope_json(job_scope)
    return True


def _source_retry_kind(status: str, checkpoint: _Checkpoint | None) -> str:
    if checkpoint is None:
        return "source_unattempted"
    if status == "failed":
        return "source_capture_failed"
    if status == "unusable":
        return "source_unusable"
    if status in {"pending", "running"}:
        return "source_unattempted"
    return "source_capture_incomplete"


def _attach_source_capture(state: dict[str, Any], source_key: str, source_record: Mapping[str, Any]) -> None:
    for source in state["sources"]:
        if source.get("source_key") == source_key:
            source["capture"] = deepcopy(dict(source_record))
            return


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _input_manifest(
    sources_path: Path,
    checkpoint_path: Path,
    hydration_path: Path | None,
    catalog_path: Path,
) -> dict[str, Any]:
    files: list[dict[str, str]] = []
    direct_paths = [sources_path, catalog_path]
    if hydration_path is not None:
        direct_paths.append(hydration_path)
    for path in direct_paths:
        files.append({"path": str(path.resolve()), "sha256": _file_sha256(path)})
    for path in _checkpoint_files(checkpoint_path):
        files.append({"path": str(path.resolve()), "sha256": _file_sha256(path)})
    return {"files": files}


def _check_output_path(output: Path, inputs: Sequence[Path]) -> None:
    output = output.resolve()
    for input_path in inputs:
        resolved = input_path.resolve()
        if resolved.is_dir() and output.is_relative_to(resolved):
            raise ValueError("Output directory must be separate from input directories")
        if resolved.is_file() and output == resolved:
            raise ValueError("Output directory must not be an input file")


def _prepare_output(output: Path, manifest: Mapping[str, Any], resume: bool) -> None:
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / OUTPUT_FILES["manifest"]
    existing = list(output.iterdir())
    if resume:
        if not manifest_path.is_file():
            raise ValueError("--resume requires an existing replay manifest")
        old = _read_json(manifest_path)
        if (
            not isinstance(old, Mapping)
            or old.get("schema_version") != manifest.get("schema_version")
            or old.get("policy_scope") != manifest.get("policy_scope")
            or old.get("inputs") != manifest.get("inputs")
            or old.get("profile") != manifest.get("profile")
        ):
            raise ValueError("--resume requires the same replay schema, policy, frozen inputs, and title-policy profile")
        return
    if existing:
        raise ValueError("Output directory is not empty; use --resume for the same frozen inputs")


def _company_list_status(statuses: Sequence[str]) -> str:
    if not statuses:
        return "pending"
    counts = Counter(statuses)
    if all(status == "complete" for status in statuses):
        return "complete"
    if all(status == "failed" for status in statuses):
        return "failed"
    if all(status == "unusable" for status in statuses):
        return "unusable"
    if all(status == "pending" for status in statuses):
        return "pending"
    if counts.get("running") and len(counts) == 1:
        return "running"
    return "partial"


def _job_availability(job: Mapping[str, Any]) -> str:
    value = _casefold_text(job.get("availability_status"))
    return value if value in {"active", "inactive"} else "active"


def _stored_detail_retry_required(policy: _TitlePolicy, job: Mapping[str, Any]) -> bool:
    checker = policy.stored_detail_retry_required
    if checker is None:
        # Keep fixture policies compatible while always using the shared raw-job rule.
        from packages.matching.title_policy import stored_detail_retry_required as checker

    return bool(checker(job))


def _existing_capture_state(job: Mapping[str, Any], policy: _TitlePolicy | None = None) -> str:
    """Classify stored detail without turning absent legacy metadata into pending."""

    policy = policy or _load_title_policy()
    if not _stored_detail_retry_required(policy, job):
        return "healthy"
    status = _casefold_text(_first(job, ("capture_status", "detail_status", "hydration_status", "status")))
    if status in _FAILURE_OUTCOMES or status.endswith("_failed") or status in {"error", "exception"}:
        return "failed"
    if _text(job.get("capture_failure_reason")):
        return "failed"
    return "pending"


def _existing_jobs_payload(existing: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "job_id": item["job_id"],
            "job": deepcopy(item["job"]),
            "analysis": deepcopy(item["analysis"]),
        }
        for item in existing
    ]


def _valid_score(value: Any) -> int | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or not 0 <= numeric <= 100:
        return None
    return int(round(numeric))


def _existing_score_info(entry: Mapping[str, Any]) -> tuple[bool, int | None, str | None]:
    job = entry.get("job") if isinstance(entry.get("job"), Mapping) else {}
    job_score = _valid_score(job.get("match_score"))
    if job_score is not None:
        return True, job_score, "job.match_score"
    analysis = entry.get("analysis")
    if not isinstance(analysis, Mapping):
        analysis = job.get("analysis") if isinstance(job.get("analysis"), Mapping) else None
    if isinstance(analysis, Mapping):
        status = _casefold_text(analysis.get("analysis_status") or analysis.get("status"))
        analysis_score = _valid_score(analysis.get("match_score", analysis.get("score")))
        if status == "complete" and analysis_score is not None:
            return True, analysis_score, "analysis.match_score"
    return False, None, None


def _repaired_existing_job(
    existing_job: Mapping[str, Any],
    company_id: str,
    title: str,
    title_key: str,
    *,
    jd_raw: str,
    capture_evidence: Mapping[str, Any] | None,
    detail_url: str | None,
) -> dict[str, Any]:
    repaired = _proposed_job(
        existing_job,
        company_id,
        title,
        title_key,
        capture_status="complete",
        jd_raw=jd_raw,
        capture_evidence=capture_evidence,
        detail_url=detail_url,
    )
    if "match_score" in existing_job:
        repaired["match_score"] = deepcopy(existing_job["match_score"])
    return repaired


def _existing_attempted(job: Mapping[str, Any]) -> bool:
    value = _first(job, ("request_made", "detail_attempted", "attempted", "fetch_attempted"))
    return value is True or _casefold_text(value) in {"true", "1", "yes"}


def _proposed_job(
    candidate: Mapping[str, Any],
    company_id: str,
    title: str,
    title_key: str,
    *,
    capture_status: str,
    capture_failure_reason: str | None = None,
    jd_raw: str | None = None,
    capture_evidence: Mapping[str, Any] | None = None,
    detail_url: str | None = None,
) -> dict[str, Any]:
    from packages.recruitment_core.offerbiu_policy import apply_offerbiu_cohort

    result = apply_offerbiu_cohort(candidate)
    result["company_id"] = company_id
    result["title"] = title
    result["title_key"] = title_key
    result["capture_status"] = capture_status
    result["availability_status"] = "active"
    result["capture_failure_reason"] = capture_failure_reason
    if jd_raw is not None:
        result["jd_raw"] = jd_raw
    elif capture_status in {"failed", "pending"}:
        # List rows may carry teaser text; a placeholder must not expose it as a captured JD.
        result["jd_raw"] = None
    if capture_evidence is not None:
        result["capture_evidence"] = deepcopy(dict(capture_evidence))
    elif capture_status in {"failed", "pending"}:
        result["capture_evidence"] = {}
    if detail_url:
        result["detail_url"] = detail_url
    result["match_score"] = None
    return result


def _deactivation_plan(
    sources: Sequence[_Source],
    checkpoints: Mapping[str, _Checkpoint],
    company_states: Mapping[str, dict[str, Any]],
    catalog_index: Mapping[tuple[str, str], list[dict[str, Any]]],
    observed_titles: Mapping[str, set[str]],
    policy: _TitlePolicy,
    resolver: _CompanyResolver,
) -> dict[str, Any]:
    by_company: dict[str, list[tuple[_Source, _Checkpoint]]] = defaultdict(list)
    for source in sources:
        checkpoint = checkpoints.get(source.source_key)
        if checkpoint is not None:
            by_company[source.company_id].append((source, checkpoint))

    plans: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    catalog_jobs_by_company: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entries in catalog_index.values():
        for entry in entries:
            catalog_jobs_by_company[entry["company_id"]].append(entry)

    for company_id, state in company_states.items():
        entries = by_company.get(company_id, [])
        if not entries or len(entries) != len(state["sources"]):
            blocked.append(
                {
                    "company_id": company_id,
                    "company_name": state["company_name"],
                    "reason": "not_all_company_sources_have_checkpoints",
                }
            )
            continue
        if not all(
            source.source_url
            and checkpoint.status == "complete"
            and checkpoint.list_complete
            for source, checkpoint in entries
        ):
            blocked.append(
                {
                    "company_id": company_id,
                    "company_name": state["company_name"],
                    "reason": "list_capture_not_complete_and_known",
                    "source_statuses": [checkpoint.status for _source, checkpoint in entries],
                }
            )
            continue
        scope_bad = [
            source.source_key
            for source, checkpoint in entries
            if not _source_scope_matches(source, checkpoint)
        ]
        if scope_bad:
            blocked.append(
                {
                    "company_id": company_id,
                    "company_name": state["company_name"],
                    "reason": "source_scope_not_2027_autumn_or_sources_disagree",
                    "source_keys": scope_bad,
                }
            )
            continue
        observed = observed_titles.get(company_id, set())
        for entry in catalog_jobs_by_company.get(company_id, []):
            job = entry["job"]
            if not all(_catalog_scope_matches(source, checkpoint, job) for source, checkpoint in entries):
                continue
            title_key = _title_key(policy, _job_title(job))
            if not title_key:
                continue
            base = {
                "company_id": company_id,
                "company_name": state["company_name"] or resolver.name(company_id),
                "catalog_job_id": entry["job_id"],
                "title": _job_title(job),
                "title_key": title_key,
                "current_availability_status": _job_availability(job),
                "apply_allowed": False,
                "historical_simulation_only": True,
                "catalog_job": deepcopy(job),
            }
            key = _company_title_key(policy, company_id, _job_title(job))
            if key is None:
                continue
            if title_key in observed:
                if _job_availability(job) == "inactive":
                    plans.append({**base, "action": "would_restore_active"})
            else:
                plans.append({**base, "action": "would_mark_inactive"})
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry_run_only",
        "apply_allowed": False,
        "historical_simulation_only": True,
        "blocked_reason": "Historical snapshot absence cannot authorize formal inactivity writes.",
        "plans": plans,
        "blocked_companies": blocked,
        "counts": {
            "would_mark_inactive": sum(item["action"] == "would_mark_inactive" for item in plans),
            "would_restore_active": sum(item["action"] == "would_restore_active" for item in plans),
            "blocked_companies": len(blocked),
            "formal_writes_applied": 0,
        },
    }


def _application_preservation(catalog: _Catalog) -> dict[str, Any]:
    if catalog.applications:
        return {
            "records_available": True,
            "records_preserved": len(catalog.applications),
            "source": "catalog.applications_or_application_records",
            "mutated": False,
        }
    audit = deepcopy(catalog.application_audit) if catalog.application_audit is not None else None
    count = audit.get("count") if isinstance(audit, Mapping) else None
    return {
        "records_available": False,
        "records_preserved": 0,
        "record_count_from_audit": count,
        "source_audit": audit,
        "mutated": False,
        "reason": "Catalog snapshot supplied no application rows; no deletion or rewrite was attempted.",
    }


def replay(
    sources_path: str | Path,
    checkpoint_path: str | Path,
    hydration_jobs_path: str | Path | None,
    catalog_snapshot_path: str | Path,
    output_dir: str | Path,
    *,
    profile: Any = None,
    resume: bool = False,
) -> dict[str, Any]:
    """Replay frozen capture evidence into a separate, read-only output directory."""

    sources_file = Path(sources_path).resolve()
    checkpoints_file = Path(checkpoint_path).resolve()
    hydration_input = Path(hydration_jobs_path).resolve() if hydration_jobs_path else None
    hydration_file = _resolve_hydration_path(hydration_input) if hydration_input else None
    catalog_file = Path(catalog_snapshot_path).resolve()
    output = Path(output_dir).resolve()
    for path in (sources_file, catalog_file):
        if not path.is_file():
            raise ValueError(f"Input file does not exist: {path}")
    input_paths = [sources_file, checkpoints_file, catalog_file]
    if hydration_input is not None:
        input_paths.append(hydration_input)
    _check_output_path(output, input_paths)

    input_manifest = _input_manifest(sources_file, checkpoints_file, hydration_file, catalog_file)
    replay_manifest = {
        "schema_version": SCHEMA_VERSION,
        "policy_scope": POLICY_SCOPE,
        "inputs": input_manifest,
        "profile": _jsonable(profile),
        "read_only": True,
        "network_calls": 0,
        "model_calls": 0,
        "database_writes": 0,
        "score_calls": 0,
    }
    _prepare_output(output, replay_manifest, resume)

    policy = _load_title_policy()
    catalog = _load_catalog(catalog_file)
    resolver = _catalog_resolver(catalog)
    sources = _load_sources(sources_file, resolver)
    # Register source identifiers after catalog aliases so an exact name can map to the catalog ID.
    for source in sources:
        resolver.register(source.company_id, source.company_name)
    checkpoints, checkpoint_extras = _load_checkpoints(checkpoints_file, sources)
    # Unusable destinations are discovery evidence only.  They must not
    # create a company row, title candidate, retry task, or catalog input.
    # The next source discovery pass will evaluate the upstream link again.
    excluded_unusable_sources = {
        source.source_key
        for source in sources
        if not source.source_url
        or (
            source.source_key in checkpoints
            and checkpoints[source.source_key].status == "unusable"
        )
    }
    sources = [
        source for source in sources if source.source_key not in excluded_unusable_sources
    ]
    hydration, hydration_counts = _load_hydration(hydration_file, resolver, policy)
    catalog_index = _build_catalog_index(catalog, resolver, policy)

    company_states: dict[str, dict[str, Any]] = {}
    for source in sources:
        state = company_states.setdefault(
            source.company_id,
            {
                "company_id": source.company_id,
                "company_name": source.company_name or resolver.name(source.company_id),
                "sources": [],
                "source_statuses": [],
                "list_rows": 0,
                "observed_titles": 0,
                "admitted_titles": 0,
                "existing_skipped": 0,
                "successful_jd_reuse": 0,
                "new_failures": 0,
                "missing_jd": 0,
                "detail_failure_count": 0,
                "detail_pending_count": 0,
                "existing_detail_failure_count": 0,
                "existing_detail_pending_count": 0,
                "existing_repair_success": 0,
                "existing_repair_failure": 0,
                "existing_repair_pending": 0,
                "existing_repair_suppressed": 0,
                "existing_skipped_rows": 0,
                "existing_repair_rows": 0,
                "existing_unrediscovered_failure_count": 0,
                "existing_unrediscovered_pending_count": 0,
            },
        )
        state["sources"].append({"source_key": source.source_key, "source_url": source.source_url, "raw": deepcopy(source.raw)})

    observed_titles: dict[str, set[str]] = defaultdict(set)
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    screening_counts: Counter[str] = Counter()
    source_records: dict[str, dict[str, Any]] = {}
    output_paths = {name: output / filename for name, filename in OUTPUT_FILES.items()}
    observed_writer = _JsonlWriter(output_paths["observed_titles"])

    for source in sources:
        checkpoint = checkpoints.get(source.source_key)
        if checkpoint is None:
            source_status = "unusable" if not source.source_url else "pending"
            source_record = {
                "source_key": source.source_key,
                "company_id": source.company_id,
                "company_name": source.company_name,
                "source_url": source.source_url,
                "status": source_status,
                "list_complete": False,
                "checkpoint": None,
                "raw_source": deepcopy(source.raw),
                "reason": (
                    "No public entry URL was supplied."
                    if source_status == "unusable"
                    else "No list checkpoint was supplied; this source was not attempted."
                ),
                "reason_code": "missing_entry" if source_status == "unusable" else "checkpoint_missing",
                "raw_job_count": 0,
                "observed_title_count": 0,
                "pages_seen": None,
                "total_pages": None,
                "advertised_total": None,
                "source_runs": [],
            }
            source_records[source.source_key] = source_record
            _attach_source_capture(company_states[source.company_id], source.source_key, source_record)
            company_states[source.company_id]["source_statuses"].append(source_status)
            continue

        source_status = "unusable" if not source.source_url else checkpoint.status
        source_record = {
            "source_key": source.source_key,
            "company_id": source.company_id,
            "company_name": source.company_name,
            "source_url": source.source_url,
            "status": source_status,
            "raw_status": checkpoint.raw_status,
            "list_complete": checkpoint.list_complete,
            "pagination_complete": checkpoint.pagination_complete,
            "completeness_known": checkpoint.completeness_known,
            "has_more": checkpoint.has_more,
            "pages_seen": checkpoint.pages_seen,
            "total_pages": checkpoint.total_pages,
            "advertised_total": checkpoint.advertised_total,
            "source_runs": deepcopy(checkpoint.source_runs),
            "checkpoint": str(checkpoint.path),
            "raw_source": deepcopy(source.raw),
            "raw_checkpoint": deepcopy(checkpoint.raw),
            "reason": checkpoint.reason,
            "reason_code": checkpoint.reason_code,
            "raw_job_count": len(checkpoint.jobs),
            "observed_title_count": 0,
        }
        source_records[source.source_key] = source_record
        _attach_source_capture(company_states[source.company_id], source.source_key, source_record)
        company_states[source.company_id]["source_statuses"].append(source_status)
        company_states[source.company_id]["list_rows"] += len(checkpoint.jobs)

        for list_index, raw_job in enumerate(checkpoint.jobs):
            title = _job_title(raw_job)
            title_key = _title_key(policy, title)
            if title_key:
                observed_titles[source.company_id].add(title_key)
            screening, eligible, screening_error = _screen_title(policy, title, profile)
            screening_counts["observed"] += 1
            screening_counts["eligible"] += int(eligible)
            if not eligible:
                screening_counts["excluded"] += 1
                status = _casefold_text(screening.get("analysis_status")) or "excluded"
                screening_counts[f"excluded:{status}"] += 1
            if screening_error:
                screening_counts["errors"] += 1
            observed = {
                "source_key": source.source_key,
                "company_id": source.company_id,
                "company_name": source.company_name,
                "source_url": source.source_url,
                "checkpoint": str(checkpoint.path),
                "list_index": list_index,
                "title": title,
                "title_key": title_key or None,
                "screened_in": eligible,
                "screening_scope": "title_only",
                "screening": screening,
                "screening_error": screening_error,
                "raw_job": deepcopy(raw_job),
            }
            observed_writer.write(observed)
            source_record["observed_title_count"] += int(bool(title_key))
            company_states[source.company_id]["observed_titles"] += int(bool(title_key))
            if not eligible or not title_key:
                continue
            key = _company_title_key(policy, source.company_id, title)
            if key is None:
                continue
            candidate = candidates.setdefault(
                key,
                {
                    "company_id": source.company_id,
                    "company_name": source.company_name,
                    "title": title,
                    "title_key": title_key,
                    "representative_job": deepcopy(raw_job),
                    "observations": [],
                    "source_keys": [],
                    "source_urls": [],
                    "screening": screening,
                },
            )
            candidate["observations"].append(
                {
                    "source_key": source.source_key,
                    "source_url": source.source_url,
                    "checkpoint": str(checkpoint.path),
                    "list_index": list_index,
                    "raw_job": deepcopy(raw_job),
                }
            )
            if source.source_key not in candidate["source_keys"]:
                candidate["source_keys"].append(source.source_key)
            if source.source_url not in candidate["source_urls"]:
                candidate["source_urls"].append(source.source_url)

    for state in company_states.values():
        state["admitted_titles"] = sum(1 for candidate in candidates.values() if candidate["company_id"] == state["company_id"])

    # Keep unresolved historical detail visible for current companies, but do
    # not create a retry candidate unless its title was rediscovered above.
    for existing_key, entries in catalog_index.items():
        if existing_key in candidates or not entries:
            continue
        state = company_states.get(entries[0]["company_id"])
        if state is None:
            continue
        for item in entries:
            capture_state = _existing_capture_state(item["job"], policy)
            if capture_state == "failed":
                state["existing_unrediscovered_failure_count"] += 1
            elif capture_state == "pending":
                state["existing_unrediscovered_pending_count"] += 1

    observed_writer.close()
    result_rows: Counter[str] = Counter()
    deactivation_input_titles = {company_id: set(values) for company_id, values in observed_titles.items()}
    with ExitStack() as stack:
        writers = {
            name: stack.enter_context(_JsonlWriter(path))
            for name, path in output_paths.items()
            if name
            in {
                "filtered_jobs",
                "existing_skipped",
                "existing_repairs",
                "successful_jd_reuse",
                "new_failures",
                "missing_jd",
                "retry_candidates",
                "applications",
            }
        }
        for key, candidate in candidates.items():
            company_id = candidate["company_id"]
            title = candidate["title"]
            title_key = candidate["title_key"]
            existing = catalog_index.get(key, [])
            common = {
                "company_id": company_id,
                "company_name": candidate["company_name"] or resolver.name(company_id),
                "title": title,
                "title_key": title_key,
                "source_keys": list(candidate["source_keys"]),
                "source_urls": list(candidate["source_urls"]),
                "observed_count": len(candidate["observations"]),
                "observations": deepcopy(candidate["observations"]),
                "screening": deepcopy(candidate["screening"]),
            }
            if existing:
                reactivation = any(_job_availability(item["job"]) == "inactive" for item in existing)
                company_state = company_states[company_id]
                retry_flags = [
                    (item, _stored_detail_retry_required(policy, item["job"]))
                    for item in existing
                ]
                retry_entries = [item for item, retry_required in retry_flags if retry_required]
                healthy_entries = [item for item, retry_required in retry_flags if not retry_required]

                # A healthy representative makes a mixed duplicate group a skip;
                # the other stored rows must not trigger a second detail fetch.
                if healthy_entries or not retry_entries:
                    existing_failures = [
                        item for item in existing if _existing_capture_state(item["job"], policy) == "failed"
                    ]
                    existing_pending = [
                        item for item in existing if _existing_capture_state(item["job"], policy) == "pending"
                    ]
                    row = {
                        **common,
                        "disposition": "existing_skipped",
                        "detail_fetch": "skipped",
                        "score_action": "preserve",
                        "rediscovered": reactivation,
                        "proposed_availability_status": "active" if reactivation else None,
                        "existing_job_ids": [item["job_id"] for item in existing],
                        "existing_detail_failure_ids": [item["job_id"] for item in existing_failures],
                        "existing_detail_pending_ids": [item["job_id"] for item in existing_pending],
                        "existing_repair_required_ids": [item["job_id"] for item in retry_entries],
                        "existing_repair_suppressed": bool(retry_entries),
                        "existing_jobs": _existing_jobs_payload(existing),
                    }
                    writers["existing_skipped"].write(row)
                    writers["filtered_jobs"].write(row)
                    result_rows["existing_skipped"] += 1
                    company_state["existing_skipped"] += 1
                    company_state["existing_skipped_rows"] += len(existing)
                    if retry_entries:
                        company_state["existing_repair_suppressed"] += len(retry_entries)
                    continue

                bucket = hydration.get(key)
                successful = bucket.successful if bucket else None
                failed = bucket.failed if bucket else None
                pending = bucket.pending if bucket else None
                existing_jobs = _existing_jobs_payload(existing)
                existing_ids = [item["job_id"] for item in existing]
                repair_ids = [item["job_id"] for item in retry_entries]

                if successful is not None:
                    hydration_job = _hydration_job(successful.raw)
                    detail = _effective_jd(successful.raw, hydration_job)
                    evidence = _capture_evidence(successful.raw, hydration_job)
                    score_valid, preserved_score, score_source = _existing_score_info(existing[0])
                    captured_url = _job_detail_url(hydration_job) or _text(
                        _first(successful.raw, ("detail_url", "detailUrl", "jd_url", "jdUrl"))
                    )
                    repaired_job = _repaired_existing_job(
                        existing[0]["job"],
                        company_id,
                        title,
                        title_key,
                        jd_raw=detail,
                        capture_evidence=evidence,
                        detail_url=captured_url or _job_detail_url(existing[0]["job"]),
                    )
                    repaired_job["match_score"] = preserved_score if score_valid else None
                    row = {
                        **common,
                        "disposition": "existing_repair_success",
                        "capture_status": "complete",
                        "availability_status": "active",
                        "detail_url": captured_url or _job_detail_url(existing[0]["job"]),
                        "detail_fetch": "reused",
                        "score_action": "preserve" if score_valid else "needs_scoring",
                        "needs_scoring": not score_valid,
                        "score_preserved": score_valid,
                        "score_source": score_source,
                        "analysis_preserved": True,
                        "preserved_analysis": deepcopy(existing[0]["analysis"]),
                        "imported": False,
                        "rediscovered": True,
                        "existing_job_ids": existing_ids,
                        "existing_repair_job_ids": repair_ids,
                        "existing_jobs": existing_jobs,
                        "repaired_job": repaired_job,
                        "hydration_source": deepcopy(successful.raw),
                        "hydration_match_count": bucket.total if bucket else 1,
                        "reuse_line_number": successful.line_number,
                        "capture_binding": _jsonable(
                            _capture_binding(successful.raw, hydration_job, title, policy)[1]
                        ),
                    }
                    writers["existing_repairs"].write(row)
                    writers["filtered_jobs"].write(row)
                    result_rows["existing_repairs"] += 1
                    result_rows["existing_repair_success"] += 1
                    company_state["existing_repair_success"] += 1
                    company_state["existing_repair_rows"] += len(retry_entries)
                    continue

                if failed is not None:
                    failure_job = _hydration_job(failed.raw)
                    failure_reason = failed.reason
                    failure_binding = _capture_binding(failed.raw, failure_job, title, policy)[1]
                    row = {
                        **common,
                        "disposition": "existing_repair_failure",
                        "capture_status": "failed",
                        "capture_failure_reason": failure_reason,
                        "availability_status": "active",
                        "detail_url": _job_detail_url(existing[0]["job"]),
                        "detail_fetch": "attempted_failed",
                        "attempted": failed.attempted,
                        "failure_vs_unattempted": "failed",
                        "score_action": "preserve",
                        "imported": False,
                        "rediscovered": True,
                        "existing_job_ids": existing_ids,
                        "existing_repair_job_ids": repair_ids,
                        "existing_jobs": existing_jobs,
                        "repair_target_job": deepcopy(existing[0]["job"]),
                        "hydration_source": deepcopy(failed.raw),
                        "hydration_match_count": bucket.total if bucket else 1,
                        "hydration_line_number": failed.line_number,
                        "capture_binding": _jsonable(failure_binding),
                    }
                    writers["existing_repairs"].write(row)
                    writers["filtered_jobs"].write(row)
                    writers["retry_candidates"].write(
                        {
                            **common,
                            "candidate_type": "existing_detail_failure",
                            "failure_vs_unattempted": "failed",
                            "existing_job_ids": existing_ids,
                            "existing_repair_job_ids": repair_ids,
                            "existing_jobs": existing_jobs,
                            "reason": failure_reason,
                            "executed": False,
                            "requires_explicit_repair": True,
                            "retry_action": "explicit_existing_detail_repair",
                        }
                    )
                    result_rows["existing_repairs"] += 1
                    result_rows["existing_repair_failure"] += 1
                    result_rows["retry_candidates"] += 1
                    company_state["existing_repair_failure"] += 1
                    company_state["existing_repair_rows"] += len(retry_entries)
                    company_state["existing_detail_failure_count"] += len(retry_entries)
                    continue

                pending_attempted = pending.attempted if pending is not None else False
                pending_state = "pending" if pending_attempted else "unattempted"
                pending_reason = (
                    pending.reason
                    if pending is not None
                    else "No hydration record exists for this existing title; detail was not attempted."
                )
                row = {
                    **common,
                    "disposition": "existing_repair_pending",
                    "capture_status": "pending",
                    "capture_failure_reason": None,
                    "availability_status": "active",
                    "detail_url": _job_detail_url(existing[0]["job"]),
                    "detail_fetch": "pending" if pending_attempted else "not_attempted",
                    "attempted": pending_attempted,
                    "failure_vs_unattempted": pending_state,
                    "score_action": "preserve",
                    "imported": False,
                    "rediscovered": True,
                    "pending_reason": pending_reason,
                    "existing_job_ids": existing_ids,
                    "existing_repair_job_ids": repair_ids,
                    "existing_jobs": existing_jobs,
                    "repair_target_job": deepcopy(existing[0]["job"]),
                    "hydration_source": deepcopy(pending.raw) if pending is not None else None,
                    "hydration_match_count": bucket.total if bucket else 0,
                }
                writers["existing_repairs"].write(row)
                writers["filtered_jobs"].write(row)
                writers["retry_candidates"].write(
                    {
                        **common,
                        "candidate_type": "existing_detail_pending" if pending_attempted else "existing_detail_missing",
                        "failure_vs_unattempted": pending_state,
                        "existing_job_ids": existing_ids,
                        "existing_repair_job_ids": repair_ids,
                        "existing_jobs": existing_jobs,
                        "reason": pending_reason,
                        "executed": False,
                        "requires_explicit_repair": True,
                        "retry_action": (
                            "explicit_existing_detail_continuation"
                            if pending_attempted
                            else "explicit_existing_detail_capture"
                        ),
                    }
                )
                result_rows["existing_repairs"] += 1
                result_rows["existing_repair_pending"] += 1
                result_rows["retry_candidates"] += 1
                company_state["existing_repair_pending"] += 1
                company_state["existing_repair_rows"] += len(retry_entries)
                company_state["existing_detail_pending_count"] += len(retry_entries)
                continue

            bucket = hydration.get(key)
            successful = bucket.successful if bucket else None
            failed = bucket.failed if bucket else None
            pending = bucket.pending if bucket else None
            if successful is not None:
                hydration_job = _hydration_job(successful.raw)
                detail = _effective_jd(successful.raw, hydration_job)
                evidence = _capture_evidence(successful.raw, hydration_job)
                captured_url = _job_detail_url(hydration_job) or _text(
                    _first(successful.raw, ("detail_url", "detailUrl", "jd_url", "jdUrl"))
                )
                proposed = _proposed_job(
                    candidate["representative_job"],
                    company_id,
                    title,
                    title_key,
                    capture_status="complete",
                    jd_raw=detail,
                    capture_evidence=evidence,
                    detail_url=captured_url or _job_detail_url(candidate["representative_job"]),
                )
                row = {
                    **common,
                    "disposition": "successful_jd_reuse",
                    "capture_status": "complete",
                    "availability_status": "active",
                    "detail_url": captured_url or _job_detail_url(candidate["representative_job"]),
                    "score_action": "not_scored",
                    "imported": False,
                    "proposed_job": proposed,
                    "hydration_source": deepcopy(successful.raw),
                    "hydration_match_count": bucket.total if bucket else 1,
                    "reuse_line_number": successful.line_number,
                    "capture_binding": _jsonable(_capture_binding(successful.raw, hydration_job, title, policy)[1]),
                }
                writers["successful_jd_reuse"].write(row)
                writers["filtered_jobs"].write(row)
                result_rows["successful_jd_reuse"] += 1
                company_states[company_id]["successful_jd_reuse"] += 1
                continue

            if failed is not None:
                failure_job = _hydration_job(failed.raw)
                failure_reason = failed.reason
                failure_binding = _capture_binding(failed.raw, failure_job, title, policy)[1]
                placeholder = _proposed_job(
                    candidate["representative_job"],
                    company_id,
                    title,
                    title_key,
                    capture_status="failed",
                    capture_failure_reason=failure_reason,
                    jd_raw=None,
                    detail_url=_job_detail_url(candidate["representative_job"]),
                )
                row = {
                    **common,
                    "disposition": "new_failure_placeholder",
                    "capture_status": "failed",
                    "capture_failure_reason": failure_reason,
                    "availability_status": "active",
                    "detail_url": _job_detail_url(candidate["representative_job"]),
                    "attempted": failed.attempted,
                    "score_action": "not_scored",
                    "imported": False,
                    "placeholder": placeholder,
                    "hydration_source": deepcopy(failed.raw),
                    "hydration_match_count": bucket.total if bucket else 1,
                    "hydration_line_number": failed.line_number,
                    "historical_job": deepcopy(failure_job),
                    "capture_binding": _jsonable(failure_binding),
                }
                writers["new_failures"].write(row)
                writers["filtered_jobs"].write(row)
                writers["retry_candidates"].write(
                    {
                        **common,
                        "candidate_type": "detail_failure",
                        "failure_vs_unattempted": "failed",
                        "attempted": failed.attempted,
                        "reason": failure_reason,
                        "executed": False,
                        "requires_explicit_repair": True,
                        "retry_action": "manual_or_explicit_detail_repair",
                    }
                )
                result_rows["new_failures"] += 1
                result_rows["retry_candidates"] += 1
                company_states[company_id]["new_failures"] += 1
                company_states[company_id]["detail_failure_count"] += 1
                continue

            pending_attempted = pending.attempted if pending is not None else False
            pending_state = "pending" if pending_attempted else "unattempted"
            pending_reason = (
                pending.reason
                if pending is not None
                else "No hydration record exists for this title; detail was not attempted."
            )
            pending_job = _proposed_job(
                candidate["representative_job"],
                company_id,
                title,
                title_key,
                capture_status="pending",
                detail_url=_job_detail_url(candidate["representative_job"]),
            )
            row = {
                **common,
                "disposition": "missing_jd_waiting",
                "capture_status": "pending",
                "capture_failure_reason": None,
                "availability_status": "active",
                "detail_url": _job_detail_url(candidate["representative_job"]),
                "attempted": pending_attempted,
                "failure_vs_unattempted": pending_state,
                "score_action": "not_scored",
                "imported": False,
                "pending_reason": pending_reason,
                "pending_job": pending_job,
                "hydration_source": deepcopy(pending.raw) if pending is not None else None,
                "hydration_match_count": bucket.total if bucket else 0,
            }
            writers["missing_jd"].write(row)
            writers["filtered_jobs"].write(row)
            writers["retry_candidates"].write(
                {
                    **common,
                    "candidate_type": "detail_pending" if pending_attempted else "detail_missing",
                    "failure_vs_unattempted": pending_state,
                    "reason": pending_reason,
                    "executed": False,
                    "requires_explicit_repair": True,
                    "retry_action": (
                        "manual_or_explicit_detail_continuation"
                        if pending_attempted
                        else "manual_or_explicit_detail_capture"
                    ),
                }
            )
            result_rows["missing_jd"] += 1
            result_rows["retry_candidates"] += 1
            company_states[company_id]["missing_jd"] += 1
            company_states[company_id]["detail_pending_count"] += 1

        for source in sources:
            source_record = source_records[source.source_key]
            if source_record["status"] != "complete":
                writers["retry_candidates"].write(
                    {
                        "candidate_type": _source_retry_kind(source_record["status"], checkpoints.get(source.source_key)),
                        "failure_vs_unattempted": (
                            "failed"
                            if source_record["status"] == "failed"
                            else "unattempted"
                            if source_record["status"] in {"pending", "running"}
                            else "source_incomplete"
                        ),
                        "company_id": source.company_id,
                        "company_name": source.company_name,
                        "source_key": source.source_key,
                        "source_url": source.source_url,
                        "reason": source_record["reason"],
                        "reason_code": source_record.get("reason_code"),
                        "executed": False,
                        "requires_explicit_repair": True,
                        "retry_action": "manual_or_explicit_source_repair",
                    }
                )
                result_rows["retry_candidates"] += 1

        for application in catalog.applications:
            writers["applications"].write(deepcopy(application))

    deactivation = _deactivation_plan(
        sources,
        checkpoints,
        company_states,
        catalog_index,
        deactivation_input_titles,
        policy,
        resolver,
    )
    _write_json(output_paths["deactivation_plan"], deactivation)

    company_rows: list[dict[str, Any]] = []
    for state in company_states.values():
        list_status = _company_list_status(state["source_statuses"])
        unresolved_failures = (
            state["detail_failure_count"]
            + state["existing_detail_failure_count"]
            + state["existing_unrediscovered_failure_count"]
        )
        unresolved_pending = (
            state["detail_pending_count"]
            + state["existing_detail_pending_count"]
            + state["existing_unrediscovered_pending_count"]
        )
        final_status = "partial" if unresolved_failures else list_status
        if unresolved_failures == 0 and unresolved_pending and final_status == "complete":
            final_status = "partial"
        source_status_counts = dict(Counter(state["source_statuses"]))
        company_row = {
            "company_id": state["company_id"],
            "company_name": state["company_name"],
            "status": final_status,
            "list_status": list_status,
            "detail_status": "failed"
            if unresolved_failures
            else "pending"
            if unresolved_pending
            else "complete",
            "source_count": len(state["sources"]),
            "source_status_counts": source_status_counts,
            "sources": deepcopy(state["sources"]),
            "list_rows": state["list_rows"],
            "observed_titles": state["observed_titles"],
            "admitted_titles": state["admitted_titles"],
            "existing_skipped": state["existing_skipped"],
            "successful_jd_reuse": state["successful_jd_reuse"],
            "new_failures": state["new_failures"],
            "missing_jd": state["missing_jd"],
            "detail_failure_count": state["detail_failure_count"],
            "detail_pending_count": state["detail_pending_count"],
            "existing_detail_failure_count": state["existing_detail_failure_count"],
            "existing_detail_pending_count": state["existing_detail_pending_count"],
            "existing_repair_success": state["existing_repair_success"],
            "existing_repair_failure": state["existing_repair_failure"],
            "existing_repair_pending": state["existing_repair_pending"],
            "existing_repair_total": (
                state["existing_repair_success"]
                + state["existing_repair_failure"]
                + state["existing_repair_pending"]
            ),
            "existing_repair_suppressed": state["existing_repair_suppressed"],
            "existing_skipped_rows": state["existing_skipped_rows"],
            "existing_repair_rows": state["existing_repair_rows"],
            "existing_unrediscovered_failure_count": state["existing_unrediscovered_failure_count"],
            "existing_unrediscovered_pending_count": state["existing_unrediscovered_pending_count"],
            "unresolved_detail_failure_count": unresolved_failures,
            "unresolved_detail_pending_count": unresolved_pending,
            "retry_required": bool(
                final_status != "complete" or unresolved_failures or unresolved_pending
            ),
            "historical_cohort_fields_preserved": True,
        }
        company_rows.append(company_row)

    with _JsonlWriter(output_paths["company_status"]) as writer:
        for row in company_rows:
            writer.write(row)

    application_preservation = _application_preservation(catalog)
    _write_json(output_paths["application_preservation"], application_preservation)

    title_group_partition = {
        "existing_skipped": result_rows.get("existing_skipped", 0),
        "existing_repair_total": result_rows.get("existing_repairs", 0),
        "new_successful_jd_reuse": result_rows.get("successful_jd_reuse", 0),
        "new_failure_placeholders": result_rows.get("new_failures", 0),
        "new_missing_jd_waiting": result_rows.get("missing_jd", 0),
    }
    title_group_partition_total = sum(title_group_partition.values())

    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "policy_scope": POLICY_SCOPE,
        "read_only": True,
        "network_calls": 0,
        "model_calls": 0,
        "database_writes": 0,
        "score_calls": 0,
        "imported_rows": 0,
        "formal_deactivation_writes": 0,
        "resume": {
            "requested": resume,
            "retested": 0,
            "failures_preserved": bool(resume),
            "previous_failures_are_not_successes": True,
        },
        "inputs": {
            "sources": str(sources_file),
            "checkpoints": str(checkpoints_file),
            "hydration_jobs": str(hydration_file) if hydration_file else None,
            "catalog": str(catalog_file),
            "checkpoint_files": len(_checkpoint_files(checkpoints_file)),
            "checkpoint_extras": checkpoint_extras,
        },
        "counts": {
            "companies": len(company_rows),
            "source_records": len(sources),
            "sources_with_checkpoints": len(checkpoints),
            "list_rows_observed": screening_counts.get("observed", 0),
            "title_screened_in": screening_counts.get("eligible", 0),
            "title_screened_out": screening_counts.get("excluded", 0),
            "screening_errors": screening_counts.get("errors", 0),
            "title_groups_after_dedupe": len(candidates),
            "title_groups": len(candidates),
            "title_groups_partition_total": title_group_partition_total,
            "title_groups_partition_valid": title_group_partition_total == len(candidates),
            "deduped_duplicate_rows": max(screening_counts.get("eligible", 0) - len(candidates), 0),
            "existing_skipped": result_rows.get("existing_skipped", 0),
            "existing_skipped_rows": sum(row["existing_skipped_rows"] for row in company_rows),
            "existing_repair_total": result_rows.get("existing_repairs", 0),
            "existing_repair_rows": sum(row["existing_repair_rows"] for row in company_rows),
            "existing_repair_success": result_rows.get("existing_repair_success", 0),
            "existing_repair_failure": result_rows.get("existing_repair_failure", 0),
            "existing_repair_pending": result_rows.get("existing_repair_pending", 0),
            "successful_jd_reuse": result_rows.get("successful_jd_reuse", 0),
            "new_failure_placeholders": result_rows.get("new_failures", 0),
            "missing_jd_waiting": result_rows.get("missing_jd", 0),
            "retry_candidates": result_rows.get("retry_candidates", 0),
            "existing_detail_failures": sum(
                row["existing_detail_failure_count"] for row in company_rows
            ),
            "existing_detail_pending": sum(
                row["existing_detail_pending_count"] for row in company_rows
            ),
            "existing_unrediscovered_failures": sum(
                row["existing_unrediscovered_failure_count"] for row in company_rows
            ),
            "existing_unrediscovered_pending": sum(
                row["existing_unrediscovered_pending_count"] for row in company_rows
            ),
            "unresolved_detail_failures": sum(
                row["unresolved_detail_failure_count"] for row in company_rows
            ),
            "unresolved_detail_pending": sum(
                row["unresolved_detail_pending_count"] for row in company_rows
            ),
            "deactivation_would_mark_inactive": deactivation["counts"]["would_mark_inactive"],
            "deactivation_would_restore_active": deactivation["counts"]["would_restore_active"],
        },
        "screening_exclusion_counts": {
            key.split(":", 1)[1]: value
            for key, value in screening_counts.items()
            if key.startswith("excluded:")
        },
        "hydration": hydration_counts,
        "catalog": {
            "jobs": len(catalog.jobs),
            "analyses": len(catalog.analyses),
            "applications": len(catalog.applications),
            "application_preservation": application_preservation,
        },
        "company_statuses": dict(Counter(row["status"] for row in company_rows)),
        "title_group_partition": title_group_partition,
        "deactivation": {
            "mode": deactivation["mode"],
            "apply_allowed": False,
            "blocked_companies": deactivation["counts"]["blocked_companies"],
        },
        "files": {name: str(path) for name, path in output_paths.items()},
    }
    _write_json(output_paths["summary"], summary)
    _write_json(output_paths["manifest"], replay_manifest)
    return summary


def replay_title_first_capture(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Descriptive alias used by callers that prefer the operation name."""

    return replay(*args, **kwargs)


run = replay


__all__ = [
    "OUTPUT_FILES",
    "POLICY_SCOPE",
    "SCHEMA_VERSION",
    "replay",
    "replay_title_first_capture",
    "run",
]
