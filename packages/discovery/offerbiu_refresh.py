"""Bounded OfferBiu discovery with isolated rows and resumable normal pages."""

from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Mapping
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable
from uuid import uuid4

import requests
from sqlalchemy import func, select

from packages.storage.models import CompanySnapshot
from packages.recruitment_core.entry import diagnose_candidate_entry

from .company_registry import CompanySourceRecord, CompanySourceRegistry
from .offerbiu_registry import import_offerbiu_sources, offerbiu_row_error, selected_industry_groups
from .reconciliation import normalize_company_name, source_identity_for_url


OFFERBIU_ENDPOINT = "https://offerbiu.com/api/recruitment/postings"
OFFERBIU_COMPANIES_URL = "https://offerbiu.com/companies/"


class OfferBiuCheckpointError(OSError):
    """A source checkpoint could not be safely persisted or quarantined."""


def _replace_checkpoint(source: str | Path, destination: Path,
                        *, sleeper: Callable[[float], None] = time.sleep) -> None:
    for attempt in range(3):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            if attempt == 2:
                raise OfferBiuCheckpointError("OfferBiu checkpoint replacement failed") from exc
            sleeper(0.05 * (2 ** attempt))
        except OSError as exc:
            raise OfferBiuCheckpointError("OfferBiu checkpoint replacement failed") from exc


