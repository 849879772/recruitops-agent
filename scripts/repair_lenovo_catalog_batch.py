"""Read-only, bounded Lenovo JD repair with schema-1 evidence output."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit

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
MAX_LIMIT = 80
MAX_WORKERS = 4
MAX_PER_HOST = 2
DEFAULT_LIMIT = 80
DEFAULT_ROW_TIMEOUT_SECONDS = 30.0
DEFAULT_TOTAL_BUDGET_SECONDS = 20 * 60
RETRYABLE_STATUSES = frozenset({"timeout", "fetch_failed", "api_variant_unsupported"})
LENOVO_HOST = "talent.lenovo.com.cn"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
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
            time.sleep(0.2 * (attempt + 1))


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError(f"unsupported Lenovo repair report: {path}")
    if not isinstance(payload.get("results"), list):
        raise ValueError(f"Lenovo repair report has no results list: {path}")
    return payload


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


def _query_rows(connection: Connection, *, limit: int) -> list[dict[str, Any]]:
    result = connection.execute(
        text(
            """
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
            WHERE lower(coalesce(j.source_platform, '')) = 'lenovo'
              AND lower(j.detail_url) LIKE
                  'https://talent.lenovo.com.cn/position/detail?id=%'
              AND j.cohort = 2027
              AND lower(coalesce(j.cohort_status, '')) = 'confirmed'
              AND a.model IS NULL
              AND lower(coalesce(a.analysis_status, '')) = 'jd_incomplete'
            ORDER BY j.id
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return [dict(row._mapping) for row in result]


def _hydration_input(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        **dict(row),
        "id": row.get("id"),
        "company_id": row.get("company_id"),
        "company": row.get("company_name") or row.get("company_id") or "",
        "jd_url": row.get("detail_url") or "",
        "detail_url": row.get("detail_url") or "",
        "careers_url": row.get("company_campus_url") or "",
        "source_platform": row.get("source_platform") or row.get("company_crawler_key") or "",
        # Force the existing helper past the stored-content fast path.
        "jd_raw": "",
    }


def _row_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return str(row.get("id") or ""), content_sha256(row.get("jd_raw") or "")


def _stored_numeric_id(url: object) -> str:
    parsed = urlsplit(str(url or ""))
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != LENOVO_HOST
        or parsed.path.rstrip("/").casefold() != "/position/detail"
    ):
        return ""
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        if name.casefold() == "id" and value.isdigit():
            return value
    return ""


def _target_binding_reason(row: Mapping[str, Any]) -> str:
    if not _stored_numeric_id(row.get("detail_url")):
        return "lenovo_detail_route_unbound"
    company_url = str(row.get("company_campus_url") or "").strip()
    parsed = urlsplit(company_url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() != LENOVO_HOST
    ):
        return "lenovo_company_host_mismatch"
    project_path = parsed.path.rstrip("/").casefold()
    if project_path != "/position" and not project_path.startswith("/position/"):
        return "lenovo_project_binding_missing"
    company = str(row.get("company_name") or "").strip()
    if not company or ("联想" not in company and "lenovo" not in company.casefold()):
        return "lenovo_company_identity_untrusted"
    platform = str(row.get("source_platform") or "").strip().casefold()
    if platform != "lenovo":
        return "lenovo_source_platform_mismatch"
    id_ok, _id_evidence, id_failure = job_details._lenovo_identity_binding(
        row, _stored_numeric_id(row.get("detail_url"))
    )
    if not id_ok:
        return id_failure
    return ""


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


def _host(row: Mapping[str, Any]) -> str:
    return (urlsplit(str(row.get("detail_url") or "")).hostname or "<missing>").casefold()


