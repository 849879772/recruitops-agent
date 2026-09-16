"""Run a resumable, read-only crawl evaluation for every eligible OC lead."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.discovery import consolidate_source_leads
from packages.discovery.oc_snapshot import filter_oc_snapshot
from packages.tools.oc_candidates import (
    OcCandidateCrawlBatchInput,
    OcCandidateRunner,
    infer_candidate_crawler,
)

DEFAULT_SNAPSHOT = ROOT / ".data" / "discovery" / "givemeoc_latest.json"
DEFAULT_CONFIG = ROOT / "config" / "companies.yaml"
DEFAULT_CHECKPOINT = ROOT / ".data" / "evals" / "oc_full_crawl_checkpoint.jsonl"
DEFAULT_OUTPUT = ROOT / ".data" / "evals" / "oc_full_crawl_latest.json"


def _lead_key(lead: Any) -> str:
    source_url = next(
        (url for url in lead.source_urls if infer_candidate_crawler(url) is not None),
        "",
    )
    value = f"{lead.canonical_name}\0{source_url}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _load_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid checkpoint JSON on line {line_number}") from exc
        if not isinstance(row, dict) or not isinstance(row.get("lead_key"), str):
            raise ValueError(f"invalid checkpoint row on line {line_number}")
        rows[row["lead_key"]] = row
    return rows


def _drop_retry_statuses(
    rows: dict[str, dict[str, Any]],
    statuses: set[str],
    crawlers: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    if not statuses:
        return rows
    return {
        key: row
        for key, row in rows.items()
        if not (
            str(row.get("integration_status") or "unresolved") in statuses
            and (
                not crawlers
                or str(row.get("crawler_key") or "") in crawlers
            )
        )
    }


def _append_checkpoint(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    error_codes: dict[str, int] = {}
    for row in rows:
        status = str(row.get("integration_status") or "unresolved")
        statuses[status] = statuses.get(status, 0) + 1
        error_code = str(row.get("error_code") or "").strip()
        if error_code:
            error_codes[error_code] = error_codes.get(error_code, 0) + 1
    job_keys = [
        str(job.get("job_key") or "")
        for row in rows
        for job in row.get("job_evidence") or []
        if str(job.get("job_key") or "")
    ]
    source_project_count = sum(
        len(row.get("source_projects") or []) or 1
        for row in rows
    )
    return {
        "company_count": len(rows),
        "source_project_count": source_project_count,
        "deduplicated_entry_count": max(0, source_project_count - len(rows)),
        "integration_statuses": dict(sorted(statuses.items())),
        "raw_job_count": sum(int(row.get("raw_job_count") or 0) for row in rows),
        "accepted_job_count": sum(int(row.get("accepted_count") or 0) for row in rows),
        "rejected_job_count": sum(int(row.get("rejected_count") or 0) for row in rows),
        "complete_jd_count": sum(int(row.get("complete_jd_count") or 0) for row in rows),
        "incomplete_jd_count": sum(int(row.get("incomplete_jd_count") or 0) for row in rows),
        "job_evidence_count": sum(len(row.get("job_evidence") or []) for row in rows),
        "unique_job_evidence_count": len(set(job_keys)),
        "duplicate_job_evidence_count": len(job_keys) - len(set(job_keys)),
        "error_codes": dict(sorted(error_codes.items())),
    }


def _write_report(
    path: Path,
    *,
    snapshot_path: Path,
    snapshot: Any,
    rows: list[dict[str, Any]],
    started_at: str,
    elapsed_seconds: float,
) -> None:
    payload = {
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(elapsed_seconds, 3),
        "read_only": True,
        "snapshot_path": str(snapshot_path),
        "snapshot_captured_at": snapshot.captured_at,
        "snapshot_rows_seen": snapshot.rows_seen,
        "snapshot_pages_fetched": snapshot.pages_fetched,
        "summary": _summary(rows),
        "results": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--companies", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=3, choices=range(1, 7))
    parser.add_argument("--timeout-seconds", type=int, default=110, choices=range(10, 181))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--retry-status",
        action="append",
        default=[],
        choices=(
            "connected_complete",
            "connected_partial",
            "needs_adapter",
            "invalid_entry",
            "jd_hydration_required",
            "no_eligible_jobs",
            "unresolved",
        ),
        help="With --resume, rerun prior rows having this integration status.",
    )
    parser.add_argument(
        "--retry-crawler",
        action="append",
        default=[],
        help="Limit --retry-status rows to these prior crawler keys.",
    )
    parser.add_argument(
        "--hydrate-details",
        action="store_true",
        help="Fetch missing JD details for otherwise eligible jobs before acceptance.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    snapshot_path = args.snapshot.expanduser().resolve()
    checkpoint_path = args.checkpoint.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    snapshot = filter_oc_snapshot(snapshot_path)
    leads = list(consolidate_source_leads(snapshot.leads))
    if args.limit is not None:
        leads = leads[: max(0, args.limit)]

    completed = _load_checkpoint(checkpoint_path) if args.resume else {}
    completed = _drop_retry_statuses(
        completed,
        set(args.retry_status),
        set(args.retry_crawler),
    )
    if not args.resume and checkpoint_path.exists():
        checkpoint_path.unlink()

    request = OcCandidateCrawlBatchInput(
        limit=3,
        expected_cohort=2027,
        require_complete_jd=True,
        include_job_evidence=True,
        per_company_timeout_seconds=args.timeout_seconds,
        timeout_ms=min(120_000, (args.timeout_seconds + 10) * 1_000),
    )
    runner = OcCandidateRunner(snapshot_path, args.companies)
    pending = [lead for lead in leads if _lead_key(lead) not in completed]
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    print(json.dumps({
        "event": "started",
        "total": len(leads),
        "resumed": len(completed),
        "pending": len(pending),
        "workers": args.workers,
        "read_only": True,
    }, ensure_ascii=False), flush=True)

    def crawl(lead: Any) -> dict[str, Any]:
        item = runner.crawl_lead(
            lead,
            request,
            hydrate_details=args.hydrate_details,
        )
        return {
            "lead_key": _lead_key(lead),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            **item.model_dump(mode="json"),
        }

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(crawl, lead): lead for lead in pending}
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            completed[row["lead_key"]] = row
            _append_checkpoint(checkpoint_path, row)
            done = len(completed)
            if index == 1 or done % 10 == 0 or index == len(pending):
                print(json.dumps({
                    "event": "progress",
                    "completed": done,
                    "total": len(leads),
                    "company": row["company"],
                    "integration_status": row["integration_status"],
                    "raw_job_count": row["raw_job_count"],
                }, ensure_ascii=False), flush=True)

    ordered = [completed[_lead_key(lead)] for lead in leads if _lead_key(lead) in completed]
    _write_report(
        output_path,
        snapshot_path=snapshot_path,
        snapshot=snapshot,
        rows=ordered,
        started_at=started_at,
        elapsed_seconds=time.monotonic() - started,
    )
    print(json.dumps({
        "event": "completed",
        "output": str(output_path),
        "checkpoint": str(checkpoint_path),
        "summary": _summary(ordered),
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
