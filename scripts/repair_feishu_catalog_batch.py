"""Run a read-only, resumable Feishu JD repair batch.

The runner selects only rows that are still waiting for JD completion.  It
uses the existing public Feishu detail API helper, never renders a browser,
never calls a model, and never writes catalog data.  A report is the only
durable output and is written atomically after every completed item.
"""

from __future__ import annotations

import argparse
from collections import Counter, deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import bindparam, text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import get_settings
from packages.recruitment_core.jd_repair import (
    content_sha256,
    repair_decision,
)
from packages.recruitment_core.job_details import fetch_feishu_job_description_status
from packages.storage.database import create_storage_engine
from scripts.repair_catalog_jd import _record_for_row


SCHEMA = 1
MAX_ROWS = 500
CONCURRENCY = 3
REQUEST_TIMEOUT_SECONDS = 12.0
BATCH_TIMEOUT_SECONDS = 20 * 60.0
SMOKE_COUNT = 3
STORED_JD_CHARS = 500
LENGTH_MODE_EXACT_500 = "exact_500"
LENGTH_MODE_NON500 = "non500"
LENGTH_MODES = (LENGTH_MODE_EXACT_500, LENGTH_MODE_NON500)
DEFAULT_OUTPUT = ROOT / ".data" / "jd-repair" / "feishu-jd-wave03-20260906.json"

_FEISHU_ROUTE_RE = re.compile(r"/position/(?P<route_id>[^/]+)/detail(?:/|$)", re.I)
_REQUEST_CONTEXT: ContextVar[tuple[float, str | None] | None] = ContextVar(
    "feishu_request_context", default=None
)
_REQUEST_PATCH_LOCK = threading.Lock()
_REQUEST_GUARD_INSTALLED = False

try:
    import requests

    _ORIGINAL_REQUESTS_GET = requests.get
except ImportError:  # pragma: no cover - requests is a runtime dependency
    requests = None
    _ORIGINAL_REQUESTS_GET = None


