"""Compare two read-only OC crawl evaluations by stable lead key."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


STATUS_RANK = {
    "unresolved": 0,
    "invalid_entry": 0,
    "needs_adapter": 1,
    "no_eligible_jobs": 2,
    "connected_partial": 3,
    "connected_complete": 4,
}


def _load(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = {
        str(row["lead_key"]): row
        for row in payload.get("results") or []
        if isinstance(row, dict) and row.get("lead_key")
    }
    return payload, rows


def _change(before: dict[str, Any] | None, after: dict[str, Any] | None) -> str:
    if before is None:
        return "added"
    if after is None:
        return "removed"
    old = str(before.get("integration_status") or "unresolved")
    new = str(after.get("integration_status") or "unresolved")
    if old == new:
        return "unchanged"
    if STATUS_RANK.get(new, 0) > STATUS_RANK.get(old, 0):
        return "improved"
    if STATUS_RANK.get(new, 0) < STATUS_RANK.get(old, 0):
        return "regressed"
    return "changed"


def compare(before_path: Path, after_path: Path) -> dict[str, Any]:
    before_payload, before = _load(before_path)
    after_payload, after = _load(after_path)
    changes = []
    counts: dict[str, int] = {}
    for lead_key in sorted(set(before) | set(after)):
        old = before.get(lead_key)
        new = after.get(lead_key)
        kind = _change(old, new)
        counts[kind] = counts.get(kind, 0) + 1
        old_status = str((old or {}).get("integration_status") or "missing")
        new_status = str((new or {}).get("integration_status") or "missing")
        old_error = str((old or {}).get("error_code") or "")
        new_error = str((new or {}).get("error_code") or "")
        changes.append({
            "lead_key": lead_key,
            "company": str((new or old or {}).get("company") or ""),
            "change": kind,
            "status_before": old_status,
            "status_after": new_status,
            "error_before": old_error,
            "error_after": new_error,
            "error_changed": old_error != new_error,
            "jobs_before": int((old or {}).get("raw_job_count") or 0),
            "jobs_after": int((new or {}).get("raw_job_count") or 0),
        })
    return {
        "before": str(before_path),
        "after": str(after_path),
        "before_summary": before_payload.get("summary") or {},
        "after_summary": after_payload.get("summary") or {},
        "change_counts": dict(sorted(counts.items())),
        "changes": changes,
    }


def _markdown(payload: dict[str, Any]) -> str:
    rows = [
        "# OC full-crawl status changes",
        "",
        f"- Before: `{payload['before']}`",
        f"- After: `{payload['after']}`",
        f"- Change counts: `{json.dumps(payload['change_counts'], ensure_ascii=False)}`",
        "",
        "| Company | Change | Before | After | Error before | Error after | Jobs before | Jobs after |",
        "|---|---|---|---|---|---|---:|---:|",
    ]
    for item in payload["changes"]:
        if item["change"] == "unchanged" and not item["error_changed"]:
            continue
        values = [
            item["company"], item["change"], item["status_before"], item["status_after"],
            item["error_before"], item["error_after"], item["jobs_before"], item["jobs_after"],
        ]
        rows.append("| " + " | ".join(str(value).replace("|", "\\|") for value in values) + " |")
    return "\n".join(rows) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = compare(args.before.resolve(), args.after.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output.with_suffix(".md").write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps(payload["change_counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
