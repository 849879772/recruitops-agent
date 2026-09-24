"""Bounded OfferBiu discovery refresh with all-or-nothing source registration."""

from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Mapping
import time
from typing import Any, Callable

import requests
from sqlalchemy import func, select

from packages.storage.models import CompanySnapshot
from packages.recruitment_core.entry import diagnose_candidate_entry

from .company_registry import CompanySourceRecord, CompanySourceRegistry
from .offerbiu_registry import import_offerbiu_sources, selected_industry_groups
from .reconciliation import normalize_company_name, source_identity_for_url


OFFERBIU_ENDPOINT = "https://offerbiu.com/api/recruitment/postings"
OFFERBIU_COMPANIES_URL = "https://offerbiu.com/companies/"


def capture_offerbiu_snapshot(
    *,
    max_pages: int = 150,
    page_size: int = 9,
    delay_seconds: float = 0.25,
    session: Any | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    scope: Mapping[str, Any] | None = None,
    progress_callback: Callable[[int, int, int], None] | None = None,
) -> dict[str, Any]:
    """Read a validated 2027 autumn-recruitment snapshot from the public API."""

    groups = selected_industry_groups(scope)
    client = session or requests.Session()
    if session is None:
        # BIU is a public source and does not need the local browser proxy or cookies.
        client.trust_env = False
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
        "filters": {
            "seasonYear": 2027,
            "recruitType": "秋招",
            "industryGroups": sorted(groups),
        },
        "pages": [],
        "items": [],
        "complete": False,
        "stop_reason": None,
    }
    seen: set[str] = set()
    baseline: tuple[int, int] | None = None
    try:
        for page in range(max_pages):
            if hasattr(client, "cookies"):
                client.cookies.clear()
            response = client.get(
                OFFERBIU_ENDPOINT,
                params=[*params, ("page", str(page))],
                timeout=20,
                allow_redirects=False,
            )
            if response.status_code != 200:
                result["stop_reason"] = f"http_{response.status_code}"
                break
            payload = response.json()
            data = payload.get("data") or {}
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
                or not isinstance(total_items, int)
                or not isinstance(total_pages, int)
            ):
                result["stop_reason"] = "pagination_mismatch"
                break
            current_baseline = (total_items, total_pages)
            if baseline is None:
                baseline = current_baseline
            elif current_baseline != baseline:
                result["stop_reason"] = "source_changed_during_capture"
                break
            row_ids = [str(row.get("id") or "") for row in rows]
            if any(not row_id or row_id in seen for row_id in row_ids):
                result["stop_reason"] = "duplicate_or_missing_record_id"
                break
            if any(
                row.get("recruitType") != "秋招"
                or 2027 not in row.get("targetYears", [])
                or not groups.intersection(row.get("industryGroupCodes", []))
                for row in rows
            ):
                result["stop_reason"] = "source_filter_mismatch"
                break
            seen.update(row_ids)
            result["pages"].append({
                key: data.get(key)
                for key in ("page", "size", "totalItems", "totalPages")
            })
            result["items"].extend(rows)
            if progress_callback is not None:
                progress_callback(page + 1, total_pages, len(result["items"]))
            if page + 1 >= total_pages:
                result["complete"] = len(seen) == total_items
                result["stop_reason"] = "complete" if result["complete"] else "total_mismatch"
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
    return result


class OfferBiuRefreshService:
    """Discover and register usable BIU company entries after full validation."""

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
    ) -> dict[str, Any]:
        snapshot = capture_offerbiu_snapshot(
            max_pages=max_pages,
            page_size=page_size,
            delay_seconds=delay_seconds,
            session=self.session,
            scope=self.scope,
            progress_callback=progress_callback,
        )
        self.last_registered_ids = ()
        result: dict[str, Any] = {
            "complete": bool(snapshot["complete"]),
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
        if not snapshot["complete"] or not apply:
            return result

        with self.registry.storage.session() as db:
            existing_companies = set(db.scalars(select(CompanySourceRecord.company_name)))
            existing_companies.update(db.scalars(select(CompanySnapshot.name)))
            existing = set(db.scalars(
                select(CompanySourceRecord.source_record_id).where(
                    CompanySourceRecord.source == "offerbiu"
                )
            ))
        imported = import_offerbiu_sources(self.registry, snapshot, scope=self.scope)
        registered_ids = list(imported["ids"])
        self.last_registered_ids = tuple(registered_ids)
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
    "capture_offerbiu_snapshot",
]
