from __future__ import annotations

import asyncio
import tomllib
from types import SimpleNamespace

import pytest

from packages.automation.startup import AutomationStartupError, start_automation_thread
from packages.codex_runtime.client import JsonRpcRemoteError
from packages.codex_runtime.config import CodexHomeConfig


class StartupService:
    def __init__(self, results):
        self.results = list(results)
        self.start_calls = 0
        self.turn_calls = 0

    async def thread_start(self):
        self.start_calls += 1
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def turn_start(self, *args, **kwargs):
        self.turn_calls += 1
        raise AssertionError("startup must not execute a business turn")


def handshake_error(message="required MCP recruitops timed out handshaking after 20s"):
    return JsonRpcRemoteError(code=-32603, message=f"error creating thread: {message}")


def test_startup_retries_handshake_then_starts_only_one_thread():
    async def scenario():
        thread = SimpleNamespace(id="scheduled-thread")
        service = StartupService([handshake_error(), thread])
        sleeps, events = [], []

        async def sleeper(delay):
            sleeps.append(delay)

        async def on_retry(**event):
            events.append(event)

        assert await start_automation_thread(service, sleeper=sleeper, on_retry=on_retry) is thread
        assert service.start_calls == 2
        assert service.turn_calls == 0
        assert sleeps == [2.0]
        assert events[0]["attempt"] == 1
        assert events[0]["max_attempts"] == 3
        assert "handshaking" in events[0]["error"]

    asyncio.run(scenario())


def test_persistent_failure_stops_after_three_attempts_and_redacts_diagnostics():
    async def scenario():
        secret = "sk-" + "a" * 30
        service = StartupService([handshake_error(f"connection refused; api_key={secret}")] * 3)
        sleeps, events = [], []

        async def sleeper(delay):
            sleeps.append(delay)

        with pytest.raises(AutomationStartupError) as caught:
            await start_automation_thread(
                service, sleeper=sleeper, on_retry=lambda **event: events.append(event)
            )
        assert caught.value.attempts == 3
        assert caught.value.error_code == "automation_startup_unavailable"
        assert "已尝试 3 次" in str(caught.value)
        assert secret not in str(caught.value)
        assert all(secret not in event["error"] for event in events)
        assert "REDACTED" in caught.value.detail
        assert sleeps == [2.0, 5.0]
        assert [event["attempt"] for event in events] == [1, 2]
        assert service.start_calls == 3 and service.turn_calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [
    JsonRpcRemoteError(code=401, message="service unavailable"),
    JsonRpcRemoteError(code=403, message="timed out handshaking"),
    RuntimeError("invalid API key; connection timeout"),
    RuntimeError("unknown provider; connection refused"),
    ValueError("invalid configuration"),
    RuntimeError("response entity is missing thread"),
])
def test_nontransient_startup_error_is_not_retried(error):
    async def scenario():
        service = StartupService([error])

        async def no_sleep(_delay):
            raise AssertionError("permanent failures must not sleep")

        with pytest.raises(type(error)) as caught:
            await start_automation_thread(service, sleeper=no_sleep)
        assert caught.value is error
        assert service.start_calls == 1 and service.turn_calls == 0

    asyncio.run(scenario())


def test_startup_cancellation_propagates_immediately():
    async def scenario():
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        class WaitingService:
            async def thread_start(self):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        task = asyncio.create_task(start_automation_thread(WaitingService()))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()

    asyncio.run(scenario())


def test_hanging_startup_is_bounded_before_any_turn():
    async def scenario():
        class WaitingService:
            calls = 0
            cancelled = 0

            async def thread_start(self):
                self.calls += 1
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled += 1

        service, delays = WaitingService(), []

        async def sleeper(delay):
            delays.append(delay)

        with pytest.raises(AutomationStartupError) as caught:
            await start_automation_thread(
                service, sleeper=sleeper, startup_timeout_seconds=0.001
            )
        assert caught.value.attempts == 3
        assert "连接超时" in str(caught.value)
        assert service.calls == service.cancelled == 3
        assert delays == [2.0, 5.0]

    asyncio.run(scenario())


def test_mcp_cold_start_has_sixty_seconds_and_stays_required():
    config = CodexHomeConfig(
        model="deepseek-v4-pro", provider_id="deepseek", base_url="https://api.deepseek.com",
        api_key_env="RECRUITOPS_LLM_API_KEY", mcp_command="python",
    )
    parsed = tomllib.loads(config.render_toml())
    assert parsed["mcp_servers"]["recruitops"]["startup_timeout_sec"] == 60
    assert parsed["mcp_servers"]["recruitops"]["required"] is True
    assert parsed["mcp_servers"]["recruitops"]["default_tools_approval_mode"] == "approve"
    assert parsed["sandbox_mode"] == "read-only"
