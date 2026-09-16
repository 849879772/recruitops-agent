"""Read-only, bounded Moka JD repair with a frozen 12-row gate.

The script reads the existing catalog, hydrates only exact stored Moka detail
URLs through the existing isolated job-detail worker, and writes schema-1
importer-compatible evidence.  It never opens a write transaction, calls a
model, crawls a company list, or changes catalog identity fields.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import requests
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import get_settings
from packages.pipeline.isolation import (
    IsolatedOperationTimeout,
    IsolatedWorkerError,
    fetch_job_detail_result_isolated,
)
from packages.recruitment_core import job_details
from packages.recruitment_core.jd_repair import (
    content_sha256,
    repair_decision,
    validate_candidate,
)
from packages.storage.database import create_storage_engine


SCHEMA = 1
MAX_LIMIT = 500
MAX_WORKERS = 4
MAX_PER_HOST = 2
DEFAULT_LIMIT = 500
DEFAULT_ROW_TIMEOUT_SECONDS = 30.0
DEFAULT_TOTAL_BUDGET_SECONDS = 20 * 60
DEFAULT_OUTPUT = ROOT / ".data" / "jd-repair" / "moka-wave01"
BASELINE_COMPANIES = (
    "九号公司", "绿盟", "中微公司", "文远知行", "速腾聚创", "途游",
)
RETRYABLE_STATUSES = frozenset(
    {"timeout", "fetch_failed", "render_failed", "api_variant_unsupported"}
)
BLOCKING_STATUSES = frozenset(
    {
        "timeout", "fetch_failed", "render_failed", "api_variant_unsupported",
        "identity_mismatch", "identity_ambiguous", "login_required",
        "captcha_required", "access_denied", "not_applicable", "no_detail_url",
        "cohort_ineligible", "budget_exhausted",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 7:
                raise
            # Windows readers can briefly hold the destination during polling.
            time.sleep(0.2 * (attempt + 1))


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError(f"unsupported Moka repair report: {path}")
    if not isinstance(payload.get("results"), list):
        raise ValueError(f"Moka repair report has no results list: {path}")
    return payload


def _exclude_report_paths(args: argparse.Namespace) -> tuple[Path, ...]:
    return tuple(Path(path).resolve() for path in (args.exclude_report or []))


def _load_exclude_context(
    paths: tuple[Path, ...],
) -> tuple[set[str], list[tuple[Path, dict[str, Any]]]]:
    excluded: set[str] = set()
    reports: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        report = _read_json(path)
        checkpoint_path = path.parent / "checkpoint.json"
        if not checkpoint_path.exists():
            raise FileNotFoundError(
                f"exclude report has no checkpoint: {checkpoint_path}"
            )
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if not isinstance(checkpoint, dict) or checkpoint.get("schema") != SCHEMA:
            raise ValueError(f"unsupported exclude checkpoint: {checkpoint_path}")
        if checkpoint.get("status") not in {"complete", "budget_exhausted"}:
            raise ValueError(f"exclude report checkpoint is not terminal: {checkpoint_path}")
        if checkpoint.get("in_flight_job_ids"):
            raise ValueError(f"exclude report checkpoint still has in-flight jobs: {checkpoint_path}")
        report_ids = {
            str(item.get("job_id") or "").strip()
            for item in report["results"]
            if str(item.get("job_id") or "").strip()
        }
        checkpoint_ids = {
            str(job_id).strip()
            for job_id in (checkpoint.get("completed_job_ids") or [])
            if str(job_id).strip()
        }
        if report_ids != checkpoint_ids:
            raise ValueError(
                f"exclude report/checkpoint job_ids differ: {path}"
            )
        reports.append((path, report))
        excluded.update(report_ids)
    return excluded, reports


def _confirmed_path_proof(
    reports: list[tuple[Path, dict[str, Any]]],
) -> dict[str, Any] | None:
    if not reports:
        return None
    proofs: list[tuple[Path, dict[str, Any]]] = []
    for path, report in reports:
        confirmation = report.get("path_confirmation")
        if not isinstance(confirmation, dict) or confirmation.get("status") != "confirmed":
            raise ValueError(f"exclude report has no confirmed path proof: {path}")
        if confirmation.get("failures"):
            raise ValueError(f"exclude report path proof contains failures: {path}")
        if report.get("read_only") is not True:
            raise ValueError(f"exclude report is not marked read-only: {path}")
        if report.get("model_calls", 0) or report.get("database_writes", 0):
            raise ValueError(f"exclude report is not model/db-write free: {path}")
        if int(confirmation.get("sample_size") or 0) < 1:
            raise ValueError(f"exclude report path proof has no samples: {path}")
        proofs.append((path, confirmation))

    companies = sorted({
        str(company)
        for _, confirmation in proofs
        for company in (confirmation.get("companies") or [])
        if str(company)
    })
    return {
        "status": "confirmed",
        "source": "prior_report",
        "source_reports": [str(path) for path, _ in proofs],
        "source_sample_sizes": [
            int(confirmation.get("sample_size") or 0)
            for _, confirmation in proofs
        ],
        "sample_size": max(
            int(confirmation.get("sample_size") or 0)
            for _, confirmation in proofs
        ),
        "companies": companies,
        "failures": [],
        "new_samples": 0,
        "proof_read_only": True,
    }


def _readonly_engine(database_url: str) -> Engine:
    if database_url.startswith("postgresql"):
        return create_storage_engine(
            database_url,
            connect_args={
                "options": "-c default_transaction_read_only=on -c statement_timeout=10000",
            },
        )
    return create_storage_engine(database_url)


def _set_sqlite_read_only(connection: Connection) -> None:
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("PRAGMA query_only = ON")


def _query_rows(
    connection: Connection,
    *,
    limit: int,
    include_render: bool = False,
) -> list[dict[str, Any]]:
    platform_filter = (
        "lower(coalesce(j.source_platform, '')) IN ('moka', 'render')"
        if include_render
        else "lower(coalesce(j.source_platform, '')) = 'moka'"
    )
    rows = connection.execute(
        text(
            f"""
            SELECT
                j.id, j.company_id, j.title, j.city, j.detail_url, j.jd_raw,
                j.cohort, j.cohort_status, j.batch, j.source_platform,
                j.source_tenant, j.native_job_id, j.recruitment_campaign_id,
                c.name AS company_name, c.campus_url AS company_campus_url,
                c.crawler_key AS company_crawler_key,
                a.model, a.analysis_status
            FROM job_snapshots j
            JOIN company_snapshots c ON c.id = j.company_id
            JOIN job_analysis_snapshots a ON a.job_id = j.id
            WHERE {platform_filter}
              AND j.cohort = 2027
              AND lower(coalesce(j.cohort_status, '')) = 'confirmed'
              AND a.model IS NULL
              AND lower(coalesce(a.analysis_status, '')) = 'jd_incomplete'
            ORDER BY c.name, j.id
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return [dict(row._mapping) for row in rows]


