"""Small, typed provenance boundary for ByteDance list API captures.

The caller must build a context from the response observed by the crawler.  A
plain mapping is deliberately not accepted by the job parser as the observed
response context.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup


OFFICIAL_BYTEDANCE_HOST = "jobs.bytedance.com"
OFFICIAL_BYTEDANCE_API_PATH = "/api/v1/search/job/posts"
OFFICIAL_BYTEDANCE_DETAIL_RE = re.compile(
    r"^/campus/position/(?P<post_id>[0-9]+)/detail/?$",
    re.IGNORECASE,
)
OFFICIAL_JD_FIELDS = ("description", "requirement")

_TRUST_MARKER = object()


def _json_bytes(value: object) -> bytes | None:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str | None:
    encoded = _json_bytes(value)
    return hashlib.sha256(encoded).hexdigest() if encoded is not None else None


def native_post_id(value: object) -> str:
    """Return only an ASCII decimal ByteDance post ID."""

    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value) if value >= 0 else ""
    if isinstance(value, str):
        return value if re.fullmatch(r"[0-9]+", value) else ""
    return ""


def _coerce_native_post_id(value: object) -> str:
    return native_post_id(value)


def _native_post_id(row: Mapping[str, Any]) -> str:
    return native_post_id(row.get("id"))


def _title(row: Mapping[str, Any]) -> str:
    value = row.get("title")
    return value.strip() if isinstance(value, str) else ""


def _clean_official_field(value: object) -> str:
    """Match the existing API detail parser's HTML-to-lines normalization."""

    if not isinstance(value, str) or not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    return "\n".join(
        line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
    )


def normalize_official_jd(description: object, requirement: object) -> str:
    """Rebuild ByteDance list fields without truncation or semantic summarizing."""

    if not isinstance(description, str) or not isinstance(requirement, str):
        return ""
    parts = []
    clean_description = _clean_official_field(description)
    clean_requirement = _clean_official_field(requirement)
    if clean_description:
        parts.extend(("职位描述", clean_description))
    if clean_requirement:
        parts.extend(("任职要求", clean_requirement))
    return "\n".join(parts)


def _detail_post_id(detail_url: object) -> str:
    try:
        parts = urlsplit(str(detail_url or ""))
        port = parts.port
    except ValueError:
        return ""
    if not _is_official_url(parts, OFFICIAL_BYTEDANCE_HOST, port):
        return ""
    match = OFFICIAL_BYTEDANCE_DETAIL_RE.fullmatch(parts.path)
    return unquote(match.group("post_id")).strip() if match else ""


