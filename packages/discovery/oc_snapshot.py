"""Deterministic filtering for an Agent-owned GiveMeOC snapshot.

There is intentionally no browser, HTTP client, or login flow in this module.
The only accepted input is an in-memory snapshot mapping or a local JSON file.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import SourceLead, SourceSyncResult, compact_text
from .oc_capture import classify_oc_destination_url, normalize_oc_destination_url
from .reconciliation import normalize_company_name, source_identity_for_url


PRIVATE_COMPANY_TYPE = "民企"
TARGET_COHORT = "2027"
ALLOWED_RECRUITMENT_TYPES = frozenset({"秋招", "秋招提前批"})
TARGET_INDUSTRY_KEYWORDS = (
    "软件",
    "科技",
    "互联网",
    "机器人",
    "人工智能",
    "游戏",
    "新能源",
    "车企",
)
OC_FILTER_VERSION = "oc-private-2027-autumn-industry-v1"
_TYPE_SEPARATOR_RE = re.compile(r"[,，、/|;；\n]+")


class OcSnapshotError(ValueError):
    """Raised when a local OC snapshot cannot be interpreted safely."""


def load_oc_snapshot(path: str | Path) -> dict[str, Any]:
    """Load one local JSON snapshot without contacting the OC website."""

    snapshot_path = Path(path).expanduser()
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OcSnapshotError(f"unable to read OC snapshot: {snapshot_path}") from exc
    if not isinstance(payload, dict):
        raise OcSnapshotError("OC snapshot must contain a JSON object")
    return payload


def _snapshot_payload(snapshot: Mapping[str, Any] | str | Path) -> Mapping[str, Any]:
    if isinstance(snapshot, (str, Path)):
        return load_oc_snapshot(snapshot)
    if not isinstance(snapshot, Mapping):
        raise OcSnapshotError("OC snapshot must be a mapping or local JSON path")
    return snapshot


def _value(record: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in record:
            return record[name]
    return None


def _text(value: object) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(compact_text(item) for item in value if compact_text(item))
    return compact_text(value)


def _values(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (compact_text(value),) if compact_text(value) else ()
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        result: list[str] = []
        for item in value:
            text = compact_text(item)
            if text and text not in result:
                result.append(text)
        return tuple(result)
    text = compact_text(value)
    return (text,) if text else ()


def _company(record: Mapping[str, Any]) -> str:
    return _text(_value(record, "company", "name", "company_name"))


def _company_type(record: Mapping[str, Any]) -> str:
    return _text(_value(record, "company_type", "companyType"))


def _recruitment_target(record: Mapping[str, Any]) -> str:
    return _text(_value(record, "recruitment_target", "recruitmentTarget", "recruitTargets"))


def _recruitment_type(record: Mapping[str, Any]) -> object:
    return _value(record, "recruitment_type", "recruitmentType", "recruitTypes")


def _industry(record: Mapping[str, Any]) -> object:
    return _value(record, "industry", "industries", "industryTags")


def _apply_urls(record: Mapping[str, Any]) -> tuple[str, ...]:
    value = _value(
        record,
        "resolved_apply_urls",
        "resolvedApplyUrls",
        "apply_url",
        "applyUrl",
        "apply_urls",
        "applyUrls",
    )
    result: list[str] = []
    for value_url in _values(value):
        url = normalize_oc_destination_url(value_url)
        if classify_oc_destination_url(url) is None and url not in result:
            result.append(url)
    return tuple(result)


def _is_excluded_non_job_entry(record: Mapping[str, Any]) -> bool:
    if _text(record.get("link_resolution")) == "excluded_non_job_entry":
        return not _apply_urls(record)
    resolved = _values(record.get("resolved_apply_urls"))
    return bool(resolved) and all(classify_oc_destination_url(url) for url in resolved)


def _recruitment_type_components(value: object) -> frozenset[str]:
    values = _values(value)
    components: set[str] = set()
    for item in values:
        components.update(part.strip() for part in _TYPE_SEPARATOR_RE.split(item) if part.strip())
    return frozenset(components)


def is_private_company(record: Mapping[str, Any]) -> bool:
    """Require the exact private-company label selected in OC."""

    return _company_type(record) == PRIVATE_COMPANY_TYPE


def is_target_cohort(record: Mapping[str, Any]) -> bool:
    """Accept a record only when its recruitment target explicitly contains 2027."""

    return TARGET_COHORT in _recruitment_target(record)


def is_allowed_recruitment_type(record: Mapping[str, Any]) -> bool:
    """Accept a row when it contains autumn or early-autumn recruitment."""

    components = _recruitment_type_components(_recruitment_type(record))
    return bool(components & ALLOWED_RECRUITMENT_TYPES)


def matches_target_industry(record: Mapping[str, Any]) -> bool:
    """Match any requested keyword by containment, including list-valued fields."""

    industry = _text(_industry(record))
    return any(keyword in industry for keyword in TARGET_INDUSTRY_KEYWORDS)


def is_eligible_record(record: Mapping[str, Any]) -> bool:
    return (
        is_private_company(record)
        and is_target_cohort(record)
        and is_allowed_recruitment_type(record)
        and matches_target_industry(record)
    )


def _filter_reason(record: Mapping[str, Any], conflict_keys: set[str]) -> str | None:
    company_key = normalize_company_name(_company(record))
    if not company_key:
        return "missing_company"
    if company_key in conflict_keys:
        return "company_type_conflict"
    if not is_private_company(record):
        return "company_type"
    if not is_target_cohort(record):
        return "cohort"
    if not is_allowed_recruitment_type(record):
        return "recruitment_type"
    if not matches_target_industry(record):
        return "industry"
    if _is_excluded_non_job_entry(record):
        return "excluded_non_job_entry"
    return None


def _pagination_evidence(source: Mapping[str, Any], record_count: int) -> dict[str, Any]:
    pagination = source.get("pagination")
    if not isinstance(pagination, Mapping):
        return {
            "complete": False,
            "termination_reason": "pagination_evidence_missing",
            "total_pages": None,
            "advertised_total_items": None,
            "page_counts_total": None,
            "record_count": record_count,
        }
    total_pages = pagination.get("total_pages")
    total_items = pagination.get("total_items")
    page_counts = pagination.get("page_counts")
    page_counts_valid = (
        isinstance(page_counts, list)
        and all(isinstance(value, int) and value >= 0 for value in page_counts)
    )
    page_counts_total = sum(page_counts) if page_counts_valid else None
    complete = bool(
        pagination.get("complete") is True
        and isinstance(total_pages, int)
        and total_pages >= 1
        and page_counts_valid
        and len(page_counts) == total_pages
        and isinstance(total_items, int)
        and total_items == record_count
        and page_counts_total == total_items
    )
    if complete:
        reason = "advertised_last_page_reached"
    elif pagination.get("complete") is not True:
        reason = "source_reported_incomplete"
    else:
        reason = "pagination_evidence_inconsistent"
    return {
        "complete": complete,
        "termination_reason": reason,
        "total_pages": total_pages if isinstance(total_pages, int) else None,
        "advertised_total_items": total_items if isinstance(total_items, int) else None,
        "page_counts_total": page_counts_total,
        "record_count": record_count,
    }


def _records(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    records = snapshot.get("records")
    if not isinstance(records, list):
        raise OcSnapshotError("OC snapshot must contain a records list")
    if not all(isinstance(record, Mapping) for record in records):
        raise OcSnapshotError("OC snapshot records must be JSON objects")
    return list(records)


def _type_conflict_keys(records: Sequence[Mapping[str, Any]]) -> set[str]:
    all_types: dict[str, set[str]] = defaultdict(set)
    for record in records:
        company_key = normalize_company_name(_company(record))
        if company_key:
            all_types[company_key].add(_company_type(record))
    return {
        company_key
        for company_key, types in all_types.items()
        if PRIVATE_COMPANY_TYPE in types and len(types) > 1
    }


def _unique_values(rows: Sequence[Mapping[str, Any]], extractor) -> tuple[str, ...]:
    values: set[str] = set()
    for row in rows:
        value = extractor(row)
        if isinstance(value, (tuple, list, set, frozenset)):
            values.update(_text(item) for item in value if _text(item))
        else:
            text = _text(value)
            if text:
                values.add(text)
    return tuple(sorted(values, key=lambda item: (item.casefold(), item)))


def filter_oc_snapshot(snapshot: Mapping[str, Any] | str | Path) -> SourceSyncResult:
    """Return strictly eligible, company-grouped leads from a local snapshot.

    The source snapshot is never changed.  Companies whose rows carry both a
    private and another company type are quarantined rather than treated as
    private, preserving a deterministic safety boundary for later review.
    """

    source = _snapshot_payload(snapshot)
    records = _records(source)
    conflict_keys = _type_conflict_keys(records)
    exclusion_counts: dict[str, int] = defaultdict(int)
    eligible_records: list[Mapping[str, Any]] = []
    for record in records:
        reason = _filter_reason(record, conflict_keys)
        if reason is None:
            eligible_records.append(record)
        else:
            exclusion_counts[reason] += 1
    source_eligible_records = [
        record for record in records
        if _filter_reason(record, conflict_keys) in {None, "excluded_non_job_entry"}
    ]

    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in eligible_records:
        grouped[normalize_company_name(_company(record))].append(record)

    leads: list[SourceLead] = []
    for company_key in sorted(grouped):
        rows = grouped[company_key]
        display_name = sorted(
            {_company(row) for row in rows},
            key=lambda item: (item.casefold(), item),
        )[0]
        urls = sorted(
            {url for row in rows for url in _apply_urls(row)},
            key=lambda item: (item.casefold(), item),
        )
        identities = sorted(
            {
                identity
                for url in urls
                if (identity := source_identity_for_url(url))
            }
        )
        metadata = {
            "company_type": PRIVATE_COMPANY_TYPE,
            "industries": _unique_values(rows, _industry),
            "recruitment_types": _unique_values(rows, _recruitment_type),
            "recruitment_targets": _unique_values(rows, _recruitment_target),
            "apply_urls": urls,
            "source_rows": len(rows),
        }
        leads.append(
            SourceLead(
                canonical_name=display_name,
                source="oc_snapshot",
                source_name=display_name,
                source_urls=tuple(urls),
                source_identity=identities[0] if len(identities) == 1 else None,
                metadata=metadata,
            )
        )

    pagination_evidence = _pagination_evidence(source, len(records))
    pages_fetched = pagination_evidence["total_pages"] or 0
    source_url = _text(source.get("source_url") or source.get("source"))
    captured_at = _text(source.get("captured_at") or source.get("source_captured_at")) or None
    metadata = {
        "filters": dict(source.get("filters") or {}) if isinstance(source.get("filters"), Mapping) else {},
        "filter_version": OC_FILTER_VERSION,
        "private_company_type": PRIVATE_COMPANY_TYPE,
        "target_cohort_contains": TARGET_COHORT,
        "allowed_recruitment_types": tuple(sorted(ALLOWED_RECRUITMENT_TYPES)),
        "industry_contains_any": TARGET_INDUSTRY_KEYWORDS,
        "quarantined_company_type_conflicts": len(conflict_keys),
        "eligible_rows_before_entry_validation": len(source_eligible_records),
        "excluded_non_job_entry_rows": len(source_eligible_records) - len(eligible_records),
        "exclusion_counts": dict(sorted(exclusion_counts.items())),
        "pagination_evidence": pagination_evidence,
    }
    return SourceSyncResult(
        source="oc_snapshot",
        source_url=source_url,
        leads=tuple(leads),
        rows_seen=len(records),
        accepted_rows=len(eligible_records),
        pages_fetched=pages_fetched,
        captured_at=captured_at,
        metadata=metadata,
    )


filter_oc_companies = filter_oc_snapshot
filter_snapshot = filter_oc_snapshot


__all__ = [
    "ALLOWED_RECRUITMENT_TYPES",
    "OcSnapshotError",
    "OC_FILTER_VERSION",
    "PRIVATE_COMPANY_TYPE",
    "TARGET_COHORT",
    "TARGET_INDUSTRY_KEYWORDS",
    "filter_oc_companies",
    "filter_oc_snapshot",
    "filter_snapshot",
    "is_allowed_recruitment_type",
    "is_eligible_record",
    "is_private_company",
    "is_target_cohort",
    "load_oc_snapshot",
    "matches_target_industry",
]