def _hydration_input(row: dict[str, Any]) -> dict[str, Any]:
    """Build worker input while retaining every stored identity coordinate."""

    return {
        **row,
        "id": row.get("id"),
        "company_id": row.get("company_id"),
        "company": row.get("company_name") or row.get("company_id") or "",
        "jd_url": row.get("detail_url") or "",
        "detail_url": row.get("detail_url") or "",
        "careers_url": (
            row.get("_moka_render_site_url")
            or row.get("company_campus_url")
            or ""
        ),
        "source_platform": row.get("source_platform") or row.get("company_crawler_key") or "",
        # Force the helper past the stored-content fast path.
        "jd_raw": "",
    }


def _host(row: dict[str, Any]) -> str:
    return (urlsplit(str(row.get("detail_url") or "")).hostname or "<missing>").casefold()


def _report_safe_url(value: object) -> str:
    """Keep host/path evidence while dropping query and fragment parameters."""

    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return raw.split("?", 1)[0].split("#", 1)[0]


def _render_moka_tenant_matches(
    row: dict[str, Any],
    site_url: str,
    *,
    company_site_bound: bool = False,
) -> tuple[bool, str]:
    tenant = str(row.get("source_tenant") or "").strip()
    kind, separator, value = tenant.partition(":")
    kind = kind.casefold()
    if not separator or not value:
        return False, "render_moka_source_tenant_missing_or_untrusted"
    if kind == "moka":
        tenant_id = value.split(":", 1)[0].strip().casefold()
        path_parts = {
            part.casefold()
            for part in urlsplit(site_url).path.split("/")
            if part
        }
        if tenant_id and tenant_id in path_parts:
            return True, "render_moka_tenant_matches_site_path"
        return False, "render_moka_tenant_site_mismatch"
    if kind == "web":
        tenant_host = value.split(":", 1)[0].strip().casefold()
        site_host = (urlsplit(site_url).hostname or "").casefold()
        shared_moka_host = site_host == "mokahr.com" or site_host.endswith(".mokahr.com")
        if shared_moka_host and not company_site_bound:
            return False, "render_moka_web_tenant_shared_host_unbound"
        if tenant_host and tenant_host == site_host:
            return True, "render_moka_web_tenant_matches_site_host"
        return False, "render_moka_web_tenant_host_mismatch"
    return False, "render_moka_source_tenant_untrusted"


