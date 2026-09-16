"""Exact company reconciliation for discovery results.

This module only reads the supplied company records.  It deliberately has no
YAML writer and no dependency on the legacy autumn-system repository.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qsl, parse_qs, urlsplit, urlunsplit

from .models import CompanyReconciliationResult, SourceLead, compact_text
from .oc_capture import normalize_oc_destination_url


_TRACKING_QUERY_KEYS = {
    "from",
    "recommendcode",
    "ref",
    "referrer",
    "shareid",
    "source",
}
_PROJECT_SUFFIX_RE = re.compile(
    r"(?:计划|项目|专项|专场|储备|招聘|校招|校园|管培|人才工程|program|project|recruit)",
    re.I,
)
_PROJECT_BOUNDARY_RE = re.compile(r"^[\s\-—_·:：/（(]+")


def normalize_company_name(value: object) -> str:
    """Return a conservative exact-match key for a company display name.

    Unicode compatibility forms, case, whitespace and punctuation are
    normalized.  No legal-suffix removal, substring matching, or fuzzy
    similarity is performed, so similarly named companies remain distinct.
    """

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in text if character.isalnum())


def _canonical_query(url: str, *, keep: set[str] | None = None) -> str:
    pairs = []
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        key_lower = key.casefold()
        if key_lower.startswith("utm_") or key_lower in _TRACKING_QUERY_KEYS:
            continue
        if keep is not None and key_lower not in keep:
            continue
        pairs.append((key_lower, value))
    return "&".join(f"{key}={value}" for key, value in sorted(pairs))


def _host_without_default_port(parsed: Any) -> str:
    host = str(parsed.hostname or "").casefold().removeprefix("www.")
    port = parsed.port
    if port and not ((parsed.scheme.casefold() == "https" and port == 443) or (parsed.scheme.casefold() == "http" and port == 80)):
        host = f"{host}:{port}"
    return host


def source_identity_for_url(url: object, source_kind: str | None = None) -> str:
    """Build a stable source identity suitable for exact fallback matching.

    Known ATS platforms use their durable tenant/project identity.  Unknown
    web sources retain host and path, avoiding a broad host-only match that
    could incorrectly merge unrelated companies.
    """

    raw = normalize_oc_destination_url(compact_text(url))
    if not raw:
        return ""
    parsed = urlsplit(raw)
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.netloc:
        return ""

    host = _host_without_default_port(parsed)
    path = parsed.path.rstrip("/") or "/"
    kind = compact_text(source_kind).casefold()
    is_moka = (
        "mokahr.com" in host
        or kind == "moka"
        or re.search(r"/(?:campus_apply|campus-recruitment)/", path, re.I) is not None
    )
    is_hotjob = "hotjob.cn" in host or kind == "hotjob"
    is_feishu = "feishu.cn" in host or "mioffice.cn" in host or kind == "feishu"
    is_beisen = host.endswith(".zhiye.com") or "/campus/jobs" in path.casefold() or kind == "beisen"
    is_alibaba = "alibaba.com" in host or kind == "alibaba"

    if is_moka:
        match = re.search(r"/(?:campus_apply|campus-recruitment)/([^/?#]+)", path, re.I)
        if match:
            return f"moka:{match.group(1).casefold()}"
        return f"moka:{host}:{path.casefold()}"

    if is_hotjob:
        match = re.search(r"/(SU[0-9a-z]+)", path, re.I)
        if match:
            return f"hotjob:{match.group(1).casefold()}"
        return f"hotjob:{host}:{path.casefold()}"

    if is_feishu:
        return f"feishu:{host}"

    if is_beisen:
        query = parse_qs(parsed.query)
        department = (query.get("p") or query.get("department") or [""])[0]
        route = path.casefold() or "/campus/jobs"
        return f"beisen:{host}:{route}:{department}"

    if is_alibaba:
        query = parse_qs(parsed.query)
        batch_id = (query.get("batchId") or [""])[0]
        raw_filters = (query.get("filterParams") or [""])[0]
        try:
            filters = json.dumps(
                json.loads(raw_filters),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) if raw_filters else ""
        except (TypeError, ValueError, json.JSONDecodeError):
            filters = raw_filters
        qualifier = filters
        if qualifier:
            return f"alibaba:{host}:{batch_id}:{qualifier}"
        return f"alibaba:{host}:{batch_id}"

    query = _canonical_query(raw)
    suffix = f"?{query}" if query else ""
    return f"web:{host}:{path.casefold()}{suffix}"


source_identity = source_identity_for_url


def consolidate_source_leads(leads: Iterable[SourceLead]) -> tuple[SourceLead, ...]:
    """Collapse projects that resolve to the same reusable recruitment source.

    The operation is source-oriented: it does not claim that similarly named
    companies are the same legal entity.  Every original project name remains
    in metadata so one crawl result can be mapped back to all source rows.
    """

    rows = list(leads)
    parent = list(range(len(rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    identity_owner: dict[str, int] = {}
    identities_by_row: list[set[str]] = []
    for index, lead in enumerate(rows):
        identities = _lead_identities(lead)
        identities_by_row.append(identities)
        for identity in identities:
            owner = identity_owner.setdefault(identity, index)
            union(index, owner)

    grouped: dict[int, list[int]] = {}
    for index in range(len(rows)):
        grouped.setdefault(find(index), []).append(index)

    consolidated: list[SourceLead] = []
    for indexes in grouped.values():
        members = [rows[index] for index in indexes]
        if len(members) == 1:
            lead = members[0]
            normalized_urls = tuple(dict.fromkeys(
                normalize_oc_destination_url(url) for url in lead.source_urls
            ))
            consolidated.append(replace(lead, source_urls=normalized_urls))
            continue

        names = tuple(dict.fromkeys(member.canonical_name for member in members))
        canonical = min(
            names,
            key=lambda name: (
                1 if _PROJECT_SUFFIX_RE.search(name) else 0,
                len(normalize_company_name(name)),
                name.casefold(),
            ),
        )
        urls = tuple(dict.fromkeys(
            normalize_oc_destination_url(url)
            for member in members
            for url in member.source_urls
        ))
        identities = sorted({
            identity for index in indexes for identity in identities_by_row[index]
        })
        metadata: dict[str, Any] = {}
        for member in members:
            for key, value in member.metadata.items():
                if key not in metadata and value not in (None, "", (), [], {}):
                    metadata[key] = value
        metadata["source_project_names"] = list(names)
        metadata["source_project_count"] = len(names)
        metadata["source_group_identities"] = identities
        metadata["source_rows"] = sum(
            int(member.metadata.get("source_rows") or 1) for member in members
        )
        consolidated.append(SourceLead(
            canonical_name=canonical,
            source=members[0].source,
            source_name=canonical,
            source_urls=urls,
            source_identity=identities[0] if len(identities) == 1 else None,
            metadata=metadata,
        ))

    consolidated.sort(key=lambda lead: (
        normalize_company_name(lead.canonical_name),
        lead.source.casefold(),
        lead.source_urls,
    ))
    return tuple(consolidated)


def _field(record: object, *names: str) -> Any:
    if isinstance(record, Mapping):
        for name in names:
            if name in record:
                return record[name]
        return None
    for name in names:
        if hasattr(record, name):
            return getattr(record, name)
    return None


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Sequence[object] = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = value
    else:
        values = (value,)
    result: list[str] = []
    for item in values:
        text = compact_text(item)
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _company_name(record: object) -> str:
    return compact_text(_field(record, "name", "company", "company_name"))


def _company_aliases(record: object) -> tuple[str, ...]:
    return _strings(_field(record, "aliases", "alias"))


_URL_FIELDS = (
    "careers_url",
    "campus_url",
    "url",
    "source_url",
    "source_urls",
    "campaign_url",
    "campaign_urls",
    "apply_url",
    "apply_urls",
)


def _company_source_identities(record: object) -> set[str]:
    identities = {
        compact_text(_field(record, "source_identity"))
    }
    for field_name in _URL_FIELDS:
        for url in _strings(_field(record, field_name)):
            identity = source_identity_for_url(url, _field(record, "crawler", "crawler_key"))
            if identity:
                identities.add(identity)
    return {identity for identity in identities if identity}


def _record_key(record: object, index: int) -> str:
    explicit_id = compact_text(_field(record, "id", "key", "company_id"))
    if explicit_id:
        return f"id:{explicit_id}"
    return f"row:{index}"


def _record_display_name(record: object, index: int) -> str:
    return _company_name(record) or compact_text(_field(record, "id", "key", "company_id")) or f"row-{index}"


def _project_parent_keys(lead_name: str, records: Mapping[str, tuple[str, object]]) -> set[str]:
    """Match explicit recruitment-project names to one configured parent company."""

    normalized_lead = unicodedata.normalize("NFKC", compact_text(lead_name)).casefold()
    candidates: list[tuple[int, str]] = []
    for key, (display_name, record) in records.items():
        names = (_company_name(record), *_company_aliases(record), display_name)
        for name in names:
            candidate = unicodedata.normalize("NFKC", compact_text(name)).casefold()
            if len(normalize_company_name(candidate)) < 4 or not normalized_lead.startswith(candidate):
                continue
            suffix = normalized_lead[len(candidate):]
            boundary = _PROJECT_BOUNDARY_RE.match(suffix)
            if boundary is None:
                continue
            project_text = suffix[boundary.end():]
            if project_text and _PROJECT_SUFFIX_RE.search(project_text):
                candidates.append((len(candidate), key))
    if not candidates:
        return set()
    longest = max(length for length, _ in candidates)
    return {key for length, key in candidates if length == longest}


def _lead_identities(lead: SourceLead) -> set[str]:
    identities: set[str] = set()
    if lead.source_identity:
        identities.add(lead.source_identity)
    source_kind = lead.metadata.get("crawler") if isinstance(lead.metadata, Mapping) else None
    for url in lead.source_urls:
        identity = source_identity_for_url(url, str(source_kind or ""))
        if identity:
            identities.add(identity)
    return identities


def _lead_key(lead: SourceLead, index: int) -> str:
    identity = lead.source_identity or "|".join(sorted(_lead_identities(lead)))
    return f"{index}:{lead.source}:{normalize_company_name(lead.canonical_name)}:{identity}"


def reconcile_companies(
    leads: Iterable[SourceLead],
    companies: Iterable[object],
) -> CompanyReconciliationResult:
    """Reconcile leads by exact normalized names, aliases, then unique identity.

    A name collision, identity collision, or disagreement between a name match
    and an identity match is classified as ``ambiguous``.  No input object is
    mutated and no configuration file is opened.
    """

    company_rows = list(companies)
    records: dict[str, tuple[str, object]] = {}
    name_index: dict[str, set[str]] = {}
    identity_index: dict[str, set[str]] = {}

    for index, company in enumerate(company_rows):
        key = _record_key(company, index)
        display_name = _record_display_name(company, index)
        records[key] = (display_name, company)
        names = (_company_name(company), *_company_aliases(company))
        for name in names:
            normalized = normalize_company_name(name)
            if normalized:
                name_index.setdefault(normalized, set()).add(key)
        for identity in _company_source_identities(company):
            identity_index.setdefault(identity, set()).add(key)

    existing: list[SourceLead] = []
    new: list[SourceLead] = []
    ambiguous: list[SourceLead] = []
    matched_companies: dict[str, str] = {}
    ambiguous_reasons: dict[str, tuple[str, ...]] = {}

    for index, lead in enumerate(leads):
        name_matches: set[str] = set()
        for name in (lead.canonical_name, lead.source_name):
            normalized = normalize_company_name(name)
            if normalized:
                name_matches.update(name_index.get(normalized, set()))
        project_matches: set[str] = set()
        if not name_matches:
            project_matches = _project_parent_keys(lead.canonical_name, records)
            name_matches.update(project_matches)

        identity_matches: set[str] = set()
        for identity in _lead_identities(lead):
            identity_matches.update(identity_index.get(identity, set()))

        reasons: list[str] = []
        if len(name_matches) > 1:
            reasons.append("normalized company name or alias matches multiple configured companies")
        if len(identity_matches) > 1:
            reasons.append("source identity matches multiple configured companies")
        if name_matches and identity_matches and name_matches != identity_matches:
            reasons.append("name match and source identity match different configured companies")

        key = _lead_key(lead, index)
        if reasons:
            ambiguous.append(lead)
            ambiguous_reasons[key] = tuple(reasons)
            continue

        matches = name_matches or identity_matches
        if len(matches) == 1:
            matched_key = next(iter(matches))
            display_name = records[matched_key][0]
            metadata = dict(lead.metadata)
            if project_matches:
                metadata["source_project_name"] = lead.canonical_name
                metadata["source_project_parent"] = display_name
            matched = replace(lead, matched_company=display_name, metadata=metadata)
            existing.append(matched)
            matched_companies[key] = display_name
        else:
            new.append(lead)

    def sort_key(lead: SourceLead) -> tuple[str, str, tuple[str, ...]]:
        return (
            normalize_company_name(lead.canonical_name),
            lead.source.casefold(),
            tuple(url.casefold() for url in lead.source_urls),
        )

    existing.sort(key=sort_key)
    new.sort(key=sort_key)
    ambiguous.sort(key=sort_key)
    return CompanyReconciliationResult(
        existing=tuple(existing),
        new=tuple(new),
        ambiguous=tuple(ambiguous),
        matched_companies=matched_companies,
        ambiguous_reasons=ambiguous_reasons,
    )


reconcile_source_leads = reconcile_companies


__all__ = [
    "consolidate_source_leads",
    "normalize_company_name",
    "reconcile_companies",
    "reconcile_source_leads",
    "source_identity",
    "source_identity_for_url",
]
