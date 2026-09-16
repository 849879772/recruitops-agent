"""Bounded, provider-neutral turn streaming guards.

The app server owns model execution, but the local runtime still owns the
contract that a turn must eventually stop.  This module keeps that contract
outside prompts: it watches normalized events, counts real tool-call items,
recognizes cursor-based polling as useful progress, and requests an
interrupt when a bound is reached.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from time import monotonic
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from .events import CodexEvent, CodexEventType, normalize_event


class TurnLimits(BaseModel):
    """Runtime limits for one app-server turn.

    The aliases keep this boundary easy to configure from older callers that
    use ``max_tool_calls`` or ``max_repeated_no_progress``.  Defaults are
    deliberately large enough for a bounded batch operation with incremental
    status polling, while still stopping a stuck turn without model guidance.
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    turn_timeout_seconds: float = Field(
        default=600.0,
        gt=0,
        le=86_400,
        validation_alias=AliasChoices(
            "turn_timeout_seconds",
            "max_duration_seconds",
            "turn_time_limit_seconds",
        ),
    )
    tool_call_budget: int = Field(
        default=64,
        ge=0,
        le=10_000,
        validation_alias=AliasChoices(
            "tool_call_budget",
            "max_tool_calls",
        ),
    )
    repeated_no_progress_limit: int = Field(
        default=4,
        ge=1,
        le=100,
        validation_alias=AliasChoices(
            "repeated_no_progress_limit",
            "max_repeated_no_progress",
            "no_progress_limit",
        ),
    )
    interrupt_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    turn_start_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    @property
    def max_duration_seconds(self) -> float:
        return self.turn_timeout_seconds

    @property
    def max_tool_calls(self) -> int:
        return self.tool_call_budget

    @property
    def max_repeated_no_progress(self) -> int:
        return self.repeated_no_progress_limit


CodexTurnLimits = TurnLimits
TurnBudget = TurnLimits


class TurnInterruptionReason(StrEnum):
    TURN_TIMEOUT = "turn_timeout"
    TOOL_CALL_BUDGET = "tool_call_budget"
    REPEATED_NO_PROGRESS = "repeated_no_progress"


TurnLimitReason = TurnInterruptionReason


@dataclass(frozen=True, slots=True)
class TurnInterruption:
    reason: TurnInterruptionReason
    message: str
    elapsed_seconds: float
    tool_calls: int
    no_progress_events: int
    interrupt_requested: bool = False
    interrupt_error: str | None = None

    @property
    def code(self) -> str:
        return self.reason.value


