from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from packages.codex_runtime import (
    CodexEventType,
    TurnInterruptionReason,
    TurnLimits,
    TurnLoopStatus,
    TurnStreamLoop,
)


async def _events(values: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for value in values:
        yield value


def _error_event() -> dict[str, Any]:
    return {
        "method": "error",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "error": {"code": "provider_timeout"},
        },
    }


def _tool_event(item_id: str, *, after_sequence: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "threadId": "thread-1",
        "turnId": "turn-1",
        "item": {
            "id": item_id,
            "type": "mcp_tool_call",
            "name": "browser_operation_status",
            "arguments": {"operation_id": "op-1"},
        },
    }
    if after_sequence is not None:
        payload.update(
            {
                "after_sequence": after_sequence,
                "next_sequence": after_sequence,
            }
        )
    return {"method": "item/started", "params": payload}


def _completed_event() -> dict[str, Any]:
    return {
        "method": "turn/completed",
        "params": {"threadId": "thread-1", "turnId": "turn-1"},
    }


def test_turn_loop_interrupts_after_repeated_runtime_failures() -> None:
    async def scenario() -> None:
        interrupts: list[str] = []

        async def interrupt() -> None:
            interrupts.append("turn-1")

        loop = TurnStreamLoop(
            _events([_error_event(), _error_event(), _error_event()]),
            thread_id="thread-1",
            turn_id="turn-1",
            limits=TurnLimits(
                turn_timeout_seconds=10,
                repeated_no_progress_limit=3,
            ),
            interrupt=interrupt,
        )

        result = await loop.run()

        assert result.status is TurnLoopStatus.INTERRUPTED
        assert result.interruption is not None
        assert result.interruption.reason is TurnInterruptionReason.REPEATED_NO_PROGRESS
        assert result.interruption.interrupt_requested is True
        assert result.no_progress_events == 3
        assert interrupts == ["turn-1"]

    asyncio.run(scenario())


def test_turn_loop_interrupts_when_tool_call_budget_is_exceeded() -> None:
    async def scenario() -> None:
        interrupts = 0

        async def interrupt() -> None:
            nonlocal interrupts
            interrupts += 1

        loop = TurnStreamLoop(
            _events([_tool_event("item-1"), _tool_event("item-2"), _tool_event("item-3")]),
            thread_id="thread-1",
            turn_id="turn-1",
            limits=TurnLimits(
                turn_timeout_seconds=10,
                tool_call_budget=2,
                repeated_no_progress_limit=10,
            ),
            interrupt=interrupt,
        )

        result = await loop.run()

        assert result.status is TurnLoopStatus.INTERRUPTED
        assert result.interruption is not None
        assert result.interruption.reason is TurnInterruptionReason.TOOL_CALL_BUDGET
        assert result.tool_calls == 3
        assert interrupts == 1

    asyncio.run(scenario())


def test_cursor_advanced_polling_counts_as_progress_and_can_complete() -> None:
    async def scenario() -> None:
        loop = TurnStreamLoop(
            _events(
                [
                    _tool_event("item-1", after_sequence=0),
                    _tool_event("item-2", after_sequence=1),
                    _tool_event("item-3", after_sequence=2),
                    _completed_event(),
                ]
            ),
            thread_id="thread-1",
            turn_id="turn-1",
            limits=TurnLimits(
                turn_timeout_seconds=10,
                tool_call_budget=8,
                repeated_no_progress_limit=2,
            ),
        )

        result = await loop.run()

        assert result.status is TurnLoopStatus.COMPLETED
        assert result.interruption is None
        assert result.tool_calls == 3
        assert loop.no_progress_events == 0

    asyncio.run(scenario())


def test_turn_loop_interrupts_an_idle_stream_by_time_limit() -> None:
    async def scenario() -> None:
        gate = asyncio.Event()
        interrupts = 0

        async def idle_events() -> AsyncIterator[dict[str, Any]]:
            await gate.wait()
            yield _completed_event()

        async def interrupt() -> None:
            nonlocal interrupts
            interrupts += 1

        loop = TurnStreamLoop(
            idle_events(),
            thread_id="thread-1",
            turn_id="turn-1",
            limits=TurnLimits(
                turn_timeout_seconds=0.01,
                interrupt_timeout_seconds=0.1,
            ),
            interrupt=interrupt,
        )

        result = await loop.run()

        assert result.status is TurnLoopStatus.INTERRUPTED
        assert result.interruption is not None
        assert result.interruption.reason is TurnInterruptionReason.TURN_TIMEOUT
        assert interrupts == 1

    asyncio.run(scenario())


def test_turn_limits_accept_legacy_budget_aliases_and_keep_defaults_sane() -> None:
    limits = TurnLimits(
        max_duration_seconds=120,
        max_tool_calls=12,
        max_repeated_no_progress=5,
    )

    assert limits.turn_timeout_seconds == 120
    assert limits.tool_call_budget == 12
    assert limits.repeated_no_progress_limit == 5
    assert limits.max_duration_seconds == limits.turn_timeout_seconds
    assert limits.max_tool_calls == limits.tool_call_budget
    assert limits.max_repeated_no_progress == limits.repeated_no_progress_limit
    assert TurnLimits().turn_timeout_seconds >= 300
    assert TurnLimits().tool_call_budget >= 32
    assert CodexEventType.TURN_COMPLETED.value == "turn_completed"
