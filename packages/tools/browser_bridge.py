"""Typed MCP tools for the persistent Edge browser bridge."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import StrEnum
from time import perf_counter, sleep
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from packages.browser_bridge import (
    BrowserBridgeStore,
    BrowserOperation,
    BrowserOperationEvent,
    OperationName,
    OperationStatus,
    TERMINAL_STATUSES,
    normalize_status,
)
from packages.domain.urls import normalize_http_page_url
from packages.repositories.base import RecruitmentRepository
from packages.security import redact_sensitive
from packages.tools.browser_status_update import (
    BrowserStatusUpdateInput,
    UpdateStatus,
    browser_status_update,
)

from .typed import EvidenceSource, ToolStatus


MAX_APPLICATION_ID_LENGTH = 255
MAX_REASON_LENGTH = 2_000


class BrowserBridgeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class BrowserBridgeInput(BrowserBridgeModel):
    timeout_ms: int = Field(default=5_000, ge=1, le=120_000)


class BrowserErrorCode(StrEnum):
    """Stable error codes exposed by the MCP browser-bridge boundary."""

    BRIDGE_DEPENDENCY_MISSING = "bridge_dependency_missing"
    CONNECTION_STATUS_UNAVAILABLE = "connection_status_unavailable"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INVALID_INPUT = "invalid_input"
    INTERNAL_ERROR = "internal_error"
    OPERATION_NOT_ACTIVE = "operation_not_active"
    OPERATION_NOT_FOUND = "operation_not_found"
    OPERATION_FAILED = "operation_failed"
    OPERATION_STATE_UNCLEAR = "operation_state_unclear"
    STORE_UNAVAILABLE = "store_unavailable"
    TIMEOUT = "timeout"


class EdgeConnectionState(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


class EdgeConnectionStatusInput(BrowserBridgeInput):
    """Read the persisted or explicitly supplied status for one Edge device."""

    device_id: str | None = Field(default=None, min_length=1, max_length=128)


class ReviewAndUpdateApplicationStatusInput(BrowserBridgeInput):
    """Start one durable review command without an additional confirmation turn."""

    application_id: str = Field(min_length=1, max_length=MAX_APPLICATION_ID_LENGTH)
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)
    operation_id: str | None = Field(default=None, min_length=1, max_length=128)
    application_url: str | None = Field(default=None, max_length=2_048)
    target_status: str | None = Field(default=None, min_length=1, max_length=128)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("application_url")
    @classmethod
    def validate_application_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("application_url must be an HTTP(S) URL")
        if parsed.username or parsed.password:
            raise ValueError("application_url must not contain credentials")
        return value


class ObserveApplicationStatusPageInput(ReviewAndUpdateApplicationStatusInput):
    """Ask Edge for bounded page evidence without interpreting or writing status."""

    timeout_ms: int = Field(default=45_000, ge=1, le=120_000)
    include_vision: bool = Field(
        default=False,
        description=(
            "Disabled by default. Enable only for a second, individual observation after a prior "
            "DOM-only observation returned no bindable structured status evidence and the single "
            "page has a clear target."
        ),
    )
    vision_fallback_reason: Literal["no_structured_evidence_visible_status_likely"] | None = Field(
        default=None,
        description=(
            "Required with include_vision=true. It attests that the prior DOM-only observation had "
            "no bindable structured evidence while the visible screenshot could still contain status text."
        ),
    )
    retain_on_pause: bool = True
    application_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_vision_fallback(self) -> "ObserveApplicationStatusPageInput":
        if self.include_vision and (
            self.vision_fallback_reason != "no_structured_evidence_visible_status_likely"
        ):
            raise ValueError(
                "include_vision=true requires vision_fallback_reason="
                "'no_structured_evidence_visible_status_likely'"
            )
        if not self.include_vision and self.vision_fallback_reason is not None:
            raise ValueError("vision_fallback_reason is only valid when include_vision=true")
        if self.include_vision and "timeout_ms" not in self.model_fields_set:
            self.timeout_ms = 120_000
        return self

    @field_validator("application_ids", mode="before")
    @classmethod
    def coerce_application_ids(cls, value: object) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("application_ids must be a list")
        values: list[str] = []
        for item in value:
            if isinstance(item, bool) or item is None:
                raise ValueError("application_ids must contain strings or integers")
            text = str(item).strip()
            if not text or len(text) > MAX_APPLICATION_ID_LENGTH:
                raise ValueError("application_ids contains an invalid application id")
            if text not in values:
                values.append(text)
        return values


def _observation_application_ids(request: ObserveApplicationStatusPageInput) -> list[str]:
    values = [request.application_id, *request.application_ids]
    return list(dict.fromkeys(value for value in values if value))


class CaptureOcSnapshotInput(BrowserBridgeInput):
    """Capture the fixed eligible-company OC view through the logged-in Edge session."""

    timeout_ms: int = Field(default=900_000, ge=1, le=1_200_000)
    device_id: str | None = Field(default=None, min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=255)
    operation_id: str | None = Field(default=None, min_length=1, max_length=128)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)


class BrowserOperationStatusInput(BrowserBridgeInput):
    operation_id: str = Field(min_length=1, max_length=128)
    after_sequence: int | None = Field(default=None, ge=0)
    event_limit: int = Field(default=10, ge=1, le=50)


class CancelBrowserOperationInput(BrowserBridgeInput):
    operation_id: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, min_length=1, max_length=MAX_REASON_LENGTH)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)
    task_id: str | None = Field(default=None, min_length=1, max_length=128)


class BrowserAuditFields(BrowserBridgeModel):
    """Audit context returned with every browser-bridge tool result."""

    audit_id: str = Field(min_length=1, max_length=255)
    tool_name: str = Field(min_length=1, max_length=128)
    operation: str = Field(min_length=1, max_length=128)
    operation_id: str | None = Field(default=None, max_length=128)
    application_id: str | None = Field(default=None, max_length=MAX_APPLICATION_ID_LENGTH)
    device_id: str | None = Field(default=None, max_length=128)
    idempotency_key: str | None = Field(default=None, max_length=255)
    task_id: str | None = Field(default=None, max_length=128)
    side_effect: bool
    read_only: bool
    idempotent_replay: bool = False
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EdgeConnectionStatusData(BrowserBridgeModel):
    device_id: str | None = None
    status: EdgeConnectionState
    connected: bool | None = None
    available: bool = True
    pending_outbox_count: int = Field(default=0, ge=0)
    last_seen_at: datetime | None = None
    reason: str | None = None


class BrowserOperationData(BrowserBridgeModel):
    operation_id: str
    operation: str
    application_id: str | None = None
    device_id: str
    idempotency_key: str
    status: OperationStatus
    command: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    terminal_result: dict[str, Any] | None = None
    error_code: str | None = None
    last_event_sequence: int = Field(ge=0)
    last_outbox_sequence: int | None = Field(default=None, ge=1)
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    terminal: bool


class BrowserOperationEventData(BrowserBridgeModel):
    event_id: str
    operation_id: str
    sequence: int = Field(ge=1)
    status: OperationStatus
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None
    created_at: datetime


class BrowserOperationStatusData(BrowserBridgeModel):
    operation: BrowserOperationData
    operation_id: str
    status: OperationStatus
    events: list[BrowserOperationEventData] = Field(default_factory=list)
    next_sequence: int = Field(ge=0)
    has_more_events: bool = False
    changed: bool
    wait_timed_out: bool = False
    terminal: bool
    terminal_result: dict[str, Any] | None = None


class ReviewAndUpdateApplicationStatusData(BrowserOperationData):
    idempotent_replay: bool = False
    requires_confirmation: Literal[False] = False
    verification: dict[str, Any] | None = None


class ObserveApplicationStatusPageData(BrowserOperationData):
    idempotent_replay: bool = False
    observation: dict[str, Any] | None = None


class CaptureOcSnapshotData(BrowserOperationData):
    idempotent_replay: bool = False
    snapshot_path: str | None = None
    record_count: int | None = Field(default=None, ge=0)
    total_pages: int | None = Field(default=None, ge=0)
    total_items: int | None = Field(default=None, ge=0)
    sha256: str | None = None


class CancelBrowserOperationData(BrowserOperationData):
    idempotent_replay: bool = False
    cancelled: bool = True


T = TypeVar("T", bound=BaseModel)


class BrowserBridgeResponse(BrowserBridgeModel, Generic[T]):
    tool_name: str
    status: ToolStatus
    success: bool
    data: T | None = None
    evidence: list[EvidenceSource] = Field(min_length=1)
    error_code: BrowserErrorCode | None = None
    error_message: str | None = None
    timeout_ms: int = Field(ge=1)
    timed_out: bool = False
    elapsed_ms: int = Field(ge=0)
    audit: BrowserAuditFields
    read_only: bool

    @model_validator(mode="after")
    def validate_status_fields(self) -> "BrowserBridgeResponse[T]":
        expected_success = self.status is ToolStatus.SUCCESS
        if self.success != expected_success:
            raise ValueError("success must agree with status")
        if self.status is ToolStatus.SUCCESS and self.error_code is not None:
            raise ValueError("successful responses cannot contain an error code")
        if self.status is not ToolStatus.SUCCESS and self.error_code is None:
            raise ValueError("non-success responses require an error code")
        if self.audit.read_only != self.read_only:
            raise ValueError("audit read_only must agree with response read_only")
        if self.audit.side_effect == self.read_only:
            raise ValueError("audit side_effect must be inverse of response read_only")
        return self


class BrowserReadResponse(BrowserBridgeResponse[T]):
    read_only: Literal[True] = True


class BrowserActionResponse(BrowserBridgeResponse[T]):
    read_only: Literal[False] = False


class EdgeConnectionStatusResponse(BrowserReadResponse[EdgeConnectionStatusData]):
    pass


class ReviewAndUpdateApplicationStatusResponse(
    BrowserActionResponse[ReviewAndUpdateApplicationStatusData]
):
    pass


class ObserveApplicationStatusPageResponse(
    BrowserActionResponse[ObserveApplicationStatusPageData]
):
    pass


class CaptureOcSnapshotResponse(BrowserActionResponse[CaptureOcSnapshotData]):
    pass


class BrowserOperationStatusResponse(BrowserReadResponse[BrowserOperationStatusData]):
    pass


class CancelBrowserOperationResponse(BrowserActionResponse[CancelBrowserOperationData]):
    pass


ConnectionStatusProvider = Callable[
    [str | None], EdgeConnectionStatusData | Mapping[str, Any]
]


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((perf_counter() - started) * 1_000)))


def _audit_id(tool_name: str, operation_id: str | None, idempotency_key: str | None) -> str:
    if operation_id:
        return f"browser-operation:{operation_id}"
    if idempotency_key:
        return f"browser-request:{idempotency_key}"
    return f"browser-tool:{tool_name}"


def _audit(
    *,
    tool_name: str,
    operation: str,
    read_only: bool,
    operation_id: str | None = None,
    application_id: str | None = None,
    device_id: str | None = None,
    idempotency_key: str | None = None,
    task_id: str | None = None,
    idempotent_replay: bool = False,
) -> BrowserAuditFields:
    return BrowserAuditFields(
        audit_id=_audit_id(tool_name, operation_id, idempotency_key),
        tool_name=tool_name,
        operation=operation,
        operation_id=operation_id,
        application_id=application_id,
        device_id=device_id,
        idempotency_key=idempotency_key,
        task_id=task_id,
        side_effect=not read_only,
        read_only=read_only,
        idempotent_replay=idempotent_replay,
    )


def _evidence(tool_name: str, reference: str | None = None) -> list[EvidenceSource]:
    return [EvidenceSource(source="browser_bridge_store", source_ref=reference or tool_name)]


def _redacted_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    safe = redact_sensitive(dict(value))
    return safe if isinstance(safe, dict) else {}


def _operation_data(operation: BrowserOperation) -> BrowserOperationData:
    command = _redacted_mapping(operation.command)
    application_id = command.get("application_id")
    if not isinstance(application_id, str):
        application_id = None
    result = _redacted_mapping(operation.result)
    result_value = result if operation.result is not None else None
    status = normalize_status(operation.status)
    return BrowserOperationData(
        operation_id=operation.operation_id,
        operation=operation.operation,
        application_id=application_id,
        device_id=operation.device_id,
        idempotency_key=operation.idempotency_key,
        status=status,
        command=command,
        result=result_value,
        terminal_result=result_value,
        error_code=operation.error_code,
        last_event_sequence=operation.last_event_sequence,
        last_outbox_sequence=operation.last_outbox_sequence,
        completed_at=operation.completed_at,
        created_at=operation.created_at,
        updated_at=operation.updated_at,
        terminal=status in TERMINAL_STATUSES,
    )


def _event_data(event: BrowserOperationEvent) -> BrowserOperationEventData:
    payload = _redacted_mapping(event.payload)
    return BrowserOperationEventData(
        event_id=event.event_id,
        operation_id=event.operation_id,
        sequence=event.sequence,
        status=normalize_status(event.status),
        event_type=event.event_type,
        payload=payload,
        occurred_at=event.occurred_at,
        created_at=event.created_at,
    )


def _error_code_for(exc: Exception, *, default: BrowserErrorCode) -> BrowserErrorCode:
    message = str(exc).casefold()
    if isinstance(exc, KeyError) or "not found" in message:
        return BrowserErrorCode.OPERATION_NOT_FOUND
    if "idempotency" in message or "conflicts with" in message:
        return BrowserErrorCode.IDEMPOTENCY_CONFLICT
    if "cannot cancel" in message or "terminal" in message:
        return BrowserErrorCode.OPERATION_NOT_ACTIVE
    return default


def _response(
    response_type: type[BrowserBridgeResponse[Any]],
    *,
    tool_name: str,
    operation: str,
    read_only: bool,
    request: BrowserBridgeInput,
    started: float,
    data: BaseModel | None = None,
    error_code: BrowserErrorCode | None = None,
    error_message: str | None = None,
    operation_id: str | None = None,
    application_id: str | None = None,
    device_id: str | None = None,
    idempotency_key: str | None = None,
    task_id: str | None = None,
    idempotent_replay: bool = False,
    timed_out: bool = False,
) -> BrowserBridgeResponse[Any]:
    return response_type(
        tool_name=tool_name,
        status=ToolStatus.SUCCESS if error_code is None else ToolStatus.FAILURE,
        success=error_code is None,
        data=data,
        evidence=_evidence(tool_name, operation_id or device_id),
        error_code=error_code,
        error_message=error_message,
        timeout_ms=request.timeout_ms,
        timed_out=timed_out,
        elapsed_ms=_elapsed_ms(started),
        audit=_audit(
            tool_name=tool_name,
            operation=operation,
            read_only=read_only,
            operation_id=operation_id,
            application_id=application_id,
            device_id=device_id,
            idempotency_key=idempotency_key,
            task_id=task_id,
            idempotent_replay=idempotent_replay,
        ),
        read_only=read_only,
    )


def _missing_store(
    response_type: type[BrowserBridgeResponse[Any]],
    request: BrowserBridgeInput,
    *,
    tool_name: str,
    operation: str,
    read_only: bool,
    application_id: str | None = None,
    device_id: str | None = None,
    idempotency_key: str | None = None,
    operation_id: str | None = None,
    task_id: str | None = None,
) -> BrowserBridgeResponse[Any]:
    return _response(
        response_type,
        tool_name=tool_name,
        operation=operation,
        read_only=read_only,
        request=request,
        started=perf_counter(),
        error_code=BrowserErrorCode.BRIDGE_DEPENDENCY_MISSING,
        error_message="BrowserBridgeStore was not injected into the MCP tool boundary.",
        operation_id=operation_id,
        application_id=application_id,
        device_id=device_id,
        idempotency_key=idempotency_key,
        task_id=task_id,
    )


def _connection_data(
    value: EdgeConnectionStatusData | Mapping[str, Any],
    *,
    device_id: str | None,
) -> EdgeConnectionStatusData:
    if isinstance(value, EdgeConnectionStatusData):
        return value
    raw = dict(value)
    raw.setdefault("device_id", device_id)
    if "status" not in raw:
        raw["status"] = raw.get("connection_state", raw.get("state", "unknown"))
    if "connected" not in raw and raw["status"] in {
        EdgeConnectionState.CONNECTED.value,
        EdgeConnectionState.DISCONNECTED.value,
    }:
        raw["connected"] = raw["status"] == EdgeConnectionState.CONNECTED.value
    return EdgeConnectionStatusData.model_validate(redact_sensitive(raw))


def edge_connection_status(
    request: EdgeConnectionStatusInput,
    browser_bridge_store: BrowserBridgeStore | None = None,
    connection_status_provider: ConnectionStatusProvider | None = None,
) -> EdgeConnectionStatusResponse:
    """Read bridge connection evidence without creating or mutating an operation."""

    started = perf_counter()
    tool_name = "edge_connection_status"
    if browser_bridge_store is None:
        return _missing_store(
            EdgeConnectionStatusResponse,
            request,
            tool_name=tool_name,
            operation=tool_name,
            read_only=True,
            device_id=request.device_id,
        )  # type: ignore[return-value]

    if connection_status_provider is not None:
        try:
            data = _connection_data(
                connection_status_provider(request.device_id), device_id=request.device_id
            )
        except Exception:
            return _response(
                EdgeConnectionStatusResponse,
                tool_name=tool_name,
                operation=tool_name,
                read_only=True,
                request=request,
                started=started,
                error_code=BrowserErrorCode.CONNECTION_STATUS_UNAVAILABLE,
                error_message="The injected Edge connection status provider is unavailable.",
                device_id=request.device_id,
            )  # type: ignore[return-value]
        return _response(
            EdgeConnectionStatusResponse,
            tool_name=tool_name,
            operation=tool_name,
            read_only=True,
            request=request,
            started=started,
            data=data,
            device_id=request.device_id,
        )  # type: ignore[return-value]

    try:
        device_id = request.device_id
        if device_id is None:
            connected_ids = browser_bridge_store.list_connected_device_ids()
            if len(connected_ids) == 1:
                device_id = connected_ids[0]
            elif len(connected_ids) > 1:
                data = EdgeConnectionStatusData(
                    status=EdgeConnectionState.UNKNOWN,
                    connected=None,
                    available=False,
                    reason="multiple_connected_devices",
                )
                return _response(
                    EdgeConnectionStatusResponse,
                    tool_name=tool_name,
                    operation=tool_name,
                    read_only=True,
                    request=request,
                    started=started,
                    data=data,
                )  # type: ignore[return-value]
        if device_id is not None:
            persisted = browser_bridge_store.get_connection_status(device_id)
            if persisted is not None:
                data = _connection_data(dict(persisted), device_id=device_id)
                return _response(
                    EdgeConnectionStatusResponse,
                    tool_name=tool_name,
                    operation=tool_name,
                    read_only=True,
                    request=request,
                    started=started,
                    data=data,
                    device_id=device_id,
                )  # type: ignore[return-value]
    except Exception:
            return _response(
                EdgeConnectionStatusResponse,
                tool_name=tool_name,
                operation=tool_name,
                read_only=True,
                request=request,
                started=started,
                error_code=BrowserErrorCode.CONNECTION_STATUS_UNAVAILABLE,
                error_message="The browser bridge connection status could not be read.",
                device_id=request.device_id,
            )  # type: ignore[return-value]

    data = EdgeConnectionStatusData(
        device_id=request.device_id,
        status=(
            EdgeConnectionState.UNKNOWN
            if request.device_id is not None
            else EdgeConnectionState.DISCONNECTED
        ),
        connected=None if request.device_id is not None else False,
        available=request.device_id is None,
        pending_outbox_count=0,
        reason=(
            "device_presence_not_found"
            if request.device_id is not None
            else "no_connected_edge_device"
        ),
    )
    return _response(
        EdgeConnectionStatusResponse,
        tool_name=tool_name,
        operation=tool_name,
        read_only=True,
        request=request,
        started=started,
        data=data,
        device_id=request.device_id,
    )  # type: ignore[return-value]


def observe_application_status_page(
    request: ObserveApplicationStatusPageInput,
    browser_bridge_store: BrowserBridgeStore | None = None,
) -> ObserveApplicationStatusPageResponse:
    """Create a durable Edge observation operation for one application page."""

    tool_name = "observe_application_status_page"
    operation_name = OperationName.OBSERVE_APPLICATION_STATUS_PAGE.value
    if browser_bridge_store is None:
        return _missing_store(
            ObserveApplicationStatusPageResponse,
            request,
            tool_name=tool_name,
            operation=operation_name,
            read_only=False,
            application_id=request.application_id,
            device_id=request.device_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]

    started = perf_counter()
    if request.application_url is None:
        return _response(
            ObserveApplicationStatusPageResponse,
            tool_name=tool_name,
            operation=operation_name,
            read_only=False,
            request=request,
            started=started,
            error_code=BrowserErrorCode.INVALID_INPUT,
            error_message="An application URL is required before dispatching an Edge observation.",
            application_id=request.application_id,
            device_id=request.device_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]
    normalized_application_url = normalize_http_page_url(request.application_url)
    if normalized_application_url is None:
        return _response(
            ObserveApplicationStatusPageResponse,
            tool_name=tool_name,
            operation=operation_name,
            read_only=False,
            request=request,
            started=started,
            error_code=BrowserErrorCode.INVALID_INPUT,
            error_message="An application URL is required before dispatching an Edge observation.",
            application_id=request.application_id,
            device_id=request.device_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]
    device_id = request.device_id
    if device_id is None:
        try:
            connected_ids = browser_bridge_store.list_connected_device_ids()
        except Exception:
            connected_ids = []
        if len(connected_ids) != 1:
            return _response(
                ObserveApplicationStatusPageResponse,
                tool_name=tool_name,
                operation=operation_name,
                read_only=False,
                request=request,
                started=started,
                error_code=BrowserErrorCode.CONNECTION_STATUS_UNAVAILABLE,
                error_message="Exactly one connected Edge device is required.",
                application_id=request.application_id,
                idempotency_key=request.idempotency_key,
                operation_id=request.operation_id,
                task_id=request.task_id,
            )  # type: ignore[return-value]
        device_id = connected_ids[0]

    application_ids = _observation_application_ids(request)
    command: dict[str, Any] = {
        "application_id": request.application_id,
        "application_ids": application_ids,
        "action": "observe_application_page",
        "selector_key": "application_page",
        "params": {
            "include_vision": request.include_vision,
            "vision_fallback_reason": request.vision_fallback_reason,
            "retain_on_pause": request.retain_on_pause,
        },
    }
    if normalized_application_url is not None:
        command["application_url"] = normalized_application_url
        command["page_url"] = normalized_application_url
        command["origin"] = urlparse(normalized_application_url)._replace(
            path="", params="", query="", fragment=""
        ).geturl().rstrip("/")

    try:
        replay = browser_bridge_store.get_by_idempotency_key(request.idempotency_key) is not None
        operation = browser_bridge_store.create(
            OperationName.OBSERVE_APPLICATION_STATUS_PAGE,
            device_id=device_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            command=command,
        )
    except Exception as exc:
        code = _error_code_for(exc, default=BrowserErrorCode.STORE_UNAVAILABLE)
        return _response(
            ObserveApplicationStatusPageResponse,
            tool_name=tool_name,
            operation=operation_name,
            read_only=False,
            request=request,
            started=started,
            error_code=code,
            error_message="The browser observation could not be persisted.",
            application_id=request.application_id,
            device_id=device_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]

    data = ObserveApplicationStatusPageData(
        **_operation_data(operation).model_dump(),
        idempotent_replay=replay,
    )
    return _response(
        ObserveApplicationStatusPageResponse,
        tool_name=tool_name,
        operation=operation_name,
        read_only=False,
        request=request,
        started=started,
        data=data,
        operation_id=operation.operation_id,
        application_id=request.application_id,
        device_id=device_id,
        idempotency_key=request.idempotency_key,
        task_id=request.task_id,
        idempotent_replay=replay,
    )  # type: ignore[return-value]


def capture_oc_snapshot(
    request: CaptureOcSnapshotInput,
    browser_bridge_store: BrowserBridgeStore | None = None,
) -> CaptureOcSnapshotResponse:
    """Create one durable fixed-filter GiveMeOC snapshot operation."""

    tool_name = "capture_oc_snapshot"
    operation_name = OperationName.CAPTURE_OC_SNAPSHOT.value
    if browser_bridge_store is None:
        return _missing_store(
            CaptureOcSnapshotResponse,
            request,
            tool_name=tool_name,
            operation=operation_name,
            read_only=False,
            device_id=request.device_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]
    started = perf_counter()
    device_id = request.device_id
    if device_id is None:
        try:
            connected_ids = browser_bridge_store.list_connected_device_ids()
        except Exception:
            connected_ids = []
        if len(connected_ids) != 1:
            return _response(
                CaptureOcSnapshotResponse,
                tool_name=tool_name,
                operation=operation_name,
                read_only=False,
                request=request,
                started=started,
                error_code=BrowserErrorCode.CONNECTION_STATUS_UNAVAILABLE,
                error_message="Exactly one connected Edge device is required.",
                idempotency_key=request.idempotency_key,
                operation_id=request.operation_id,
                task_id=request.task_id,
            )  # type: ignore[return-value]
        device_id = connected_ids[0]
    command = {
        "application_id": "oc-snapshot",
        "application_ids": ["oc-snapshot"],
        "action": "capture_oc_page",
        "selector_key": "oc_company_table",
        "params": {"page": 1, "apply_filters": True},
        "page_url": "https://www.givemeoc.com/",
        "origin": "https://www.givemeoc.com",
    }
    try:
        replay = browser_bridge_store.get_by_idempotency_key(request.idempotency_key) is not None
        operation = browser_bridge_store.create(
            OperationName.CAPTURE_OC_SNAPSHOT,
            device_id=device_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            command=command,
        )
    except Exception as exc:
        return _response(
            CaptureOcSnapshotResponse,
            tool_name=tool_name,
            operation=operation_name,
            read_only=False,
            request=request,
            started=started,
            error_code=_error_code_for(exc, default=BrowserErrorCode.STORE_UNAVAILABLE),
            error_message="The OC capture operation could not be persisted.",
            device_id=device_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]
    data = CaptureOcSnapshotData(
        **_operation_data(operation).model_dump(),
        idempotent_replay=replay,
    )
    return _response(
        CaptureOcSnapshotResponse,
        tool_name=tool_name,
        operation=operation_name,
        read_only=False,
        request=request,
        started=started,
        data=data,
        operation_id=operation.operation_id,
        device_id=device_id,
        idempotency_key=request.idempotency_key,
        task_id=request.task_id,
        idempotent_replay=replay,
    )  # type: ignore[return-value]


async def capture_oc_snapshot_workflow(
    request: CaptureOcSnapshotInput,
    browser_bridge_store: BrowserBridgeStore | None,
) -> CaptureOcSnapshotResponse:
    """Dispatch OC capture and wait for the extension to persist the snapshot."""

    started = perf_counter()
    created = capture_oc_snapshot(request, browser_bridge_store)
    if not created.success or created.data is None or browser_bridge_store is None:
        return created
    operation_id = created.data.operation_id
    deadline = asyncio.get_running_loop().time() + request.timeout_ms / 1_000
    while asyncio.get_running_loop().time() < deadline:
        operation = browser_bridge_store.get_operation(operation_id)
        if operation is None:
            break
        status = normalize_status(operation.status)
        if status in TERMINAL_STATUSES:
            result = _redacted_mapping(operation.result)
            data = CaptureOcSnapshotData(
                **_operation_data(operation).model_dump(),
                idempotent_replay=created.data.idempotent_replay,
                snapshot_path=result.get("snapshot_path") if isinstance(result.get("snapshot_path"), str) else None,
                record_count=result.get("record_count") if isinstance(result.get("record_count"), int) else None,
                total_pages=result.get("total_pages") if isinstance(result.get("total_pages"), int) else None,
                total_items=result.get("total_items") if isinstance(result.get("total_items"), int) else None,
                sha256=result.get("sha256") if isinstance(result.get("sha256"), str) else None,
            )
            failed = status is not OperationStatus.SUCCEEDED
            return _response(
                CaptureOcSnapshotResponse,
                tool_name="capture_oc_snapshot",
                operation=operation.operation,
                read_only=False,
                request=request,
                started=started,
                data=data,
                error_code=BrowserErrorCode.OPERATION_FAILED if failed else None,
                error_message="Edge could not capture the OC snapshot." if failed else None,
                operation_id=operation_id,
                device_id=operation.device_id,
                idempotency_key=request.idempotency_key,
                task_id=request.task_id,
                idempotent_replay=created.data.idempotent_replay,
            )  # type: ignore[return-value]
        await asyncio.sleep(0.2)
    try:
        browser_bridge_store.cancel(operation_id, reason="mcp_timeout")
    except (KeyError, ValueError):
        pass
    return _response(
        CaptureOcSnapshotResponse,
        tool_name="capture_oc_snapshot",
        operation=OperationName.CAPTURE_OC_SNAPSHOT.value,
        read_only=False,
        request=request,
        started=started,
        error_code=BrowserErrorCode.TIMEOUT,
        error_message="Timed out waiting for the OC snapshot.",
        operation_id=operation_id,
        idempotency_key=request.idempotency_key,
        task_id=request.task_id,
        timed_out=True,
    )  # type: ignore[return-value]


async def observe_application_status_page_workflow(
    request: ObserveApplicationStatusPageInput,
    browser_bridge_store: BrowserBridgeStore | None,
    repository: RecruitmentRepository,
) -> ObserveApplicationStatusPageResponse:
    """Resolve the stored URL, collect evidence, and return it to the model."""

    tool_name = "observe_application_status_page"
    started = perf_counter()
    if browser_bridge_store is None:
        return observe_application_status_page(request, None)
    requested_ids = _observation_application_ids(request)
    application_map = {str(item.id): item for item in repository.list_applications()}
    matches = [application_map[item_id] for item_id in requested_ids if item_id in application_map]
    if len(matches) != len(requested_ids):
        return _response(
            ObserveApplicationStatusPageResponse,
            tool_name=tool_name,
            operation=OperationName.OBSERVE_APPLICATION_STATUS_PAGE.value,
            read_only=False,
            request=request,
            started=started,
            error_code=BrowserErrorCode.INVALID_INPUT,
            error_message="Every application id must identify one persisted application.",
            application_id=request.application_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]
    page_url = request.application_url or matches[0].record_url
    if not page_url:
        return _response(
            ObserveApplicationStatusPageResponse,
            tool_name=tool_name,
            operation=OperationName.OBSERVE_APPLICATION_STATUS_PAGE.value,
            read_only=False,
            request=request,
            started=started,
            error_code=BrowserErrorCode.INVALID_INPUT,
            error_message="The application has no persisted recruitment status URL.",
            application_id=request.application_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]

    normalized_page_url = normalize_http_page_url(page_url)
    mismatched = [
        item for item in matches
        if not item.record_url or normalize_http_page_url(item.record_url) != normalized_page_url
    ]
    if normalized_page_url is None or mismatched:
        return _response(
            ObserveApplicationStatusPageResponse,
            tool_name=tool_name,
            operation=OperationName.OBSERVE_APPLICATION_STATUS_PAGE.value,
            read_only=False,
            request=request,
            started=started,
            error_code=BrowserErrorCode.INVALID_INPUT,
            error_message="All grouped applications must share the same normalized status page URL.",
            application_id=request.application_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]

    resolved = request.model_copy(update={"application_url": page_url, "application_ids": requested_ids})
    created = observe_application_status_page(resolved, browser_bridge_store)
    if not created.success or created.data is None:
        return created
    operation_id = created.data.operation_id
    deadline = asyncio.get_running_loop().time() + request.timeout_ms / 1_000
    poll_seconds = min(0.2, max(0.02, request.timeout_ms / 20_000))
    while asyncio.get_running_loop().time() < deadline:
        operation = browser_bridge_store.get_operation(operation_id)
        if operation is None:
            break
        status = normalize_status(operation.status)
        events = browser_bridge_store.get_events(operation_id)
        if status is OperationStatus.VALIDATING:
            observation = next(
                (
                    dict(event.payload.get("result"))
                    for event in reversed(events)
                    if event.event_type == "observation"
                    and isinstance(event.payload, Mapping)
                    and isinstance(event.payload.get("result"), Mapping)
                ),
                None,
            )
            if observation is None:
                await asyncio.sleep(poll_seconds)
                continue
            operation = browser_bridge_store.terminal_result(
                operation_id,
                observation,
                status=OperationStatus.SUCCEEDED,
                event_id=f"observed-{operation_id}",
            )
            data = ObserveApplicationStatusPageData(
                **_operation_data(operation).model_dump(),
                idempotent_replay=created.data.idempotent_replay,
                observation=_redacted_mapping(observation),
            )
            return _response(
                ObserveApplicationStatusPageResponse,
                tool_name=tool_name,
                operation=operation.operation,
                read_only=False,
                request=request,
                started=started,
                data=data,
                operation_id=operation_id,
                application_id=request.application_id,
                device_id=operation.device_id,
                idempotency_key=request.idempotency_key,
                task_id=request.task_id,
                idempotent_replay=created.data.idempotent_replay,
            )  # type: ignore[return-value]
        if status in TERMINAL_STATUSES:
            data = ObserveApplicationStatusPageData(
                **_operation_data(operation).model_dump(),
                idempotent_replay=created.data.idempotent_replay,
            )
            return _response(
                ObserveApplicationStatusPageResponse,
                tool_name=tool_name,
                operation=operation.operation,
                read_only=False,
                request=request,
                started=started,
                data=data,
                error_code=(BrowserErrorCode.OPERATION_FAILED if status is OperationStatus.FAILED else BrowserErrorCode.OPERATION_STATE_UNCLEAR),
                error_message="Edge could not return a usable observation.",
                operation_id=operation_id,
                application_id=request.application_id,
                device_id=operation.device_id,
                idempotency_key=request.idempotency_key,
                task_id=request.task_id,
            )  # type: ignore[return-value]
        await asyncio.sleep(poll_seconds)
    try:
        operation = browser_bridge_store.get_operation(operation_id)
        if operation is not None and normalize_status(operation.status) not in TERMINAL_STATUSES:
            browser_bridge_store.cancel(
                operation_id,
                reason="edge_observation_timeout",
                event_id=f"timeout-{operation_id}",
            )
    except Exception:
        pass
    return _response(
        ObserveApplicationStatusPageResponse,
        tool_name=tool_name,
        operation=OperationName.OBSERVE_APPLICATION_STATUS_PAGE.value,
        read_only=False,
        request=request,
        started=started,
        error_code=BrowserErrorCode.TIMEOUT,
        error_message="Timed out waiting for Edge evidence.",
        application_id=request.application_id,
        idempotency_key=request.idempotency_key,
        operation_id=operation_id,
        task_id=request.task_id,
        timed_out=True,
    )  # type: ignore[return-value]


def review_and_update_application_status(
    request: ReviewAndUpdateApplicationStatusInput,
    browser_bridge_store: BrowserBridgeStore | None = None,
) -> ReviewAndUpdateApplicationStatusResponse:
    """Create one durable, idempotent browser review operation immediately."""

    tool_name = "review_and_update_application_status"
    operation_name = OperationName.REVIEW_AND_UPDATE_APPLICATION_STATUS.value
    if browser_bridge_store is None:
        return _missing_store(
            ReviewAndUpdateApplicationStatusResponse,
            request,
            tool_name=tool_name,
            operation=operation_name,
            read_only=False,
            application_id=request.application_id,
            device_id=request.device_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]

    started = perf_counter()
    device_id = request.device_id
    if device_id is None:
        try:
            connected_ids = browser_bridge_store.list_connected_device_ids()
        except Exception:
            connected_ids = []
        if len(connected_ids) != 1:
            return _response(
                ReviewAndUpdateApplicationStatusResponse,
                tool_name=tool_name,
                operation=operation_name,
                read_only=False,
                request=request,
                started=started,
                error_code=BrowserErrorCode.CONNECTION_STATUS_UNAVAILABLE,
                error_message=(
                    "Exactly one connected Edge device is required when device_id is omitted."
                ),
                application_id=request.application_id,
                idempotency_key=request.idempotency_key,
                operation_id=request.operation_id,
                task_id=request.task_id,
            )  # type: ignore[return-value]
        device_id = connected_ids[0]
    command: dict[str, Any] = {"application_id": request.application_id}
    if request.application_url is not None:
        command["application_url"] = request.application_url
        command["page_url"] = request.application_url
        command["origin"] = urlparse(request.application_url)._replace(
            path="", params="", query="", fragment=""
        ).geturl().rstrip("/")
    command["application_ids"] = [request.application_id]
    command["action"] = "read_application_status"
    command["selector_key"] = "application_status"
    command["params"] = {}
    if request.target_status is not None:
        command["target_status"] = request.target_status

    replay = False
    try:
        replay = browser_bridge_store.get_by_idempotency_key(request.idempotency_key) is not None
        operation = browser_bridge_store.create(
            OperationName.REVIEW_AND_UPDATE_APPLICATION_STATUS,
            device_id=device_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            command=command,
        )
    except Exception as exc:
        code = _error_code_for(exc, default=BrowserErrorCode.STORE_UNAVAILABLE)
        return _response(
            ReviewAndUpdateApplicationStatusResponse,
            tool_name=tool_name,
            operation=operation_name,
            read_only=False,
            request=request,
            started=started,
            error_code=code,
            error_message=(
                "The idempotency key conflicts with an existing browser operation."
                if code is BrowserErrorCode.IDEMPOTENCY_CONFLICT
                else "The browser operation could not be persisted."
            ),
            application_id=request.application_id,
            device_id=device_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]

    data = ReviewAndUpdateApplicationStatusData(
        **_operation_data(operation).model_dump(),
        idempotent_replay=replay,
        requires_confirmation=False,
    )
    return _response(
        ReviewAndUpdateApplicationStatusResponse,
        tool_name=tool_name,
        operation=operation_name,
        read_only=False,
        request=request,
        started=started,
        data=data,
        operation_id=operation.operation_id,
        application_id=request.application_id,
        device_id=device_id,
        idempotency_key=request.idempotency_key,
        task_id=request.task_id,
        idempotent_replay=replay,
    )  # type: ignore[return-value]


async def review_and_update_application_status_workflow(
    request: ReviewAndUpdateApplicationStatusInput,
    browser_bridge_store: BrowserBridgeStore | None,
    repository: RecruitmentRepository,
) -> ReviewAndUpdateApplicationStatusResponse:
    """Push to Edge, wait for sanitized evidence, validate it and write safely."""

    tool_name = "review_and_update_application_status"
    started = perf_counter()
    if browser_bridge_store is None:
        return review_and_update_application_status(request, None)

    matches = [
        item for item in repository.list_applications()
        if str(item.id) == request.application_id
    ]
    if len(matches) != 1:
        return _response(
            ReviewAndUpdateApplicationStatusResponse,
            tool_name=tool_name,
            operation=OperationName.REVIEW_AND_UPDATE_APPLICATION_STATUS.value,
            read_only=False,
            request=request,
            started=started,
            error_code=BrowserErrorCode.INVALID_INPUT,
            error_message="The application_id must identify exactly one persisted application.",
            application_id=request.application_id,
            device_id=request.device_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]

    application = matches[0]
    page_url = request.application_url or application.record_url
    if not page_url:
        return _response(
            ReviewAndUpdateApplicationStatusResponse,
            tool_name=tool_name,
            operation=OperationName.REVIEW_AND_UPDATE_APPLICATION_STATUS.value,
            read_only=False,
            request=request,
            started=started,
            error_code=BrowserErrorCode.INVALID_INPUT,
            error_message="The application has no persisted recruitment status URL.",
            application_id=request.application_id,
            device_id=request.device_id,
            idempotency_key=request.idempotency_key,
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]

    resolved_request = request.model_copy(update={"application_url": page_url})
    created = review_and_update_application_status(resolved_request, browser_bridge_store)
    if not created.success or created.data is None:
        return created

    operation_id = created.data.operation_id
    deadline = asyncio.get_running_loop().time() + request.timeout_ms / 1_000
    poll_seconds = min(0.2, max(0.02, request.timeout_ms / 20_000))

    while asyncio.get_running_loop().time() < deadline:
        operation = browser_bridge_store.get_operation(operation_id)
        if operation is None:
            break
        operation_status = normalize_status(operation.status)
        events = browser_bridge_store.get_events(operation_id)

        if operation_status is OperationStatus.VALIDATING:
            observation: dict[str, Any] | None = None
            for event in reversed(events):
                payload = event.payload if isinstance(event.payload, Mapping) else {}
                candidate = payload.get("result")
                if event.event_type == "observation" and isinstance(candidate, Mapping):
                    observation = dict(candidate)
                    break
            if observation is None:
                await asyncio.sleep(poll_seconds)
                continue

            browser_bridge_store.append_event(
                operation_id,
                f"updating-{operation_id}",
                OperationStatus.UPDATING,
                {"application_id": request.application_id},
                event_type="validation",
            )
            verification = browser_status_update(
                BrowserStatusUpdateInput(
                    application_id=request.application_id,
                    page_url=page_url,
                    operation_id=operation_id,
                    terminal_result={
                        "operation_id": operation_id,
                        "idempotency_key": request.idempotency_key,
                        "status": "SUCCEEDED",
                        "result": observation,
                    },
                ),
                browser_bridge_store.storage,
            )
            verification_data = verification.model_dump(mode="json")
            if verification.status in {UpdateStatus.UPDATED, UpdateStatus.UNCHANGED}:
                final_status = OperationStatus.SUCCEEDED
                error_code = None
            elif verification.status is UpdateStatus.FAILED:
                final_status = OperationStatus.FAILED
                error_code = verification.error_code or "database_update_failed"
            else:
                final_status = OperationStatus.STATE_UNCLEAR
                error_code = verification.error_code or verification.status.value

            operation = browser_bridge_store.terminal_result(
                operation_id,
                {"browser": observation, "verification": verification_data},
                status=final_status,
                event_id=f"verified-{operation_id}",
                error_code=error_code,
            )
            operation_data = _operation_data(operation)
            data = ReviewAndUpdateApplicationStatusData(
                **operation_data.model_dump(),
                idempotent_replay=created.data.idempotent_replay,
                requires_confirmation=False,
                verification=verification_data,
            )
            if final_status is OperationStatus.SUCCEEDED:
                return _response(
                    ReviewAndUpdateApplicationStatusResponse,
                    tool_name=tool_name,
                    operation=operation.operation,
                    read_only=False,
                    request=request,
                    started=started,
                    data=data,
                    operation_id=operation_id,
                    application_id=request.application_id,
                    device_id=operation.device_id,
                    idempotency_key=request.idempotency_key,
                    task_id=request.task_id,
                    idempotent_replay=created.data.idempotent_replay,
                )  # type: ignore[return-value]
            return _response(
                ReviewAndUpdateApplicationStatusResponse,
                tool_name=tool_name,
                operation=operation.operation,
                read_only=False,
                request=request,
                started=started,
                data=data,
                error_code=(
                    BrowserErrorCode.OPERATION_FAILED
                    if final_status is OperationStatus.FAILED
                    else BrowserErrorCode.OPERATION_STATE_UNCLEAR
                ),
                error_message=(
                    verification.error_message
                    or "The Edge evidence could not be applied automatically."
                ),
                operation_id=operation_id,
                application_id=request.application_id,
                device_id=operation.device_id,
                idempotency_key=request.idempotency_key,
                task_id=request.task_id,
                idempotent_replay=created.data.idempotent_replay,
            )  # type: ignore[return-value]

        if operation_status in TERMINAL_STATUSES:
            operation_data = _operation_data(operation)
            data = ReviewAndUpdateApplicationStatusData(
                **operation_data.model_dump(),
                idempotent_replay=created.data.idempotent_replay,
                requires_confirmation=False,
            )
            return _response(
                ReviewAndUpdateApplicationStatusResponse,
                tool_name=tool_name,
                operation=operation.operation,
                read_only=False,
                request=request,
                started=started,
                data=data,
                error_code=(
                    BrowserErrorCode.OPERATION_STATE_UNCLEAR
                    if operation_status is OperationStatus.STATE_UNCLEAR
                    else BrowserErrorCode.OPERATION_FAILED
                ),
                error_message="The Edge operation ended without a verified database update.",
                operation_id=operation_id,
                application_id=request.application_id,
                device_id=operation.device_id,
                idempotency_key=request.idempotency_key,
                task_id=request.task_id,
            )  # type: ignore[return-value]
        await asyncio.sleep(poll_seconds)

    try:
        browser_bridge_store.cancel(operation_id, reason="mcp_timeout")
    except (KeyError, ValueError):
        pass
    return _response(
        ReviewAndUpdateApplicationStatusResponse,
        tool_name=tool_name,
        operation=OperationName.REVIEW_AND_UPDATE_APPLICATION_STATUS.value,
        read_only=False,
        request=request,
        started=started,
        error_code=BrowserErrorCode.TIMEOUT,
        error_message="Timed out while waiting for the Edge browser operation.",
        operation_id=operation_id,
        application_id=request.application_id,
        device_id=created.data.device_id,
        idempotency_key=request.idempotency_key,
        task_id=request.task_id,
    )  # type: ignore[return-value]


def browser_operation_status(
    request: BrowserOperationStatusInput,
    browser_bridge_store: BrowserBridgeStore | None = None,
) -> BrowserOperationStatusResponse:
    """Read bounded events, optionally waiting for progress after a cursor."""

    tool_name = "browser_operation_status"
    if browser_bridge_store is None:
        return _missing_store(
            BrowserOperationStatusResponse,
            request,
            tool_name=tool_name,
            operation=tool_name,
            read_only=True,
            operation_id=request.operation_id,
        )  # type: ignore[return-value]

    started = perf_counter()
    try:
        operation = browser_bridge_store.get_operation(request.operation_id)
        if operation is None:
            return _response(
                BrowserOperationStatusResponse,
                tool_name=tool_name,
                operation=tool_name,
                read_only=True,
                request=request,
                started=started,
                error_code=BrowserErrorCode.OPERATION_NOT_FOUND,
                error_message="The requested browser operation was not found.",
                operation_id=request.operation_id,
            )  # type: ignore[return-value]
        cursor = request.after_sequence
        wait_timed_out = False
        if cursor is not None:
            deadline = perf_counter() + request.timeout_ms / 1_000
            while (
                normalize_status(operation.status) not in TERMINAL_STATUSES
                and operation.last_event_sequence <= cursor
            ):
                remaining = deadline - perf_counter()
                if remaining <= 0:
                    wait_timed_out = True
                    break
                sleep(min(0.25, remaining))
                operation = browser_bridge_store.get_operation(request.operation_id)
                if operation is None:
                    raise KeyError(request.operation_id)
            events = browser_bridge_store.get_events(
                request.operation_id,
                cursor,
                limit=request.event_limit,
            )
        else:
            all_events = browser_bridge_store.get_events(request.operation_id)
            events = all_events[-request.event_limit :]
    except Exception as exc:
        return _response(
            BrowserOperationStatusResponse,
            tool_name=tool_name,
            operation=tool_name,
            read_only=True,
            request=request,
            started=started,
            error_code=_error_code_for(exc, default=BrowserErrorCode.STORE_UNAVAILABLE),
            error_message="The browser operation status could not be read.",
            operation_id=request.operation_id,
        )  # type: ignore[return-value]

    operation_data = _operation_data(operation)
    next_sequence = (
        events[-1].sequence
        if events
        else (request.after_sequence or operation.last_event_sequence)
    )
    data = BrowserOperationStatusData(
        operation=operation_data,
        operation_id=operation.operation_id,
        status=operation_data.status,
        events=[_event_data(event) for event in events],
        next_sequence=next_sequence,
        has_more_events=operation.last_event_sequence > next_sequence,
        changed=(request.after_sequence is None or bool(events) or operation_data.terminal),
        wait_timed_out=wait_timed_out,
        terminal=operation_data.terminal,
        terminal_result=operation_data.result,
    )
    return _response(
        BrowserOperationStatusResponse,
        tool_name=tool_name,
        operation=operation.operation,
        read_only=True,
        request=request,
        started=started,
        data=data,
        operation_id=operation.operation_id,
        device_id=operation.device_id,
        idempotency_key=operation.idempotency_key,
    )  # type: ignore[return-value]


def cancel_browser_operation(
    request: CancelBrowserOperationInput,
    browser_bridge_store: BrowserBridgeStore | None = None,
) -> CancelBrowserOperationResponse:
    """Cancel an active operation and return the durable cancellation event."""

    tool_name = "cancel_browser_operation"
    if browser_bridge_store is None:
        return _missing_store(
            CancelBrowserOperationResponse,
            request,
            tool_name=tool_name,
            operation=tool_name,
            read_only=False,
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]

    started = perf_counter()
    try:
        operation = browser_bridge_store.get_operation(request.operation_id)
        if operation is None:
            return _response(
                CancelBrowserOperationResponse,
                tool_name=tool_name,
                operation=tool_name,
                read_only=False,
                request=request,
                started=started,
                error_code=BrowserErrorCode.OPERATION_NOT_FOUND,
                error_message="The requested browser operation was not found.",
                operation_id=request.operation_id,
                task_id=request.task_id,
            )  # type: ignore[return-value]

        current_status = normalize_status(operation.status)
        replay = current_status is OperationStatus.CANCELLED
        if current_status in TERMINAL_STATUSES and not replay:
            return _response(
                CancelBrowserOperationResponse,
                tool_name=tool_name,
                operation=operation.operation,
                read_only=False,
                request=request,
                started=started,
                error_code=BrowserErrorCode.OPERATION_NOT_ACTIVE,
                error_message="Only an active browser operation can be cancelled.",
                operation_id=operation.operation_id,
                application_id=_operation_data(operation).application_id,
                device_id=operation.device_id,
                idempotency_key=request.idempotency_key or operation.idempotency_key,
                task_id=request.task_id,
            )  # type: ignore[return-value]

        if not replay:
            operation = browser_bridge_store.cancel(
                request.operation_id,
                reason=request.reason,
            )
        events = browser_bridge_store.get_events(request.operation_id)
    except Exception as exc:
        code = _error_code_for(exc, default=BrowserErrorCode.STORE_UNAVAILABLE)
        return _response(
            CancelBrowserOperationResponse,
            tool_name=tool_name,
            operation=tool_name,
            read_only=False,
            request=request,
            started=started,
            error_code=code,
            error_message=(
                "Only an active browser operation can be cancelled."
                if code is BrowserErrorCode.OPERATION_NOT_ACTIVE
                else "The browser operation could not be cancelled."
            ),
            operation_id=request.operation_id,
            task_id=request.task_id,
        )  # type: ignore[return-value]

    operation_data = _operation_data(operation)
    data = CancelBrowserOperationData(
        **operation_data.model_dump(),
        idempotent_replay=replay,
        cancelled=True,
    )
    return _response(
        CancelBrowserOperationResponse,
        tool_name=tool_name,
        operation=operation.operation,
        read_only=False,
        request=request,
        started=started,
        data=data,
        operation_id=operation.operation_id,
        application_id=operation_data.application_id,
        device_id=operation.device_id,
        idempotency_key=request.idempotency_key or operation.idempotency_key,
        task_id=request.task_id,
        idempotent_replay=replay,
    )  # type: ignore[return-value]


# Short aliases for callers that use the operation-oriented vocabulary.
BrowserOperationStatusDataModel = BrowserOperationStatusData
BrowserOperationStatusRequest = BrowserOperationStatusInput
ReviewApplicationStatusInput = ReviewAndUpdateApplicationStatusInput
ReviewApplicationStatusResponse = ReviewAndUpdateApplicationStatusResponse


__all__ = [
    "BrowserActionResponse",
    "BrowserAuditFields",
    "BrowserBridgeInput",
    "BrowserBridgeModel",
    "BrowserBridgeResponse",
    "BrowserErrorCode",
    "BrowserOperationData",
    "BrowserOperationEventData",
    "BrowserOperationStatusData",
    "BrowserOperationStatusDataModel",
    "BrowserOperationStatusInput",
    "BrowserOperationStatusRequest",
    "BrowserOperationStatusResponse",
    "BrowserReadResponse",
    "CancelBrowserOperationData",
    "CancelBrowserOperationInput",
    "CancelBrowserOperationResponse",
    "CaptureOcSnapshotData",
    "CaptureOcSnapshotInput",
    "CaptureOcSnapshotResponse",
    "ConnectionStatusProvider",
    "EdgeConnectionState",
    "EdgeConnectionStatusData",
    "EdgeConnectionStatusInput",
    "EdgeConnectionStatusResponse",
    "ObserveApplicationStatusPageData",
    "ObserveApplicationStatusPageInput",
    "ObserveApplicationStatusPageResponse",
    "ReviewAndUpdateApplicationStatusData",
    "ReviewAndUpdateApplicationStatusInput",
    "ReviewAndUpdateApplicationStatusResponse",
    "ReviewApplicationStatusInput",
    "ReviewApplicationStatusResponse",
    "browser_operation_status",
    "cancel_browser_operation",
    "capture_oc_snapshot",
    "capture_oc_snapshot_workflow",
    "edge_connection_status",
    "observe_application_status_page",
    "observe_application_status_page_workflow",
    "review_and_update_application_status",
    "review_and_update_application_status_workflow",
]
