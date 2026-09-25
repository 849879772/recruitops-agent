"""Compact index helpers for durable per-company crawl receipts."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping


COMPACT_COMPANY_CHECKPOINT_VERSION = 2
COMPACT_COMPANY_CHECKPOINT_MIN_SCOPE = 100


def company_receipt_path(checkpoint: Path, receipt_set_id: str, company_id: str) -> Path:
    digest = hashlib.sha256(company_id.encode("utf-8")).hexdigest()[:32]
    return checkpoint.parent / f".c-{receipt_set_id[:12]}" / f"{digest}.json"


def company_checkpoint_progress(payload: Mapping[str, Any]) -> dict[str, int] | None:
    company_ids = payload.get("company_ids")
    entries = payload.get("companies")
    if not isinstance(company_ids, list) or not isinstance(entries, Mapping):
        return None
    scoped = set(str(value) for value in company_ids)
    if set(str(key) for key in entries) - scoped:
        return None
    attempted = len(entries)
    complete = sum(
        isinstance(value, Mapping) and value.get("status") == "complete"
        for value in entries.values()
    )
    total = len(scoped)
    return {
        "scope_total": total,
        "attempted_unique": attempted,
        "confirmed_complete": complete,
        "retry_pending": max(0, attempted - complete),
        "not_started": max(0, total - attempted),
        "remaining": max(0, total - complete),
    }
