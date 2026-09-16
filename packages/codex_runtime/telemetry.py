"""Local, redacted telemetry for normalized Codex app-server events.

The module deliberately projects events onto a small allow-list.  It never
stores ``CodexEvent.text`` or the raw event payload, because either can contain
user messages, mail bodies, page markup, cookies, or provider credentials.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .events import CodexEvent, CodexEventType


_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SENSITIVE_LABEL = re.compile(
    r"(?:bearer|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"cookie|session|password|secret|private[_-]?key|sk-[A-Za-z0-9])",
    re.IGNORECASE,
)
_ERROR_KEYS = {"error", "errors", "exception", "failure", "failed"}
_ERROR_CODE_KEYS = {"error_code", "errorcode", "code", "error_type", "category"}
_SUCCESS_KEYS = {"success", "ok"}
_USAGE_KEYS = {"usage", "token_usage", "tokenusage", "tokens"}
_INPUT_TOKEN_KEYS = {
    "input_tokens",
    "inputtokens",
    "prompt_tokens",
    "prompttokens",
    "token_input",
    "tokeninput",
}
_OUTPUT_TOKEN_KEYS = {
    "output_tokens",
    "outputtokens",
    "completion_tokens",
    "completiontokens",
    "token_output",
    "tokenoutput",
}
_TOTAL_TOKEN_KEYS = {"total_tokens", "totaltokens", "token_total", "tokentotal"}


class CodexTrace(BaseModel):
    """Safe projection of one normalized Codex event.

    Only identifiers, stage labels, timing, token counts, and a short error
    category are retained.  In particular, this model has no text or payload
    field by design.
    """

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(default_factory=lambda: uuid4().hex, min_length=1)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    event_type: str = Field(min_length=1)
    method: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    thread_id: str | None = None
    turn_id: str | None = None
    item_id: str | None = None
    tool_name: str | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    success: bool | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    error_code: str | None = None

    @property
    def elapsed_ms(self) -> float | None:
        """Compatibility spelling for consumers that call latency elapsed time."""

        return self.latency_ms

    @property
    def token_input(self) -> int | None:
        return self.input_tokens

    @property
    def token_output(self) -> int | None:
        return self.output_tokens

    @property
    def tool(self) -> str | None:
        return self.tool_name


class TraceRecorder(Protocol):
    """Minimal sink interface accepted by :class:`CodexTelemetry`."""

    def record(self, trace: CodexTrace) -> None: ...


class InMemoryTraceRecorder:
    """Deterministic recorder for tests and local development."""

    def __init__(self) -> None:
        self.events: list[CodexTrace] = []

    def record(self, trace: CodexTrace) -> None:
        self.events.append(trace)


class JsonlTraceRecorder:
    """Append structured traces to a local JSONL file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = Lock()

    def record(self, trace: CodexTrace) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(trace.model_dump_json() + "\n")

    def read(self, *, limit: int = 200) -> list[CodexTrace]:
        """Return the newest valid records and ignore an incomplete last line."""

        if limit < 1 or not self.path.is_file():
            return []
        with self._lock, self.path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        traces: list[CodexTrace] = []
        for line in lines[-limit:]:
            try:
                traces.append(CodexTrace.model_validate_json(line))
            except ValueError:
                continue
        return traces


