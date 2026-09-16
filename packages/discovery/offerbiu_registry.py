"""Import crawlable OfferBiu company entries into the source registry."""

from collections.abc import Mapping
from hashlib import sha256
from typing import Any

from .company_registry import CompanySourceRegistry
from .oc_capture import classify_oc_destination_url


INDUSTRY_GROUPS = frozenset({
    "internet-tech", "manufacturing-equipment", "auto-transport-equipment",
})


def import_offerbiu_sources(registry: CompanySourceRegistry, snapshot: Mapping[str, Any]) -> dict:
    if snapshot.get("source") != "offerbiu" or not isinstance(snapshot.get("items"), list):
        raise ValueError("Expected an OfferBiu snapshot with items")
    result = {"retained": 0, "excluded_unusable": 0, "excluded_reasons": {}, "out_of_scope": 0, "ids": []}
    for item in snapshot["items"]:
        if (2027 not in item.get("targetYears", []) or item.get("recruitType") != "秋招"
                or not INDUSTRY_GROUPS.intersection(item.get("industryGroupCodes", []))):
            result["out_of_scope"] += 1
            continue
        if not item.get("id") or not item.get("companyName"):
            raise ValueError("Source record must contain its stable ID and company name")
        urls = item.get("applyUrl") or ""
        urls = list(dict.fromkeys(urls)) if isinstance(urls, list) else [str(urls)]
        for url in urls or [""]:
            exclusion = classify_oc_destination_url(url) if url else (
                "missing_entry",
                "No public application URL supplied.",
            )
            if exclusion:
                # The next full OfferBiu discovery pass evaluates the source
                # again.  Invalid entries are run evidence, not catalog rows.
                result["excluded_unusable"] += 1
                code = exclusion[0]
                result["excluded_reasons"][code] = result["excluded_reasons"].get(code, 0) + 1
                continue
            record_id = f"{item['id']}:{sha256(url.encode('utf-8')).hexdigest()[:16]}"
            record = registry.upsert_source(
                source="offerbiu", source_record_id=record_id,
                company_name=item["companyName"],
                source_url=str(snapshot.get("source_url") or "https://offerbiu.com/companies/"),
                entry_url=url,
            )
            result["retained"] += 1
            result["ids"].append(record["id"])
    return result