Fetcher = Callable[[Mapping[str, Any], float], Mapping[str, Any]]
RowLoader = Callable[[], tuple[list[dict[str, Any]], int]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a complete checkpoint and replace the previous one atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_report(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError(f"unsupported repair report: {path}")
    if not isinstance(payload.get("results"), list):
        raise ValueError(f"repair report has no results list: {path}")
    if not isinstance(payload.get("not_run", []), list):
        raise ValueError(f"repair report has invalid not_run list: {path}")
    return payload


def _report_ids(
    path: Path,
    *,
    successful_only: bool = False,
    skipped_only: bool = False,
) -> set[str]:
    """Read only IDs from a prior report; report content is never trusted as rows."""

    payload = _read_report(path)
    ids: set[str] = set()
    for item in payload.get("results") or []:
        if not isinstance(item, Mapping) or not item.get("job_id"):
            continue
        if successful_only and not (
            (item.get("selection") or {}).get("selected")
            and (item.get("validation") or {}).get("passed")
        ):
            continue
        if skipped_only and (item.get("selection") or {}).get("selected"):
            continue
        ids.add(str(item["job_id"]))
    return ids


def _report_skipped_items(paths: Sequence[Path]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in paths:
        payload = _read_report(path)
        items.extend(
            dict(item)
            for item in payload.get("results") or []
            if isinstance(item, Mapping)
            and item.get("job_id")
            and not (item.get("selection") or {}).get("selected")
        )
    return items


def _readonly_engine(database_url: str):
    if str(database_url).startswith("postgresql"):
        return create_storage_engine(
            database_url,
            connect_args={
                "options": "-c default_transaction_read_only=on -c statement_timeout=10000",
            },
        )
    return create_storage_engine(database_url)


def _set_sqlite_read_only(connection) -> None:
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("PRAGMA query_only = ON")


_CATALOG_COLUMNS = """
    j.id, j.company_id, j.title, j.city, j.detail_url, j.jd_raw,
    j.cohort, j.cohort_status, j.batch, j.recruitment_campaign_id,
    j.source_platform, j.source_tenant, j.native_job_id,
    a.analysis_status, a.match_score AS analysis_match_score,
    a.model AS analysis_model, a.analysis_version,
    a.content_fingerprint AS analysis_content_fingerprint,
    c.name AS company_name, c.campus_url AS company_campus_url,
    c.crawler_key AS company_crawler_key
"""


def _length_predicate(length_mode: str) -> str:
    if length_mode == LENGTH_MODE_EXACT_500:
        return "length(coalesce(j.jd_raw, '')) = :stored_jd_chars"
    if length_mode == LENGTH_MODE_NON500:
        return "length(coalesce(j.jd_raw, '')) <> :stored_jd_chars"
    raise ValueError(f"unsupported length mode: {length_mode}")


def _query_rows(
    connection,
    *,
    max_rows: int = MAX_ROWS,
    length_mode: str = LENGTH_MODE_EXACT_500,
    excluded_job_ids: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], int]:
    """Read the disjoint repair scope and expose the unbounded total."""

    predicates = [
        "lower(trim(coalesce(a.analysis_status, ''))) = 'jd_incomplete'",
        "a.match_score IS NULL",
        "nullif(trim(coalesce(a.model, '')), '') IS NULL",
        "j.cohort = 2027",
        "lower(trim(coalesce(j.cohort_status, ''))) = 'confirmed'",
        _length_predicate(length_mode),
        "(lower(trim(coalesce(j.source_platform, ''))) IN ('feishu', 'lark') "
        "OR lower(trim(coalesce(j.source_tenant, ''))) LIKE 'feishu:%' "
        "OR lower(trim(coalesce(j.source_tenant, ''))) LIKE 'lark:%')",
    ]
    params: dict[str, Any] = {"stored_jd_chars": STORED_JD_CHARS, "max_rows": max_rows}
    excluded = sorted({str(job_id) for job_id in excluded_job_ids if str(job_id)})
    if excluded:
        predicates.append("j.id NOT IN :excluded_job_ids")
        params["excluded_job_ids"] = excluded
    statement = text(
        f"""
        SELECT {_CATALOG_COLUMNS}, COUNT(*) OVER () AS scope_total
        FROM job_snapshots j
        INNER JOIN job_analysis_snapshots a ON a.job_id = j.id
        LEFT JOIN company_snapshots c ON c.id = j.company_id
        WHERE {' AND '.join(predicates)}
        ORDER BY j.company_id, j.id
        LIMIT :max_rows
        """
    )
    if excluded:
        statement = statement.bindparams(bindparam("excluded_job_ids", expanding=True))
    rows = [
        dict(row._mapping)
        for row in connection.execute(
            statement,
            params,
        )
    ]
    scope_total = int(rows[0].get("scope_total") or 0) if rows else 0
    return rows, scope_total


def _query_diagnostic_rows(connection, job_ids: Sequence[str]) -> list[dict[str, Any]]:
    ids = sorted({str(job_id) for job_id in job_ids if str(job_id)})
    if not ids:
        return []
    statement = text(
        f"""
        SELECT {_CATALOG_COLUMNS}
        FROM job_snapshots j
        INNER JOIN job_analysis_snapshots a ON a.job_id = j.id
        LEFT JOIN company_snapshots c ON c.id = j.company_id
        WHERE j.id IN :diagnostic_job_ids
        ORDER BY j.company_id, j.id
        """
    ).bindparams(bindparam("diagnostic_job_ids", expanding=True))
    return [
        dict(row._mapping)
        for row in connection.execute(statement, {"diagnostic_job_ids": ids})
    ]


def _load_database_rows(
    database_url: str,
    *,
    max_rows: int,
    length_mode: str = LENGTH_MODE_EXACT_500,
    excluded_job_ids: Sequence[str] = (),
    diagnostic_job_ids: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    engine = _readonly_engine(database_url)
    try:
        with engine.connect() as connection:
            _set_sqlite_read_only(connection)
            rows, scope_total = _query_rows(
                connection,
                max_rows=max_rows,
                length_mode=length_mode,
                excluded_job_ids=excluded_job_ids,
            )
            diagnostics = _query_diagnostic_rows(connection, diagnostic_job_ids)
            return rows, scope_total, diagnostics
    finally:
        engine.dispose()


def _cap_timeout(value: Any, cap: float) -> Any:
    if isinstance(value, tuple):
        return tuple(min(float(item), cap) for item in value)
    if isinstance(value, (int, float)):
        return min(float(value), cap)
    return cap


def _bounded_direct_get(*args, **kwargs):
    """Apply the per-thread timeout and selected public Feishu proxy."""

    context = _REQUEST_CONTEXT.get()
    if context is not None:
        timeout, proxy_url = context
        kwargs["timeout"] = _cap_timeout(kwargs.get("timeout"), timeout)
        if proxy_url:
            kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
        else:
            # no_proxy=* prevents requests from reintroducing the process proxy.
            kwargs["proxies"] = {
                "http": None,
                "https": None,
                "all": None,
                "no_proxy": "*",
            }
    return _ORIGINAL_REQUESTS_GET(*args, **kwargs)


def _install_request_guard() -> None:
    global _REQUEST_GUARD_INSTALLED
    if requests is None or _ORIGINAL_REQUESTS_GET is None:
        return
    with _REQUEST_PATCH_LOCK:
        if not _REQUEST_GUARD_INSTALLED:
            requests.get = _bounded_direct_get
            _REQUEST_GUARD_INSTALLED = True


def _restore_request_guard() -> None:
    global _REQUEST_GUARD_INSTALLED
    if requests is None or _ORIGINAL_REQUESTS_GET is None:
        return
    with _REQUEST_PATCH_LOCK:
        if _REQUEST_GUARD_INSTALLED and requests.get is _bounded_direct_get:
            requests.get = _ORIGINAL_REQUESTS_GET
            _REQUEST_GUARD_INSTALLED = False


def fetch_job_detail_result_http(
    job: Mapping[str, Any],
    timeout_seconds: float,
    *,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """Adapt the existing Feishu detail API helper to the report contract."""

    url = str(job.get("detail_url") or job.get("jd_url") or "").strip()
    token = _REQUEST_CONTEXT.set((max(0.1, float(timeout_seconds)), proxy_url))
    try:
        outcome = fetch_feishu_job_description_status(url, identity=job)
    finally:
        _REQUEST_CONTEXT.reset(token)

    detail, status = outcome
    return {
        "detail": str(detail or ""),
        "status": str(status or "fetch_failed"),
        "source": "feishu_api",
        "detail_url": str(getattr(outcome, "detail_url", "") or url),
        "attempts": list(getattr(outcome, "attempts", ()) or ()),
        "error_type": str(getattr(outcome, "error_type", "") or ""),
        "identity_status": str(getattr(outcome, "identity_status", "") or ""),
        "identity_evidence": list(getattr(outcome, "identity_evidence", ()) or ()),
    }


def _explicit_feishu_source(row: Mapping[str, Any]) -> bool:
    platform = str(row.get("source_platform") or "").strip().casefold()
    tenant = str(row.get("source_tenant") or "").strip().casefold()
    return platform in {"feishu", "lark"} or tenant.startswith(("feishu:", "lark:"))


def _analysis_exclusion(row: Mapping[str, Any]) -> str:
    status = str(row.get("analysis_status") or "").strip().casefold()
    if status != "jd_incomplete":
        return "analysis_status_not_jd_incomplete"
    if row.get("analysis_match_score") not in (None, "") or row.get("match_score") not in (None, ""):
        return "already_scored"
    model = str(row.get("analysis_model") or row.get("model") or "").strip()
    if model:
        return "model_semantic_exclusion"
    return ""


def _selection_decision(
    row: Mapping[str, Any],
    *,
    length_mode: str = LENGTH_MODE_EXACT_500,
) -> dict[str, Any]:
    """Apply the batch mutex before the shared deterministic repair gates."""

    reason = _analysis_exclusion(row)
    if reason:
        return {"selected": False, "reason": reason, "evidence": []}
    if not _explicit_feishu_source(row):
        return {"selected": False, "reason": "source_not_explicit_feishu", "evidence": []}
    stored_length = len(str(row.get("jd_raw") or ""))
    if length_mode == LENGTH_MODE_EXACT_500 and stored_length != STORED_JD_CHARS:
        return {"selected": False, "reason": "stored_jd_length_not_500", "evidence": []}
    if length_mode == LENGTH_MODE_NON500 and stored_length == STORED_JD_CHARS:
        return {"selected": False, "reason": "stored_jd_length_is_500", "evidence": []}
    if length_mode not in LENGTH_MODES:
        raise ValueError(f"unsupported length mode: {length_mode}")
    return repair_decision(row)


def _base_record(row: Mapping[str, Any], decision: Mapping[str, Any]) -> dict[str, Any]:
    original = str(row.get("jd_raw") or "")
    reason = str(decision.get("reason") or "not_selected")
    return {
        "job_id": str(row.get("id") or ""),
        "company_id": str(row.get("company_id") or ""),
        "company": str(row.get("company_name") or ""),
        "title": str(row.get("title") or ""),
        "detail_url": str(row.get("detail_url") or ""),
        "original_chars": len(original),
        "original_sha256": content_sha256(original),
        "selection": dict(decision),
        "candidate_jd": "",
        "candidate_sha256": "",
        "validation": {
            "passed": False,
            "status": "not_selected",
            "source": "",
            "detail_url": str(row.get("detail_url") or ""),
            "identity_status": "",
            "identity_evidence": [],
            "failure_reasons": [reason],
        },
        "failure_reason": reason,
    }


def _route_id(detail_url: object) -> str:
    match = _FEISHU_ROUTE_RE.search(str(detail_url or ""))
    return match.group("route_id") if match else ""


def _detail_project(detail_url: object) -> str:
    parsed = urlsplit(str(detail_url or ""))
    parts = [part for part in parsed.path.split("/") if part]
    try:
        position_index = next(index for index, part in enumerate(parts) if part.casefold() == "position")
    except StopIteration:
        return ""
    return "/".join(parts[:position_index])


def _identity_record(row: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    validation = record.get("validation") if isinstance(record.get("validation"), Mapping) else {}
    detail_url = str(record.get("detail_url") or row.get("detail_url") or "")
    provenance_evidence = list(validation.get("provenance_evidence") or [])
    identity_evidence = list(validation.get("identity_evidence") or [])
    raw_campus_url = str(row.get("company_campus_url") or "")
    company_identity = {
        "company_id": str(row.get("company_id") or ""),
        "company": str(row.get("company_name") or ""),
        "source_platform": str(row.get("source_platform") or ""),
        "source_tenant": str(row.get("source_tenant") or ""),
        "company_campus_url": raw_campus_url,
        "crawler_key": str(row.get("company_crawler_key") or ""),
        "status": str(validation.get("provenance_status") or ""),
        "evidence": provenance_evidence,
        "passed": str(validation.get("provenance_status") or "")
        in {"company_and_campaign_bound", "company_bound"},
    }
    job_identity = {
        "catalog_job_id": str(row.get("id") or ""),
        "stored_native_job_id": str(row.get("native_job_id") or ""),
        "detail_route_id": _route_id(detail_url),
        "detail_url": detail_url,
        "title": str(row.get("title") or ""),
        "identity_status": str(validation.get("identity_status") or ""),
        "identity_evidence": identity_evidence,
        "passed": bool(
            str(validation.get("identity_status") or "") in {"matched", "request_bound"}
            and identity_evidence
        ),
        "source": str(validation.get("source") or ""),
        "status": str(validation.get("status") or ""),
    }
    project_identity = {
        "company_campus_url": raw_campus_url,
        "recruitment_campaign_id": str(row.get("recruitment_campaign_id") or ""),
        "detail_project": _detail_project(detail_url),
        "status": str(validation.get("provenance_status") or ""),
        "evidence": provenance_evidence,
    }
    return {
        "company_identity": company_identity,
        "job_identity": job_identity,
        "project_identity": project_identity,
    }


def _assess_candidate_quality(row: Mapping[str, Any], candidate: str) -> dict[str, Any]:
    """Read the shared deterministic quality assessor without changing it."""

    if not candidate:
        return {
            "assessed": False,
            "complete": None,
            "reason_code": "not_assessed",
            "reason": "candidate JD empty",
            "text_length": 0,
            "raw_length": 0,
        }
    try:
        from packages.matching.jd_quality import assess_jd_quality

        quality = assess_jd_quality({**dict(row), "jd_raw": candidate})
    except Exception as exc:  # quality availability must not stop fetching
        return {
            "assessed": False,
            "complete": None,
            "reason_code": "quality_assessment_unavailable",
            "reason": type(exc).__name__,
            "text_length": len(candidate),
            "raw_length": len(candidate),
        }
    return {
        "assessed": True,
        "complete": bool(quality.complete),
        "reason_code": str(quality.reason_code),
        "reason": str(quality.reason),
        "text_length": int(quality.text_length),
        "raw_length": int(quality.raw_length),
    }


def _enrich_record(row: Mapping[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    record.update(_identity_record(row, record))
    validation = record.get("validation") if isinstance(record.get("validation"), Mapping) else {}
    reasons = list(validation.get("failure_reasons") or [])
    if not reasons and record.get("failure_reason"):
        reasons = [str(record["failure_reason"])]
    quality = _assess_candidate_quality(row, str(record.get("candidate_jd") or ""))
    record["quality"] = quality
    record["quality_complete"] = quality.get("complete")
    if quality.get("assessed") and quality.get("complete") is False:
        reasons.append(f"quality:{quality.get('reason_code') or 'incomplete'}")
    if isinstance(record.get("validation"), dict):
        record["validation"]["quality_complete"] = quality.get("complete")
        record["validation"]["quality_reason_code"] = quality.get("reason_code")
    record["quality_rejection_reasons"] = reasons
    record["analysis_status"] = str(row.get("analysis_status") or "")
    record["analysis_snapshot"] = {
        "analysis_status": str(row.get("analysis_status") or ""),
        "match_score": row.get("analysis_match_score", row.get("match_score")),
        "model": str(row.get("analysis_model") or row.get("model") or ""),
        "analysis_version": str(row.get("analysis_version") or ""),
        "content_fingerprint": str(row.get("analysis_content_fingerprint") or ""),
    }
    record["source"] = str(validation.get("source") or "")
    if not record.get("failure_reason") and reasons and not validation.get("passed"):
        record["failure_reason"] = ";".join(reasons)
    return record


def _record_for_row_with_selection(
    row: Mapping[str, Any],
    *,
    fetcher: Fetcher,
    timeout_seconds: float,
    length_mode: str = LENGTH_MODE_EXACT_500,
) -> dict[str, Any]:
    decision = _selection_decision(row, length_mode=length_mode)
    if not decision.get("selected"):
        return _enrich_record(row, _base_record(row, decision))
    try:
        record = _record_for_row(row, fetcher=fetcher, timeout_seconds=timeout_seconds)
    except Exception as exc:  # a single row must never stop the batch
        record = _base_record(
            row,
            {
                "selected": True,
                "reason": str(decision.get("reason") or "selected"),
                "evidence": list(decision.get("evidence") or []),
            },
        )
        record["validation"] = {
            "passed": False,
            "status": "runner_exception",
            "source": "",
            "detail_url": str(row.get("detail_url") or ""),
            "identity_status": "",
            "identity_evidence": [],
            "failure_reasons": [f"runner_exception:{type(exc).__name__}"],
        }
        record["failure_reason"] = f"runner_exception:{type(exc).__name__}"
    return _enrich_record(row, record)


def _diagnostic_record(
    row: Mapping[str, Any],
    prior_item: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Re-check binding for a prior skip without attempting a detail fetch."""

    from packages.recruitment_core.jd_repair import validate_provenance

    provenance = validate_provenance(row)
    decision = _selection_decision(row, length_mode=LENGTH_MODE_EXACT_500)
    prior_reason = str((prior_item or {}).get("failure_reason") or "")
    reasons = [reason for reason in (prior_reason, provenance.failure_reason, str(decision.get("reason") or "")) if reason]
    validation = {
        "passed": False,
        "status": "diagnostic_only",
        "source": "",
        "detail_url": str(row.get("detail_url") or ""),
        "identity_status": "",
        "identity_evidence": [],
        "provenance_status": provenance.status,
        "provenance_evidence": list(provenance.evidence),
        "failure_reasons": list(dict.fromkeys(reasons)),
    }
    record = _base_record(row, decision)
    record["validation"] = validation
    record["failure_reason"] = ";".join(dict.fromkeys(reasons))
    record["diagnostic_only"] = True
    record["fetch_attempted"] = False
    result = _enrich_record(row, record)
    result["prior_failure_reason"] = prior_reason
    result["binding_diagnosis"] = {
        "ok": provenance.ok,
        "status": provenance.status,
        "failure_reason": provenance.failure_reason,
        "evidence": list(provenance.evidence),
        "same_project_official_evidence": provenance.ok,
    }
    return result


def _build_diagnostics(
    prior_items: Sequence[Mapping[str, Any]],
    diagnostic_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    prior_by_id = {
        str(item.get("job_id") or ""): item
        for item in prior_items
        if item.get("job_id")
    }
    rows_by_id = {str(row.get("id") or ""): row for row in diagnostic_rows if row.get("id")}
    records: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for job_id, prior_item in sorted(prior_by_id.items()):
        row = rows_by_id.get(job_id)
        if row is None:
            missing.append(
                {
                    "job_id": job_id,
                    "company_id": str(prior_item.get("company_id") or ""),
                    "company": str(prior_item.get("company") or ""),
                    "title": str(prior_item.get("title") or ""),
                    "prior_failure_reason": str(prior_item.get("failure_reason") or ""),
                    "binding_diagnosis": {
                        "ok": False,
                        "status": "not_found",
                        "failure_reason": "job_not_found_in_current_catalog",
                        "evidence": [],
                        "same_project_official_evidence": False,
                    },
                    "fetch_attempted": False,
                }
            )
            continue
        records.append(_diagnostic_record(row, prior_item))

    prior_reasons = Counter(str(item.get("failure_reason") or "unknown") for item in prior_by_id.values())
    current_reasons = Counter(
        str(item.get("binding_diagnosis", {}).get("failure_reason") or item.get("binding_diagnosis", {}).get("status") or "unknown")
        for item in [*records, *missing]
    )
    binding_statuses = Counter(
        str(item.get("binding_diagnosis", {}).get("status") or "unknown")
        for item in [*records, *missing]
    )
    bound = sum(bool(item.get("binding_diagnosis", {}).get("ok")) for item in records)
    return {
        "requested": len(prior_by_id),
        "found": len(records),
        "missing": len(missing),
        "fetch_attempted": 0,
        "prior_reason_counts": dict(prior_reasons),
        "current_binding_reason_counts": dict(current_reasons),
        "current_binding_status_counts": dict(binding_statuses),
        "binding_passed": bound,
        "binding_rejected_or_missing": len(records) + len(missing) - bound,
        "same_project_official_evidence_required": True,
        "records": [*records, *missing],
    }


def _not_run_item(row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "job_id": str(row.get("id") or ""),
        "company_id": str(row.get("company_id") or ""),
        "company": str(row.get("company_name") or ""),
        "title": str(row.get("title") or ""),
        "original_sha256": content_sha256(row.get("jd_raw") or ""),
        "count": 1,
        "failure_reason": reason,
    }


def _not_run_count(items: Sequence[Mapping[str, Any]]) -> int:
    return sum(max(1, int(item.get("count") or 1)) for item in items)


def _update_summary(report: dict[str, Any]) -> None:
    results = list(report.get("results") or [])
    selected = [item for item in results if (item.get("selection") or {}).get("selected")]
    passed = [item for item in selected if (item.get("validation") or {}).get("passed")]
    detail_fetch_passed = [
        item
        for item in selected
        if str((item.get("validation") or {}).get("status") or "") == "complete"
        and bool(str(item.get("candidate_jd") or ""))
    ]
    identity_passed = [
        item
        for item in selected
        if str((item.get("validation") or {}).get("identity_status") or "").casefold()
        in {"matched", "request_bound"}
        and bool((item.get("validation") or {}).get("identity_evidence"))
    ]
    quality_assessed = [item for item in selected if (item.get("quality") or {}).get("assessed")]
    quality_complete = [item for item in quality_assessed if (item.get("quality") or {}).get("complete")]
    reasons = Counter(
        str(reason)
        for item in results
        for reason in (item.get("quality_rejection_reasons") or [])
        if reason
    )
    report["summary"] = {
        "scanned": len(results),
        "selected": len(selected),
        "passed": len(passed),
        "detail_fetch_passed": len(detail_fetch_passed),
        "identity_passed": len(identity_passed),
        "validation_passed": len(passed),
        "quality_assessed": len(quality_assessed),
        "quality_complete": len(quality_complete),
        "quality_incomplete": len(quality_assessed) - len(quality_complete),
        "quality_unavailable": len(selected) - len(quality_assessed),
        "failed": len(selected) - len(passed),
        "skipped": len(results) - len(selected),
        "not_run": _not_run_count(report.get("not_run") or []),
        "average_jd_length": (
            round(
                sum(len(str(item.get("candidate_jd") or "")) for item in passed) / len(passed),
                2,
            )
            if passed
            else None
        ),
        "quality_rejection_reasons": dict(reasons),
    }
    report["updated_at"] = _utc_now()


def _update_coverage(
    report: dict[str, Any],
    *,
    rows: Sequence[Mapping[str, Any]],
    scope_total: int | None,
    current_keys: set[tuple[str, str]],
    selected_total: int | None,
    scope_limit: int = MAX_ROWS,
    reason: str = "",
) -> None:
    result_keys = {
        (str(item.get("job_id") or ""), str(item.get("original_sha256") or ""))
        for item in report.get("results") or []
    }
    processed = len(current_keys & result_keys)
    not_run = _not_run_count(report.get("not_run") or [])
    loaded = len(current_keys)
    fully_covered = (
        scope_total is not None
        and scope_total == loaded
        and processed == loaded
        and not_run == 0
        and not report.get("in_flight")
    )
    if not reason:
        if scope_total is None:
            reason = "scope_total_unknown"
        elif scope_total > loaded:
            reason = "scope_limit_reached"
        elif not_run:
            reason = "batch_budget_exhausted"
        elif fully_covered:
            reason = "all_loaded_rows_processed"
        else:
            reason = "incomplete"
    report["coverage"] = {
        "actual_scope_total": scope_total,
        "rows_loaded": loaded,
        "processed_total": processed,
        "selected_total": selected_total,
        "not_run_total": not_run,
        "fully_covered": fully_covered,
        "reason": reason,
        "scope_limit": scope_limit,
    }


def _run_window(
    report: dict[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    fetcher: Fetcher,
    timeout_seconds: float,
    deadline: float,
    monotonic: Callable[[], float],
    checkpoint: Callable[[], None],
    concurrency: int,
    length_mode: str = LENGTH_MODE_EXACT_500,
) -> list[dict[str, Any]]:
    """Process a bounded window with at most ``concurrency`` active calls."""

    pending = deque(rows)
    inflight: dict[Future, tuple[Mapping[str, Any], tuple[str, str]]] = {}
    executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="feishu-jd")

    def submit_available() -> None:
        while pending and len(inflight) < concurrency and monotonic() < deadline:
            row = pending.popleft()
            key = (str(row.get("id") or ""), content_sha256(row.get("jd_raw") or ""))
            future = executor.submit(
                _record_for_row_with_selection,
                row,
                fetcher=fetcher,
                timeout_seconds=timeout_seconds,
                length_mode=length_mode,
            )
            inflight[future] = (row, key)
            report.setdefault("in_flight", []).append(
                {"job_id": key[0], "original_sha256": key[1]}
            )
        checkpoint()

    submit_available()
    while inflight:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        done, _ = wait(tuple(inflight), timeout=remaining, return_when=FIRST_COMPLETED)
        if not done:
            break
        for future in done:
            row, key = inflight.pop(future)
            try:
                item = future.result()
            except Exception as exc:  # defensive guard around worker code
                item = _enrich_record(
                    row,
                    {
                        **_base_record(row, {"selected": True, "reason": "selected", "evidence": []}),
                        "validation": {
                            "passed": False,
                            "status": "worker_exception",
                            "source": "",
                            "detail_url": str(row.get("detail_url") or ""),
                            "identity_status": "",
                            "identity_evidence": [],
                            "failure_reasons": [f"worker_exception:{type(exc).__name__}"],
                        },
                        "failure_reason": f"worker_exception:{type(exc).__name__}",
                    },
                )
            report["results"].append(item)
            report["in_flight"] = [
                entry
                for entry in report.get("in_flight") or []
                if (str(entry.get("job_id") or ""), str(entry.get("original_sha256") or "")) != key
            ]
            _update_summary(report)
            checkpoint()
        submit_available()

    unfinished = [row for row, _key in inflight]
    unfinished.extend(list(pending))
    if unfinished:
        report["not_run"].extend(
            _not_run_item(row, "batch_budget_exhausted") for row in unfinished
        )
        report["in_flight"] = []
        checkpoint()
    executor.shutdown(wait=False, cancel_futures=True)
    return [
        item
        for item in report.get("results") or []
        if (item.get("job_id"), item.get("original_sha256"))
        in {
            (str(row.get("id") or ""), content_sha256(row.get("jd_raw") or ""))
            for row in rows
        }
    ]


def _smoke_check(
    rows: Sequence[Mapping[str, Any]],
    results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_key = {
        (str(item.get("job_id") or ""), str(item.get("original_sha256") or "")): item
        for item in results
    }
    checks: list[str] = []
    passed = True
    for row in rows:
        key = (str(row.get("id") or ""), content_sha256(row.get("jd_raw") or ""))
        item = by_key.get(key)
        if item is None:
            passed = False
            checks.append(f"missing_result:{key[0]}")
            continue
        if item.get("company_identity", {}).get("company_campus_url") != str(
            row.get("company_campus_url") or ""
        ):
            passed = False
            checks.append(f"company_url_changed:{key[0]}")
        if item.get("selection", {}).get("selected"):
            candidate = str(item.get("candidate_jd") or "")
            if candidate and item.get("candidate_sha256") != content_sha256(candidate):
                passed = False
                checks.append(f"candidate_hash_mismatch:{key[0]}")
            validation = item.get("validation") or {}
            if validation.get("passed") and validation.get("source") != "feishu_api":
                passed = False
                checks.append(f"unexpected_source:{key[0]}")
    return {"requested": len(rows), "processed": len(by_key), "passed": passed, "checks": checks}


def _new_report(metadata: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "status": "running",
        "metadata": dict(metadata),
        "results": [],
        "not_run": [],
        "in_flight": [],
        "coverage": {
            "actual_scope_total": None,
            "rows_loaded": 0,
            "processed_total": 0,
            "selected_total": None,
            "not_run_total": 0,
            "fully_covered": False,
            "reason": "not_started",
            "scope_limit": MAX_ROWS,
        },
        "summary": {
            "scanned": 0,
            "selected": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "not_run": 0,
        },
    }


def _metadata(
    *,
    input_mode: str,
    max_rows: int,
    concurrency: int,
    request_timeout: float,
    batch_timeout: float,
    proxy_url: str | None = None,
    length_mode: str = LENGTH_MODE_EXACT_500,
    excluded_job_ids: Sequence[str] = (),
    diagnostic_job_ids: Sequence[str] = (),
) -> dict[str, Any]:
    excluded = sorted({str(job_id) for job_id in excluded_job_ids if str(job_id)})
    diagnostic = sorted({str(job_id) for job_id in diagnostic_job_ids if str(job_id)})
    return {
        "mode": "api-only-readonly-batch",
        "input_mode": input_mode,
        "browser_used": False,
        "cookies_used": False,
        "database_write": False,
        "external_model_used": False,
        "raw_api_body_saved": False,
        "access_control_bypass": False,
        "network_proxy": proxy_url or "direct_no_proxy",
        "transport": "feishu_detail_api_only",
        "quality_assessment": "packages.matching.jd_quality.assess_jd_quality",
        "quality_complete_is_separate_from_validation_passed": True,
        "request_timeout_seconds": request_timeout,
        "batch_timeout_seconds": batch_timeout,
        "proxy_url": proxy_url or "",
        "concurrency": concurrency,
        "run_parameters": {
            "max_rows": max_rows,
            "concurrency": concurrency,
            "request_timeout_seconds": request_timeout,
            "batch_timeout_seconds": batch_timeout,
            "stored_jd_chars": STORED_JD_CHARS,
            "length_mode": length_mode,
            "smoke_count": SMOKE_COUNT,
            "excluded_job_count": len(excluded),
            "excluded_job_ids_sha256": content_sha256("\n".join(excluded)) if excluded else "",
            "diagnostic_job_count": len(diagnostic),
            "diagnostic_job_ids_sha256": content_sha256("\n".join(diagnostic)) if diagnostic else "",
        },
        "selection": {
            "analysis_status": "jd_incomplete",
            "exclude_scored_or_model_semantic": True,
            "cohort": 2027,
            "cohort_status": "confirmed",
            "source": "explicit_feishu_source_fields",
            "stored_jd_length": STORED_JD_CHARS,
            "length_mode": length_mode,
            "max_rows": max_rows,
            "order": "company_id,id",
        },
    }


def _assert_resume_metadata(report: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    actual = dict((report.get("metadata") or {}).get("run_parameters") or {})
    if actual != dict(expected.get("run_parameters") or {}):
        raise ValueError("resume arguments differ from the existing report; choose a new --output")


def _read_offline_rows(path: Path) -> tuple[list[dict[str, Any]], int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [dict(row) for row in payload], len(payload)
    if not isinstance(payload, Mapping):
        raise ValueError("offline rows must be a JSON list or an object with rows")
    raw_rows = payload.get("rows") or payload.get("jobs")
    if not isinstance(raw_rows, list):
        raise ValueError("offline rows object has no rows list")
    return [dict(row) for row in raw_rows], int(payload.get("scope_total") or len(raw_rows))


def _read_offline_hydrations(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping) and isinstance(payload.get("results"), list):
        values = payload["results"]
    elif isinstance(payload, Mapping):
        values = [dict(value, job_id=key) for key, value in payload.items()]
    elif isinstance(payload, list):
        values = payload
    else:
        raise ValueError("offline hydrations must be a mapping or list")
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, Mapping) or not value.get("job_id"):
            raise ValueError("offline hydration entries require job_id")
        result[str(value["job_id"])] = dict(value)
    return result


def _offline_fetcher(hydrations: Mapping[str, Mapping[str, Any]]) -> Fetcher:
    def fetch(row: Mapping[str, Any], _timeout_seconds: float) -> Mapping[str, Any]:
        value = hydrations.get(str(row.get("id") or ""))
        if value is None:
            return {
                "detail": "",
                "status": "offline_not_run",
                "source": "offline",
                "detail_url": str(row.get("detail_url") or ""),
                "identity_status": "",
                "identity_evidence": [],
            }
        return value

    return fetch


def run_batch(
    *,
    output: Path,
    database_url: str = "",
    resume: bool = False,
    input_rows: Sequence[Mapping[str, Any]] | None = None,
    input_scope_total: int | None = None,
    diagnostic_rows: Sequence[Mapping[str, Any]] | None = None,
    row_loader: RowLoader | None = None,
    fetcher: Fetcher | None = None,
    length_mode: str = LENGTH_MODE_EXACT_500,
    exclude_job_ids: Sequence[str] = (),
    exclude_report_paths: Sequence[Path] = (),
    diagnose_report_paths: Sequence[Path] = (),
    max_rows: int = MAX_ROWS,
    concurrency: int = CONCURRENCY,
    request_timeout: float = REQUEST_TIMEOUT_SECONDS,
    batch_timeout: float = BATCH_TIMEOUT_SECONDS,
    proxy_url: str | None = None,
    smoke_count: int = SMOKE_COUNT,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    if not 1 <= max_rows <= MAX_ROWS:
        raise ValueError(f"max_rows must be between 1 and {MAX_ROWS}")
    if length_mode not in LENGTH_MODES:
        raise ValueError(f"length_mode must be one of {LENGTH_MODES}")
    if concurrency != CONCURRENCY:
        raise ValueError(f"concurrency must be exactly {CONCURRENCY}")
    if request_timeout <= 0 or batch_timeout <= 0:
        raise ValueError("timeouts must be positive")
    if not 0 <= smoke_count <= 3:
        raise ValueError("smoke_count must be between 0 and 3")
    if input_rows is not None and row_loader is not None:
        raise ValueError("input_rows and row_loader are mutually exclusive")

    excluded_ids = {
        str(job_id)
        for job_id in exclude_job_ids
        if str(job_id)
    }
    for path in exclude_report_paths:
        excluded_ids.update(_report_ids(Path(path), successful_only=True))
    diagnostic_ids: set[str] = set()
    for path in diagnose_report_paths:
        diagnostic_ids.update(_report_ids(Path(path), skipped_only=True))

    output = Path(output).resolve()
    input_mode = "offline" if input_rows is not None or row_loader is not None else "postgres_readonly"
    metadata = _metadata(
        input_mode=input_mode,
        max_rows=max_rows,
        concurrency=concurrency,
        request_timeout=request_timeout,
        batch_timeout=batch_timeout,
        proxy_url=proxy_url,
        length_mode=length_mode,
        excluded_job_ids=sorted(excluded_ids),
        diagnostic_job_ids=sorted(diagnostic_ids),
    )
    if output.exists() and not resume:
        raise FileExistsError(f"output exists; pass --resume or choose another path: {output}")
    report = _read_report(output) if resume else _new_report(metadata)
    if resume:
        _assert_resume_metadata(report, metadata)
    report["status"] = "running"
    report["in_flight"] = []
    _atomic_write(output, report)

    try:
        if input_rows is not None:
            raw_rows = [dict(row) for row in input_rows]
            rows = [row for row in raw_rows if str(row.get("id") or "") not in excluded_ids][:max_rows]
            raw_scope_total = int(input_scope_total if input_scope_total is not None else len(raw_rows))
            excluded_in_input = {
                str(row.get("id") or "") for row in raw_rows if str(row.get("id") or "") in excluded_ids
            }
            scope_total = max(0, raw_scope_total - len(excluded_in_input))
            loaded_diagnostics = list(diagnostic_rows or [])
        elif row_loader is not None:
            raw_rows, raw_scope_total = row_loader()
            rows = [
                dict(row)
                for row in raw_rows
                if str(row.get("id") or "") not in excluded_ids
            ][:max_rows]
            excluded_in_input = {
                str(row.get("id") or "") for row in raw_rows if str(row.get("id") or "") in excluded_ids
            }
            scope_total = max(0, int(raw_scope_total) - len(excluded_in_input))
            loaded_diagnostics = list(diagnostic_rows or [])
        else:
            rows, scope_total, loaded_diagnostics = _load_database_rows(
                database_url,
                max_rows=max_rows,
                length_mode=length_mode,
                excluded_job_ids=sorted(excluded_ids),
                diagnostic_job_ids=sorted(diagnostic_ids),
            )
    except Exception as exc:
        report["status"] = "blocked"
        report["error"] = {
            "failure_reason": "database_unavailable",
            "error_type": type(exc).__name__,
        }
        report["not_run"] = []
        _update_coverage(
            report,
            rows=[],
            scope_total=None,
            current_keys=set(),
            selected_total=None,
            scope_limit=max_rows,
            reason="database_unavailable",
        )
        _update_summary(report)
        report["in_flight"] = []
        _atomic_write(output, report)
        return report

    current_keys = {
        (str(row.get("id") or ""), content_sha256(row.get("jd_raw") or ""))
        for row in rows
    }
    prior = {
        (str(item.get("job_id") or ""), str(item.get("original_sha256") or ""))
        for item in report.get("results") or []
    }
    report["not_run"] = []
    report.pop("error", None)
    report["coverage"] = {
        **dict(report.get("coverage") or {}),
        "actual_scope_total": scope_total,
        "rows_loaded": len(current_keys),
    }

    if diagnose_report_paths:
        report["diagnostics"] = _build_diagnostics(
            _report_skipped_items(diagnose_report_paths),
            loaded_diagnostics,
        )

    skipped_records: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("id") or ""), content_sha256(row.get("jd_raw") or ""))
        if key in prior:
            continue
        decision = _selection_decision(row, length_mode=length_mode)
        if decision.get("selected"):
            candidates.append(row)
        else:
            skipped_records.append(_enrich_record(row, _base_record(row, decision)))

    report["results"].extend(skipped_records)
    _update_summary(report)
    checkpoint = lambda: (_update_summary(report), _atomic_write(output, report))
    checkpoint()

    active_fetcher = fetcher
    if active_fetcher is None:
        if proxy_url:
            active_fetcher = lambda row, timeout: fetch_job_detail_result_http(
                row,
                timeout,
                proxy_url=proxy_url,
            )
        else:
            active_fetcher = fetch_job_detail_result_http
        _install_request_guard()
    deadline = monotonic() + batch_timeout
    try:
        smoke_rows = candidates[:smoke_count]
        smoke_pending = [
            row
            for row in smoke_rows
            if (str(row.get("id") or ""), content_sha256(row.get("jd_raw") or "")) not in prior
        ]
        _run_window(
            report,
            smoke_pending,
            fetcher=active_fetcher,
            timeout_seconds=request_timeout,
            deadline=deadline,
            monotonic=monotonic,
            checkpoint=checkpoint,
            concurrency=concurrency,
            length_mode=length_mode,
        )
        smoke_results = [
            item
            for item in report.get("results") or []
            if (str(item.get("job_id") or ""), str(item.get("original_sha256") or ""))
            in {
                (str(row.get("id") or ""), content_sha256(row.get("jd_raw") or ""))
                for row in smoke_rows
            }
        ]
        smoke = _smoke_check(smoke_rows, smoke_results)
        report["smoke_check"] = smoke
        checkpoint()
        if not smoke.get("passed"):
            remaining = [
                row
                for row in candidates[smoke_count:]
                if (str(row.get("id") or ""), content_sha256(row.get("jd_raw") or "")) not in prior
            ]
            report["not_run"].extend(_not_run_item(row, "smoke_check_failed") for row in remaining)
            report["status"] = "smoke_failed"
        else:
            remaining = [
                row
                for row in candidates[smoke_count:]
                if (str(row.get("id") or ""), content_sha256(row.get("jd_raw") or "")) not in prior
            ]
            _run_window(
                report,
                remaining,
                fetcher=active_fetcher,
                timeout_seconds=request_timeout,
                deadline=deadline,
                monotonic=monotonic,
                checkpoint=checkpoint,
                concurrency=concurrency,
                length_mode=length_mode,
            )
            report["status"] = "complete"
    finally:
        if fetcher is None:
            _restore_request_guard()

    selected_total = len(candidates) if scope_total == len(current_keys) else None
    if scope_total > len(current_keys):
        report["not_run"].append(
            {
                "job_id": "",
                "original_sha256": "",
                "count": scope_total - len(current_keys),
                "failure_reason": "scope_limit_reached",
            }
        )
        if report.get("status") == "complete":
            report["status"] = "partial"
    if report.get("not_run") and report.get("status") == "complete":
        report["status"] = "partial"
    coverage_reason = ""
    if report.get("status") == "smoke_failed":
        coverage_reason = "smoke_check_failed"
    _update_coverage(
        report,
        rows=rows,
        scope_total=scope_total,
        current_keys=current_keys,
        selected_total=selected_total,
        scope_limit=max_rows,
        reason=coverage_reason,
    )
    _update_summary(report)
    report["in_flight"] = []
    _atomic_write(output, report)
    return report


def _parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--offline-rows", type=Path)
    parser.add_argument("--offline-hydrations", type=Path)
    parser.add_argument(
        "--length-mode",
        choices=LENGTH_MODES,
        default=LENGTH_MODE_EXACT_500,
        help="exact_500 preserves wave03 default; non500 selects all other stored lengths",
    )
    parser.add_argument(
        "--exclude-report",
        type=Path,
        action="append",
        default=[],
        help="exclude successful job IDs from a prior report",
    )
    parser.add_argument(
        "--exclude-job-id",
        action="append",
        default=[],
        help="exclude an explicit catalog job ID",
    )
    parser.add_argument(
        "--diagnose-report",
        type=Path,
        action="append",
        default=[],
        help="re-check skipped IDs from a prior report using current DB bindings",
    )
    parser.add_argument("--max-rows", type=int, default=MAX_ROWS)
    parser.add_argument("--request-timeout", type=float, default=REQUEST_TIMEOUT_SECONDS)
    parser.add_argument("--batch-timeout", type=float, default=BATCH_TIMEOUT_SECONDS)
    parser.add_argument(
        "--proxy-url",
        default=os.environ.get("RECRUITOPS_HTTP_PROXY") or "",
        help="HTTP(S) proxy for public Feishu requests; empty keeps the existing direct mode",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if bool(args.offline_rows) != bool(args.offline_hydrations) and args.offline_hydrations:
        raise SystemExit("--offline-hydrations requires --offline-rows")
    try:
        if args.offline_rows:
            rows, scope_total = _read_offline_rows(args.offline_rows)
            hydrations = _read_offline_hydrations(args.offline_hydrations) if args.offline_hydrations else {}
            report = run_batch(
                output=args.output,
                resume=args.resume,
                input_rows=rows,
                input_scope_total=scope_total,
                fetcher=_offline_fetcher(hydrations),
                length_mode=args.length_mode,
                exclude_job_ids=args.exclude_job_id,
                exclude_report_paths=args.exclude_report,
                diagnose_report_paths=args.diagnose_report,
                max_rows=args.max_rows,
                request_timeout=args.request_timeout,
                batch_timeout=args.batch_timeout,
                proxy_url=args.proxy_url or None,
            )
        else:
            report = run_batch(
                output=args.output,
                database_url=args.database_url,
                resume=args.resume,
                length_mode=args.length_mode,
                exclude_job_ids=args.exclude_job_id,
                exclude_report_paths=args.exclude_report,
                diagnose_report_paths=args.diagnose_report,
                max_rows=args.max_rows,
                request_timeout=args.request_timeout,
                batch_timeout=args.batch_timeout,
                proxy_url=args.proxy_url or None,
            )
    except (FileExistsError, ValueError, OSError) as exc:
        print(f"repair_feishu_catalog_batch: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(Path(args.output).resolve()),
                "status": report.get("status"),
                **(report.get("summary") or {}),
                "coverage": report.get("coverage"),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