class CodexTelemetry:
    """Build redacted traces from normalized events and emit them to a sink."""

    def __init__(
        self,
        recorder: TraceRecorder | None = None,
        *,
        clock: Callable[[], float] = monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.recorder = recorder
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._starts: dict[tuple[str, str, str, str], float] = {}

    def record(
        self,
        event: CodexEvent,
        *,
        timestamp: float | datetime | None = None,
    ) -> CodexTrace:
        """Record one event and return the exact trace sent to the recorder.

        ``timestamp`` is injectable for deterministic tests.  It may be a
        monotonic float or a timezone-aware ``datetime``; omitted timestamps
        use the injected monotonic and wall clocks.
        """

        normalized = (
            event if isinstance(event, CodexEvent) else CodexEvent.model_validate(event)
        )
        elapsed_now, observed_at = self._timestamps(timestamp)
        event_name = _event_name(normalized)
        tool_name = _tool_name(normalized.payload)
        stage = _stage(normalized, tool_name)
        phase = _phase(normalized, event_name)
        span_key = _span_key(normalized, stage)
        is_start = phase == "started"
        is_end = phase in {"completed", "error"}

        latency_ms: float | None = None
        if is_start:
            self._starts[span_key] = elapsed_now
        else:
            start = self._starts.get(span_key)
            if start is not None:
                latency_ms = max(0.0, (elapsed_now - start) * 1_000)
            if is_end:
                self._starts.pop(span_key, None)
            if latency_ms is None:
                latency_ms = _payload_latency_ms(normalized.payload)

        input_tokens, output_tokens, total_tokens = _token_usage(normalized.payload)
        is_error = _is_error(normalized)
        trace = CodexTrace(
            observed_at=observed_at,
            event_type=event_name,
            method=_safe_label(normalized.method, fallback="method"),
            stage=stage,
            phase=phase,
            thread_id=_safe_identifier(normalized.thread_id),
            turn_id=_safe_identifier(normalized.turn_id),
            item_id=_safe_identifier(normalized.item_id),
            tool_name=tool_name,
            latency_ms=latency_ms,
            success=_success(normalized, is_error),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            error_code=_error_code(normalized.payload) if is_error else None,
        )
        if self.recorder is not None:
            self.recorder.record(trace)
        return trace

    def record_event(
        self,
        event: CodexEvent,
        *,
        timestamp: float | datetime | None = None,
    ) -> CodexTrace:
        return self.record(event, timestamp=timestamp)

    def observe(
        self,
        event: CodexEvent,
        *,
        timestamp: float | datetime | None = None,
    ) -> CodexTrace:
        return self.record(event, timestamp=timestamp)

    def _timestamps(
        self,
        timestamp: float | datetime | None,
    ) -> tuple[float, datetime]:
        if timestamp is None:
            return self._clock(), _as_utc(self._wall_clock())
        if isinstance(timestamp, datetime):
            normalized = _as_utc(timestamp)
            return normalized.timestamp(), normalized
        value = float(timestamp)
        return value, _as_utc(self._wall_clock())


def trace_from_event(
    event: CodexEvent,
    *,
    latency_ms: float | None = None,
    observed_at: datetime | None = None,
) -> CodexTrace:
    """Create a stateless redacted trace when span timing is supplied externally."""

    telemetry = CodexTelemetry()
    trace = (
        telemetry.record(event, timestamp=observed_at)
        if observed_at
        else telemetry.record(event)
    )
    if latency_ms is not None:
        trace = trace.model_copy(update={"latency_ms": max(0.0, float(latency_ms))})
    return trace


def _event_name(event: CodexEvent) -> str:
    value = (
        event.event_type.value
        if isinstance(event.event_type, CodexEventType)
        else str(event.event_type)
    )
    return _safe_label(value, fallback="unknown")


def _stage(event: CodexEvent, tool_name: str | None) -> str:
    method = event.method.casefold()
    if tool_name or "tool" in method:
        return "tool"
    if "thread" in method or event.event_type in {
        CodexEventType.THREAD_STARTED,
        CodexEventType.THREAD_UPDATED,
    }:
        return "thread"
    if "turn" in method or event.event_type in {
        CodexEventType.TURN_STARTED,
        CodexEventType.TURN_COMPLETED,
    }:
        return "turn"
    if "item" in method or event.event_type in {
        CodexEventType.ITEM_STARTED,
        CodexEventType.ITEM_COMPLETED,
        CodexEventType.TEXT_DELTA,
        CodexEventType.REASONING_DELTA,
    }:
        return "item"
    if event.event_type is CodexEventType.ERROR:
        if event.turn_id:
            return "turn"
        if event.item_id:
            return "item"
        if event.thread_id:
            return "thread"
        return "error"
    return event.event_type.value


def _phase(event: CodexEvent, event_name: str) -> str:
    if _is_error(event):
        return "error"
    if event.event_type in {
        CodexEventType.THREAD_STARTED,
        CodexEventType.TURN_STARTED,
        CodexEventType.ITEM_STARTED,
    }:
        return "started"
    if event.event_type in {
        CodexEventType.TURN_COMPLETED,
        CodexEventType.ITEM_COMPLETED,
    }:
        return "completed"
    if event.event_type in {CodexEventType.TEXT_DELTA, CodexEventType.REASONING_DELTA}:
        return "delta"
    if event.event_type is CodexEventType.THREAD_UPDATED:
        return "updated"
    return event_name


def _span_key(event: CodexEvent, stage: str) -> tuple[str, str, str, str]:
    method = event.method.casefold()
    if stage == "thread" or "thread" in method:
        kind = "thread"
    elif stage == "turn" or "turn" in method:
        kind = "turn"
    else:
        kind = "item"
    return (
        kind,
        event.thread_id or "",
        event.turn_id or "",
        event.item_id or "",
    )


def _success(event: CodexEvent, is_error: bool) -> bool | None:
    if is_error:
        return False
    if event.event_type in {
        CodexEventType.TURN_COMPLETED,
        CodexEventType.ITEM_COMPLETED,
    }:
        return True
    for mapping in _walk_mappings(event.payload):
        for key, value in mapping.items():
            if str(key).casefold() in _SUCCESS_KEYS and isinstance(value, bool):
                return value
    return None


def _is_error(event: CodexEvent) -> bool:
    if event.event_type is CodexEventType.ERROR:
        return True
    method = event.method.casefold()
    if "error" in method or "failed" in method or "failure" in method:
        return True
    for mapping in _walk_mappings(event.payload):
        for key, value in mapping.items():
            normalized_key = str(key).casefold().replace("-", "_")
            if normalized_key in _ERROR_KEYS and value not in (None, False, "", [], {}):
                return True
            if normalized_key in {"status", "state", "outcome"} and isinstance(value, str):
                if value.casefold() in {"error", "failed", "failure", "timeout"}:
                    return True
    return False


def _error_code(payload: Mapping[str, Any]) -> str | None:
    for mapping in _walk_mappings(payload):
        for key, value in mapping.items():
            if str(key).casefold().replace("-", "_") in _ERROR_CODE_KEYS:
                if isinstance(value, str) and value.strip():
                    return _safe_label(value, fallback="error")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return _safe_label(str(value), fallback="error")
    return "error"


def _tool_name(payload: Mapping[str, Any]) -> str | None:
    candidates: list[Any] = []
    for mapping in _walk_mappings(payload):
        for key, value in mapping.items():
            normalized_key = str(key).casefold().replace("-", "_")
            if normalized_key in {"tool", "tool_name", "toolname"}:
                candidates.append(value)
            elif normalized_key in {"item_type", "itemtype"} and _looks_like_tool_type(value):
                candidates.append(value)
            elif normalized_key in {"item", "function", "tool_call", "toolcall"} and isinstance(
                value, Mapping
            ):
                for name in ("tool_name", "tool", "name"):
                    if isinstance(value.get(name), str):
                        candidates.append(value[name])
                if _looks_like_tool_type(value.get("type")):
                    candidates.append(value["type"])
            elif normalized_key == "name" and _looks_like_tool_mapping(mapping):
                candidates.append(value)
            elif normalized_key == "type" and _looks_like_tool_mapping(mapping):
                candidates.append(value)
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return _safe_label(candidate, fallback="tool")
    return None


def _looks_like_tool_mapping(mapping: Mapping[str, Any]) -> bool:
    keys = {str(key).casefold().replace("-", "_") for key in mapping}
    return bool(keys & {"tool", "tool_name", "arguments", "input", "command", "function"})


def _looks_like_tool_type(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.casefold().replace("-", "_")
    return any(token in normalized for token in ("tool", "function", "command", "mcp", "shell"))


def _token_usage(payload: Mapping[str, Any]) -> tuple[int | None, int | None, int | None]:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    for mapping in _walk_mappings(payload):
        for key, value in mapping.items():
            normalized_key = str(key).casefold().replace("-", "_")
            number = _nonnegative_int(value)
            if number is None:
                continue
            if normalized_key in _INPUT_TOKEN_KEYS:
                input_tokens = number
            elif normalized_key in _OUTPUT_TOKEN_KEYS:
                output_tokens = number
            elif normalized_key in _TOTAL_TOKEN_KEYS:
                total_tokens = number
            elif normalized_key in _USAGE_KEYS and isinstance(value, Mapping):
                nested_input, nested_output, nested_total = _token_usage(value)
                input_tokens = nested_input if nested_input is not None else input_tokens
                output_tokens = nested_output if nested_output is not None else output_tokens
                total_tokens = nested_total if nested_total is not None else total_tokens
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None, None, None
    return input_tokens, output_tokens, total_tokens


def _payload_latency_ms(payload: Mapping[str, Any]) -> float | None:
    for mapping in _walk_mappings(payload):
        for key, value in mapping.items():
            if str(key).casefold().replace("-", "_") in {
                "latency_ms",
                "elapsed_ms",
                "duration_ms",
            }:
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    return float(value)
    return None


def _walk_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _walk_mappings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_mappings(nested)


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _safe_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    return _safe_label(value, fallback="id")


def _safe_label(value: Any, *, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    candidate = value.strip()
    if _SENSITIVE_LABEL.search(candidate) or not _SAFE_LABEL.fullmatch(candidate):
        return "[REDACTED]"
    return candidate


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


InMemoryCodexTraceRecorder = InMemoryTraceRecorder
JsonlCodexTraceRecorder = JsonlTraceRecorder


__all__ = [
    "CodexTelemetry",
    "CodexTrace",
    "InMemoryCodexTraceRecorder",
    "InMemoryTraceRecorder",
    "JsonlCodexTraceRecorder",
    "JsonlTraceRecorder",
    "TraceRecorder",
    "trace_from_event",
]
