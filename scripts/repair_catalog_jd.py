"""Bounded, resumable catalog JD repair report generator.

The tool is read-only and dry-run only.  It reads only confirmed 2027 rows,
hydrates selected detail URLs serially through the existing isolated worker,
and atomically checkpoints a JSON report.  Database import is intentionally
owned by a separate reviewed workflow.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.config import get_settings
from packages.pipeline.isolation import fetch_job_detail_result_isolated
from packages.recruitment_core.jd_repair import (
    content_sha256,
    repair_decision,
    validate_candidate,
)
from packages.storage.database import create_storage_engine


SCHEMA = 1
DEFAULT_OUTPUT = ROOT / ".data" / "jd-repair" / "catalog-jd-repair.json"
MAX_LIMIT = 50
DEFAULT_TIMEOUT_SECONDS = 30.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
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


def _query_rows(
    connection: Connection,
    *,
    company_id: str = "",
    job_id: str = "",
    include_all_incomplete: bool = False,
    offset: int = 0,
    limit: int = MAX_LIMIT,
) -> list[dict[str, Any]]:
    predicates = [
        "j.cohort = 2027",
        "lower(j.cohort_status) = 'confirmed'",
    ]
    # A direct job-id probe is an explicit repair request and must not be
    # hidden by any length heuristic.  The broad mode scans pages of the
    # confirmed catalog and lets repair_decision apply content gates.
    if not include_all_incomplete and not job_id:
        predicates.append("length(coalesce(j.jd_raw, '')) = 500")
    params: dict[str, Any] = {"offset": offset, "limit": limit}
    if company_id:
        predicates.append("j.company_id = :company_id")
        params["company_id"] = company_id
    if job_id:
        predicates.append("j.id = :job_id")
        params["job_id"] = job_id
    statement = text(
        """
        SELECT
            j.id, j.company_id, j.title, j.city, j.detail_url, j.jd_raw, j.capture_evidence,
            j.cohort, j.cohort_status, j.recruitment_campaign_id,
            j.source_platform, j.source_tenant, j.native_job_id,
            c.name AS company_name, c.campus_url AS company_campus_url,
            c.crawler_key AS company_crawler_key
        FROM job_snapshots j
        LEFT JOIN company_snapshots c ON c.id = j.company_id
        WHERE """
        + " AND ".join(predicates)
        + " ORDER BY j.company_id, j.id LIMIT :limit OFFSET :offset"
    )
    rows = [dict(row._mapping) for row in connection.execute(statement, params)]
    for row in rows:
        if isinstance(row.get("capture_evidence"), str):
            row["capture_evidence"] = json.loads(row["capture_evidence"])
    return rows


def _hydration_input(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build the helper input while preserving the stored row for hashing."""

    return {
        **dict(row),
        "id": row.get("id"),
        "company_id": row.get("company_id"),
        "company": row.get("company_name") or row.get("company_id") or "",
        "jd_url": row.get("detail_url") or "",
        "detail_url": row.get("detail_url") or "",
        "careers_url": row.get("company_campus_url") or "",
        "source_platform": row.get("source_platform") or row.get("company_crawler_key") or "",
        # Force the existing helper past its stored-content fast path.  The
        # original remains in the report and is never silently overwritten.
        "jd_raw": "",
    }


def _result_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return str(row.get("id") or ""), content_sha256(row.get("jd_raw") or "")


def _report_metadata(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "mode": "dry-run",
        "company_id": args.company_id or None,
        "job_id": args.job_id or None,
        "include_all_incomplete": bool(args.include_all_incomplete),
        "offset": args.offset,
        "limit": args.limit,
        "timeout_seconds": args.timeout_seconds,
    }


def _new_report(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "metadata": _report_metadata(args),
        "results": [],
        "summary": {"scanned": 0, "selected": 0, "passed": 0, "failed": 0, "skipped": 0},
    }


def _assert_resume_metadata(report: Mapping[str, Any], args: argparse.Namespace) -> None:
    if dict(report.get("metadata") or {}) != _report_metadata(args):
        raise ValueError("resume arguments differ from the existing report; choose a new --output")


