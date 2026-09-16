from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .client import JsonRpcNotification


class CodexEventType(StrEnum):
    UNKNOWN = "unknown"
    THREAD_STARTED = "thread_started"
    THREAD_UPDATED = "thread_updated"
    TURN_STARTED = "turn_started"
    TURN_COMPLETED = "turn_completed"
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    ITEM_STARTED = "item_started"
    ITEM_COMPLETED = "item_completed"
    ERROR = "error"
    HEALTH = "health"


EventType = CodexEventType


class CodexEvent(BaseModel):
    """Stable envelope for notifications from different app-server versions."""

    model_config = ConfigDict(extra="forbid")

    event_type: CodexEventType
    method: str = Field(min_length=1)
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    text: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def kind(self) -> CodexEventType:
        return self.event_type

    @property
    def data(self) -> dict[str, Any]:
        return self.payload


NormalizedEvent = CodexEvent


def normalize_event(
    notification: JsonRpcNotification | Mapping[str, Any] | str,
    params: Any = None,
) -> CodexEvent:
    """Normalize raw JSON-RPC notification names and common ID aliases."""

    if isinstance(notification, JsonRpcNotification):
        method = notification.method
        raw_params = notification.params
    elif isinstance(notification, Mapping):
        method = notification.get("method")
        raw_params = notification.get("params")
        if not isinstance(method, str):
            raise ValueError("notification method is required")
    else:
        method = notification
        raw_params = params
    if not isinstance(method, str) or not method.strip():
        raise ValueError("notification method is required")

    payload = dict(raw_params) if isinstance(raw_params, Mapping) else {"value": raw_params}
    event_name = str(payload.get("type") or method)
    event_type = _classify(event_name)
    return CodexEvent(
        event_type=event_type,
        method=method,
        thread_id=_find_string(
            payload,
            "thread_id",
            "threadId",
            "conversation_id",
            "conversationId",
        ) or _find_entity_id(payload, "thread"),
        turn_id=_find_string(payload, "turn_id", "turnId") or _find_entity_id(payload, "turn"),
        item_id=_find_string(payload, "item_id", "itemId") or _find_entity_id(payload, "item"),
        text=_event_text(payload),
        payload=payload,
    )


def _classify(method: str) -> CodexEventType:
    name = method.casefold().replace(".", "/").replace("-", "/")
    if "error" in name or "failed" in name:
        return CodexEventType.ERROR
    if "health" in name:
        return CodexEventType.HEALTH
    if "reasoning" in name and "delta" in name:
        return CodexEventType.REASONING_DELTA
    if "delta" in name and any(token in name for token in ("agentmessage", "agent/message")):
        return CodexEventType.TEXT_DELTA
    if "thread" in name:
        if "start" in name or "create" in name:
            return CodexEventType.THREAD_STARTED
        return CodexEventType.THREAD_UPDATED
    if "turn" in name:
        if any(token in name for token in ("complete", "done", "finish")):
            return CodexEventType.TURN_COMPLETED
        return CodexEventType.TURN_STARTED
    if "item" in name:
        if any(token in name for token in ("complete", "done", "finish")):
            return CodexEventType.ITEM_COMPLETED
        return CodexEventType.ITEM_STARTED
    return CodexEventType.UNKNOWN


def _find_string(payload: Mapping[str, Any], *keys: str) -> str | None:
    wanted = {key.casefold() for key in keys}
    for mapping in _walk_mappings(payload):
        for key, value in mapping.items():
            if str(key).casefold() in wanted and isinstance(value, str) and value:
                return value
    return None


def _find_entity_id(payload: Mapping[str, Any], entity: str) -> str | None:
    value = payload.get(entity)
    if isinstance(value, Mapping):
        candidate = value.get("id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _walk_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _walk_mappings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_mappings(nested)


def _event_text(payload: Mapping[str, Any]) -> str | None:
    delta = payload.get("delta")
    if isinstance(delta, str):
        return delta
    if isinstance(delta, Mapping):
        for key in ("text", "value", "content"):
            value = delta.get(key)
            if isinstance(value, str):
                return value
    for key in ("text", "content", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    error = payload.get("error")
    if isinstance(error, Mapping):
        for key in ("message", "detail", "code"):
            value = error.get(key)
            if isinstance(value, str) and value:
                return value
    return None


__all__ = [
    "CodexEvent",
    "CodexEventType",
    "EventType",
    "NormalizedEvent",
    "normalize_event",
]
