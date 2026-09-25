"""Import crawlable OfferBiu company entries into the source registry."""

from collections.abc import Mapping
from hashlib import sha256
from typing import Any, Callable

from packages.config import DEFAULT_OFFERBIU_INDUSTRY_GROUPS, OFFERBIU_INDUSTRY_GROUP_OPTIONS

from .company_registry import CompanySourceRegistry
from .oc_capture import classify_oc_destination_url


INDUSTRY_GROUPS = frozenset(DEFAULT_OFFERBIU_INDUSTRY_GROUPS)


def selected_industry_groups(scope: Mapping[str, Any] | None = None) -> frozenset[str]:
    if scope is None:
        return INDUSTRY_GROUPS
    values = scope.get("industry_groups")
    allowed = {code for code, _ in OFFERBIU_INDUSTRY_GROUP_OPTIONS}
    if (not isinstance(values, (list, tuple)) or not values
            or any(not isinstance(value, str) or value not in allowed for value in values)):
        raise ValueError("scope.industry_groups must contain supported industry codes")
    return frozenset(values)


def offerbiu_row_error(item: Any) -> str | None:
    """Validate data boundaries before they can interrupt a source batch."""
    if not isinstance(item, Mapping):
        return "invalid_row_structure"
    record_id = item.get("id")
    if (isinstance(record_id, bool) or not isinstance(record_id, (str, int))
            or not str(record_id).strip()):
        return "missing_record_id"
    if len(str(record_id)) > 495:  # URL hash suffix occupies 17 registry characters.
        return "invalid_record_id"
    company = item.get("companyName")
    if not isinstance(company, str) or not company.strip():
        return "missing_company_name"
    if len(company.strip()) > 255:
        return "invalid_company_name"
    urls = item.get("applyUrl")
    if urls is not None and not isinstance(urls, (str, list)):
        return "invalid_entry_structure"
    if isinstance(urls, list) and any(not isinstance(url, str) for url in urls):
        return "invalid_entry_structure"
    return None


def import_offerbiu_sources(registry: CompanySourceRegistry, snapshot: Mapping[str, Any],
                           *, scope: Mapping[str, Any] | None = None,
                           on_registered: Callable[[str], None] | None = None) -> dict:
    groups = selected_industry_groups(scope)
    if snapshot.get("source") != "offerbiu" or not isinstance(snapshot.get("items"), list):
        raise ValueError("Expected an OfferBiu snapshot with items")
    if snapshot.get("previewLimited") is True:
        raise ValueError("Restricted OfferBiu preview rows cannot be registered")
    filters = snapshot.get("filters")
    # Only verified normal API pages inherit the actual request's entire scope.
    # Legacy completed year-filtered snapshots retain their existing year trust;
    # unfiltered imports still need row-level evidence for every scope dimension.
    trusted_scope = (
        snapshot.get("scope_verified") is True
        and isinstance(filters, Mapping)
        and filters.get("seasonYear") == 2027
        and filters.get("recruitType") == "秋招"
        and isinstance(filters.get("industryGroups"), list)
        and all(isinstance(value, str) for value in filters["industryGroups"])
        and set(filters["industryGroups"]) == groups
    )
    trusted_2027_scope = (
        trusted_scope or (snapshot.get("complete") is True
        and isinstance(filters, Mapping)
        and filters.get("seasonYear") == 2027)
    )
    result = {"retained": 0, "excluded_unusable": 0, "excluded_reasons": {},
              "out_of_scope": 0, "quarantined_rows": 0, "quarantine_reasons": {},
              "duplicate_ids": 0, "conflicting_ids": 0, "ids": []}

    def quarantine(reason: str) -> None:
        result["quarantined_rows"] += 1
        reasons = result["quarantine_reasons"]
        reasons[reason] = reasons.get(reason, 0) + 1

    # Resolve duplicates before the first write so an ambiguous ID never picks
    # whichever row happened to arrive first.
    unique: dict[str, Mapping[str, Any]] = {}
    conflicts: set[str] = set()
    for item in snapshot["items"]:
        error = offerbiu_row_error(item)
        if error:
            quarantine(error)
            continue
        key = str(item["id"])
        if key in conflicts:
            result["duplicate_ids"] += 1
            continue
        if key in unique:
            result["duplicate_ids"] += 1
            if unique[key] != item:
                conflicts.add(key)
                unique.pop(key)
                result["conflicting_ids"] += 1
                quarantine("conflicting_record_id")
            continue
        unique[key] = item

    for item in unique.values():
        years = item.get("targetYears")
        industries = item.get("industryGroupCodes")
        year_matches = isinstance(years, list) and 2027 in years
        industry_matches = (isinstance(industries, list)
                            and any(isinstance(value, str) and value in groups for value in industries))
        if ((not trusted_2027_scope and not year_matches)
                or (not trusted_scope and (item.get("recruitType") != "秋招" or not industry_matches))):
            result["out_of_scope"] += 1
            continue
        urls = item.get("applyUrl") or ""
        urls = list(dict.fromkeys(urls)) if isinstance(urls, list) else [urls]
        for url in urls or [""]:
            try:
                if len(url) > 2048:
                    raise ValueError("entry_url exceeds 2048 characters")
                exclusion = classify_oc_destination_url(url) if url else (
                    "missing_entry",
                    "No public application URL supplied.",
                )
            except ValueError:
                quarantine("invalid_entry_url")
                continue
            if exclusion:
                # The next full OfferBiu discovery pass evaluates the source
                # again.  Invalid entries are run evidence, not catalog rows.
                result["excluded_unusable"] += 1
                code = exclusion[0]
                result["excluded_reasons"][code] = result["excluded_reasons"].get(code, 0) + 1
                continue
            record_id = f"{item['id']}:{sha256(url.encode('utf-8')).hexdigest()[:16]}"
            try:
                record = registry.upsert_source(
                    source="offerbiu", source_record_id=record_id,
                    company_name=item["companyName"],
                    source_url=str(snapshot.get("source_url") or "https://offerbiu.com/companies/"),
                    entry_url=url,
                )
            except ValueError:
                # Registry validation errors concern this row; database errors
                # deliberately propagate, preserving prior per-row commits.
                quarantine("registry_validation_error")
                continue
            result["retained"] += 1
            result["ids"].append(record["id"])
            if on_registered is not None:
                on_registered(record["id"])
    return result
