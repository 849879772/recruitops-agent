"""Small, strict boundary models for the Edge browser bridge."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any
from urllib.parse import urlparse

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


MAX_OPERATION_ID_LENGTH = 128
MAX_DEVICE_ID_LENGTH = 128
MAX_OPERATION_NAME_LENGTH = 128
MAX_IDEMPOTENCY_KEY_LENGTH = 255
MAX_EVENT_ID_LENGTH = 128
MAX_EVENT_TYPE_LENGTH = 64
MAX_ERROR_CODE_LENGTH = 128
MAX_PAYLOAD_FIELDS = 64
MAX_PAYLOAD_ITEMS = 500
MAX_PAYLOAD_DEPTH = 8
MAX_PAYLOAD_STRING_LENGTH = 20_000
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_PAGE_TEXT_LENGTH = 20_000
MAX_PAGE_TITLE_LENGTH = 500
MAX_PAGE_LINKS = 200
MAX_NETWORK_REQUESTS = 500
MAX_STATUS_ENTRIES = 100
MAX_EVIDENCE_ENTRIES = 100


class OperationStatus(StrEnum):
    CONNECTING = "CONNECTING"
    DISPATCHED = "DISPATCHED"
    NAVIGATING = "NAVIGATING"
    WAITING_FOR_LOGIN = "WAITING_FOR_LOGIN"
    EXTRACTING = "EXTRACTING"
    VALIDATING = "VALIDATING"
    UPDATING = "UPDATING"
    SUCCEEDED = "SUCCEEDED"
    STATE_UNCLEAR = "STATE_UNCLEAR"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class OperationName(StrEnum):
    """Canonical operation currently supported by the bridge contract."""

    REVIEW_AND_UPDATE_APPLICATION_STATUS = "review_and_update_application_status"
    OBSERVE_APPLICATION_STATUS_PAGE = "observe_application_status_page"
    CAPTURE_OC_SNAPSHOT = "capture_oc_snapshot"


OperationState = OperationStatus
BrowserOperationStatus = OperationStatus
BrowserOperationName = OperationName

TERMINAL_STATUSES = frozenset(
    {
        OperationStatus.SUCCEEDED,
        OperationStatus.STATE_UNCLEAR,
        OperationStatus.FAILED,
        OperationStatus.CANCELLED,
    }
)


class BridgeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


@dataclass(frozen=True, slots=True)
class BrowserConnectionStatus(Mapping[str, Any]):
    """Database-backed connection evidence safe to expose to read-only callers."""

    device_id: str
    connected: bool
    last_seen_at: datetime | None
    pending_outbox_count: int = 0
    available: bool = True

    @property
    def status(self) -> str:
        return "connected" if self.connected else "disconnected"

    def __getitem__(self, key: str) -> Any:
        if key == "device_id":
            return self.device_id
        if key == "status":
            return self.status
        if key == "connected":
            return self.connected
        if key == "last_seen_at":
            return self.last_seen_at
        if key == "pending_outbox_count":
            return self.pending_outbox_count
        if key == "available":
            return self.available
        raise KeyError(key)

    def __iter__(self):
        return iter(
            (
                "device_id",
                "status",
                "connected",
                "last_seen_at",
                "pending_outbox_count",
                "available",
            )
        )

    def __len__(self) -> int:
        return 6


def _http_url(value: str, field_name: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{field_name} must not contain credentials")
    return value


BoundedUrl = Annotated[str, Field(min_length=1, max_length=2_048)]


class WebPageNetworkRequest(BridgeModel):
    """Metadata-only request evidence; headers and bodies are not accepted."""

    method: str = Field(min_length=1, max_length=16)
    url: BoundedUrl
    status_code: int | None = Field(default=None, ge=100, le=599)
    resource_type: str | None = Field(default=None, max_length=80)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _http_url(value, "network request URL")


class WebPageStatus(BridgeModel):
    """One extracted application-status candidate from a page."""

    application_id: str | None = Field(default=None, max_length=255)
    status: str = Field(min_length=1, max_length=128)
    label: str = Field(default="", max_length=200)
    context: str = Field(default="", max_length=1_000)
    evidence: str = Field(default="", max_length=2_000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class WebPageEvidence(BridgeModel):
    """Bounded, model-facing evidence without raw DOM or executable content."""

    source: str = Field(default="browser", min_length=1, max_length=128)
    source_ref: str | None = Field(default=None, max_length=2_048)
    field: str | None = Field(default=None, max_length=128)
    value: str = Field(default="", max_length=2_000)
    text: str = Field(default="", max_length=2_000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class WebPageData(BridgeModel):
    """Allowlisted and size-bounded page data accepted from Edge."""

    page_url: str | None = Field(
        default=None,
        max_length=2_048,
        validation_alias=AliasChoices("page_url", "url"),
    )
    origin: str | None = Field(default=None, max_length=2_048)
    path: str | None = Field(default=None, max_length=2_048)
    title: str = Field(default="", max_length=MAX_PAGE_TITLE_LENGTH)
    text: str = Field(
        default="",
        max_length=MAX_PAGE_TEXT_LENGTH,
        validation_alias=AliasChoices("text", "page_text"),
    )
    links: list[BoundedUrl] = Field(default_factory=list, max_length=MAX_PAGE_LINKS)
    network_requests: list[WebPageNetworkRequest] = Field(
        default_factory=list,
        max_length=MAX_NETWORK_REQUESTS,
        validation_alias=AliasChoices("network_requests", "networkRequests"),
    )
    captured_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("captured_at", "capturedAt"),
    )
    statuses: list[WebPageStatus] = Field(
        default_factory=list,
        max_length=MAX_STATUS_ENTRIES,
        validation_alias=AliasChoices(
            "statuses",
            "application_statuses",
            "applicationStatuses",
            "entries",
        ),
    )
    evidence: list[WebPageEvidence] = Field(
        default_factory=list,
        max_length=MAX_EVIDENCE_ENTRIES,
    )
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("page_url", "origin")
    @classmethod
    def validate_page_urls(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return value
        return _http_url(value, str(info.field_name))

    @field_validator("links")
    @classmethod
    def validate_links(cls, values: list[str]) -> list[str]:
        return [_http_url(value, "page link URL") for value in values]

    @field_validator("captured_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("captured_at must include a timezone")
        return value


def validate_web_page_data(value: WebPageData | Mapping[str, Any]) -> dict[str, Any]:
    """Validate and JSON-normalize one untrusted page snapshot."""

    model = value if isinstance(value, WebPageData) else WebPageData.model_validate(value)
    return bounded_json(model.model_dump(mode="json", exclude_none=True))


_PAGE_CONTAINER_KEYS = frozenset({"page", "page_data", "web_page", "web_data", "snapshot"})
_PAGE_FIELDS = frozenset(
    {
        "page_url",
        "url",
        "origin",
        "path",
        "title",
        "text",
        "page_text",
        "links",
        "network_requests",
        "networkRequests",
        "captured_at",
        "capturedAt",
        "statuses",
        "application_statuses",
        "applicationStatuses",
        "entries",
        "evidence",
        "confidence",
    }
)
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "cookie",
        "cookies",
        "document",
        "dom",
        "headers",
        "html",
        "javascript",
        "raw_dom",
        "raw_html",
        "script",
    }
)


def _bounded_json(value: Any, *, depth: int, path: str) -> Any:
    if depth > MAX_PAYLOAD_DEPTH:
        raise ValueError(f"{path} exceeds maximum payload depth")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > MAX_PAYLOAD_STRING_LENGTH:
            raise ValueError(f"{path} exceeds maximum string length")
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_PAYLOAD_FIELDS:
            raise ValueError(f"{path} exceeds maximum field count")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ValueError(f"{path} contains an invalid field name")
            if key.casefold() in _FORBIDDEN_PAYLOAD_KEYS:
                raise ValueError(f"{path}.{key} is not an accepted browser payload field")
            result[key] = _bounded_json(item, depth=depth + 1, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_PAYLOAD_ITEMS:
            raise ValueError(f"{path} exceeds maximum item count")
        return [
            _bounded_json(item, depth=depth + 1, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ValueError(f"{path} contains a non-JSON value")


def bounded_json(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy a JSON object after enforcing bounded, non-executable content."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("browser payload must be an object")
    normalized = _bounded_json(value, depth=0, path="payload")
    encoded = json.dumps(normalized, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("browser payload exceeds maximum size")
    return normalized


def validate_bridge_payload(
    value: WebPageData | Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Validate a command/event payload and strictly parse embedded page data."""

    if value is None:
        return {}
    if isinstance(value, WebPageData):
        return validate_web_page_data(value)
    if not isinstance(value, Mapping):
        raise ValueError("browser payload must be an object")

    raw_value = dict(value)
    for key in _PAGE_CONTAINER_KEYS:
        nested = raw_value.get(key)
        if isinstance(nested, WebPageData):
            raw_value[key] = validate_web_page_data(nested)

    keys = set(raw_value)
    if keys and keys <= _PAGE_FIELDS and keys & _PAGE_FIELDS:
        return validate_web_page_data(raw_value)

    normalized = bounded_json(raw_value)
    for key in _PAGE_CONTAINER_KEYS:
        if key in normalized:
            normalized[key] = validate_web_page_data(normalized[key])
    return bounded_json(normalized)


