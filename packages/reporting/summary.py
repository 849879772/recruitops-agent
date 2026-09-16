"""Deterministic summaries for the Agent daily pipeline and crawler health.

This module only consumes structured values and read-only repository methods.
It never imports crawler implementations, the legacy project, an HTTP client,
or a model client.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
import json
from typing import Any


REPORT_SCHEMA_VERSION = "recruitops.reporting.v1"
DEFAULT_QUANTITY_CHANGE_THRESHOLD = 0.5
HEALTH_ISSUE_TYPES = (
    "crawler_failed",
    "zero_results",
    "pagination_incomplete",
    "quantity_change",
)

_COUNT_FIELDS = (
    "raw_job_count",
    "raw_count",
    "observed_job_count",
    "observed_count",
    "job_count",
    "jobs_count",
    "total_jobs",
    "accepted_job_count",
    "accepted_count",
    "count",
)
_FAILURE_STATUSES = {"failed", "failure", "error", "timed_out", "timeout"}
_CRAWLER_FAILURE_REASONS = {
    "crawler_failed",
    "crawler_audit_failed",
    "fetch_failed",
    "render_failed",
    "source_unavailable",
}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        converted = asdict(value)
        return converted if isinstance(converted, Mapping) else {}
    for method_name in ("to_dict", "model_dump", "dict"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        try:
            converted = method()
        except TypeError:
            try:
                converted = method(mode="json")
            except (TypeError, ValueError):
                continue
        if isinstance(converted, Mapping):
            return converted
    return {}


def _result_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, (list, tuple)):
        return {}
    result = _mapping(value)
    nested = _mapping(result.get("result")) if "result" in result else {}
    if nested and not any(key in result for key in ("companies", "company_results")):
        return nested
    return result


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.casefold().strip()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _first_int(row: Mapping[str, Any], fields: Sequence[str]) -> tuple[int | None, str | None]:
    for field in fields:
        if field not in row:
            continue
        value = _as_int(row.get(field))
        if value is not None:
            return value, field
    return None, None


def _count(row: Mapping[str, Any]) -> tuple[int | None, str | None]:
    value, field = _first_int(row, _COUNT_FIELDS)
    if value is not None:
        return value, field

    parts = []
    for field in ("new_count", "changed_count", "reused_count"):
        if field in row:
            parsed = _as_int(row.get(field))
            if parsed is not None:
                parts.append(parsed)
    if parts:
        return sum(parts), "new_changed_reused"
    return None, None


def _company_rows(value: Any) -> tuple[list[dict[str, Any]], bool]:
    if isinstance(value, (list, tuple)):
        raw_rows = value
        has_rows_field = True
    else:
        result = _result_mapping(value)
        if "companies" in result:
            raw_rows = result.get("companies")
            has_rows_field = True
        elif "company_results" in result:
            raw_rows = result.get("company_results")
            has_rows_field = True
        else:
            return [], False

    if isinstance(raw_rows, Mapping):
        if any(key in raw_rows for key in ("company_id", "id", "company", "name")):
            raw_rows = [raw_rows]
        else:
            raw_rows = [
                {"company_id": str(key), **(_mapping(item) or {})}
                for key, item in raw_rows.items()
            ]
    if not isinstance(raw_rows, Sequence) or isinstance(raw_rows, (str, bytes, bytearray)):
        return [], has_rows_field
    rows = [dict(_mapping(item)) for item in raw_rows if _mapping(item)]
    return rows, has_rows_field


def _company_identity(row: Mapping[str, Any], index: int = 0) -> tuple[str, str]:
    company_id = _text(
        row.get("company_id") or row.get("id") or row.get("key") or row.get("company")
    )
    company_name = _text(row.get("company_name") or row.get("name") or row.get("company"))
    return company_id or company_name or f"company-{index}", company_name or company_id


def _catalog_rows(companies: Sequence[Any] | None) -> list[dict[str, Any]]:
    if companies is None:
        return []
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(companies):
        raw = dict(_mapping(item))
        if not raw:
            continue
        company_id, company_name = _company_identity(raw, index)
        raw.setdefault("company_id", company_id)
        raw.setdefault("company_name", company_name)
        rows.append(raw)
    return rows


def _lookup(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    by_id: dict[str, Mapping[str, Any]] = {}
    by_name: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        company_id, company_name = _company_identity(row, index)
        if company_id:
            by_id[company_id] = row
        if company_name:
            by_name[company_name.casefold()] = row
    return by_id, by_name


def _enrich_rows(
    rows: Sequence[Mapping[str, Any]],
    catalog: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    catalog_by_id, catalog_by_name = _lookup(catalog)
    enriched: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        row = dict(item)
        company_id, company_name = _company_identity(row, index)
        match = catalog_by_id.get(company_id) or catalog_by_name.get(company_name.casefold())
        if match:
            catalog_id, catalog_name = _company_identity(match, index)
            row.setdefault("company_id", catalog_id)
            row.setdefault("company_name", catalog_name)
            if "integration_status" in match:
                row.setdefault("integration_status", match.get("integration_status"))
        row.setdefault("company_id", company_id)
        row.setdefault("company_name", company_name)
        enriched.append(row)
    return enriched


def _sorted_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    counts: dict[str, int] = {}
    for key, raw in value.items():
        parsed = _as_int(raw)
        if parsed is not None:
            counts[str(key)] = parsed
    return {key: counts[key] for key in sorted(counts)}


def _metric(mapping: Mapping[str, Any], *fields: str) -> int | None:
    value, _field = _first_int(mapping, fields)
    return value


def _company_summary(row: Mapping[str, Any], index: int = 0) -> dict[str, Any]:
    company_id, company_name = _company_identity(row, index)
    fields = (
        "raw_job_count",
        "accepted_job_count",
        "new_count",
        "changed_count",
        "reused_count",
        "rejected_count",
        "failed_count",
        "filtered_count",
    )
    result: dict[str, Any] = {
        "company_id": company_id,
        "company_name": company_name,
        "status": _text(row.get("status")) or "unknown",
    }
    for field in fields:
        result[field] = _as_int(row.get(field)) if field in row else None
    result.update(
        {
            "failure_reason": _text(row.get("failure_reason")) or None,
            "run_reason": _text(row.get("run_reason")) or None,
            "rejection_reasons": _sorted_counts(row.get("rejection_reasons")),
            "filtered_reasons": _sorted_counts(row.get("filtered_reasons")),
            "crawl_evidence": dict(row.get("crawl_evidence") or {}),
            "jd_results": list(row.get("jd_results") or []),
        }
    )
    return result


def daily_pipeline_summary(pipeline_result: Any = None) -> dict[str, Any]:
    """Return a JSON-safe summary of one structured daily pipeline result."""

    result = _result_mapping(pipeline_result)
    rows, has_rows_field = _company_rows(pipeline_result)
    if not result and not rows:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_type": "daily_pipeline_summary",
            "available": False,
            "source": "none",
            "status": "not_available",
            "counts": {},
            "companies": [],
            "failure_reasons": {},
            "rejection_reasons": {},
        }

    failed_companies = _metric(result, "failed_companies", "failed_company_count")
    failed_jobs = _metric(result, "failed_jobs", "failed_job_count")
    failed = _metric(result, "failed", "failed_count")
    if failed is None and failed_companies is not None and failed_jobs is not None:
        failed = failed_companies + failed_jobs
    counts = {
        "total_companies": _metric(result, "total_companies") or len(rows),
        "selected_companies": _metric(result, "selected_companies"),
        "crawled_companies": _metric(result, "crawled_companies"),
        "new": _metric(result, "new", "new_count"),
        "changed": _metric(result, "changed", "changed_count"),
        "reused": _metric(result, "reused", "reused_count"),
        "rejected": _metric(result, "rejected", "rejected_count"),
        "failed": failed,
        "failed_companies": failed_companies,
        "failed_jobs": failed_jobs,
        "filtered": _metric(result, "filtered", "filtered_count"),
    }
    status = _text(result.get("status"))
    if not status:
        status = "dry_run" if result.get("dry_run") is True else "completed"
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "daily_pipeline_summary",
        "available": True,
        "source": "daily_pipeline_result",
        "status": status,
        "dry_run": bool(result.get("dry_run", False)),
        "written": bool(result.get("written", False)),
        "counts": counts,
        "skipped_companies": sorted(
            _text(item) for item in (result.get("skipped_companies") or []) if _text(item)
        ),
        "new_job_ids": sorted(
            _text(item) for item in (result.get("new_job_ids") or []) if _text(item)
        ),
        "changed_job_ids": sorted(
            _text(item) for item in (result.get("changed_job_ids") or []) if _text(item)
        ),
        "reused_job_ids": sorted(
            _text(item) for item in (result.get("reused_job_ids") or []) if _text(item)
        ),
        "rejected_job_ids": sorted(
            _text(item) for item in (result.get("rejected_job_ids") or []) if _text(item)
        ),
        "failed_job_ids": sorted(
            _text(item) for item in (result.get("failed_job_ids") or []) if _text(item)
        ),
        "failure_reasons": _sorted_counts(result.get("failure_reasons")),
        "rejection_reasons": _sorted_counts(result.get("rejection_reasons")),
        "companies": [
            _company_summary(row, index)
            for index, row in sorted(
                enumerate(rows),
                key=lambda item: _company_identity(item[1], item[0])[0],
            )
        ]
        if has_rows_field
        else [],
    }


def _baseline_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        if "counts" in value and not any(
            key in value for key in ("companies", "company_results")
        ):
            value = value.get("counts")
        if isinstance(value, Mapping) and not any(
            key in value for key in ("company_id", "id", "company", "name")
        ):
            rows: list[dict[str, Any]] = []
            for key, item in value.items():
                if isinstance(item, Mapping):
                    row = dict(item)
                    row.setdefault("company_id", str(key))
                else:
                    row = {"company_id": str(key), "observed_count": item}
                rows.append(row)
            return rows
    rows, _has_rows_field = _company_rows(value)
    return rows


def _persisted_count(repository: Any, company_id: str) -> tuple[int | None, str | None]:
    search_jobs = getattr(repository, "search_jobs", None)
    if not callable(search_jobs):
        return None, "search_jobs_unavailable"
    try:
        page = search_jobs(company=company_id, limit=1, offset=0)
    except Exception as exc:  # A report should preserve other companies' observations.
        return None, type(exc).__name__
    total = page.get("total") if isinstance(page, Mapping) else getattr(page, "total", None)
    parsed = _as_int(total)
    return parsed, None if parsed is not None else "total_unavailable"


def _failure_reason(row: Mapping[str, Any]) -> str:
    return _text(row.get("failure_reason") or row.get("error_code")).casefold()


def _crawler_failed(row: Mapping[str, Any]) -> bool:
    status = _text(row.get("status")).casefold()
    reason = _failure_reason(row)
    return (
        status in _FAILURE_STATUSES
        or reason in _CRAWLER_FAILURE_REASONS
        or ("crawler" in reason and ("fail" in reason or "error" in reason))
    )


def _pagination_incomplete(row: Mapping[str, Any]) -> bool:
    reason = _failure_reason(row)
    if reason == "pagination_incomplete" or "pagination_incomplete" in reason:
        return True
    complete = _as_bool(row.get("pagination_complete"))
    if complete is False:
        return True
    if _as_bool(row.get("has_more")) is True:
        return True
    pages_seen = _as_int(row.get("pages_seen"))
    total_pages = _as_int(row.get("total_pages"))
    return pages_seen is not None and total_pages is not None and pages_seen < total_pages


def _zero_results(row: Mapping[str, Any], count: int | None, *, crawler_failed: bool) -> bool:
    reason = _failure_reason(row)
    if reason == "no_results":
        return True
    if crawler_failed:
        return False
    return count == 0


def _issue(
    company_id: str,
    company_name: str,
    issue_type: str,
    severity: str,
    details: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "company_id": company_id,
        "company_name": company_name,
        "type": issue_type,
        "severity": severity,
        "details": dict(details),
    }


def crawler_health_reconciliation(
    current_result: Any = None,
    previous_result: Any = None,
    *,
    repository: Any = None,
    baseline: Mapping[str, Any] | Sequence[Any] | None = None,
    companies: Sequence[Any] | None = None,
    quantity_change_threshold: float = DEFAULT_QUANTITY_CHANGE_THRESHOLD,
    minimum_count_delta: int = 1,
) -> dict[str, Any]:
    """Reconcile crawler observations without running crawlers or writing state.

    ``previous_result`` or ``baseline`` is optional. Without either one the
    report still detects failures, empty observations, and incomplete pages,
    while explicitly marking quantity comparison as unavailable.
    """

    if quantity_change_threshold <= 0:
        raise ValueError("quantity_change_threshold must be positive")
    if minimum_count_delta < 0:
        raise ValueError("minimum_count_delta cannot be negative")

    if companies is None and repository is not None:
        list_companies = getattr(repository, "list_companies", None)
        if callable(list_companies):
            companies = list_companies()
    catalog = _catalog_rows(companies)
    current_rows, has_rows_field = _company_rows(current_result)
    if has_rows_field:
        observed_rows = _enrich_rows(current_rows, catalog)
        observation_source = "daily_pipeline_result"
        query_errors: list[dict[str, str]] = []
    else:
        observed_rows = _enrich_rows(catalog, catalog)
        observation_source = "agent_postgres"
        query_errors = []
        if repository is not None:
            aggregate_counts = getattr(repository, "job_counts_by_company", None)
            counts_by_company: Mapping[str, Any] | None = None
            if callable(aggregate_counts):
                try:
                    counts_by_company = aggregate_counts()
                except Exception as exc:
                    query_errors.append({"company_id": "*", "error": type(exc).__name__})
            for row in observed_rows:
                company_id, _company_name = _company_identity(row)
                if counts_by_company is not None:
                    row["observed_count"] = _as_int(counts_by_company.get(company_id)) or 0
                    row["observed_count_source"] = "persisted_job_snapshots"
                    continue
                count, error = _persisted_count(repository, company_id)
                if count is not None:
                    row["observed_count"] = count
                    row["observed_count_source"] = "persisted_job_snapshots"
                if error:
                    query_errors.append({"company_id": company_id, "error": error})

    if baseline is not None:
        previous_rows = _baseline_rows(baseline)
        baseline_source = "explicit_baseline"
    elif previous_result is not None:
        previous_rows, _has_previous_rows = _company_rows(previous_result)
        baseline_source = "previous_pipeline_result"
    else:
        previous_rows = []
        baseline_source = "none"
    previous_by_id, previous_by_name = _lookup(previous_rows)

    issues: list[dict[str, Any]] = []
    company_reports: list[dict[str, Any]] = []
    issue_counts = {issue_type: 0 for issue_type in HEALTH_ISSUE_TYPES}
    for index, raw_row in enumerate(observed_rows):
        row = dict(raw_row)
        company_id, company_name = _company_identity(row, index)
        status = _text(row.get("status")) or (
            "observed" if observation_source == "agent_postgres" else "unknown"
        )
        integration_status = _text(row.get("integration_status"))
        count, count_field = _count(row)
        crawler_failed = _crawler_failed(row)
        pagination_incomplete = _pagination_incomplete(row)
        zero_results = _zero_results(row, count, crawler_failed=crawler_failed)
        expected_to_crawl = integration_status.casefold() in {"", "connected"}
        company_issue_types: list[str] = []

        if expected_to_crawl and crawler_failed:
            issue = _issue(
                company_id,
                company_name,
                "crawler_failed",
                "error",
                {
                    "status": status,
                    "failure_reason": _failure_reason(row) or None,
                },
            )
            issues.append(issue)
            company_issue_types.append("crawler_failed")
            issue_counts["crawler_failed"] += 1
        if expected_to_crawl and zero_results:
            issue = _issue(
                company_id,
                company_name,
                "zero_results",
                "warning",
                {
                    "count": count,
                    "count_field": count_field,
                    "failure_reason": _failure_reason(row) or None,
                },
            )
            issues.append(issue)
            company_issue_types.append("zero_results")
            issue_counts["zero_results"] += 1
        if expected_to_crawl and pagination_incomplete:
            issue = _issue(
                company_id,
                company_name,
                "pagination_incomplete",
                "error",
                {
                    "pages_seen": _as_int(row.get("pages_seen")),
                    "total_pages": _as_int(row.get("total_pages")),
                    "has_more": _as_bool(row.get("has_more")),
                    "failure_reason": _failure_reason(row) or None,
                },
            )
            issues.append(issue)
            company_issue_types.append("pagination_incomplete")
            issue_counts["pagination_incomplete"] += 1

        previous = previous_by_id.get(company_id) or previous_by_name.get(company_name.casefold())
        previous_count, previous_count_field = _count(previous or {})
        delta = None if count is None or previous_count is None else count - previous_count
        relative_change = None
        if delta is not None and previous_count and previous_count > 0:
            relative_change = abs(delta) / previous_count
        quantity_changed = (
            expected_to_crawl
            and count is not None
            and previous_count is not None
            and not crawler_failed
            and not pagination_incomplete
            and abs(delta or 0) >= minimum_count_delta
            and (
                (previous_count == 0 and count != 0)
                or (previous_count > 0 and (relative_change or 0) >= quantity_change_threshold)
            )
        )
        if quantity_changed:
            details = {
                "current_count": count,
                "current_count_field": count_field,
                "baseline_count": previous_count,
                "baseline_count_field": previous_count_field,
                "delta": delta,
                "relative_change": relative_change,
                "threshold": quantity_change_threshold,
                "direction": "increase" if (delta or 0) > 0 else "decrease",
            }
            issue = _issue(
                company_id,
                company_name,
                "quantity_change",
                "warning",
                details,
            )
            issues.append(issue)
            company_issue_types.append("quantity_change")
            issue_counts["quantity_change"] += 1

        company_reports.append(
            {
                "company_id": company_id,
                "company_name": company_name,
                "integration_status": integration_status or None,
                "status": status,
                "raw_job_count": _as_int(row.get("raw_job_count")),
                "accepted_job_count": _as_int(row.get("accepted_job_count")),
                "observed_count": count,
                "observed_count_field": count_field,
                "observed_count_source": _text(row.get("observed_count_source"))
                or observation_source,
                "failure_reason": _failure_reason(row) or None,
                "pagination_complete": _as_bool(row.get("pagination_complete")),
                "pages_seen": _as_int(row.get("pages_seen")),
                "total_pages": _as_int(row.get("total_pages")),
                "has_more": _as_bool(row.get("has_more")),
                "issues": company_issue_types,
            }
        )

    issues.sort(key=lambda item: (item["company_id"], item["type"]))
    company_reports.sort(key=lambda item: item["company_id"])
    affected_ids = sorted({item["company_id"] for item in issues})
    issue_count = len(issues)
    if not observed_rows and current_result is None and repository is None:
        status = "unknown"
    else:
        status = "healthy" if issue_count == 0 else "degraded"
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "crawler_health_reconciliation",
        "status": status,
        "observation_source": observation_source,
        "baseline_source": baseline_source,
        "quantity_change_threshold": quantity_change_threshold,
        "configured_company_count": len(catalog),
        "observed_company_count": len(observed_rows),
        "healthy_company_count": sum(not item["issues"] for item in company_reports),
        "issue_count": issue_count,
        "affected_company_ids": affected_ids,
        "issue_counts": issue_counts,
        "companies": company_reports,
        "issues": issues,
        "data_quality": {
            "query_errors": sorted(query_errors, key=lambda item: item["company_id"]),
            "quantity_comparison_available": bool(previous_rows),
        },
    }


def build_reporting_summary(
    pipeline_result: Any = None,
    *,
    previous_result: Any = None,
    baseline: Mapping[str, Any] | Sequence[Any] | None = None,
    repository: Any = None,
    companies: Sequence[Any] | None = None,
    run_id: str | None = None,
    scheduled_for: Any = None,
    quantity_change_threshold: float = DEFAULT_QUANTITY_CHANGE_THRESHOLD,
    minimum_count_delta: int = 1,
) -> dict[str, Any]:
    """Build one machine-readable daily summary plus crawler health report."""

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "daily_pipeline_and_crawler_health",
        "run": {
            "run_id": _text(run_id) or None,
            "scheduled_for": scheduled_for.isoformat()
            if hasattr(scheduled_for, "isoformat")
            else (_text(scheduled_for) or None),
        },
        "daily_summary": daily_pipeline_summary(pipeline_result),
        "crawler_health": crawler_health_reconciliation(
            pipeline_result,
            previous_result,
            repository=repository,
            baseline=baseline,
            companies=companies,
            quantity_change_threshold=quantity_change_threshold,
            minimum_count_delta=minimum_count_delta,
        ),
        "safety": {
            "reporting_write_attempted": False,
            "agent_postgres_read_attempted": repository is not None,
            "agent_postgres_write_attempted": False,
            "legacy_system_read_attempted": False,
            "legacy_system_write_attempted": False,
            "external_web_access_attempted": False,
            "model_call_attempted": False,
            "source_write_attempted": False,
        },
    }


def report_to_json(report: Mapping[str, Any]) -> str:
    """Serialize a report deterministically for logs, files, or queues."""

    return json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


serialize_report = report_to_json
summarize_daily_pipeline = daily_pipeline_summary
reconcile_crawler_health = crawler_health_reconciliation
build_daily_report = build_reporting_summary


__all__ = [
    "DEFAULT_QUANTITY_CHANGE_THRESHOLD",
    "HEALTH_ISSUE_TYPES",
    "REPORT_SCHEMA_VERSION",
    "build_daily_report",
    "build_reporting_summary",
    "crawler_health_reconciliation",
    "daily_pipeline_summary",
    "reconcile_crawler_health",
    "report_to_json",
    "serialize_report",
    "summarize_daily_pipeline",
]
