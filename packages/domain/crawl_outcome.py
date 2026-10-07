"""Interpret crawl completion separately from successful process execution."""

from collections.abc import Mapping
from typing import Any


def _positive(value: Any) -> bool:
    try:
        return not isinstance(value, bool) and int(value or 0) > 0
    except (TypeError, ValueError, OverflowError):
        return False


def crawl_completion_gaps(result: Any) -> list[str]:
    """Return bounded, deterministic warnings, not filtering or reuse outcomes."""
    if not isinstance(result, Mapping):
        return []
    gaps = []
    rows = result.get("companies", result.get("company_results", [])) or []
    rows = [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, (list, tuple)) else []
    if result.get("source_partial"):
        gaps.append("来源读取不完整")
    if any(_positive(result.get(key)) for key in ("failed_companies", "failed_company_count")) or any(
        row.get("status") in {"failed", "incomplete"}
        or row.get("list_complete") is False
        or (row.get("status") == "partial" and row.get("list_complete") is not True)
        for row in rows
    ):
        gaps.append("部分公司列表抓取失败或不完整")
    writes = result.get("job_write_statistics") or {}
    pending_new = isinstance(writes, Mapping) and result.get("dry_run") is not True and any(
        _positive(writes.get(key)) for key in ("new_pending_count", "new_failed_count")
    )
    if pending_new or any(_positive(result.get(key)) for key in ("failed_jobs", "failed_job_count")) or any(
        _positive(row.get("detail_failure_count")) for row in rows
    ):
        gaps.append("部分岗位详情尚未补全")
    if _positive(result.get("scoring_failed")):
        gaps.append("部分岗位评分失败")
    if not gaps and (
        result.get("status") in {"partial", "degraded", "failed", "incomplete"}
        or any(_positive(result.get(key)) for key in ("failed", "failed_count"))
        or any(row.get("status") == "partial" for row in rows)
    ):
        gaps.append("部分业务步骤未完成")
    return gaps