class TurnLoopStatus(StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TurnLoopResult:
    status: TurnLoopStatus
    elapsed_seconds: float
    tool_calls: int
    no_progress_events: int
    interruption: TurnInterruption | None = None
    error: str | None = None

    @property
    def interrupted(self) -> bool:
        return self.status is TurnLoopStatus.INTERRUPTED


InterruptCallback = Callable[[], Awaitable[Any] | Any]


class TurnStreamLoop:
    """Consume one turn's event stream and enforce runtime bounds."""

    def __init__(
        self,
        events: AsyncIterator[CodexEvent | Mapping[str, Any]],
        *,
        thread_id: str,
        turn_id: str,
        limits: TurnLimits | None = None,
        interrupt: InterruptCallback | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not thread_id.strip():
            raise ValueError("thread_id must not be blank")
        if not turn_id.strip():
            raise ValueError("turn_id must not be blank")
        self.events = events
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.limits = limits or TurnLimits()
        self.interrupt = interrupt
        self._clock = clock
        self._tracker = _TurnProgressTracker(self.limits)

    @property
    def tool_calls(self) -> int:
        return self._tracker.tool_calls

    @property
    def no_progress_events(self) -> int:
        return self._tracker.no_progress_events

    def observe(self, event: CodexEvent | Mapping[str, Any]) -> TurnInterruptionReason | None:
        """Observe one event synchronously, returning a limit reason if reached."""

        normalized = _normalize_stream_event(event)
        if not _event_belongs_to_turn(normalized, self.thread_id, self.turn_id):
            return None
        if _is_turn_terminal(normalized):
            return None
        return self._tracker.observe(normalized)

    async def run(self) -> TurnLoopResult:
        started_at = self._clock()
        while True:
            remaining = self.limits.turn_timeout_seconds - (self._clock() - started_at)
            if remaining <= 0:
                return await self._interrupt_for(
                    TurnInterruptionReason.TURN_TIMEOUT,
                    started_at,
                )
            try:
                raw_event = await asyncio.wait_for(
                    self.events.__anext__(),
                    timeout=remaining,
                )
            except asyncio.CancelledError:
                raise
            except (StopAsyncIteration, EOFError):
                return self._result(TurnLoopStatus.CLOSED, started_at)
            except TimeoutError:
                return await self._interrupt_for(
                    TurnInterruptionReason.TURN_TIMEOUT,
                    started_at,
                )
            except Exception as exc:
                return self._result(
                    TurnLoopStatus.FAILED,
                    started_at,
                    error=type(exc).__name__,
                )

            event = _normalize_stream_event(raw_event)
            if not _event_belongs_to_turn(event, self.thread_id, self.turn_id):
                continue
            if _is_turn_terminal(event):
                return self._result(TurnLoopStatus.COMPLETED, started_at)

            reason = self._tracker.observe(event)
            if reason is not None:
                return await self._interrupt_for(reason, started_at)

    async def _interrupt_for(
        self,
        reason: TurnInterruptionReason,
        started_at: float,
    ) -> TurnLoopResult:
        elapsed = max(0.0, self._clock() - started_at)
        interruption = TurnInterruption(
            reason=reason,
            message=_interruption_message(reason),
            elapsed_seconds=elapsed,
            tool_calls=self._tracker.tool_calls,
            no_progress_events=self._tracker.no_progress_events,
        )
        interrupt_error: str | None = None
        if self.interrupt is not None:
            try:
                await asyncio.wait_for(
                    _await_if_needed(self.interrupt()),
                    timeout=self.limits.interrupt_timeout_seconds,
                )
                interruption = replace(interruption, interrupt_requested=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                interrupt_error = type(exc).__name__
                interruption = replace(interruption, interrupt_error=interrupt_error)
        return TurnLoopResult(
            status=TurnLoopStatus.INTERRUPTED,
            elapsed_seconds=max(0.0, self._clock() - started_at),
            tool_calls=self._tracker.tool_calls,
            no_progress_events=self._tracker.no_progress_events,
            interruption=interruption,
        )

    def _result(
        self,
        status: TurnLoopStatus,
        started_at: float,
        *,
        error: str | None = None,
    ) -> TurnLoopResult:
        return TurnLoopResult(
            status=status,
            elapsed_seconds=max(0.0, self._clock() - started_at),
            tool_calls=self._tracker.tool_calls,
            no_progress_events=self._tracker.no_progress_events,
            error=error,
        )


CodexTurnStreamLoop = TurnStreamLoop


class _TurnProgressTracker:
    def __init__(self, limits: TurnLimits) -> None:
        self.limits = limits
        self.tool_calls = 0
        self.no_progress_events = 0
        self._tool_call_keys: set[str] = set()
        self._seen_item_ids: set[str] = set()
        self._last_cursor: tuple[tuple[str, str], ...] | None = None
        self._last_tool_signature: str | None = None

    def observe(self, event: CodexEvent) -> TurnInterruptionReason | None:
        cursor = cursor_signature(event)
        cursor_changed = cursor is not None and cursor != self._last_cursor
        if cursor is not None:
            self._last_cursor = cursor

        if is_tool_call_event(event):
            if _starts_tool_call(event):
                self._count_tool_call(event)
            # A cursor-bearing status poll is a bounded wait for external work.
            # Treat each poll as progress so a slow batch is governed by the
            # turn/tool bounds rather than the duplicate-event guard.
            cursor_poll = _is_cursor_poll(event)
            signature = _tool_signature(event)
            meaningful = cursor_changed or cursor_poll
            if not meaningful and signature != self._last_tool_signature:
                meaningful = True
            self._last_tool_signature = signature
            self._record_progress() if meaningful else self._record_no_progress()
        elif event.event_type in {
            CodexEventType.TEXT_DELTA,
            CodexEventType.REASONING_DELTA,
        }:
            self._record_progress() if event.text else self._record_no_progress()
        elif event.event_type is CodexEventType.ITEM_STARTED:
            item_id = event.item_id
            if item_id and item_id not in self._seen_item_ids:
                self._seen_item_ids.add(item_id)
                self._record_progress()
            else:
                # An item start without an id is still a real lifecycle step;
                # malformed/replayed events are bounded by the other guards.
                self._record_progress()
        elif event.event_type is CodexEventType.ITEM_COMPLETED:
            if (
                cursor_changed
                or _has_success_signal(event.payload)
                or not _has_failure_signal(event.payload)
            ):
                self._record_progress()
            else:
                self._record_no_progress()
        elif event.event_type in {
            CodexEventType.THREAD_STARTED,
            CodexEventType.THREAD_UPDATED,
            CodexEventType.TURN_STARTED,
        }:
            self._record_progress()
        elif event.event_type is CodexEventType.ERROR:
            self._record_no_progress()
        elif _has_progress_signal(event):
            self._record_progress()
        else:
            self._record_no_progress()

        if self.tool_calls > self.limits.tool_call_budget:
            return TurnInterruptionReason.TOOL_CALL_BUDGET
        if self.no_progress_events >= self.limits.repeated_no_progress_limit:
            return TurnInterruptionReason.REPEATED_NO_PROGRESS
        return None

    def _count_tool_call(self, event: CodexEvent) -> None:
        if event.item_id:
            key = f"item:{event.item_id}"
        else:
            key = f"call:{self.tool_calls + len(self._tool_call_keys) + 1}"
        if key in self._tool_call_keys:
            return
        self._tool_call_keys.add(key)
        self.tool_calls += 1

    def _record_progress(self) -> None:
        self.no_progress_events = 0

    def _record_no_progress(self) -> None:
        self.no_progress_events += 1


async def _await_if_needed(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _normalize_stream_event(raw_event: CodexEvent | Mapping[str, Any]) -> CodexEvent:
    if isinstance(raw_event, CodexEvent):
        return raw_event
    if isinstance(raw_event, Mapping) and {"event_type", "method"}.issubset(raw_event):
        return CodexEvent.model_validate(raw_event)
    return normalize_event(raw_event)


def _event_belongs_to_turn(event: CodexEvent, thread_id: str, turn_id: str) -> bool:
    if event.thread_id and event.thread_id != thread_id:
        return False
    return not event.turn_id or event.turn_id == turn_id


def _is_turn_terminal(event: CodexEvent) -> bool:
    return event.event_type is CodexEventType.TURN_COMPLETED


def is_tool_call_event(event: CodexEvent) -> bool:
    """Return whether an event describes an MCP/function/command tool item."""

    method = event.method.casefold().replace("-", "/").replace(".", "/")
    if any(token in method for token in ("mcp", "tool", "functioncall", "function_call")):
        return True
    if any(token in method for token in ("commandexecution", "shell")):
        return True
    for mapping in _walk_mappings(event.payload):
        for key, value in mapping.items():
            normalized_key = _normalize_key(key)
            if normalized_key in {
                "tool",
                "tool_name",
                "toolname",
                "tool_call",
                "toolcall",
                "function_call",
                "functioncall",
            }:
                return True
            if normalized_key in {"type", "item_type", "itemtype"} and _looks_like_tool_type(value):
                return True
    return False


def cursor_signature(event: CodexEvent) -> tuple[tuple[str, str], ...] | None:
    """Extract stable cursor/sequence values used by incremental poll tools."""

    wanted = {
        "after_sequence",
        "aftersequence",
        "next_sequence",
        "nextsequence",
        "next_cursor",
        "nextcursor",
        "cursor",
        "sequence",
        "event_sequence",
        "eventsequence",
        "last_event_sequence",
        "lasteventsequence",
    }
    values: list[tuple[str, str]] = []
    for mapping in _walk_mappings(event.payload):
        for key, value in mapping.items():
            normalized_key = _normalize_key(key)
            if normalized_key not in wanted or isinstance(value, (Mapping, list, tuple, dict)):
                continue
            if isinstance(value, (str, int, float, bool)):
                values.append((normalized_key, str(value)))
    if not values:
        return None
    return tuple(sorted(set(values)))


def _is_cursor_poll(event: CodexEvent) -> bool:
    cursor = cursor_signature(event)
    if cursor is None:
        return False
    method = event.method.casefold()
    tool = _tool_name(event).casefold()
    return any(token in f"{method} {tool}" for token in ("status", "poll", "operation"))


def _starts_tool_call(event: CodexEvent) -> bool:
    if event.event_type is CodexEventType.ITEM_COMPLETED:
        return False
    method = event.method.casefold().replace("-", "/").replace(".", "/")
    if any(token in method for token in ("completed", "complete", "result", "output")):
        return False
    return True


def _tool_signature(event: CodexEvent) -> str:
    value = {
        "tool": _tool_name(event),
        "cursor": cursor_signature(event),
        "payload": _digest(_stable_value(event.payload)),
    }
    return _digest(value)


def _tool_name(event: CodexEvent) -> str:
    for mapping in _walk_mappings(event.payload):
        for key, value in mapping.items():
            if _normalize_key(key) in {"tool", "tool_name", "toolname", "name"}:
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return event.method


def _has_success_signal(payload: Mapping[str, Any]) -> bool:
    success_values = {"success", "succeeded", "ok", "completed", "complete", "done"}
    for mapping in _walk_mappings(payload):
        for key, value in mapping.items():
            normalized_key = _normalize_key(key)
            if normalized_key in {"success", "ok"} and value is True:
                return True
            if normalized_key in {"status", "state", "outcome"}:
                if isinstance(value, str) and value.casefold() in success_values:
                    return True
    return False


def _has_failure_signal(payload: Mapping[str, Any]) -> bool:
    failure_values = {"error", "failed", "failure", "timeout", "cancelled", "canceled"}
    for mapping in _walk_mappings(payload):
        for key, value in mapping.items():
            normalized_key = _normalize_key(key)
            if normalized_key in {"error", "failure", "failed"} and value not in (
                None,
                "",
                False,
                [],
                {},
            ):
                return True
            if normalized_key in {"status", "state", "outcome"}:
                if isinstance(value, str) and value.casefold() in failure_values:
                    return True
    return False


def _has_progress_signal(event: CodexEvent) -> bool:
    method = event.method.casefold()
    if "progress" in method:
        return True
    for mapping in _walk_mappings(event.payload):
        for key, value in mapping.items():
            if _normalize_key(key) == "progress" and value not in (None, "", False, [], {}):
                return True
    return False


def _looks_like_tool_type(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.casefold().replace("-", "_")
    return any(token in normalized for token in ("tool", "function", "command", "mcp", "shell"))


def _walk_mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for nested in value.values():
            yield from _walk_mappings(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            yield from _walk_mappings(nested)


def _normalize_key(value: Any) -> str:
    return str(value).casefold().replace("-", "_").replace(".", "_")


def _stable_value(value: Any, *, depth: int = 0) -> Any:
    """Build a bounded, non-logging fingerprint without retaining raw text."""

    if depth > 4:
        return "<depth>"
    if isinstance(value, Mapping):
        return {
            str(key): _stable_value(nested, depth=depth + 1)
            for key, nested in sorted(value.items(), key=lambda item: str(item[0]))
            if _normalize_key(key)
            not in {
                "timestamp",
                "created_at",
                "updated_at",
                "event_id",
                "item_id",
                "turn_id",
                "thread_id",
                "id",
            }
        }
    if isinstance(value, (list, tuple)):
        return [_stable_value(item, depth=depth + 1) for item in value[:32]]
    if isinstance(value, str):
        return {"sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(), "length": len(value)}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return type(value).__name__


def _digest(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _interruption_message(reason: TurnInterruptionReason) -> str:
    return {
        TurnInterruptionReason.TURN_TIMEOUT: (
            "Codex turn interrupted after reaching the runtime time limit."
        ),
        TurnInterruptionReason.TOOL_CALL_BUDGET: (
            "Codex turn interrupted after reaching the tool-call budget."
        ),
        TurnInterruptionReason.REPEATED_NO_PROGRESS: (
            "Codex turn interrupted after repeated events without progress."
        ),
    }[reason]


__all__ = [
    "CodexTurnLimits",
    "CodexTurnStreamLoop",
    "TurnBudget",
    "TurnInterruption",
    "TurnInterruptionReason",
    "TurnLimitReason",
    "TurnLimits",
    "TurnLoopResult",
    "TurnLoopStatus",
    "TurnStreamLoop",
    "cursor_signature",
    "is_tool_call_event",
]