@dataclass(frozen=True, slots=True)
class OfficialApiResponseContext:
    """Verified identity of one raw official API response.

    Instances can only be created through :meth:`from_captured_response` in
    normal use.  The private marker keeps parser inputs tied to this observed
    response-context type; it is not an authentication credential.
    """

    response_url: str
    request_method: str
    response_status: int
    request_payload_sha256: str
    raw_response_sha256: str
    response_payload: Mapping[str, Any]
    rows: tuple[Mapping[str, Any], ...]
    captured_at: str = ""
    _trust_marker: object = field(repr=False, compare=False, default=None)

    @classmethod
    def from_captured_response(
        cls,
        *,
        response_url: object,
        request_method: object,
        response_status: object,
        request_payload: object,
        response_text: object,
    ) -> "OfficialApiResponseContext | None":
        """Create context only from an observed, parseable official response."""

        url = str(response_url or "").strip()
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return None
        if (
            not _is_official_url(parts, OFFICIAL_BYTEDANCE_HOST, port)
            or parts.path.rstrip("/") != OFFICIAL_BYTEDANCE_API_PATH.rstrip("/")
            or str(request_method or "").upper() != "POST"
        ):
            return None
        if isinstance(response_status, bool):
            return None
        try:
            status = int(response_status)
        except (TypeError, ValueError):
            return None
        if status != 200 or not isinstance(request_payload, Mapping):
            return None

        if isinstance(response_text, bytes):
            try:
                raw_text = response_text.decode("utf-8")
            except UnicodeDecodeError:
                return None
        elif isinstance(response_text, str):
            raw_text = response_text
        else:
            return None
        if not raw_text.strip():
            return None
        try:
            payload = json.loads(raw_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        code = payload.get("code")
        if isinstance(code, bool) or code not in (0, "0"):
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        rows = data.get("job_post_list")
        if not isinstance(rows, list):
            return None
        if _explicitly_truncated(payload) or _explicitly_truncated(data):
            return None

        request_hash = _sha256_json(request_payload)
        if request_hash is None:
            return None
        return cls(
            response_url=url,
            request_method="POST",
            response_status=status,
            request_payload_sha256=request_hash,
            raw_response_sha256=_sha256_text(raw_text),
            response_payload=payload,
            rows=tuple(row for row in rows if isinstance(row, Mapping)),
            captured_at=datetime.now(timezone.utc).isoformat(),
            _trust_marker=_TRUST_MARKER,
        )

    @classmethod
    def from_response(cls, **kwargs: object) -> "OfficialApiResponseContext | None":
        """Readable alias for callers that do not use the capture wording."""

        return cls.from_captured_response(**kwargs)

    @property
    def verified(self) -> bool:
        return self._trust_marker is _TRUST_MARKER

    @property
    def response_identity(self) -> dict[str, object]:
        return {
            "url": self.response_url,
            "method": self.request_method,
            "status": self.response_status,
            "raw_response_sha256": self.raw_response_sha256,
            "request_payload_sha256": self.request_payload_sha256,
        }

    def row_matches(self, item: object) -> bool:
        """Require exact raw row identity, including native ID and title."""

        if not self.verified or not isinstance(item, Mapping):
            return False
        native_id = _native_post_id(item)
        title = _title(item)
        if not native_id or not title:
            return False
        item_hash = _sha256_json(item)
        if item_hash is None:
            return False
        for row in self.rows:
            if _native_post_id(row) != native_id or _title(row) != title:
                continue
            return _sha256_json(row) == item_hash
        return False

    @staticmethod
    def has_complete_official_fields(item: object) -> bool:
        """Validate the endpoint contract without a semantic length heuristic."""

        if not isinstance(item, Mapping):
            return False
        if any(field_name not in item for field_name in OFFICIAL_JD_FIELDS):
            return False
        if any(not isinstance(item[field_name], str) for field_name in OFFICIAL_JD_FIELDS):
            return False
        return not _explicitly_truncated(item)


def _explicitly_truncated(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    for key in (
        "truncated",
        "is_truncated",
        "isTruncated",
        "partial",
        "is_partial",
        "isPartial",
        "description_truncated",
        "requirement_truncated",
    ):
        if value.get(key) is True:
            return True
    return False


def _is_official_url(parts, host: str, port: int | None) -> bool:
    return (
        parts.scheme.casefold() == "https"
        and str(parts.hostname or "").casefold() == host.casefold()
        and parts.username is None
        and parts.password is None
        and port in (None, 443)
    )


def build_official_api_capture_evidence(
    *,
    context: OfficialApiResponseContext | None,
    item: object,
    detail_text: object,
    detail_url: object,
    native_post_id: object,
    title: object,
) -> dict[str, object]:
    """Return a complete receipt only for a row bound to its raw response."""

    if not isinstance(context, OfficialApiResponseContext) or not context.verified:
        return {}
    if not context.row_matches(item) or not context.has_complete_official_fields(item):
        return {}
    if not isinstance(item, Mapping):
        return {}
    observed_id = _native_post_id(item)
    if not observed_id or _coerce_native_post_id(native_post_id) != observed_id:
        return {}
    if _detail_post_id(detail_url) != observed_id:
        return {}
    if not isinstance(title, str) or title.strip() != _title(item):
        return {}
    expected_text = normalize_official_jd(
        item.get("description"), item.get("requirement")
    )
    if not expected_text or not isinstance(detail_text, str) or detail_text != expected_text:
        return {}

    normalized_text = detail_text.strip()
    fields = list(OFFICIAL_JD_FIELDS)
    identity = context.response_identity
    return {
        "status": "complete",
        "method": "official_api",
        "captured_at": context.captured_at,
        "source_url": str(detail_url),
        "api_source_url": context.response_url,
        "detail_url": str(detail_url),
        "identity_verified": True,
        "identity_evidence": [
            f"native_id:{observed_id}",
            f"title:{_title(item)}",
            f"response_sha256:{context.raw_response_sha256}",
        ],
        "native_post_id": observed_id,
        "native_job_id": observed_id,
        "source_fields": fields,
        "field_names": fields,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": _sha256_text(normalized_text),
        "raw_response_sha256": context.raw_response_sha256,
        "response_sha256": context.raw_response_sha256,
        "request_payload_sha256": context.request_payload_sha256,
        "response_identity": identity,
    }


build_capture_evidence = build_official_api_capture_evidence


__all__ = [
    "OFFICIAL_BYTEDANCE_API_PATH",
    "OFFICIAL_BYTEDANCE_DETAIL_RE",
    "OFFICIAL_BYTEDANCE_HOST",
    "OFFICIAL_JD_FIELDS",
    "OfficialApiResponseContext",
    "build_capture_evidence",
    "build_official_api_capture_evidence",
    "native_post_id",
    "normalize_official_jd",
]