def normalize_status(value: OperationStatus | str) -> OperationStatus:
    if isinstance(value, OperationStatus):
        return value
    if not isinstance(value, str):
        raise ValueError("browser operation status must be a supported string")
    key = value.strip().upper().replace("-", "_").replace(" ", "_")
    try:
        return OperationStatus[key]
    except KeyError as exc:
        raise ValueError(f"unsupported browser operation status: {value}") from exc


def normalize_operation(value: OperationName | str) -> str:
    if isinstance(value, StrEnum):
        value = value.value
    if not isinstance(value, str):
        raise ValueError("browser operation must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_OPERATION_NAME_LENGTH:
        raise ValueError("browser operation is empty or too long")
    return normalized


__all__ = [
    "BrowserOperationName",
    "BrowserOperationStatus",
    "BrowserConnectionStatus",
    "BridgeModel",
    "MAX_EVENT_ID_LENGTH",
    "MAX_EVENT_TYPE_LENGTH",
    "MAX_PAYLOAD_BYTES",
    "MAX_PAYLOAD_FIELDS",
    "MAX_PAYLOAD_ITEMS",
    "MAX_PAGE_TEXT_LENGTH",
    "OperationName",
    "OperationState",
    "OperationStatus",
    "TERMINAL_STATUSES",
    "WebPageData",
    "WebPageEvidence",
    "WebPageNetworkRequest",
    "WebPageStatus",
    "bounded_json",
    "normalize_operation",
    "normalize_status",
    "validate_bridge_payload",
    "validate_web_page_data",
]