def _budget_result(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "detail": "",
        "status": "budget_exhausted",
        "source": "lenovo_official_api",
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
                "detail": "", "status": "timeout", "source": "lenovo_official_api",
                "detail_url": row.get("detail_url") or "", "attempts": [],
                "error_type": type(exc).__name__, "identity_status": "",
                "identity_evidence": [],
            }
        except IsolatedWorkerError as exc:
            hydration = {
                "detail": "", "status": "fetch_failed", "source": "lenovo_official_api",
                "detail_url": row.get("detail_url") or "", "attempts": [],
                "error_type": exc.error_type or type(exc).__name__,
                "identity_status": "", "identity_evidence": [],
            }
        except Exception as exc:  # one row must not stop the batch
            hydration = {
                "detail": "", "status": "fetch_failed", "source": "lenovo_official_api",
                "detail_url": row.get("detail_url") or "", "attempts": [],
                "error_type": type(exc).__name__, "identity_status": "",
                "identity_evidence": [],
            }
        last = dict(hydration)
        status = str(last.get("status") or "fetch_failed")
        if status not in RETRYABLE_STATUSES or attempt >= retries:
            return last
        delay = min(4.0, 0.75 * (2**attempt))
        if time.monotonic() + delay >= deadline:
            return last
        time.sleep(delay)
    return last or _budget_result(row)


def _identity_record(row: Mapping[str, Any], validation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "expected": {
            "company_id": row.get("company_id"),
            "company": row.get("company_name"),
            "title": row.get("title"),
            "detail_url": row.get("detail_url"),
            "native_job_id": row.get("native_job_id"),
            "project_url": row.get("company_campus_url"),
        },
        "observed": {
            "status": validation.get("identity_status", ""),
            "evidence": list(validation.get("identity_evidence") or []),
            "resolved_detail_url": validation.get("detail_url", ""),
        },
    }


