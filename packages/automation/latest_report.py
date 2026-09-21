"""One replaceable summary, separate from recovery checkpoints and job data."""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from packages.security.boundaries import redact_sensitive


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


def _redact_diagnostic(value: str | None, settings) -> str | None:
    if value is None:
        return None
    secrets = [getattr(settings, name, "") for name in (
        "llm_api_key", "mail_imap_password", "database_url",
    )]
    # Replace whole configured values before generic redaction alters a DSN.
    for secret in sorted((s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED:secret]")
    return redact_sensitive(value)


def summarize(result: dict, execution_id: str, *, settings=None) -> dict:
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
    # Early guards return diagnostics before a daily_sync payload exists.
    error = daily.get("error") or result.get("error")
    if not succeeded:
        error = error or result.get("message") or f"全量任务执行失败（状态：{result.get('status') or 'unknown'}）"
        if result.get("missing"):
            error = f"{error}\n缺少配置项：{'、'.join(result['missing'])}"
    return {
        "execution_id": execution_id,
        "status": ("partial" if partial or result.get("sync_status") == "degraded" else "succeeded") if succeeded else "failed",
        "source_status": result.get("status"),
        "message": _redact_diagnostic(result.get("message"), settings),
        "missing": result.get("missing", []),
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
        "error": _redact_diagnostic(error, settings),
    }
