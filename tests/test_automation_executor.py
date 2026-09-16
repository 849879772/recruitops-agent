from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from dataclasses import replace

from apps.api.automation import CodexAutomationExecutor
from packages.automation import ClaimedAutomation
from packages.codex_runtime.events import CodexEvent, CodexEventType


class _Subscription:
    def __init__(self, events: list[CodexEvent]) -> None:
        self.queue: asyncio.Queue[CodexEvent] = asyncio.Queue()
        for event in events:
            self.queue.put_nowait(event)
        self.closed = False

    async def get(self) -> CodexEvent:
        return await self.queue.get()

    def close(self) -> None:
        self.closed = True


class _Service:
    def __init__(self, events: list[CodexEvent]) -> None:
        self.subscription = _Subscription(events)
        self.interrupted: list[tuple[str, str]] = []

    async def thread_start(self):
        return SimpleNamespace(id="thread-1")

    def subscribe(self, thread_id: str):
        assert thread_id == "thread-1"
        return self.subscription

    async def turn_start(self, thread_id: str, prompt: str):
        assert thread_id == "thread-1"
        assert prompt
        return SimpleNamespace(id="turn-1")

    async def turn_interrupt(self, thread_id: str, turn_id: str):
        self.interrupted.append((thread_id, turn_id))


class _Store:
    def __init__(self) -> None:
        self.context: tuple[str, str, str] | None = None

    def mark_running_context(self, execution_id: str, *, thread_id: str, turn_id: str):
        self.context = (execution_id, thread_id, turn_id)


def _task() -> ClaimedAutomation:
    return ClaimedAutomation(
        execution_id="execution-1",
        schedule_id="schedule-1",
        task_id="application_progress",
        task_label="投递复核",
        target_kind="application",
        target_id="24",
        target_label="新华三 / 软件开发工程师",
        scheduled_for=datetime(2026, 9, 7, tzinfo=timezone.utc),
    )


def test_daily_automation_preserves_bounded_company_scope():
    task = replace(_task(), task_id="daily_recruitment_intelligence", target_kind="company", target_id="config-12")
    prompt = CodexAutomationExecutor._prompt(task)
    assert 'company_ids=["config-12"]' in prompt
    assert "最终结果" in prompt
    assert "daily_recruitment_sync" in prompt and "operation_run" not in prompt


def test_unfinished_or_missing_tool_is_not_success():
    assert CodexAutomationExecutor._terminal_status("任务未完成，没有名为 operation_run 的工具") == "failed"
    assert CodexAutomationExecutor._terminal_status("") == "failed"


def test_zero_blocked_counts_do_not_override_success():
    assert CodexAutomationExecutor._terminal_status("复核完成。状态未变化 1、需要登录或验证 0、无法确认 0、执行失败 0。") == "succeeded"
    assert CodexAutomationExecutor._terminal_status("需要登录或验证 1、无法确认 0。") == "blocked"


def _event(event_type: CodexEventType, *, text: str | None = None) -> CodexEvent:
    return CodexEvent(
        event_type=event_type,
        method=event_type.value,
        thread_id="thread-1",
        turn_id="turn-1",
        text=text,
    )


def test_reconnect_notice_is_not_terminal_when_turn_recovers() -> None:
    service = _Service(
        [
            _event(CodexEventType.ERROR, text="Reconnecting... waiting for network"),
            _event(CodexEventType.TEXT_DELTA, text="状态未变化"),
            _event(CodexEventType.TURN_COMPLETED),
        ]
    )
    store = _Store()

    result = asyncio.run(CodexAutomationExecutor(service, store)(_task()))

    assert result.status == "succeeded"
    assert result.summary == "状态未变化"
    assert result.error is None
    assert service.interrupted == []
    assert service.subscription.closed is True


def test_reconnect_timeout_returns_stable_error_and_interrupts_turn() -> None:
    service = _Service(
        [_event(CodexEventType.ERROR, text="Reconnecting... waiting for network")]
    )
    executor = CodexAutomationExecutor(
        service,
        _Store(),
        reconnect_grace_seconds=0.01,
    )

    result = asyncio.run(executor(_task()))

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.startswith("CODEX_NETWORK_UNAVAILABLE:")
    assert "Reconnecting... waiting for network" in result.error
    assert service.interrupted == [("thread-1", "turn-1")]


def test_non_retryable_error_remains_immediately_terminal() -> None:
    service = _Service(
        [_event(CodexEventType.ERROR, text="401 Unauthorized: invalid API key")]
    )

    result = asyncio.run(CodexAutomationExecutor(service, _Store())(_task()))

    assert result.status == "failed"
    assert result.error == "401 Unauthorized: invalid API key"
    assert service.interrupted == []