def _failure_item(row: Mapping[str, Any], reason: str, *, status: str) -> dict[str, Any]:
    original = str(row.get("jd_raw") or "")
    validation = {
        "passed": False,
        "status": status,
        "source": "",
        "detail_url": str(row.get("detail_url") or ""),
        "candidate_chars": 0,
        "candidate_sha256": "",
        "identity_status": "",
        "identity_evidence": [],
        "provenance_status": "",
        "provenance_evidence": [],
        "failure_reasons": [reason],
    }
    return {
        "job_id": str(row.get("id") or ""),
        "company_id": str(row.get("company_id") or ""),
        "company": str(row.get("company_name") or ""),
        "title": str(row.get("title") or ""),
        "detail_url": str(row.get("detail_url") or ""),
        "original_chars": len(original),
        "original_sha256": content_sha256(original),
        "selection": {"selected": True, "reason": reason, "evidence": []},
        "candidate_jd": "",
        "candidate_sha256": "",
        "validation": validation,
        "identity": _identity_record(row, validation),
        "failure_reason": reason,
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
    base = {
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
        "failure_reason": "",
    }
    if not selection.get("selected"):
        base["failure_reason"] = str(selection.get("reason") or "not_selected")
        base["identity"] = _identity_record(row, {})
        return base
    target_reason = _target_binding_reason(row)
    if target_reason:
        return _failure_item(row, target_reason, status="identity_mismatch")

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
    base["candidate_jd"] = candidate
    base["candidate_sha256"] = content_sha256(candidate) if candidate else ""
    base["validation"] = validation
    base["identity"] = _identity_record(row, validation)
    if not validation["passed"]:
        base["failure_reason"] = ";".join(validation["failure_reasons"])
    return base


def _metadata(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "mode": "read-only-dry-run",
        "platform": "lenovo",
        "cohort": 2027,
        "cohort_status": "confirmed",
        "analysis_model": None,
        "analysis_status": "jd_incomplete",
        "target_host": LENOVO_HOST,
        "requested_limit": args.limit,
        "row_timeout_seconds": args.row_timeout_seconds,
        "total_budget_seconds": args.total_budget_seconds,
        "max_workers": args.max_workers,
        "per_host_concurrency": args.per_host_concurrency,
        "retries": args.retries,
    }


def _new_report(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "metadata": _metadata(args),
        "read_only": True,
        "model_calls": 0,
        "database_writes": 0,
        "config_writes": 0,
        "results": [],
        "summary": {},
    }


def _assert_resume_metadata(report: Mapping[str, Any], args: argparse.Namespace) -> None:
    if dict(report.get("metadata") or {}) != _metadata(args):
        raise ValueError("resume arguments differ from the existing Lenovo report")


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


def _write_checkpoint(
    output: Path,
    report: Mapping[str, Any],
    *,
    status: str,
    in_flight: list[str] | None = None,
) -> None:
    _atomic_write(
        output / "checkpoint.json",
        {
            "schema": SCHEMA,
            "status": status,
            "updated_at": _utc_now(),
            "completed_job_ids": [
                str(item.get("job_id") or "") for item in report.get("results", [])
            ],
            "in_flight_job_ids": list(in_flight or []),
            "summary": report.get("summary", {}),
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
    in_flight = [str(row.get("id") or "") for row in pending]
    report["in_flight"] = in_flight
    _update_summary(report, candidate_pool=candidate_pool)
    _atomic_write(output / "report.json", report)
    _write_checkpoint(output, report, status="running", in_flight=in_flight)
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
            except Exception as exc:  # defensive isolation around checkpointing
                item = _failure_item(
                    row,
                    f"record_exception:{type(exc).__name__}",
                    status="record_exception",
                )
            report.setdefault("results", []).append(item)
            report["in_flight"] = [
                value for value in report.get("in_flight", [])
                if value != str(row.get("id") or "")
            ]
            _update_summary(report, candidate_pool=candidate_pool)
            _atomic_write(output / "report.json", report)
            _write_checkpoint(
                output,
                report,
                status="running",
                in_flight=report["in_flight"],
            )
    report.pop("in_flight", None)
    report["results"] = sorted(
        report.get("results", []), key=lambda item: str(item.get("job_id") or "")
    )
    _update_summary(report, candidate_pool=candidate_pool)
    _atomic_write(output / "report.json", report)


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not 1 <= args.limit <= MAX_LIMIT:
        raise ValueError(f"--limit must be between 1 and {MAX_LIMIT}")
    if not 1 <= args.max_workers <= MAX_WORKERS:
        raise ValueError(f"--max-workers must be between 1 and {MAX_WORKERS}")
    if not 1 <= args.per_host_concurrency <= MAX_PER_HOST:
        raise ValueError(f"--per-host-concurrency must be 1 or {MAX_PER_HOST}")
    if args.row_timeout_seconds <= 0:
        raise ValueError("--row-timeout-seconds must be positive")
    if not 0 < args.total_budget_seconds <= DEFAULT_TOTAL_BUDGET_SECONDS:
        raise ValueError("--total-budget-seconds must be between 0 and 1200")
    if args.retries < 0 or args.retries > 2:
        raise ValueError("--retries must be between 0 and 2")

    output = Path(args.output).resolve()
    report_path = output / "report.json"
    if args.resume:
        if not report_path.exists():
            raise FileNotFoundError(f"resume report does not exist: {report_path}")
        report = _read_json(report_path)
        _assert_resume_metadata(report, args)
    else:
        existing = {path.name for path in output.iterdir()} if output.exists() else set()
        if existing - {"diagnostic.json"}:
            raise FileExistsError(f"output directory is not empty; use --resume: {output}")
        report = _new_report(args)
        _atomic_write(report_path, report)

    engine = _readonly_engine(args.database_url)
    try:
        with engine.connect() as connection:
            _set_sqlite_read_only(connection)
            rows = _query_rows(connection, limit=args.limit)
    finally:
        engine.dispose()

    candidate_pool = len(rows)
    _update_summary(report, candidate_pool=candidate_pool)
    _atomic_write(report_path, report)
    started = time.monotonic()
    _process_rows(
        report,
        rows,
        output=output,
        args=args,
        deadline=started + args.total_budget_seconds,
        candidate_pool=candidate_pool,
    )
    report.pop("in_flight", None)
    report["results"] = sorted(
        report.get("results", []), key=lambda item: str(item.get("job_id") or "")
    )
    _update_summary(report, candidate_pool=candidate_pool)
    report["completed_at"] = _utc_now()
    report["status"] = (
        "complete"
        if report["summary"]["scanned"] >= candidate_pool
        else "budget_exhausted"
    )
    _atomic_write(report_path, report)
    _write_checkpoint(output, report, status=report["status"])
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    settings = get_settings()
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--output", type=Path, default=ROOT / ".data" / "jd-repair" / "lenovo-wave01")
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--row-timeout-seconds", type=float, default=DEFAULT_ROW_TIMEOUT_SECONDS)
    parser.add_argument("--total-budget-seconds", type=float, default=DEFAULT_TOTAL_BUDGET_SECONDS)
    parser.add_argument("--max-workers", type=int, default=MAX_WORKERS)
    parser.add_argument("--per-host-concurrency", type=int, default=MAX_PER_HOST)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    report = run(args)
    print(json.dumps(report.get("summary", {}), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
