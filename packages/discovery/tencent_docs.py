"""Independent read-only connector for a public Tencent Docs SmartSheet."""

from __future__ import annotations

import base64
import html
import json
import re
import zlib
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests

from .models import SourceLead, SourceSyncResult, compact_text
from .reconciliation import source_identity_for_url


DEFAULT_SOURCE_URL = "https://docs.qq.com/smartsheet/DY3pHYkNvb0ZRSHdi?tab=t0gmEC&viewId=vUQPXH"
TARGET_TAG = "27届秋招"
PAGE_SIZE = 60
MAX_PAGE_REQUESTS = 20
_JSONP_PREFIX = "clientVarsCallback("

# Public-sheet campaign labels are often more specific than the Agent's
# company names.  This table is local to the Agent connector and does not
# import the legacy project's alias table.
SOURCE_ALIASES = {
    "DJI大疆": "大疆",
    "科大讯飞-飞凡计划": "科大讯飞",
    "京东-TET管理培训生": "京东",
    "百度-校招&管培生": "百度",
    "思特威-岗位陆续上新": "思特威",
    "思特威-(未官宣岗位陆续上新)": "思特威",
    "MiniMax Top Talent 计划": "MiniMax",
    "远景能源-看备注，主要C9": "远景科技",
    "学而思-陆续上新": "学而思",
    "卓驭-原大疆车载": "卓驭",
    "文远知行WeRid(未官宣)": "文远知行",
    "文远知行WeRid": "文远知行",
    "Momenta-M Star": "Momenta",
    "影石Insta360": "影石",
    "搜狐畅游-下周一官宣": "搜狐畅游",
    "柠檬微趣-下周官宣": "柠檬微趣",
    "哔哩哔哩": "bilibili",
    "阿里淘天": "淘天",
    "阿里-淘宝闪购": "淘天",
    "阿里-平头哥": "阿里平头哥",
    "创维集团": "创维",
}
_SOURCE_ALIASES_CASEFOLD = {
    compact_text(key).casefold(): compact_text(value)
    for key, value in SOURCE_ALIASES.items()
}


def canonical_company_name(name: object) -> str:
    value = compact_text(name)
    return _SOURCE_ALIASES_CASEFOLD.get(value.casefold(), value)