def _write_checkpoint(path: Path, state: dict[str, Any],
                      *, sleeper: Callable[[float], None] = time.sleep) -> None:
    """Publish only a fully written checkpoint, even if the process is stopped."""
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_checkpoint(temporary, path, sleeper=sleeper)
    except OSError as exc:
        if isinstance(exc, OfferBiuCheckpointError):
            raise
        raise OfferBiuCheckpointError("OfferBiu checkpoint write failed") from exc
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def _retry_after_seconds(response: Any) -> float | None:
    value = str(getattr(response, "headers", {}).get("Retry-After", "")).strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def capture_offerbiu_snapshot(
    *,
    max_pages: int = 150,
    page_size: int = 9,
    delay_seconds: float = 0.25,
    session: Any | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    scope: Mapping[str, Any] | None = None,
    progress_callback: Callable[[int, int, int], None] | None = None,
    checkpoint_path: str | Path | None = None,
    checkpoint_ttl_seconds: float = 86400,
    max_retries: int = 2,
    retry_backoff_seconds: float = 0.25,
    max_overlap_pages: int = 2,
) -> dict[str, Any]:
    """Read scoped public pages; incomplete and isolated data never imply completeness."""

    if (max_pages < 1 or page_size < 1 or max_retries < 0 or max_overlap_pages < 0
            or delay_seconds < 0 or retry_backoff_seconds < 0 or checkpoint_ttl_seconds <= 0):
        raise ValueError("OfferBiu capture budgets must be positive or non-negative")
    groups = selected_industry_groups(scope)
    filters = {"seasonYear": 2027, "recruitType": "秋招", "industryGroups": sorted(groups)}
    checkpoint_scope = {"endpoint": OFFERBIU_ENDPOINT, "filters": filters, "page_size": page_size}
    checkpoint = Path(checkpoint_path) if checkpoint_path is not None else None
    params = [
        ("seasonYear", "2027"),
        ("recruitType", "秋招"),
        *[("industryGroup", group) for group in sorted(groups)],
        ("size", str(page_size)),
    ]
    result: dict[str, Any] = {
        "source": "offerbiu",
        "source_url": OFFERBIU_COMPANIES_URL,
        "endpoint": OFFERBIU_ENDPOINT,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "filters": filters,
        "pages": [],
        "items": [],
        "complete": False,
        "partial": True,
        "usable": False,
        "scope_verified": False,
        "resumed": False,
        "fresh": False,
        "quarantine_reasons": {},
        "counters": {"requests": 0, "retries": 0, "duplicate_ids": 0,
                     "quarantined_rows": 0, "conflicting_ids": 0, "total_changes": 0,
                     "resumed_records": 0, "fresh_records": 0, "resumed_pages": 0,
                     "pages_fetched": 0, "checkpoint_resets": 0},
        "stop_reason": None,
    }
    counters = result["counters"]
    records: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    conflicts: set[str] = set()
    fresh_ids: set[str] = set()
    page_evidence: dict[int, dict[str, Any]] = {}
    baseline: tuple[int, int] | None = None
    next_page = 0
    overlap_pending: list[int] = []
    overlap_used = 0
    traversal_finished = False
    started_at = result["captured_at"]
    if checkpoint is not None and checkpoint.exists():
        try:
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or saved.get("version") != 1:
                raise ValueError("Unsupported source checkpoint")
            if saved.get("scope") != checkpoint_scope:
                raise ValueError("Source checkpoint scope mismatch")
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(saved["started_at"])).total_seconds()
            if not saved.get("traversal_finished", saved.get("complete")) and 0 <= age < checkpoint_ttl_seconds:
                saved_items = saved["items"]
                saved_seen = saved["seen_ids"]
                saved_conflicts = saved["conflicting_ids"]
                saved_baseline = saved["baseline"]
                saved_counters = saved["counters"]
                saved_pages = saved["pages"]
                if (not isinstance(saved_items, list)
                        or any(offerbiu_row_error(row) for row in saved_items)
                        or not isinstance(saved_seen, list)
                        or any(not isinstance(key, str) for key in saved_seen)
                        or not isinstance(saved_conflicts, list)
                        or any(not isinstance(key, str) for key in saved_conflicts)
                        or not isinstance(saved_counters, dict)
                        or not isinstance(saved_pages, list)
                        or any(not isinstance(value, dict)
                               or any(type(value.get(key)) is not int or value[key] < 0
                                      for key in ("page", "totalItems", "totalPages"))
                               or value.get("previewLimited") is not False for value in saved_pages)
                        or type(saved["next_page"]) is not int or saved["next_page"] < 0
                        or (saved_baseline is not None and (
                            not isinstance(saved_baseline, list) or len(saved_baseline) != 2
                            or any(type(value) is not int or value < 0 for value in saved_baseline)))):
                    raise ValueError("Invalid source checkpoint state")
                records = {str(row["id"]): row for row in saved_items}
                seen = set(saved_seen)
                conflicts = set(saved_conflicts)
                if (len(records) != len(saved_items) or not set(records) <= seen
                        or not conflicts <= seen or set(records) & conflicts
                        or (saved_baseline is None and (records or saved["next_page"] or saved_pages))):
                    raise ValueError("Inconsistent source checkpoint IDs")
                for key in ("quarantined_rows", "conflicting_ids", "total_changes"):
                    count = saved_counters.get(key, 0)
                    if type(count) is not int or count < 0:
                        raise ValueError("Invalid source checkpoint counters")
                    counters[key] = count
                reasons = saved.get("quarantine_reasons", {})
                if not isinstance(reasons, dict) or any(type(value) is not int or value < 0 for value in reasons.values()):
                    raise ValueError("Invalid source quarantine evidence")
                result["quarantine_reasons"] = reasons
                baseline = tuple(saved_baseline) if saved_baseline is not None else None
                page_evidence = {value["page"]: value for value in saved_pages}
                next_page = saved["next_page"]
                # Re-read the last confirmed page before advancing. Restored
                # records remain explicitly separate from this call's evidence.
                if next_page:
                    overlap_pending = [next_page - 1]
                counters["resumed_records"] = len(records)
                counters["resumed_pages"] = len(page_evidence)
                result["resumed"] = True
                result["resumed_from"] = saved["started_at"]
                result["scope_verified"] = baseline is not None
                started_at = saved["started_at"]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # This is a source cache, never the frozen company execution scope.
            # Preserve bad evidence and restart only the explicitly requested scope.
            backup = checkpoint.with_name(checkpoint.name + f".invalid-{uuid4().hex}.json")
            _replace_checkpoint(checkpoint, backup, sleeper=sleeper)
            result["checkpoint_warning"] = f"invalid_checkpoint: {exc}"
            result["invalid_checkpoint_path"] = str(backup)
            records, seen, conflicts, page_evidence = {}, set(), set(), {}
            baseline, next_page, overlap_pending = None, 0, []
            for key in counters:
                counters[key] = 0
            counters["checkpoint_resets"] = 1
            result["quarantine_reasons"] = {}

    def persist(*, complete: bool = False) -> None:
        if checkpoint is not None:
            _write_checkpoint(checkpoint, {
                "version": 1, "scope": checkpoint_scope, "started_at": started_at,
                "updated_at": datetime.now(timezone.utc).isoformat(), "complete": complete,
                "traversal_finished": traversal_finished,
                "stop_reason": result["stop_reason"],
                "next_page": next_page, "baseline": baseline, "items": list(records.values()),
                "pages": [page_evidence[index] for index in sorted(page_evidence)],
                "seen_ids": sorted(seen), "conflicting_ids": sorted(conflicts),
                "counters": counters, "quarantine_reasons": result["quarantine_reasons"],
            }, sleeper=sleeper)

    def quarantine(reason: str) -> None:
        counters["quarantined_rows"] += 1
        reasons = result["quarantine_reasons"]
        reasons[reason] = reasons.get(reason, 0) + 1

    client = session or requests.Session()
    if session is None:
        # BIU is a public source and does not need the local browser proxy or cookies.
        client.trust_env = False
    try:
        for _ in range(max_pages):
            is_overlap = bool(overlap_pending)
            page = overlap_pending.pop(0) if is_overlap else next_page
            response = None
            payload = None
            for attempt in range(max_retries + 1):
                counters["requests"] += 1
                retry_after = None
                try:
                    if hasattr(client, "cookies"):
                        client.cookies.clear()
                    response = client.get(
                        OFFERBIU_ENDPOINT, params=[*params, ("page", str(page))],
                        timeout=20, allow_redirects=False,
                    )
                    retryable = response.status_code in {408, 425, 429, 500, 502, 503, 504}
                    if response.status_code == 200:
                        try:
                            payload = response.json()
                        except (ValueError, TypeError):
                            retryable, reason = True, "invalid_json"
                        else:
                            break
                    else:
                        reason = f"http_{response.status_code}"
                        retry_after = _retry_after_seconds(response) if retryable else None
                except requests.RequestException as exc:
                    retryable = isinstance(exc, (requests.Timeout, requests.ConnectionError))
                    reason = type(exc).__name__
                if not retryable or attempt == max_retries:
                    result["stop_reason"] = reason + ("_retry_exhausted" if retryable else "")
                    break
                if retry_after is not None and retry_after > 5.0:
                    result["stop_reason"] = "retry_after_exceeds_budget"
                    result["retry_after_seconds"] = retry_after
                    break
                counters["retries"] += 1
                sleeper(max(retry_after or 0.0, min(5.0, retry_backoff_seconds * (2 ** min(attempt, 8)))))
            if result["stop_reason"] is not None:
                break
            assert response is not None
            data = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(data, Mapping):
                result["stop_reason"] = "invalid_or_restricted_response"
                break
            rows = data.get("items")
            if payload.get("success") is not True or not isinstance(rows, list):
                result["stop_reason"] = "invalid_or_restricted_response"
                break
            if data.get("previewLimited") is not False:
                result["stop_reason"] = "preview_or_access_limit"
                break
            total_items = data.get("totalItems")
            total_pages = data.get("totalPages")
            if (
                data.get("page") != page
                or type(data.get("page")) is not int
                or type(total_items) is not int or total_items < 0
                or type(total_pages) is not int or total_pages < 0
                or (total_items > 0 and total_pages == 0)
            ):
                result["stop_reason"] = "pagination_mismatch"
                break
            current_baseline = (total_items, total_pages)
            if baseline is None:
                baseline = current_baseline
            elif current_baseline != baseline:
                counters["total_changes"] += 1
                baseline = current_baseline
                if overlap_used < max_overlap_pages:
                    overlap_pending.append(max(0, page - 1))
                    overlap_used += 1
            # Scope comes from the actual request and a normal paginated
            # response. Row metadata is retained as evidence, never rewritten.
            result["scope_verified"] = True
            for row in rows:
                error = offerbiu_row_error(row)
                if error:
                    quarantine(error)
                    continue
                key = str(row["id"])
                fresh_ids.add(key)
                if key in seen:
                    counters["duplicate_ids"] += 1
                    if key not in conflicts and records.get(key) != row:
                        conflicts.add(key)
                        records.pop(key, None)
                        counters["conflicting_ids"] += 1
                        quarantine("conflicting_record_id")
                    continue
                seen.add(key)
                records[key] = dict(row)
            evidence = {
                key: data.get(key)
                for key in ("page", "size", "totalItems", "totalPages", "previewLimited")
            }
            result["pages"].append(evidence)
            page_evidence[page] = evidence
            counters["pages_fetched"] += 1
            if not is_overlap:
                next_page = page + 1
            persist()
            if progress_callback is not None:
                progress_callback(next_page, total_pages, len(records))
            if next_page >= total_pages and not overlap_pending:
                traversal_finished = True
                if counters["total_changes"]:
                    result["stop_reason"] = "source_changed_during_capture"
                elif counters["quarantined_rows"]:
                    result["stop_reason"] = "rows_quarantined"
                elif len(seen) != total_items:
                    result["stop_reason"] = "total_mismatch"
                else:
                    result["complete"] = True
                    result["stop_reason"] = "complete"
                break
            if not rows:
                result["stop_reason"] = "unexpected_empty_page"
                break
            if delay_seconds > 0:
                sleeper(delay_seconds)
        else:
            result["stop_reason"] = "page_budget_exhausted"
    except (requests.RequestException, ValueError, TypeError) as exc:
        result["stop_reason"] = type(exc).__name__
    finally:
        if session is None:
            client.close()
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
    result["items"] = list(records.values())
    counters["fresh_records"] = len(fresh_ids - conflicts)
    result["fresh"] = bool(result["pages"])
    result["partial"] = not result["complete"]
    result["usable"] = bool(records) and result["scope_verified"]
    result["reason"] = result["stop_reason"]
    result["next_page"] = next_page
    result["expected_total"] = baseline[0] if baseline is not None else None
    result["expected_pages"] = baseline[1] if baseline is not None else None
    persist(complete=result["complete"])
    return result


