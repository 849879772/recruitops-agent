"""Independent adapter for public JD detail responses from campus.jd.com.

The JD crawler stores a SPA route, while the public detail endpoint returns the
authoritative ``publishId`` and ``positionName``.  This module keeps that
translation and its evidence receipt independent so the core hydrator can
choose how to consume the result.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


JD_OFFICIAL_HOST = "campus.jd.com"
JD_DETAIL_API_PATH = "/api/wx/position/detail"
JD_DETAIL_API_URL = f"https://{JD_OFFICIAL_HOST}{JD_DETAIL_API_PATH}"
_SPA_DETAIL_PATH = "/details"
_ID_RE = re.compile(r"^[0-9]+$")
_API_DETAIL_RE = re.compile(
    rf"^{re.escape(JD_DETAIL_API_PATH)}/(?P<publish_id>[0-9]+)/?$",
    re.IGNORECASE,
)
_LOGIN_RE = re.compile(
    r"登录|登入|请先登录|未登录|登录后|login|unauthori[sz]ed|authentication",
    re.IGNORECASE,
)
_NOT_FOUND_RE = re.compile(
    r"404|not[ -]?found|不存在|无此岗位|职位不存在|岗位不存在|已下线|已删除|已失效",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_RE = re.compile(
    r"token|auth|cookie|session|password|signature|secret|code",
    re.IGNORECASE,
)

_BODY_FIELDS: tuple[tuple[str, str], ...] = (
    ("岗位职责", "workContent"),
    ("任职要求", "qualification"),
)


def _canonical_id(value: object) -> str:
    """Normalize numeric IDs without accepting ``reqId``-like fallbacks."""

    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value) if value >= 0 else ""
    if isinstance(value, str):
        candidate = value.strip()
        return candidate if _ID_RE.fullmatch(candidate) else ""
    return ""


def _diagnostic_id(value: object) -> str:
    """Keep a scalar auxiliary ID for evidence without using it as identity."""

    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value) if value >= 0 else ""
    if isinstance(value, str):
        return value.strip()
    return ""


def _clean_text(value: object) -> str:
    """Convert one official body field to readable lines without length gates."""

    if not isinstance(value, str) or not value.strip():
        return ""
    # Let the HTML parser decode entities as text.  Unescaping before parsing
    # turns literal code such as ``&lt;T&gt;`` into tags and drops it.
    soup = BeautifulSoup(value, "html.parser")
    for node in soup.find_all(("script", "style", "noscript", "template")):
        node.decompose()
    text = soup.get_text("\n")
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip())


def clean_jd_body(body: Mapping[str, Any] | None) -> str:
    """Build the stored JD from the two official detail fields.

    Missing or empty fields are omitted.  There is intentionally no minimum
    character threshold: a short official JD is still a valid observation.
    """

    if not isinstance(body, Mapping):
        return ""
    parts: list[str] = []
    for heading, field_name in _BODY_FIELDS:
        text = _clean_text(body.get(field_name))
        if text:
            parts.extend((heading, text))
    return "\n".join(parts)


def _safe_url(value: object) -> str:
    """Keep route evidence while dropping credentials and query secrets."""

    try:
        raw = str(value or "").strip()
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        if not parsed.scheme or not hostname:
            return ""
        netloc = hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        query = "&".join(
            f"{key}={item}"
            for key, values in parse_qs(parsed.query, keep_blank_values=True).items()
            if not _SENSITIVE_QUERY_RE.search(key)
            for item in values
        )
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
    except (TypeError, ValueError):
        return ""


def _route_publish_id(url: str) -> str:
    try:
        parsed = urlsplit(url)
        match = _API_DETAIL_RE.fullmatch(parsed.path)
        if match:
            return match.group("publish_id")
        fragment_path = ""
        fragment_query = ""
        if parsed.fragment:
            fragment_path, _, fragment_query = parsed.fragment.partition("?")
        route_path = fragment_path or parsed.path
        if route_path.lstrip("#").rstrip("/").casefold() != _SPA_DETAIL_PATH.casefold():
            return ""
        query = parse_qs(parsed.query, keep_blank_values=True)
        query.update(parse_qs(fragment_query, keep_blank_values=True))
        return _canonical_id((query.get("id") or [""])[0])
    except (TypeError, ValueError):
        return ""


def jd_detail_api_url(url: object) -> str:
    """Return the canonical official detail API URL for a JD route, or ``""``."""

    raw = str(url or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != JD_OFFICIAL_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return ""
    publish_id = _route_publish_id(raw)
    return f"{JD_DETAIL_API_URL}/{publish_id}" if publish_id else ""


def _short_error(value: object, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _message(payload: Mapping[str, Any]) -> str:
    values: list[str] = []
    for key in ("message", "msg", "error", "errorMessage", "code"):
        value = payload.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            values.append(str(value))
    return " ".join(values)


def _response_text(response: object) -> str:
    value = getattr(response, "text", "")
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return ""


def _payload_from_response(response: object) -> tuple[Mapping[str, Any] | None, str]:
    try:
        payload = response.json()
    except (AttributeError, TypeError, ValueError, requests.JSONDecodeError):
        return None, "invalid_json"
    return (payload, "") if isinstance(payload, Mapping) else (None, "invalid_payload")


def _response_sha256(response: object) -> str:
    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return hashlib.sha256(content).hexdigest()
    text = _response_text(response)
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _failure_evidence(
    *,
    detail_url: str,
    request_url: str,
    status: str,
    response_status: int | None,
    observations: Mapping[str, Any],
    detail: str = "",
    identity_verified: bool = False,
    identity_evidence: tuple[str, ...] = (),
) -> dict[str, Any]:
    evidence = {
        "status": (
            "complete"
            if status == "complete" and detail and identity_verified
            else "incomplete"
        ),
        "method": "official_api",
        "source_url": detail_url,
        "api_source_url": request_url,
        "detail_url": detail_url,
        "identity_verified": identity_verified,
        "identity_evidence": list(identity_evidence),
        "terminal_observed": status == "complete",
        "remaining_controls": [],
        "content_sha256": (
            hashlib.sha256(detail.strip().encode("utf-8")).hexdigest()
            if detail.strip()
            else ""
        ),
        "response_status": response_status,
        "failure_reasons": [] if status == "complete" else [status],
        "observations": dict(observations),
    }
    return evidence


@dataclass(frozen=True, slots=True)
class JdDetailResult:
    """Non-throwing result returned by :func:`fetch_jd_detail`."""

    status: str
    detail_url: str = ""
    request_url: str = ""
    publish_id: str = ""
    title: str = ""
    detail: str = ""
    req_id: str = ""
    response_status: int | None = None
    error_type: str = ""
    error_detail: str = ""
    observations: Mapping[str, Any] = field(default_factory=dict)
    capture_evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.status == "complete"

    @property
    def body(self) -> str:
        """Alias for callers that use ``body`` for the cleaned JD text."""

        return self.detail

    @property
    def source(self) -> str:
        return "jd_official_api"

    @property
    def identity_status(self) -> str:
        identity = self.observations.get("identity", {})
        if isinstance(identity, Mapping):
            value = identity.get("status")
            if isinstance(value, str):
                return value
        return ""

    @property
    def identity_evidence(self) -> tuple[str, ...]:
        value = self.capture_evidence.get("identity_evidence", [])
        return tuple(str(item) for item in value) if isinstance(value, (list, tuple)) else ()

    def as_hydration_mapping(self) -> dict[str, Any]:
        """Return neutral fields that a core hydrator can adapt directly."""

        return {
            "detail": self.detail,
            "status": self.status,
            "source": self.source,
            "detail_url": self.detail_url,
            "identity_status": self.identity_status,
            "identity_evidence": self.identity_evidence,
            "capture_evidence": dict(self.capture_evidence),
            "publish_id": self.publish_id,
            "title": self.title,
            "req_id": self.req_id,
            "request_url": self.request_url,
            "observations": dict(self.observations),
        }


def fetch_jd_detail(
    url: str,
    *,
    expected_publish_id: object = None,
    expected_title: str | None = None,
    timeout_s: float = 20.0,
    opener: Callable[..., object] | None = None,
) -> JdDetailResult:
    """Fetch and validate one JD detail without raising on remote failures.

    ``expected_publish_id`` and ``expected_title`` are optional to support a
    first observation.  When supplied, both are compared exactly (apart from
    surrounding whitespace).  The response's ``reqId`` is recorded only as a
    separate diagnostic and is never used as the official ID.
    """

    detail_url = str(url or "").strip()
    request_url = jd_detail_api_url(detail_url)
    route_id = _route_publish_id(detail_url) if request_url else ""
    expected_id = (
        _canonical_id(expected_publish_id)
        if expected_publish_id is not None
        else route_id
    )
    if not request_url:
        return JdDetailResult(
            "invalid_url",
            detail_url=detail_url,
            error_type="InvalidJdDetailUrl",
            error_detail="URL is not a supported HTTPS campus.jd.com detail route.",
        )
    if not expected_id:
        return JdDetailResult(
            "invalid_request",
            detail_url=detail_url,
            request_url=request_url,
            error_type="InvalidPublishId",
            error_detail="Expected publishId must be a non-negative decimal ID.",
        )
    if expected_id != route_id:
        observations = {
            "schema": "jd_official_detail_observation.v1",
            "observed_at": _now(),
            "request": {"method": "GET", "url": request_url, "route_publish_id": route_id},
            "identity": {
                "status": "conflict",
                "expected_publish_id": expected_id,
                "route_publish_id": route_id,
                "observed_publish_id": None,
            },
        }
        evidence = _failure_evidence(
            detail_url=detail_url,
            request_url=request_url,
            status="publish_id_mismatch",
            response_status=None,
            observations=observations,
        )
        return JdDetailResult(
            "publish_id_mismatch",
            detail_url=detail_url,
            request_url=request_url,
            error_type="JdPublishIdMismatch",
            error_detail="Expected publishId does not match the detail route ID.",
            observations=observations,
            capture_evidence=evidence,
        )
    if expected_title is not None and not isinstance(expected_title, str):
        return JdDetailResult(
            "invalid_request",
            detail_url=detail_url,
            request_url=request_url,
            error_type="InvalidExpectedTitle",
            error_detail="Expected title must be a string or None.",
        )
    if (
        not isinstance(timeout_s, (int, float))
        or isinstance(timeout_s, bool)
        or not math.isfinite(timeout_s)
        or timeout_s <= 0
    ):
        return JdDetailResult(
            "invalid_request",
            detail_url=detail_url,
            request_url=request_url,
            error_type="InvalidTimeout",
            error_detail="Timeout must be a positive finite number.",
        )

    request = opener or requests.get
    headers = {
        "Accept": "application/json",
        "Referer": "https://campus.jd.com/",
        "User-Agent": "Mozilla/5.0",
    }
    try:
        response = request(request_url, headers=headers, timeout=float(timeout_s))
    except (requests.Timeout, TimeoutError) as exc:
        observations = {
            "schema": "jd_official_detail_observation.v1",
            "observed_at": _now(),
            "request": {"method": "GET", "url": request_url, "route_publish_id": route_id},
            "response": {"status_code": None, "outcome": "timeout"},
        }
        return JdDetailResult(
            "timeout",
            detail_url=detail_url,
            request_url=request_url,
            error_type=type(exc).__name__,
            error_detail=_short_error(exc),
            observations=observations,
            capture_evidence=_failure_evidence(
                detail_url=detail_url,
                request_url=request_url,
                status="timeout",
                response_status=None,
                observations=observations,
            ),
        )
    except requests.RequestException as exc:
        observations = {
            "schema": "jd_official_detail_observation.v1",
            "observed_at": _now(),
            "request": {"method": "GET", "url": request_url, "route_publish_id": route_id},
            "response": {"status_code": None, "outcome": "request_error"},
        }
        return JdDetailResult(
            "fetch_failed",
            detail_url=detail_url,
            request_url=request_url,
            error_type=type(exc).__name__,
            error_detail=_short_error(exc),
            observations=observations,
            capture_evidence=_failure_evidence(
                detail_url=detail_url,
                request_url=request_url,
                status="fetch_failed",
                response_status=None,
                observations=observations,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        observations = {
            "schema": "jd_official_detail_observation.v1",
            "observed_at": _now(),
            "request": {"method": "GET", "url": request_url, "route_publish_id": route_id},
            "response": {"status_code": None, "outcome": "unexpected_error"},
        }
        return JdDetailResult(
            "fetch_failed",
            detail_url=detail_url,
            request_url=request_url,
            error_type=type(exc).__name__,
            error_detail=_short_error(exc),
            observations=observations,
            capture_evidence=_failure_evidence(
                detail_url=detail_url,
                request_url=request_url,
                status="fetch_failed",
                response_status=None,
                observations=observations,
            ),
        )

    try:
        response_status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        response_status = None
    response_url = _safe_url(getattr(response, "url", "") or request_url)
    response_hash = _response_sha256(response)
    base_observations: dict[str, Any] = {
        "schema": "jd_official_detail_observation.v1",
        "observed_at": _now(),
        "request": {"method": "GET", "url": request_url, "route_publish_id": route_id},
        "response": {
            "status_code": response_status,
            "url": response_url,
            "response_sha256": response_hash,
        },
    }
    if response_status == 401 or response_status == 403:
        base_observations["response"]["outcome"] = "login_required"
        return JdDetailResult(
            "login_required",
            detail_url=detail_url,
            request_url=request_url,
            response_status=response_status,
            error_type="JdLoginRequired",
            error_detail=f"HTTP {response_status} from official detail endpoint.",
            observations=base_observations,
            capture_evidence=_failure_evidence(
                detail_url=detail_url,
                request_url=request_url,
                status="login_required",
                response_status=response_status,
                observations=base_observations,
            ),
        )
    if response_status == 404:
        base_observations["response"]["outcome"] = "not_found"
        return JdDetailResult(
            "not_found",
            detail_url=detail_url,
            request_url=request_url,
            response_status=response_status,
            error_type="JdDetailNotFound",
            error_detail="Official detail endpoint returned HTTP 404.",
            observations=base_observations,
            capture_evidence=_failure_evidence(
                detail_url=detail_url,
                request_url=request_url,
                status="not_found",
                response_status=response_status,
                observations=base_observations,
            ),
        )
    if response_status is None or not 200 <= response_status < 300:
        base_observations["response"]["outcome"] = "http_error"
        return JdDetailResult(
            "http_error",
            detail_url=detail_url,
            request_url=request_url,
            response_status=response_status,
            error_type="JdDetailHttpError",
            error_detail=f"Official detail endpoint returned HTTP {response_status}.",
            observations=base_observations,
            capture_evidence=_failure_evidence(
                detail_url=detail_url,
                request_url=request_url,
                status="http_error",
                response_status=response_status,
                observations=base_observations,
            ),
        )

    payload, payload_error = _payload_from_response(response)
    if payload is None:
        response_text = _response_text(response)
        status = "login_required" if _LOGIN_RE.search(response_text[:1000]) else "invalid_response"
        base_observations["response"]["outcome"] = status
        return JdDetailResult(
            status,
            detail_url=detail_url,
            request_url=request_url,
            response_status=response_status,
            error_type="JdLoginRequired" if status == "login_required" else "JdInvalidResponse",
            error_detail=payload_error,
            observations=base_observations,
            capture_evidence=_failure_evidence(
                detail_url=detail_url,
                request_url=request_url,
                status=status,
                response_status=response_status,
                observations=base_observations,
            ),
        )

    success = payload.get("success")
    message = _message(payload)
    if success is not True and not (isinstance(success, str) and success.casefold() == "true"):
        if _LOGIN_RE.search(message):
            status = "login_required"
        elif _NOT_FOUND_RE.search(message):
            status = "not_found"
        else:
            status = "api_error"
        base_observations["response"].update({"success": False, "outcome": status})
        if message:
            base_observations["response"]["message"] = _short_error(message)
        return JdDetailResult(
            status,
            detail_url=detail_url,
            request_url=request_url,
            response_status=response_status,
            error_type="JdLoginRequired" if status == "login_required" else "JdApiError",
            error_detail=_short_error(message) or "Official detail response was not successful.",
            observations=base_observations,
            capture_evidence=_failure_evidence(
                detail_url=detail_url,
                request_url=request_url,
                status=status,
                response_status=response_status,
                observations=base_observations,
            ),
        )

    raw_body = payload.get("body")
    if not isinstance(raw_body, Mapping):
        base_observations["response"].update({"success": True, "outcome": "invalid_response"})
        return JdDetailResult(
            "invalid_response",
            detail_url=detail_url,
            request_url=request_url,
            response_status=response_status,
            error_type="JdBodyMissing",
            error_detail="Successful response did not contain an object body.",
            observations=base_observations,
            capture_evidence=_failure_evidence(
                detail_url=detail_url,
                request_url=request_url,
                status="invalid_response",
                response_status=response_status,
                observations=base_observations,
            ),
        )

    observed_id = _canonical_id(raw_body.get("publishId"))
    req_id = _diagnostic_id(raw_body.get("reqId"))
    observed_title = raw_body.get("positionName")
    title = observed_title.strip() if isinstance(observed_title, str) else ""
    detail = clean_jd_body(raw_body)
    field_lengths = {
        field_name: len(_clean_text(raw_body.get(field_name)))
        for _heading, field_name in _BODY_FIELDS
        if isinstance(raw_body.get(field_name), str)
    }
    title_match = None if expected_title is None else title == expected_title.strip()
    publish_id_match = observed_id == expected_id if observed_id else None
    identity_status = "matched"
    status = "complete"
    failure_reasons: list[str] = []
    if not observed_id or not title:
        identity_status = "unobserved"
        status = "identity_unobserved"
        failure_reasons.extend(
            reason
            for reason, missing in (
                ("publish_id_missing", not observed_id),
                ("title_missing", not title),
            )
            if missing
        )
    elif not publish_id_match:
        identity_status = "conflict"
        status = "publish_id_mismatch"
        failure_reasons.append("publish_id_mismatch")
    elif title_match is False:
        identity_status = "conflict"
        status = "title_mismatch"
        failure_reasons.append("title_mismatch")
    if not detail:
        status = "empty_detail" if status == "complete" else status
        failure_reasons.append("detail_empty")

    identity_evidence = (
        f"publish_id:{observed_id}" if observed_id else "publish_id:unobserved",
        f"title:{title}" if title else "title:unobserved",
        f"req_id:{req_id}" if req_id else "req_id:unobserved",
    )
    base_observations.update(
        {
            "response": {
                **base_observations["response"],
                "success": True,
                "body_keys": sorted(str(key) for key in raw_body.keys()),
                "outcome": status,
            },
            "identity": {
                "status": identity_status,
                "expected_publish_id": expected_id,
                "observed_publish_id": observed_id or None,
                "route_publish_id": route_id,
                "publish_id_match": publish_id_match,
                "expected_title": expected_title,
                "observed_title": title or None,
                "title_match": title_match,
                "req_id": req_id or None,
            },
            "content": {
                "fields": [
                    field_name
                    for _heading, field_name in _BODY_FIELDS
                    if field_name in raw_body
                ],
                "field_lengths": field_lengths,
                "chars": len(detail),
                "sha256": (
                    hashlib.sha256(detail.strip().encode("utf-8")).hexdigest()
                    if detail.strip()
                    else ""
                ),
            },
            "failure_reasons": failure_reasons,
        }
    )
    evidence = _failure_evidence(
        detail_url=detail_url,
        request_url=request_url,
        status=status,
        response_status=response_status,
        observations=base_observations,
        detail=detail,
        identity_verified=status == "complete",
        identity_evidence=identity_evidence,
    )
    evidence.update(
        {
            "failure_reasons": failure_reasons,
            "native_job_id": observed_id,
            "native_post_id": observed_id,
            "publish_id": observed_id,
            "req_id": req_id,
            "source_fields": [
                field_name
                for _heading, field_name in _BODY_FIELDS
                if field_name in raw_body
            ],
            "response_identity": {
                "url": response_url or request_url,
                "method": "GET",
                "status": response_status,
                "response_sha256": response_hash,
                "route_publish_id": route_id,
                "publish_id": observed_id,
                "req_id": req_id,
            },
        }
    )
    return JdDetailResult(
        status,
        detail_url=detail_url,
        request_url=request_url,
        publish_id=observed_id,
        title=title,
        detail=detail,
        req_id=req_id,
        response_status=response_status,
        error_type="" if status == "complete" else "JdDetailValidationError",
        error_detail="; ".join(failure_reasons),
        observations=base_observations,
        capture_evidence=evidence,
    )


__all__ = [
    "JD_DETAIL_API_PATH",
    "JD_DETAIL_API_URL",
    "JD_OFFICIAL_HOST",
    "JdDetailResult",
    "clean_jd_body",
    "fetch_jd_detail",
    "jd_detail_api_url",
]