def _walk(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def parse_jsonp(text: str) -> dict[str, Any]:
    """Decode the public endpoint's JSONP envelope without evaluating code."""

    content = str(text or "").strip()
    if not content.startswith(_JSONP_PREFIX):
        raise ValueError("Tencent Docs response is not the expected JSONP envelope")
    body = content[len(_JSONP_PREFIX):].strip()
    if body.endswith(";"):
        body = body[:-1].rstrip()
    if not body.endswith(")"):
        raise ValueError("Tencent Docs response is not the expected JSONP envelope")
    try:
        payload = json.loads(body[:-1])
    except json.JSONDecodeError as exc:
        raise ValueError("Tencent Docs JSONP body is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Tencent Docs JSONP body must be an object")
    return payload


def decode_sheet_payload(text: str) -> Any:
    """Decode the compressed SmartSheet value nested in JSONP client variables."""

    envelope = parse_jsonp(text)
    try:
        compressed = envelope["clientVars"]["collab_client_vars"]["initialAttributedText"]["text"][0]["smartsheet"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("Tencent Docs JSONP has no SmartSheet payload") from exc
    if not isinstance(compressed, str) or not compressed:
        raise ValueError("Tencent Docs SmartSheet payload is empty")
    padded = compressed + "=" * (-len(compressed) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
        return json.loads(zlib.decompress(raw))
    except (ValueError, TypeError, zlib.error, json.JSONDecodeError) as exc:
        raise ValueError("Tencent Docs SmartSheet payload cannot be decoded") from exc


def _text_cell(cell: object) -> str:
    if not isinstance(cell, Mapping):
        return compact_text(cell)
    value = cell.get("k1")
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, Mapping):
                part = item.get("k2") or item.get("text") or item.get("value")
            else:
                part = item
            if part is not None:
                parts.append(str(part))
        return compact_text("".join(parts))
    return compact_text(value)


def _cell_options(cell: object) -> tuple[str, ...]:
    if not isinstance(cell, Mapping):
        return ()
    values = cell.get("k9")
    if isinstance(values, list):
        return tuple(compact_text(value) for value in values if compact_text(value))
    if values is None:
        return ()
    value = compact_text(values)
    return (value,) if value else ()


def _cell_links(cell: object) -> tuple[str, ...]:
    if not isinstance(cell, Mapping):
        return ()
    values = cell.get("k8")
    if not isinstance(values, list):
        return ()
    links: list[str] = []
    for item in values:
        if isinstance(item, Mapping):
            value = item.get("k3") or item.get("url") or item.get("href")
        else:
            value = item
        link = compact_text(value)
        if link and link not in links:
            links.append(link)
    return tuple(links)


def _field_id(payload: Any, field_name: str) -> str | None:
    for item in _walk(payload):
        for field_id, definition in item.items():
            if isinstance(definition, Mapping) and definition.get("k30") == field_name:
                return str(field_id)
    return None


def _row_names(payload: Any, company_field_id: str) -> set[str]:
    names: set[str] = set()
    for item in _walk(payload):
        cells = item.get("k1")
        if isinstance(cells, Mapping) and company_field_id in cells:
            name = _text_cell(cells.get(company_field_id))
            if name:
                names.add(name)
    return names


def _endpoint_with_row_range(endpoint: str, start: int, end: int) -> str:
    parts = urlsplit(endpoint)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({"startrow": str(start), "endrow": str(end)})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _preload_endpoint(page_text: str, source_url: str) -> str:
    for tag in re.findall(r"<link\b[^>]*>", page_text or "", flags=re.I):
        if not re.search(r"\brel\s*=\s*['\"]preload['\"]", tag, flags=re.I):
            continue
        if not re.search(r"\bas\s*=\s*['\"]script['\"]", tag, flags=re.I):
            continue
        match = re.search(r"\bhref\s*=\s*(['\"])(.*?)\1", tag, flags=re.I | re.S)
        if match and "opendoc" in html.unescape(match.group(2)).casefold():
            return urljoin(source_url, html.unescape(match.group(2)))
    raise ValueError("Tencent Docs public page has no SmartSheet data endpoint")


def parse_smartsheet_rows(payload: Any) -> list[SourceLead]:
    """Parse one or more SmartSheet payloads and keep the exact target tag."""

    field_names: dict[str, str] = {}
    options_by_field: dict[str, dict[str, str]] = {}
    required = {"公司名称", "招聘类型", "投递链接"}
    for item in _walk(payload):
        for field_id, definition in item.items():
            if not isinstance(definition, Mapping) or definition.get("k30") not in required:
                continue
            field_id = str(field_id)
            field_name = str(definition["k30"])
            field_names[field_id] = field_name
            options: dict[str, str] = {}
            raw_options = definition.get("k9")
            if isinstance(raw_options, Mapping):
                raw_options = raw_options.get("k3")
            if isinstance(raw_options, list):
                for option in raw_options:
                    if isinstance(option, Mapping):
                        key = compact_text(option.get("k1"))
                        label = compact_text(option.get("k2"))
                        if key:
                            options[key] = label or key
            options_by_field[field_id] = options

    if not required.issubset(set(field_names.values())):
        raise ValueError("Tencent Docs SmartSheet field structure is missing required fields")

    target_fields = {
        field_name: next(field_id for field_id, name in field_names.items() if name == field_name)
        for field_name in required
    }
    rows: dict[str, dict[str, Any]] = {}
    for item in _walk(payload):
        cells = item.get("k1")
        if not isinstance(cells, Mapping):
            continue
        if not all(field_id in cells for field_id in target_fields.values()):
            continue
        name = _text_cell(cells.get(target_fields["公司名称"]))
        raw_tags = _cell_options(cells.get(target_fields["招聘类型"]))
        tags = tuple(
            options_by_field[target_fields["招聘类型"]].get(tag, tag)
            for tag in raw_tags
        )
        if not name or TARGET_TAG not in tags:
            continue
        links = _cell_links(cells.get(target_fields["投递链接"]))
        key = name.casefold()
        row = rows.setdefault(key, {
            "source_name": name,
            "tags": [],
            "links": [],
        })
        for tag in tags:
            if tag not in row["tags"]:
                row["tags"].append(tag)
        for link in links:
            if link not in row["links"]:
                row["links"].append(link)

    leads: list[SourceLead] = []
    for row in rows.values():
        links = tuple(row["links"])
        identities = sorted(
            {
                identity
                for link in links
                if (identity := source_identity_for_url(link))
            }
        )
        tags = tuple(row["tags"])
        leads.append(
            SourceLead(
                canonical_name=canonical_company_name(row["source_name"]),
                source="tencent_docs",
                source_name=row["source_name"],
                source_urls=links,
                source_identity=identities[0] if len(identities) == 1 else None,
                metadata={
                    "tags": tags,
                    "target_tag": TARGET_TAG,
                    "mixed_tags": tuple(tag for tag in tags if tag != TARGET_TAG),
                },
            )
        )
    return sorted(leads, key=lambda lead: (lead.source_name.casefold(), lead.source_name))


parse_rows = parse_smartsheet_rows


class TencentDocsSmartSheetConnector:
    """Read a public SmartSheet through GET-only JSONP requests."""

    def __init__(
        self,
        source_url: str = DEFAULT_SOURCE_URL,
        *,
        session: Any | None = None,
        page_size: int = PAGE_SIZE,
        max_pages: int = MAX_PAGE_REQUESTS,
        timeout: float = 30.0,
    ) -> None:
        if page_size < 1 or max_pages < 1:
            raise ValueError("page_size and max_pages must be positive")
        self.source_url = compact_text(source_url)
        if not self.source_url:
            raise ValueError("source_url cannot be empty")
        self.session = session or requests.Session()
        self.page_size = page_size
        self.max_pages = max_pages
        self.timeout = timeout

    def _get(self, url: str, *, referer: str | None = None) -> Any:
        headers = {"User-Agent": "RecruitOps-Agent discovery reader/1.0"}
        if referer:
            headers["Referer"] = referer
        response = self.session.get(url, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        return response

    def _load_payloads(self) -> list[Any]:
        page = self._get(self.source_url)
        endpoint = _preload_endpoint(page.text, self.source_url)
        first_response = self._get(endpoint, referer=self.source_url)
        first_payload = decode_sheet_payload(first_response.text)
        payloads = [first_payload]

        company_field_id = _field_id(first_payload, "公司名称")
        if company_field_id is None:
            return payloads

        seen_names = _row_names(first_payload, company_field_id)
        empty_windows = 0
        for page_number in range(1, self.max_pages):
            start = page_number * self.page_size
            page_endpoint = _endpoint_with_row_range(
                endpoint,
                start,
                start + self.page_size,
            )
            page_response = self._get(page_endpoint, referer=self.source_url)
            page_payload = decode_sheet_payload(page_response.text)
            page_names = _row_names(page_payload, company_field_id)
            if page_names - seen_names:
                seen_names.update(page_names)
                empty_windows = 0
                payloads.append(page_payload)
                continue
            empty_windows += 1
            payloads.append(page_payload)
            if empty_windows >= 2:
                break
        return payloads

    def sync(self) -> SourceSyncResult:
        """Fetch and parse the sheet; this method performs no write request."""

        payloads = self._load_payloads()
        leads = parse_smartsheet_rows(payloads)
        return SourceSyncResult(
            source="tencent_docs",
            source_url=self.source_url,
            leads=tuple(leads),
            rows_seen=len(leads),
            accepted_rows=len(leads),
            pages_fetched=len(payloads),
            metadata={
                "target_tag": TARGET_TAG,
                "page_size": self.page_size,
                "max_pages": self.max_pages,
                "read_only": True,
            },
        )

    fetch = sync
    fetch_leads = sync


TencentDocsConnector = TencentDocsSmartSheetConnector


__all__ = [
    "DEFAULT_SOURCE_URL",
    "MAX_PAGE_REQUESTS",
    "PAGE_SIZE",
    "SOURCE_ALIASES",
    "TARGET_TAG",
    "TencentDocsConnector",
    "TencentDocsSmartSheetConnector",
    "canonical_company_name",
    "decode_sheet_payload",
    "parse_jsonp",
    "parse_rows",
    "parse_smartsheet_rows",
]
