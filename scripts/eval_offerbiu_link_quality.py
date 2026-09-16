"""Evaluate OfferBiu application links from a local snapshot without network access."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit


GROUPS = (
    "internet-tech",
    "manufacturing-equipment",
    "auto-transport-equipment",
)
TARGET_COMPANIES_PER_GROUP = 10
MAX_COMPANIES = len(GROUPS) * TARGET_COMPANIES_PER_GROUP

CLASSIFICATIONS = (
    "official_or_unknown",
    "wechat_article",
    "form",
    "missing",
)
SAMPLE_COVERAGE_TAGS = (
    "known_ats",
    "self_built_or_unknown",
    "homepage_candidate",
    "wechat_article",
    "form",
    "missing",
)

_FORM_HOST_SUFFIXES = (
    "wj.qq.com",
    "wj.toutiao.com",
    "wjx.cn",
    "wjx.com",
    "jinshuju.com",
    "jinshuju.net",
    "wenjuan.com",
    "wenjuan.net",
    "jsj.top",
    "jsjform.com",
    "yunbiz.wps.cn",
)
_WECHAT_DOC_HOST_SUFFIXES = ("weixin.qq.com",)
_WECHAT_ARTICLE_HOST_SUFFIXES = ("weixin.qq.com", "weixinbridge.com")
_KNOWN_ATS_HOST_SUFFIXES = (
    "zhiye.com",
    "mokahr.com",
    "feishu.cn",
    "hotjob.cn",
    "zhaopin.com",
    "zhaopin.com.cn",
    "51job.com",
    "51job.com.cn",
    "iguopin.com",
    "campus.163.com",
    "tal.com",
    "liepin.com",
    "zhipin.com",
    "yingjiesheng.com",
    "hire66.com",
    "dingtalk.com",
    "xiao100.com",
)
_NAVIGATION_PATHS = frozenset(
    {
        "/",
        "/career",
        "/careers",
        "/campus",
        "/jobs",
        "/recruit",
        "/recruitment",
        "/campus-recruitment",
    }
)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else str(value).strip() if value is not None else ""


def _values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    elif not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        value = [value]
    result = {_text(item) for item in value}
    return sorted(item for item in result if item)


def _host_matches(host: str, suffixes: Sequence[str]) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)


def _strip_explicit_utm_parameters(query: str) -> str:
    """Drop only utm parameters while leaving every other query segment byte-for-byte."""

    if not query:
        return ""
    kept: list[str] = []
    for segment in query.split("&"):
        key = segment.split("=", 1)[0]
        decoded_key = unquote_plus(key).casefold()
        if decoded_key == "utm" or decoded_key.startswith("utm_"):
            continue
        kept.append(segment)
    return "&".join(kept)


def normalize_url(value: object) -> str | None:
    """Normalize a URL without deleting ATS routing or project parameters."""

    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if any(ord(char) < 32 for char in raw):
        return None
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc or not hostname:
        return None
    if any(char.isspace() for char in hostname):
        return None
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            _strip_explicit_utm_parameters(parsed.query),
            parsed.fragment if parsed.fragment.startswith(("/", "!")) else "",
        )
    )


def _is_wechat_doc_form(host: str, path: str) -> bool:
    if not _host_matches(host, _WECHAT_DOC_HOST_SUFFIXES):
        return False
    lowered = path.casefold()
    return bool(
        re.match(
            r"^/(?:form|forms|survey|questionnaire|smartsheet/form|smartsheet/forms)(?:/|$)",
            lowered,
        )
    )


def _is_chaoxing_form(host: str, path: str) -> bool:
    return host == "office.chaoxing.com" and path.casefold().startswith("/apps/forms/")


def _url_family(normalized_url: str, classification: str) -> str:
    parsed = urlsplit(normalized_url)
    host = (parsed.hostname or "").casefold()
    if classification == "missing":
        return "missing"
    if classification == "wechat_article":
        if _host_matches(host, ("weixinbridge.com",)):
            return "wechat_bridge"
        if _host_matches(host, ("doc.weixin.qq.com",)):
            return "wechat_doc"
        return "wechat_article"
    if classification == "form":
        return "form"
    if _host_matches(host, _KNOWN_ATS_HOST_SUFFIXES):
        return "known_ats"
    path = parsed.path.casefold().rstrip("/") or "/"
    if path in _NAVIGATION_PATHS:
        return "homepage_candidate"
    return "self_built_or_unknown"


def _coverage_tags(classification: str, family: str) -> list[str]:
    if classification in {"wechat_article", "form", "missing"}:
        return [classification]
    tags = [family]
    if family == "homepage_candidate":
        tags.append("self_built_or_unknown")
    return [tag for tag in SAMPLE_COVERAGE_TAGS if tag in tags]


def _classify_detail(value: object) -> dict[str, Any]:
    normalized_url = normalize_url(value)
    if normalized_url is None:
        if not isinstance(value, str) or not value.strip():
            reason = "missing_apply_url"
        else:
            reason = "invalid_http_url"
        return {
            "classification": "missing",
            "reason": reason,
            "normalized_url": None,
            "host": None,
            "family": "missing",
            "coverage_tags": ["missing"],
        }

    parsed = urlsplit(normalized_url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()
    if (
        _host_matches(host, _FORM_HOST_SUFFIXES)
        or _is_wechat_doc_form(host, path)
        or _is_chaoxing_form(host, path)
    ):
        classification = "form"
        reason = "known_form_entry"
    elif _host_matches(host, _WECHAT_ARTICLE_HOST_SUFFIXES):
        classification = "wechat_article"
        reason = (
            "wechat_bridge_entry"
            if _host_matches(host, ("weixinbridge.com",))
            else "known_wechat_entry"
        )
    else:
        classification = "official_or_unknown"
        reason = "valid_http_url_not_on_static_blocklist"
    family = _url_family(normalized_url, classification)
    return {
        "classification": classification,
        "reason": reason,
        "normalized_url": normalized_url,
        "host": host,
        "family": family,
        "coverage_tags": _coverage_tags(classification, family),
    }


def classify_apply_url(value: object) -> str:
    """Return one of the four static link-quality classes."""

    return str(_classify_detail(value)["classification"])


def _compact_company_key(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _company_identity(item: Mapping[str, Any], index: int) -> tuple[str, object, object]:
    company_id = item.get("companyId")
    company_name = item.get("companyName")
    company_id_text = _text(company_id)
    company_name_text = _text(company_name)
    if company_id_text:
        return f"id:{company_id_text.casefold()}", company_id, company_name
    if company_name_text:
        return f"name:{_compact_company_key(company_name_text)}", None, company_name
    record_id = _text(item.get("id"))
    fallback = record_id.casefold() if record_id else f"row-{index}"
    return f"anonymous:{fallback}", None, None


def _record_sort_key(record: Mapping[str, Any]) -> tuple[str, str, str, str, int]:
    return (
        str(record["_company_key"]),
        json.dumps(record.get("id"), ensure_ascii=False, sort_keys=True, default=str),
        str(record.get("normalized_apply_url") or ""),
        json.dumps(record.get("apply_url"), ensure_ascii=False, sort_keys=True, default=str),
        int(record["source_item_index"]),
    )


def _make_record(
    item: Mapping[str, Any],
    index: int,
    *,
    source: str,
    source_url: object,
) -> dict[str, Any]:
    company_key, _, _ = _company_identity(item, index)
    detail = _classify_detail(item.get("applyUrl"))
    codes = [code for code in _values(item.get("industryGroupCodes"))]
    return {
        "id": item.get("id"),
        "source": source,
        "source_url": source_url,
        "source_item_index": index,
        "company_id": item.get("companyId"),
        "company_name": item.get("companyName"),
        "company_key": company_key,
        "industry_group_codes": codes,
        "recruit_type": item.get("recruitType"),
        "target_years": item.get("targetYears"),
        "apply_url": item.get("applyUrl"),
        "normalized_apply_url": detail["normalized_url"],
        "announcement_url": item.get("announcementUrl"),
        "classification": detail["classification"],
        "classification_reason": detail["reason"],
        "url_host": detail["host"],
        "url_family": detail["family"],
        "quality_tags": detail["coverage_tags"],
        "_company_key": company_key,
    }


def _public_record(record: Mapping[str, Any], assigned_group: str | None = None) -> dict[str, Any]:
    public = {key: value for key, value in record.items() if not key.startswith("_")}
    if assigned_group is not None:
        public["assigned_industry_group"] = assigned_group
    return public


def _build_companies(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record["_company_key"])].append(record)

    companies: list[dict[str, Any]] = []
    for company_key in sorted(grouped):
        company_records = sorted(grouped[company_key], key=_record_sort_key)
        company_ids = [record.get("company_id") for record in company_records if _text(record.get("company_id"))]
        company_names = [record.get("company_name") for record in company_records if _text(record.get("company_name"))]
        eligible_groups = sorted(
            {
                code
                for record in company_records
                for code in record.get("industry_group_codes", [])
                if code in GROUPS
            },
            key=GROUPS.index,
        )
        industry_codes = sorted(
            {
                code
                for record in company_records
                for code in record.get("industry_group_codes", [])
            }
        )
        coverage_tags = [
            tag
            for tag in SAMPLE_COVERAGE_TAGS
            if any(tag in record.get("quality_tags", []) for record in company_records)
        ]
        companies.append(
            {
                "_company_key": company_key,
                "company_key": company_key,
                "company_id": company_ids[0] if company_ids else None,
                "company_name": company_names[0] if company_names else None,
                "industry_group_codes": industry_codes,
                "eligible_groups": eligible_groups,
                "quality_tags": coverage_tags,
                "records": company_records,
            }
        )
    return companies


def _candidate_rank(
    company: Mapping[str, Any],
    covered_tags: set[str],
) -> tuple[int, int, int, str]:
    new_tags = len(set(company["quality_tags"]) - covered_tags)
    is_single_group = len(company["eligible_groups"]) == 1
    return (
        -new_tags,
        -int(is_single_group),
        -len(company["records"]),
        str(company["_company_key"]),
    )


def _select_companies(companies: Sequence[Mapping[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    selected: dict[str, list[dict[str, Any]]] = {group: [] for group in GROUPS}
    used_company_keys: set[str] = set()
    covered_tags: set[str] = set()

    while len(used_company_keys) < MAX_COMPANIES:
        available_by_group: dict[str, list[Mapping[str, Any]]] = {}
        for group in GROUPS:
            if len(selected[group]) >= TARGET_COMPANIES_PER_GROUP:
                continue
            available_by_group[group] = [
                company
                for company in companies
                if company["_company_key"] not in used_company_keys
                and group in company["eligible_groups"]
            ]
        available_by_group = {
            group: companies_for_group
            for group, companies_for_group in available_by_group.items()
            if companies_for_group
        }
        if not available_by_group:
            break

        group = min(
            available_by_group,
            key=lambda value: (
                len(selected[value]) / TARGET_COMPANIES_PER_GROUP,
                len(selected[value]),
                len(available_by_group[value]),
                GROUPS.index(value),
            ),
        )
        company = min(available_by_group[group], key=lambda item: _candidate_rank(item, covered_tags))
        selected[group].append(dict(company))
        company_key = str(company["_company_key"])
        used_company_keys.add(company_key)
        covered_tags.update(company["quality_tags"])

    for group in GROUPS:
        selected[group].sort(key=lambda company: str(company["_company_key"]))
    return selected, used_company_keys


def _selected_company_payload(company: Mapping[str, Any], assigned_group: str) -> dict[str, Any]:
    company_records = [
        _public_record(record, assigned_group=assigned_group)
        for record in sorted(company["records"], key=_record_sort_key)
    ]
    return {
        "company_key": company["company_key"],
        "company_id": company["company_id"],
        "company_name": company["company_name"],
        "assigned_industry_group": assigned_group,
        "eligible_industry_groups": company["eligible_groups"],
        "industry_group_codes": company["industry_group_codes"],
        "quality_tags": company["quality_tags"],
        "record_count": len(company_records),
        "record_ids": [record.get("id") for record in company_records],
        "records": company_records,
    }


def _build_samples(companies: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    selected_by_group, selected_keys = _select_companies(companies)
    selected_companies: list[dict[str, Any]] = []
    group_payload: dict[str, Any] = {}
    for group in GROUPS:
        payload_companies = [
            _selected_company_payload(company, group)
            for company in selected_by_group[group]
        ]
        selected_companies.extend(payload_companies)
        shortfall = max(0, TARGET_COMPANIES_PER_GROUP - len(payload_companies))
        group_payload[group] = {
            "target_company_count": TARGET_COMPANIES_PER_GROUP,
            "selected_company_count": len(payload_companies),
            "shortfall": shortfall,
            "company_keys": [company["company_key"] for company in payload_companies],
        }

    selected_companies.sort(
        key=lambda company: (
            GROUPS.index(company["assigned_industry_group"]),
            str(company["company_key"]),
        )
    )
    selected_records = [
        record
        for company in selected_companies
        for record in company["records"]
    ]
    selected_tags = {
        tag
        for company in selected_companies
        for tag in company["quality_tags"]
    }
    underfilled_bucket_count = sum(
        group["shortfall"] > 0 for group in group_payload.values()
    )
    return {
        "schema_version": "offerbiu-link-quality-samples-v1",
        "read_only": True,
        "source": "offerbiu",
        "max_company_count": MAX_COMPANIES,
        "target_company_count_per_group": TARGET_COMPANIES_PER_GROUP,
        "underfilled_bucket_count": underfilled_bucket_count,
        "selected_company_count": len(selected_keys),
        "selected_record_count": len(selected_records),
        "coverage": {
            "required_tags": list(SAMPLE_COVERAGE_TAGS),
            "observed_tags": [tag for tag in SAMPLE_COVERAGE_TAGS if tag in selected_tags],
            "missing_tags": [tag for tag in SAMPLE_COVERAGE_TAGS if tag not in selected_tags],
        },
        "groups": group_payload,
        "selected_companies": selected_companies,
        "selected_records": selected_records,
    }


def evaluate_snapshot(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``samples.json`` and ``quality_static.json`` payloads."""

    if not isinstance(snapshot, Mapping):
        raise ValueError("snapshot must be a JSON object")
    items = snapshot.get("items")
    if not isinstance(items, list):
        raise ValueError("snapshot.items must be a list")
    source = _text(snapshot.get("source")) or "offerbiu"
    source_url = snapshot.get("source_url")
    records = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(f"snapshot.items[{index}] must be an object")
        records.append(
            _make_record(
                item,
                index,
                source=source,
                source_url=source_url,
            )
        )
    records.sort(key=_record_sort_key)
    companies = _build_companies(records)
    samples = _build_samples(companies)

    classification_counts = Counter(record["classification"] for record in records)
    family_counts = Counter(record["url_family"] for record in records)
    company_classification_counts = Counter()
    for company in companies:
        for classification in {
            record["classification"] for record in company["records"]
        }:
            company_classification_counts[classification] += 1
    normalized_entries = [
        record["normalized_apply_url"]
        for record in records
        if record["normalized_apply_url"]
    ]
    quality = {
        "schema_version": "offerbiu-link-quality-static-v1",
        "read_only": True,
        "source": source,
        "snapshot": {
            "complete": snapshot.get("complete"),
            "filters": snapshot.get("filters"),
            "pages": snapshot.get("pages"),
        },
        "counts": {
            "records": len(records),
            "independent_companies": len(companies),
            "normalized_entries": len(normalized_entries),
            "unique_normalized_entries": len(set(normalized_entries)),
        },
        "classifications": {
            "records": {
                classification: classification_counts.get(classification, 0)
                for classification in CLASSIFICATIONS
            },
            "independent_companies": {
                classification: company_classification_counts.get(classification, 0)
                for classification in CLASSIFICATIONS
            },
            "url_families": dict(sorted(family_counts.items())),
        },
        "sample_summary": {
            "underfilled_bucket_count": samples["underfilled_bucket_count"],
            "selected_company_count": samples["selected_company_count"],
            "selected_record_count": samples["selected_record_count"],
            "groups": samples["groups"],
            "coverage": samples["coverage"],
        },
        "records": [_public_record(record) for record in records],
    }
    return samples, quality


def load_snapshot(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read snapshot: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("snapshot must be a JSON object")
    return payload


def write_outputs(snapshot_path: Path, output_dir: Path) -> dict[str, Any]:
    snapshot = load_snapshot(snapshot_path)
    samples, quality = evaluate_snapshot(snapshot)
    output_dir.mkdir(parents=True, exist_ok=True)
    samples_path = output_dir / "samples.json"
    quality_path = output_dir / "quality_static.json"
    samples_path.write_text(
        json.dumps(samples, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    quality_path.write_text(
        json.dumps(quality, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "samples": str(samples_path),
        "quality_static": str(quality_path),
        "underfilled_bucket_count": samples["underfilled_bucket_count"],
        "selected_company_count": samples["selected_company_count"],
        "selected_record_count": samples["selected_record_count"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    summary = write_outputs(args.snapshot, args.output_dir)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
