"""One replaceable summary, separate from recovery checkpoints and job data."""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def report_path(settings):
    return Path(settings.agent_root) / ".data" / "runtime" / "latest-scheduled-crawl.json"


def summarize(result: dict, execution_id: str) -> dict:
    daily = result.get("daily_sync") or {}
    pipeline = daily.get("pipeline") or {}
    discovery = daily.get("discovery") or {}
    companies = pipeline.get("companies") or []
    counts = Counter()
    for company in companies:
        status = company.get("status")
        # A complete list with failed JD captures is not a complete company.
        if status == "complete" and company.get("detail_failure_count", 0):
            status = "partial"
        counts[status] += 1
    succeeded = result.get("status") == "completed"
    partial = counts["partial"] or counts["failed"] or pipeline.get("scoring_failed", 0)
    return {
        "execution_id": execution_id,
        "status": ("partial" if partial or result.get("sync_status") == "degraded" else "succeeded") if succeeded else "failed",
        "started_at": daily.get("started_at"),
        "finished_at": daily.get("finished_at") or datetime.now(timezone.utc).isoformat(),
        "discovery_complete": discovery.get("complete"),
        "companies_seen": discovery.get("companies_seen"),
        "new_companies": discovery.get("new_companies"),
        "new_entries": discovery.get("new_entries"),
        "excluded_entries": discovery.get("excluded_unusable"),
        "excluded_reasons": discovery.get("excluded_reasons", {}),
        "out_of_scope": discovery.get("out_of_scope"),
        "company_total": pipeline.get("selected_companies"),
        "complete_companies": counts["complete"],
        "partial_companies": counts["partial"],
        "failed_companies": counts["failed"],
        "skipped_companies": len(pipeline.get("skipped_companies") or []),
        "list_jobs": sum(c.get("raw_job_count", 0) for c in companies),
        "filtered_jobs": pipeline.get("filtered"),
        "new_jobs": pipeline.get("new"),
        "reused_jobs": pipeline.get("reused"),
        "detail_success": sum(c.get("detail_success_count", 0) for c in companies),
        "detail_failed": sum(c.get("detail_failure_count", 0) for c in companies),
        "scored_jobs": pipeline.get("scored"),
        "scoring_failed": pipeline.get("scoring_failed"),
        "unscored_jobs": pipeline.get("unscored"),
        "failure_reasons": pipeline.get("failure_reasons", {}),
        "error": daily.get("error"),
    }