def _record_for_row(
    row: Mapping[str, Any],
    *,
    fetcher: Callable[[Mapping[str, Any], float], Mapping[str, Any]],
    timeout_seconds: float,
) -> dict[str, Any]:
    original = str(row.get("jd_raw") or "")
    decision = repair_decision(row)
    result: dict[str, Any] = {
        "job_id": str(row.get("id") or ""),
        "company_id": str(row.get("company_id") or ""),
        "company": str(row.get("company_name") or ""),
        "title": str(row.get("title") or ""),
        "detail_url": str(row.get("detail_url") or ""),
        "original_chars": len(original),
        "original_sha256": content_sha256(original),
        "selection": decision,
        "candidate_jd": "",
        "candidate_sha256": "",
        "validation": {},
        "failure_reason": "",
    }
    if not decision.get("selected"):
        result["failure_reason"] = str(decision.get("reason") or "not_selected")
        return result

    try:
        hydration = fetcher(_hydration_input(row), timeout_seconds)
    except Exception as exc:  # one row must not prevent checkpointing others
        result["failure_reason"] = f"hydration_exception:{type(exc).__name__}"
        result["validation"] = {
            "passed": False,
            "status": "hydration_exception",
            "source": "",
            "detail_url": str(row.get("detail_url") or ""),
            "identity_status": "",
            "identity_evidence": [],
            "failure_reasons": [result["failure_reason"]],
        }
        return result
    candidate = str(hydration.get("detail") or "")
    validation = validate_candidate(row, hydration)
    result["candidate_jd"] = candidate
    result["candidate_sha256"] = content_sha256(candidate) if candidate else ""
    result["validation"] = validation
    if not validation["passed"]:
        result["failure_reason"] = ";".join(validation["failure_reasons"])
    return result


def _isolated_fetch(job: Mapping[str, Any], timeout_seconds: float) -> Mapping[str, Any]:
    return fetch_job_detail_result_isolated(job, timeout_seconds=timeout_seconds)


def _update_summary(report: dict[str, Any]) -> None:
    counts = {"scanned": 0, "selected": 0, "passed": 0, "failed": 0, "skipped": 0}
    for item in report["results"]:
        counts["scanned"] += 1
        if item.get("selection", {}).get("selected"):
            counts["selected"] += 1
            if item.get("validation", {}).get("passed"):
                counts["passed"] += 1
            else:
                counts["failed"] += 1
        else:
            counts["skipped"] += 1
    report["summary"] = counts
    report["updated_at"] = _utc_now()


def run_repair(args: argparse.Namespace) -> dict[str, Any]:
    if not 1 <= args.limit <= MAX_LIMIT:
        raise ValueError(f"--limit must be between 1 and {MAX_LIMIT}")
    if args.offset < 0:
        raise ValueError("--offset cannot be negative")
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    if args.apply:
        raise ValueError("--apply is intentionally unsupported; this tool only produces dry-run reports")

    output = Path(args.output).resolve()
    if output.exists() and not args.resume:
        raise FileExistsError(f"output exists; pass --resume or choose another path: {output}")
    report = _read_report(output) if args.resume else _new_report(args)
    if args.resume:
        _assert_resume_metadata(report, args)

    prior = {
        (str(item.get("job_id") or ""), str(item.get("original_sha256") or ""))
        for item in report["results"]
    }
    engine = _readonly_engine(args.database_url)
    try:
        with engine.connect() as connection:
            _set_sqlite_read_only(connection)
            rows = _query_rows(
                connection,
                company_id=args.company_id,
                job_id=args.job_id,
                include_all_incomplete=args.include_all_incomplete,
                offset=args.offset,
                limit=args.limit,
            )
            for row in rows:
                key = _result_key(row)
                if key in prior:
                    continue
                report["in_flight"] = {"job_id": key[0], "original_sha256": key[1]}
                _atomic_write(output, report)
                item = _record_for_row(row, fetcher=_isolated_fetch, timeout_seconds=args.timeout_seconds)
                report["results"].append(item)
                prior.add(key)
                report.pop("in_flight", None)
                _update_summary(report)
                _atomic_write(output, report)
    finally:
        engine.dispose()
    report.pop("in_flight", None)
    _update_summary(report)
    _atomic_write(output, report)
    return report


def _parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=settings.database_url)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--company-id", default="")
    parser.add_argument("--job-id", default="")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--include-all-incomplete", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="unsupported: catalog import is performed by a separate reviewed workflow",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_repair(args)
    except (FileExistsError, ValueError, OSError) as exc:
        print(f"repair_catalog_jd: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"output": str(Path(args.output).resolve()), **report["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