def _render_moka_admission(row: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Admit a render-marked row only when its stored Moka provenance is bound."""

    detail_url = str(row.get("detail_url") or "").strip()
    detail_host = (urlsplit(detail_url).hostname or "").casefold()
    company_entry = str(
        row.get("company_campus_url") or row.get("careers_url") or ""
    ).strip()
    entry_host = (urlsplit(company_entry).hostname or "").casefold()
    evidence: dict[str, Any] = {
        "status": "rejected",
        "detail_host": detail_host,
        "company_entry_host": entry_host,
    }
    if not detail_host or not company_entry or not entry_host:
        evidence["reason"] = "render_moka_company_entry_missing"
        return False, evidence
    if detail_host != entry_host:
        evidence["reason"] = "render_moka_cross_host_company_entry"
        return False, evidence

    shared_moka_host = detail_host == "mokahr.com" or detail_host.endswith(".mokahr.com")
    detail_site_url = job_details._moka_site_url(
        detail_url,
        trusted_custom_host=not shared_moka_host,
    )
    entry_site_url = job_details._moka_site_url(
        company_entry,
        trusted_custom_host=True,
    )
    if entry_site_url and detail_site_url and entry_site_url != detail_site_url:
        evidence["reason"] = "render_moka_company_site_mismatch"
        return False, evidence
    company_site_bound = bool(
        entry_site_url and detail_site_url and entry_site_url == detail_site_url
    )
    if shared_moka_host and not company_site_bound:
        evidence["reason"] = "render_moka_shared_host_root_unbound"
        return False, evidence

    provenance_row = {**row, "careers_url": company_entry}
    site_url, custom_host = job_details._moka_provenance_site(
        provenance_row, detail_url
    )
    provenance_mode = "company_entry"
    if not site_url:
        if shared_moka_host:
            evidence["reason"] = "render_moka_shared_host_root_unbound"
            return False, evidence
        # Some verified Moka custom hosts store only a root company URL.  The
        # same-host check above lets the existing Moka parser bind the detail
        # route without trusting an arbitrary render URL.
        site_url = job_details._moka_site_url(
            detail_url,
            trusted_custom_host=True,
        )
        custom_host = bool(site_url)
        provenance_mode = "same_host_detail_route"
    if not site_url or (urlsplit(site_url).hostname or "").casefold() != detail_host:
        evidence["reason"] = "render_moka_provenance_missing"
        return False, evidence

    coordinates = job_details._moka_job_coordinates(
        detail_url,
        trusted_custom_host=custom_host,
        site_url=site_url,
    )
    if coordinates is None:
        evidence["reason"] = "render_moka_exact_job_coordinates_missing"
        return False, evidence
    resolved_site_url, job_id = coordinates
    if (urlsplit(resolved_site_url).hostname or "").casefold() != detail_host:
        evidence["reason"] = "render_moka_coordinates_cross_host"
        return False, evidence

    tenant_ok, tenant_reason = _render_moka_tenant_matches(
        row,
        resolved_site_url,
        company_site_bound=company_site_bound,
    )
    if not tenant_ok:
        evidence["reason"] = tenant_reason
        return False, evidence
    evidence.update(
        {
            "status": "accepted",
            "reason": "render_moka_provenance_and_coordinates_verified",
            "site_url": resolved_site_url,
            "job_id": job_id,
            "provenance_mode": provenance_mode,
            "custom_host": custom_host,
            "tenant_evidence": tenant_reason,
        }
    )
    return True, evidence


def _admit_render_moka_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    admitted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("source_platform") or "").casefold() != "render":
            admitted.append(row)
            continue
        accepted, evidence = _render_moka_admission(row)
        if accepted:
            admitted.append({
                **row,
                "_moka_render_gate": evidence,
                "_moka_render_site_url": evidence["site_url"],
            })
            continue
        rejected.append(
            {
                "job_id": str(row.get("id") or ""),
                "company_id": str(row.get("company_id") or ""),
                "company": str(row.get("company_name") or ""),
                "title": str(row.get("title") or ""),
                "detail_url": _report_safe_url(row.get("detail_url")),
                "source_tenant": _report_safe_url(row.get("source_tenant")),
                "reason": evidence.get("reason", "render_moka_rejected"),
                "evidence": evidence,
            }
        )
    return admitted, rejected


def _row_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("id") or ""), content_sha256(row.get("jd_raw") or "")


class _HostLimiters:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self._lock = threading.Lock()
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}

    @contextmanager
    def acquire(self, host: str):
        with self._lock:
            semaphore = self._semaphores.setdefault(
                host, threading.BoundedSemaphore(self.maximum)
            )
        semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()


def _retryable(status: str) -> bool:
    return status.casefold() in RETRYABLE_STATUSES


def _budget_result(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "detail": "",
        "status": "budget_exhausted",
        "source": "moka_official",
        "detail_url": str(row.get("detail_url") or ""),
        "attempts": [],
        "error_type": "",
        "identity_status": "",
        "identity_evidence": [],
    }


def _hydrate_row(
    row: dict[str, Any],
    *,
    timeout_seconds: float,
    retries: int,
    deadline: float,
    limiters: _HostLimiters,
) -> dict[str, Any]:
    last: dict[str, Any] | None = None
    for attempt in range(retries + 1):
        if time.monotonic() >= deadline:
            return _budget_result(row)
        try:
            with limiters.acquire(_host(row)):
                hydration = fetch_job_detail_result_isolated(
                    _hydration_input(row), timeout_seconds=timeout_seconds
                )
        except IsolatedOperationTimeout as exc:
            hydration = {
                "detail": "", "status": "timeout", "source": "moka_official",
                "detail_url": row.get("detail_url") or "", "attempts": [],
                "error_type": type(exc).__name__, "identity_status": "",
                "identity_evidence": [],
            }
        except IsolatedWorkerError as exc:
            hydration = {
                "detail": "", "status": "fetch_failed", "source": "moka_official",
                "detail_url": row.get("detail_url") or "", "attempts": [],
                "error_type": exc.error_type or type(exc).__name__,
                "identity_status": "", "identity_evidence": [],
            }
        except Exception as exc:  # one row must not stop the batch
            hydration = {
                "detail": "", "status": "fetch_failed", "source": "moka_official",
                "detail_url": row.get("detail_url") or "", "attempts": [],
                "error_type": type(exc).__name__, "identity_status": "",
                "identity_evidence": [],
            }
        last = dict(hydration)
        status = str(last.get("status") or "fetch_failed")
        if not _retryable(status) or attempt >= retries:
            return last
        delay = min(4.0, 0.75 * (2**attempt))
        if time.monotonic() + delay >= deadline:
            return last
        time.sleep(delay)
    return last or _budget_result(row)


def _identity_record(row: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "company_id": row.get("company_id"),
        "company": row.get("company_name"),
        "title": row.get("title"),
        "detail_url": row.get("detail_url"),
        "source_tenant": row.get("source_tenant"),
        "native_job_id": row.get("native_job_id"),
        "observed_identity_status": validation.get("identity_status", ""),
        "observed_identity_evidence": validation.get("identity_evidence", []),
    }


def _record_for_row(
    row: dict[str, Any],
    *,
    timeout_seconds: float,
    retries: int,
    deadline: float,
    limiters: _HostLimiters,
) -> dict[str, Any]:
    original = str(row.get("jd_raw") or "")
    selection = repair_decision(row)
    item: dict[str, Any] = {
        "job_id": str(row.get("id") or ""),
        "company_id": str(row.get("company_id") or ""),
        "company": str(row.get("company_name") or ""),
        "title": str(row.get("title") or ""),
        "detail_url": str(row.get("detail_url") or ""),
        "original_chars": len(original),
        "original_sha256": content_sha256(original),
        "selection": selection,
        "candidate_jd": "",
        "candidate_sha256": "",
        "validation": {},
        "identity": {},
        "render_gate": row.get("_moka_render_gate", {}),
        "failure_reason": "",
    }
    if not selection.get("selected"):
        item["failure_reason"] = str(selection.get("reason") or "not_selected")
        item["identity"] = _identity_record(row, {})
        return item

    hydration = _hydrate_row(
        row,
        timeout_seconds=timeout_seconds,
        retries=retries,
        deadline=deadline,
        limiters=limiters,
    )
    candidate = str(hydration.get("detail") or "")
    validation = validate_candidate(row, hydration)
    validation.update(
        {
            "attempts": list(hydration.get("attempts") or []),
            "error_type": str(hydration.get("error_type") or ""),
        }
    )
    item["candidate_jd"] = candidate
    item["candidate_sha256"] = content_sha256(candidate) if candidate else ""
    item["validation"] = validation
    item["identity"] = _identity_record(row, validation)
    if not validation["passed"]:
        item["failure_reason"] = ";".join(validation["failure_reasons"])
    return item


def _schema_meta(value: object, *, text_sha: bool = True) -> dict[str, Any]:
    if isinstance(value, str):
        result: dict[str, Any] = {"type": "string", "chars": len(value)}
        if text_sha:
            result["sha256"] = content_sha256(value)
        return result
    if isinstance(value, dict):
        return {
            "type": "object",
            "key_count": len(value),
            "keys": sorted(str(key) for key in value)[:80],
        }
    if isinstance(value, list):
        item_shapes = []
        for item in value[:5]:
            item_shapes.append(_schema_meta(item, text_sha=False))
        return {"type": "array", "items": len(value), "item_shapes": item_shapes}
    if value is None:
        return {"type": "null"}
    return {"type": type(value).__name__}


def _moka_schema_probe(row: dict[str, Any]) -> dict[str, Any]:
    """Inspect one detail response without retaining its envelope or values."""

    url = str(row.get("detail_url") or "")
    site_url = str(row.get("company_campus_url") or "")
    host = _host(row)
    trusted_custom_host = not (host == "mokahr.com" or host.endswith(".mokahr.com"))
    coordinates = job_details._moka_job_coordinates(
        url, trusted_custom_host=trusted_custom_host, site_url=site_url
    )
    if coordinates is None:
        return {"status": "not_applicable", "failure_reason": "moka_coordinates_unavailable"}
    resolved_site_url, job_id = coordinates
    try:
        org_id, site_id, aes_iv = job_details._moka_site_context(resolved_site_url)
        parsed = urlsplit(resolved_site_url)
        response = requests.post(
            f"{parsed.scheme}://{parsed.netloc}/api/outer/ats-apply/website/job",
            json={"orgId": org_id, "jobId": job_id, "siteId": site_id, "locale": "zh-CN"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": resolved_site_url},
            timeout=20,
        )
        response.raise_for_status()
        payload = job_details._decode_moka_payload(response.json(), aes_iv)
        data = payload.get("data")
        if not isinstance(data, dict):
            return {
                "status": "schema_invalid",
                "http_status": response.status_code,
                "failure_reason": "moka_detail_data_not_object",
            }
        identity_status, identity_evidence = job_details._check_identity(
            row, data, requested_id=job_id
        )
        content_fields = {}
        for field in (
            *job_details._MOKA_PRIMARY_DETAIL_FIELDS,
            *job_details._MOKA_REQUIREMENT_FIELDS,
        ):
            if field in data:
                content_fields[field] = _schema_meta(data[field])
        selected_text, selected_fields = job_details._moka_detail_content(data)
        return {
            "status": "ok",
            "http_status": response.status_code,
            "endpoint": parsed.path + "/api/outer/ats-apply/website/job",
            "data_keys": sorted(str(key) for key in data),
            "content_fields": content_fields,
            "selected_fields": list(selected_fields),
            "selected_body_chars": len(selected_text),
            "selected_body_sha256": content_sha256(selected_text),
            "identity_status": identity_status or ("matched" if identity_evidence else "request_bound"),
            "identity_evidence": list(identity_evidence),
            "nested_schema": {
                field: _schema_meta(data[field], text_sha=False)
                for field in ("customFields", "aimFields", "jobIntentions")
                if field in data
            },
        }
    except requests.Timeout as exc:
        return {
            "status": "timeout",
            "failure_reason": "moka_schema_probe_timeout",
            "error_type": type(exc).__name__,
        }
    except requests.RequestException as exc:
        return {
            "status": "access_blocked",
            "failure_reason": "moka_schema_probe_request_failed",
            "error_type": type(exc).__name__,
        }
    except Exception as exc:  # schema evidence must not stop the repair batch
        return {
            "status": "schema_probe_failed",
            "failure_reason": "moka_schema_probe_failed",
            "error_type": type(exc).__name__,
        }


def _diagnostic_record(row: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    validation = item.get("validation") or {}
    schema = _moka_schema_probe(row)
    return {
        "job_id": item["job_id"],
        "company_id": item["company_id"],
        "company": item["company"],
        "title": item["title"],
        "detail_url": item["detail_url"],
        "source_tenant": row.get("source_tenant"),
        "identity": item.get("identity", {}),
        "stored_jd": {
            "chars": item["original_chars"],
            "sha256": item["original_sha256"],
        },
        "hydrated_jd": {
            "status": validation.get("status", ""),
            "source": validation.get("source", ""),
            "chars": validation.get("candidate_chars", 0),
            "sha256": validation.get("candidate_sha256", ""),
            "is_incomplete": validation.get("status") != "complete",
            "identity_status": validation.get("identity_status", ""),
            "identity_evidence": validation.get("identity_evidence", []),
        },
        "api_schema": schema,
        "comparison": {
            "stored_to_hydrated_char_delta": (
                validation.get("candidate_chars", 0) - item["original_chars"]
            ),
            "independent_requirement_fields_observed": [
                field
                for field in job_details._MOKA_REQUIREMENT_FIELDS
                if field in (schema.get("content_fields") or {})
            ],
        },
    }


def _new_report(
    args: argparse.Namespace,
    *,
    exclude_report_paths: tuple[Path, ...] = (),
    excluded_job_count: int = 0,
    path_proof: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "metadata": {
            "mode": "read-only-dry-run",
            "platform": "moka",
            "cohort": 2027,
            "cohort_status": "confirmed",
            "analysis_model": None,
            "analysis_status": "jd_incomplete",
            "max_limit": MAX_LIMIT,
            "requested_limit": args.limit,
            "row_timeout_seconds": args.row_timeout_seconds,
            "total_budget_seconds": args.total_budget_seconds,
            "max_workers": args.max_workers,
            "per_host_concurrency": args.per_host_concurrency,
            "retries": args.retries,
            "baseline_companies": list(BASELINE_COMPANIES),
            "sample_only": bool(args.sample_only),
            "include_render": bool(args.include_render),
            "exclude_reports": [str(path) for path in exclude_report_paths],
            "excluded_job_count": excluded_job_count,
        },
        "read_only": True,
        "model_calls": 0,
        "database_writes": 0,
        "config_writes": 0,
        "path_confirmation": path_proof or {"status": "pending", "failures": []},
        "render_admission": {
            "enabled": bool(args.include_render),
            "considered": 0,
            "accepted": 0,
            "rejected": 0,
            "rejections": [],
        },
        "sample_diagnostics": [],
        "results": [],
        "summary": {},
    }


def _assert_resume_metadata(
    report: dict[str, Any],
    args: argparse.Namespace,
    *,
    exclude_report_paths: tuple[Path, ...],
    excluded_job_count: int,
) -> None:
    metadata = report.get("metadata") or {}
    expected = _new_report(
        args,
        exclude_report_paths=exclude_report_paths,
        excluded_job_count=excluded_job_count,
    )["metadata"]
    if metadata != expected:
        raise ValueError("resume arguments differ from the existing Moka report")


def _select_baseline(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("company_name") or "")].append(row)
    selected: list[dict[str, Any]] = []
    missing: list[str] = []
    for company in BASELINE_COMPANIES:
        candidates = sorted(groups.get(company, []), key=lambda item: str(item.get("id") or ""))
        if len(candidates) < 2:
            missing.append(company)
        selected.extend(candidates[:2])
    return selected, missing


def _select_targets(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    sample_only: bool,
    path_proof: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if path_proof is not None:
        return rows[:limit], []
    baseline, missing = _select_baseline(rows)
    if missing or sample_only:
        return baseline, missing
    selected_ids = {str(row.get("id") or "") for row in baseline}
    remainder = [row for row in rows if str(row.get("id") or "") not in selected_ids]
    return [*baseline, *remainder[: max(0, limit - len(baseline))]], missing


def _update_summary(report: dict[str, Any], *, candidate_pool: int) -> None:
    results = report.get("results") or []
    report["summary"] = {
        "candidate_pool": candidate_pool,
        "scanned": len(results),
        "selected": sum(bool(item.get("selection", {}).get("selected")) for item in results),
        "passed": sum(bool(item.get("validation", {}).get("passed")) for item in results),
        "failed": sum(
            bool(item.get("selection", {}).get("selected"))
            and not bool(item.get("validation", {}).get("passed"))
            for item in results
        ),
        "skipped": sum(not bool(item.get("selection", {}).get("selected")) for item in results),
        "remaining_eligible": max(0, candidate_pool - len(results)),
    }
    report["updated_at"] = _utc_now()


def _write_checkpoint(output: Path, report: dict[str, Any], *, status: str, in_flight: list[str] | None = None) -> None:
    checkpoint = {
        "schema": SCHEMA,
        "status": status,
        "updated_at": _utc_now(),
        "completed_job_ids": [str(item.get("job_id") or "") for item in report.get("results", [])],
        "in_flight_job_ids": list(in_flight or []),
        "path_confirmation": report.get("path_confirmation", {}),
        "summary": report.get("summary", {}),
    }
    _atomic_write(output / "checkpoint.json", checkpoint)


def _write_sample_diagnosis(output: Path, report: dict[str, Any]) -> None:
    samples = report.get("sample_diagnostics", [])
    _atomic_write(
        output / "sample-diagnosis.json",
        {
            "schema": SCHEMA,
            "status": report.get("path_confirmation", {}).get("status"),
            "read_only": True,
            "model_calls": 0,
            "database_writes": 0,
            "path_confirmation": report.get("path_confirmation", {}),
            "sample_count": len(samples),
            "samples": samples,
        },
    )


def _process_rows(
    report: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    output: Path,
    args: argparse.Namespace,
    deadline: float,
    candidate_pool: int,
) -> None:
    prior = {
        (str(item.get("job_id") or ""), str(item.get("original_sha256") or ""))
        for item in report.get("results", [])
    }
    pending = [row for row in rows if _row_key(row) not in prior]
    if not pending:
        return
    limiters = _HostLimiters(args.per_host_concurrency)
    ids = [str(row.get("id") or "") for row in pending]
    report["in_flight"] = ids
    _update_summary(report, candidate_pool=candidate_pool)
    _atomic_write(output / "report.json", report)
    _write_checkpoint(output, report, status="running", in_flight=ids)
    with ThreadPoolExecutor(max_workers=min(args.max_workers, len(pending))) as pool:
        futures = {
            pool.submit(
                _record_for_row,
                row,
                timeout_seconds=args.row_timeout_seconds,
                retries=args.retries,
                deadline=deadline,
                limiters=limiters,
            ): row
            for row in pending
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                item = future.result()
            except Exception as exc:  # defensive isolation around report writing
                item = {
                    "job_id": str(row.get("id") or ""),
                    "company_id": str(row.get("company_id") or ""),
                    "company": str(row.get("company_name") or ""),
                    "title": str(row.get("title") or ""),
                    "detail_url": str(row.get("detail_url") or ""),
                    "original_chars": len(str(row.get("jd_raw") or "")),
                    "original_sha256": content_sha256(row.get("jd_raw") or ""),
                    "selection": {"selected": True, "reason": "record_exception", "evidence": []},
                    "candidate_jd": "", "candidate_sha256": "", "identity": {},
                    "render_gate": row.get("_moka_render_gate", {}),
                    "validation": {
                        "passed": False, "status": "record_exception", "source": "",
                        "detail_url": row.get("detail_url") or "", "candidate_chars": 0,
                        "candidate_sha256": "", "identity_status": "",
                        "identity_evidence": [], "provenance_status": "",
                        "provenance_evidence": [], "failure_reasons": [
                            f"record_exception:{type(exc).__name__}"
                        ],
                    },
                    "failure_reason": f"record_exception:{type(exc).__name__}",
                }
            report.setdefault("results", []).append(item)
            report["in_flight"] = [
                value for value in report.get("in_flight", [])
                if value != str(row.get("id") or "")
            ]
            _update_summary(report, candidate_pool=candidate_pool)
            _atomic_write(output / "report.json", report)
            _write_checkpoint(output, report, status="running", in_flight=report["in_flight"])
    report.pop("in_flight", None)
    _update_summary(report, candidate_pool=candidate_pool)
    _atomic_write(output / "report.json", report)


def run(args: argparse.Namespace) -> dict[str, Any]:
    exclude_report_paths = _exclude_report_paths(args)
    excluded_job_ids, exclude_reports = _load_exclude_context(exclude_report_paths)
    path_proof = _confirmed_path_proof(exclude_reports)
    minimum_limit = 1 if path_proof is not None else len(BASELINE_COMPANIES) * 2
    if not minimum_limit <= args.limit <= MAX_LIMIT:
        raise ValueError(f"--limit must be between {minimum_limit} and {MAX_LIMIT}")
    if not 1 <= args.max_workers <= MAX_WORKERS:
        raise ValueError(f"--max-workers must be between 1 and {MAX_WORKERS}")
    if not 1 <= args.per_host_concurrency <= MAX_PER_HOST:
        raise ValueError(f"--per-host-concurrency must be 1 or {MAX_PER_HOST}")
    if args.row_timeout_seconds <= 0:
        raise ValueError("--row-timeout-seconds must be positive")
    if not 0 < args.total_budget_seconds <= DEFAULT_TOTAL_BUDGET_SECONDS:
        raise ValueError("--total-budget-seconds must be between 0 and 1200")
    output = Path(args.output).resolve()
    report_path = output / "report.json"
    if args.resume:
        if not report_path.exists():
            raise FileNotFoundError(f"resume report does not exist: {report_path}")
        report = _read_json(report_path)
        _assert_resume_metadata(
            report,
            args,
            exclude_report_paths=exclude_report_paths,
            excluded_job_count=len(excluded_job_ids),
        )
    else:
        if output.exists() and any(output.iterdir()):
            raise FileExistsError(f"output directory is not empty; use --resume: {output}")
        report = _new_report(
            args,
            exclude_report_paths=exclude_report_paths,
            excluded_job_count=len(excluded_job_ids),
            path_proof=path_proof,
        )
        _atomic_write(report_path, report)

    engine = _readonly_engine(args.database_url)
    try:
        with engine.connect() as connection:
            _set_sqlite_read_only(connection)
            rows = _query_rows(
                connection,
                limit=max(2 * len(BASELINE_COMPANIES), 2_000),
                include_render=args.include_render,
            )
    finally:
        engine.dispose()

    rows = [
        row for row in rows
        if str(row.get("id") or "") not in excluded_job_ids
    ]
    if args.include_render:
        render_count = sum(
            str(row.get("source_platform") or "").casefold() == "render"
            for row in rows
        )
        rows, render_rejections = _admit_render_moka_rows(rows)
        report["render_admission"] = {
            "enabled": True,
            "considered": render_count,
            "accepted": sum(
                str(row.get("source_platform") or "").casefold() == "render"
                for row in rows
            ),
            "rejected": len(render_rejections),
            "rejections": render_rejections,
        }
    else:
        report["render_admission"] = {
            "enabled": False,
            "considered": 0,
            "accepted": 0,
            "rejected": 0,
            "rejections": [],
        }
    _atomic_write(report_path, report)
    targets, missing = _select_targets(
        rows,
        limit=args.limit,
        sample_only=args.sample_only,
        path_proof=path_proof or (
            report.get("path_confirmation")
            if report.get("metadata", {}).get("exclude_reports")
            else None
        ),
    )
    if missing:
        report["path_confirmation"] = {
            "status": "blocked",
            "failures": [f"baseline_company_has_fewer_than_two_rows:{name}" for name in missing],
        }
        _update_summary(report, candidate_pool=len(rows))
        _atomic_write(report_path, report)
        _write_sample_diagnosis(output, report)
        _write_checkpoint(output, report, status="blocked")
        return report

    started = time.monotonic()
    deadline = started + args.total_budget_seconds
    reused_path_proof = bool(
        report.get("metadata", {}).get("exclude_reports")
        or path_proof is not None
    )
    baseline_keys = (
        set()
        if reused_path_proof
        else {_row_key(row) for row in _select_baseline(rows)[0]}
    )
    baseline_rows = [row for row in targets if _row_key(row) in baseline_keys]
    if not reused_path_proof:
        _process_rows(
            report,
            baseline_rows,
            output=output,
            args=args,
            deadline=deadline,
            candidate_pool=len(rows),
        )

    existing = {
        (str(item.get("job_id") or ""), str(item.get("original_sha256") or "")): item
        for item in report.get("results", [])
    }
    if not reused_path_proof and len(existing) >= len(baseline_rows):
        diagnostics_by_id = {
            str(item.get("job_id") or ""): item
            for item in report.get("sample_diagnostics", [])
        }
        for row in baseline_rows:
            key = _row_key(row)
            item = existing.get(key)
            if item is None or str(item.get("job_id") or "") in diagnostics_by_id:
                continue
            report.setdefault("sample_diagnostics", []).append(_diagnostic_record(row, item))
        baseline_diagnostics = report.get("sample_diagnostics", [])
        baseline_ids = {str(row.get("id") or "") for row in baseline_rows}
        baseline_diagnostics = [
            item for item in baseline_diagnostics if str(item.get("job_id") or "") in baseline_ids
        ]
        failures = []
        for item in baseline_diagnostics:
            status = str((item.get("hydrated_jd") or {}).get("status") or "")
            identity_status = str((item.get("hydrated_jd") or {}).get("identity_status") or "")
            if status in BLOCKING_STATUSES or identity_status in {"mismatch", "ambiguous"}:
                failures.append({
                    "job_id": item.get("job_id"),
                    "company": item.get("company"),
                    "status": status,
                    "identity_status": identity_status,
                    "reason": "baseline_path_not_usable",
                })
        report["path_confirmation"] = {
            "status": "confirmed" if not failures else "blocked",
            "sample_size": len(baseline_rows),
            "companies": list(BASELINE_COMPANIES),
            "failures": failures,
        }
        _update_summary(report, candidate_pool=len(rows))
        _atomic_write(report_path, report)
        _write_sample_diagnosis(output, report)

    if report.get("path_confirmation", {}).get("status") != "confirmed":
        _write_checkpoint(output, report, status="blocked")
        return report
    if not args.sample_only and time.monotonic() < deadline:
        expansion = [
            row for row in targets
            if reused_path_proof or _row_key(row) not in baseline_keys
        ]
        _process_rows(
            report, expansion, output=output, args=args, deadline=deadline, candidate_pool=len(rows)
        )

    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["budget_exhausted"] = time.monotonic() >= deadline
    report["remaining_after_limit"] = max(0, len(rows) - len(targets))
    report["next_batch_available"] = report["remaining_after_limit"] > 0
    _update_summary(report, candidate_pool=len(rows))
    _atomic_write(report_path, report)
    _write_sample_diagnosis(output, report)
    _write_checkpoint(
        output,
        report,
        status="complete" if not report["budget_exhausted"] else "budget_exhausted",
    )
    return report


def _parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--exclude-report",
        action="append",
        type=Path,
        default=[],
        help="repeatable schema-1 report whose result job_ids must not be retried",
    )
    parser.add_argument(
        "--include-render",
        action="store_true",
        help="include only render rows admitted by the Moka provenance gate",
    )
    parser.add_argument("--sample-only", action="store_true")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--row-timeout-seconds", type=float, default=DEFAULT_ROW_TIMEOUT_SECONDS)
    parser.add_argument("--total-budget-seconds", type=float, default=DEFAULT_TOTAL_BUDGET_SECONDS)
    parser.add_argument("--max-workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--per-host-concurrency", type=int, default=MAX_PER_HOST)
    parser.add_argument("--retries", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.retries < 0:
        print("repair_moka_catalog_batch: --retries cannot be negative", file=sys.stderr)
        return 2
    try:
        report = run(args)
    except (FileExistsError, FileNotFoundError, ValueError, OSError) as exc:
        print(f"repair_moka_catalog_batch: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(Path(args.output).resolve()),
                "summary": report.get("summary", {}),
                "path_confirmation": report.get("path_confirmation", {}),
                "render_admission": report.get("render_admission", {}),
                "remaining_after_limit": report.get("remaining_after_limit"),
                "database_writes": report.get("database_writes", 0),
                "model_calls": report.get("model_calls", 0),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