class OfferBiuRefreshService:
    """Register verified normal rows, retaining partial progress and prior commits."""

    def __init__(self, registry: CompanySourceRegistry, *, session: Any | None = None,
                 scope: Mapping[str, Any] | None = None) -> None:
        self.registry = registry
        self.session = session
        self.scope = {"industry_groups": sorted(selected_industry_groups(scope))}
        self.last_registered_ids: tuple[str, ...] = ()

    def refresh(
        self,
        *,
        apply: bool = True,
        max_pages: int = 150,
        page_size: int = 9,
        delay_seconds: float = 0.25,
        progress_callback: Callable[[int, int, int], None] | None = None,
        checkpoint_path: str | Path | None = None,
        checkpoint_ttl_seconds: float = 86400,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.25,
        max_overlap_pages: int = 2,
    ) -> dict[str, Any]:
        self.last_registered_ids = ()
        snapshot = capture_offerbiu_snapshot(
            max_pages=max_pages,
            page_size=page_size,
            delay_seconds=delay_seconds,
            session=self.session,
            scope=self.scope,
            progress_callback=progress_callback,
            checkpoint_path=checkpoint_path,
            checkpoint_ttl_seconds=checkpoint_ttl_seconds,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            max_overlap_pages=max_overlap_pages,
        )
        result: dict[str, Any] = {
            "complete": bool(snapshot["complete"]),
            "partial": bool(snapshot["partial"]),
            "usable": bool(snapshot["usable"]),
            "reason": snapshot["reason"],
            "counters": dict(snapshot["counters"]),
            "resumed": bool(snapshot["resumed"]),
            "fresh": bool(snapshot["fresh"]),
            "stop_reason": snapshot["stop_reason"],
            "pages_fetched": len(snapshot["pages"]),
            "records_seen": len(snapshot["items"]),
            "companies_seen": len({item.get("companyName") for item in snapshot["items"]}),
            "applied": False,
            "registered_entries": 0,
            "new_entries": 0,
            "linked_existing_entries": 0,
            "excluded_unusable": 0,
            "out_of_scope": 0,
            "registered_ids": [],
            "pending_entries": [],
            "registered_ids_sample_count": 0,
            "registered_ids_limited": False,
            "pending_entry_count": None,
            "pending_entries_sample_count": 0,
            "pending_entries_limited": False,
        }
        if not (snapshot["complete"] or snapshot["usable"]) or not apply:
            return result

        with self.registry.storage.session() as db:
            existing_companies = set(db.scalars(select(CompanySourceRecord.company_name)))
            existing_companies.update(db.scalars(select(CompanySnapshot.name)))
            existing = set(db.scalars(
                select(CompanySourceRecord.source_record_id).where(
                    CompanySourceRecord.source == "offerbiu"
                )
            ))
        def registered(record_id: str) -> None:
            # Each upsert commits independently. Keep this receipt available if
            # a later database or reporting operation raises.
            self.last_registered_ids = (*self.last_registered_ids, record_id)

        imported = import_offerbiu_sources(
            self.registry, snapshot, scope=self.scope, on_registered=registered,
        )
        registered_ids = list(imported["ids"])
        self.last_registered_ids = tuple(registered_ids)
        if imported["quarantined_rows"]:
            result.update(complete=False, partial=True, reason="rows_quarantined", stop_reason="rows_quarantined")
            result["counters"]["quarantined_rows"] += imported["quarantined_rows"]
        result["counters"]["duplicate_ids"] += imported["duplicate_ids"]
        result["counters"]["conflicting_ids"] += imported["conflicting_ids"]
        new_entries = 0
        new_company_names = set()
        for record_id in registered_ids:
            record = self.registry.get_source(record_id)
            if record is not None and record["source_record_id"] not in existing:
                new_entries += 1
                if record["company_name"] not in existing_companies:
                    new_company_names.add(record["company_name"])
        result.update({
            "applied": True,
            "registered_entries": int(imported["retained"]),
            "new_entries": new_entries,
            "new_companies": len(new_company_names),
            "excluded_unusable": int(imported["excluded_unusable"]),
            "excluded_reasons": imported.get("excluded_reasons", {}),
            "out_of_scope": int(imported["out_of_scope"]),
            # The full ID set is persisted; these IDs are only a bounded sample.
            "registered_ids": registered_ids[:20],
            "registered_ids_sample_count": min(20, len(registered_ids)),
            "registered_ids_limited": len(registered_ids) > 20,
        })
        company_ids_by_name: dict[str, set[str]] = {}
        with self.registry.storage.session() as db:
            companies = list(db.scalars(select(CompanySnapshot)))
        for company in companies:
            for candidate in [company.name, *(company.aliases or [])]:
                key = normalize_company_name(candidate)
                if key:
                    company_ids_by_name.setdefault(key, set()).add(company.id)

        linked_existing = 0
        with self.registry.storage.write_transaction() as db:
            unlinked_sources = list(db.scalars(select(CompanySourceRecord).where(
                CompanySourceRecord.source == "offerbiu",
                CompanySourceRecord.id.in_(registered_ids),
                CompanySourceRecord.company_id.is_(None),
                CompanySourceRecord.status != "unusable",
            )))
            for row in unlinked_sources:
                diagnosis = diagnose_candidate_entry(row.entry_url)
                if diagnosis.entry_kind in {"invalid_entry", "form_application"}:
                    row.status = "unusable"
                    row.failure_stage = "ingest"
                    row.reason_code = diagnosis.entry_kind
                    row.reason = diagnosis.reason
                    continue
                matches = company_ids_by_name.get(normalize_company_name(row.company_name), set())
                if len(matches) == 1:
                    row.company_id = next(iter(matches))
                    linked_existing += 1
        result["linked_existing_entries"] = linked_existing

        with self.registry.storage.session() as db:
            pending_filters = (
                CompanySourceRecord.source == "offerbiu",
                CompanySourceRecord.id.in_(registered_ids),
                CompanySourceRecord.company_id.is_(None),
                CompanySourceRecord.status != "unusable",
            )
            result["pending_entry_count"] = db.scalar(
                select(func.count()).select_from(CompanySourceRecord).where(*pending_filters)
            ) or 0
            pending_rows = list(db.scalars(
                select(CompanySourceRecord).where(*pending_filters).order_by(
                    CompanySourceRecord.updated_at.desc(),
                    CompanySourceRecord.id,
                ).limit(100)
            ))
        pending = []
        seen_company_names: set[str] = set()
        seen_entry_identities: set[str] = set()
        for row in pending_rows:
            name_key = row.company_name.strip().casefold()
            entry_identity = source_identity_for_url(row.entry_url)
            if (
                not name_key
                or name_key in seen_company_names
                or (entry_identity and entry_identity in seen_entry_identities)
            ):
                continue
            seen_company_names.add(name_key)
            if entry_identity:
                seen_entry_identities.add(entry_identity)
            pending.append(row)
            if len(pending) >= 20:
                break
        result["pending_entries"] = [
            {
                "record_id": row.id,
                "company_name": row.company_name,
                "entry_url": row.entry_url,
                "status": row.status,
                "job_count": row.job_count,
            }
            for row in pending
        ]
        result["pending_entries_sample_count"] = len(pending)
        result["pending_entries_limited"] = result["pending_entry_count"] > len(pending)
        return result


__all__ = [
    "OFFERBIU_COMPANIES_URL",
    "OFFERBIU_ENDPOINT",
    "OfferBiuRefreshService",
    "OfferBiuCheckpointError",
    "capture_offerbiu_snapshot",
]
