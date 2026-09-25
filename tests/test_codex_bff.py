from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from apps.api import codex_bff
from apps.api.codex_bff import CodexBffService, build_codex_bff_service
from packages import config as config_module
from packages.codex_runtime import (
    CodexEventType,
    RuntimeHealth,
    TurnInterruptionReason,
    TurnLimits,
    normalize_event,
)
from packages.codex_runtime.telemetry import CodexTelemetry, InMemoryTraceRecorder


class FakeSupervisor:
    def __init__(self) -> None:
        self.events: asyncio.Queue[Any] = asyncio.Queue()
        self.start_calls = 0
        self.stop_calls = 0
        self.health_calls = 0

    async def start(self) -> str:
        self.start_calls += 1
        return "started"

    async def stop(self) -> str:
        self.stop_calls += 1
        return "stopped"

    async def health(self) -> SimpleNamespace:
        self.health_calls += 1
        return SimpleNamespace(
            healthy=True,
            state="running",
            error=None,
        )

    async def next_event(self) -> Any:
        return await self.events.get()

    async def emit(self, method: str, params: dict[str, Any]) -> None:
        await self.events.put(normalize_event(method, params))


class FakeThreads:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def start(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("thread_start", params))
        return {"id": "thread-1"}

    async def resume(self, thread_id: str, **params: Any) -> dict[str, Any]:
        self.calls.append(("thread_resume", thread_id, params))
        return {"id": thread_id}

    async def start_turn(self, thread_id: str, text: str, **params: Any) -> dict[str, Any]:
        self.calls.append(("turn_start", thread_id, text, params))
        return {"id": "turn-1"}

    async def interrupt(self, thread_id: str, turn_id: str) -> None:
        self.calls.append(("turn_interrupt", thread_id, turn_id))

    async def delete(self, thread_id: str) -> None:
        self.calls.append(("thread_delete", thread_id))


def test_service_delegates_lifecycle_health_and_thread_turn_calls() -> None:
    async def scenario() -> None:
        supervisor = FakeSupervisor()
        threads = FakeThreads()
        service = CodexBffService(supervisor, threads)

        assert await service.start() == "started"
        assert await service.start() == "started"
        assert supervisor.start_calls == 1
        health = await service.health()
        assert isinstance(health, RuntimeHealth)
        assert health.ready is True

        assert await service.thread_start(cwd="D:/work") == {"id": "thread-1"}
        assert await service.thread_resume("thread-1", model="v4") == {"id": "thread-1"}
        assert await service.turn_start("thread-1", "hello", effort="high") == {"id": "turn-1"}
        assert await service.turn_interrupt("thread-1", "turn-1") is None
        assert await service.thread_delete("thread-1") is None
        assert threads.calls == [
            ("thread_start", {"cwd": "D:/work"}),
            ("thread_resume", "thread-1", {"model": "v4"}),
            ("turn_start", "thread-1", "hello", {"effort": "high"}),
            ("turn_interrupt", "thread-1", "turn-1"),
            ("thread_delete", "thread-1"),
        ]

        await service.stop()
        assert supervisor.stop_calls == 1

    asyncio.run(scenario())


def test_subscribers_receive_the_same_real_delta_and_only_matching_thread() -> None:
    async def scenario() -> None:
        supervisor = FakeSupervisor()
        service = CodexBffService(supervisor, FakeThreads())
        first = service.subscribe("thread-1")
        second = service.subscribe("thread-1")
        other = service.subscribe("thread-2")
        await service.start()

        await supervisor.emit(
            "item/agentMessage/delta",
            {"threadId": "thread-2", "delta": "ignored"},
        )
        await supervisor.emit(
            "item/agentMessage/delta",
            {"threadId": "thread-1", "delta": "raw"},
        )

        first_event = await asyncio.wait_for(first.get(), timeout=1)
        second_event = await asyncio.wait_for(second.__anext__(), timeout=1)
        other_event = await asyncio.wait_for(other.get(), timeout=1)
        assert first_event is second_event
        assert first_event.event_type is CodexEventType.TEXT_DELTA
        assert first_event.text == "raw"
        assert first_event.payload["delta"] == "raw"
        assert other_event.thread_id == "thread-2"
        assert other_event.payload["delta"] == "ignored"

        first.close()
        second.close()
        other.close()
        await service.stop()

    asyncio.run(scenario())


def test_published_events_are_recorded_without_message_text() -> None:
    async def scenario() -> None:
        supervisor = FakeSupervisor()
        recorder = InMemoryTraceRecorder()
        service = CodexBffService(
            supervisor,
            FakeThreads(),
            telemetry=CodexTelemetry(recorder),
        )
        subscription = service.subscribe("thread-1")
        await service.start()

        await supervisor.emit(
            "item/agentMessage/delta",
            {"threadId": "thread-1", "delta": "sensitive answer text"},
        )
        await asyncio.wait_for(subscription.get(), timeout=1)

        assert len(recorder.events) == 1
        serialized = recorder.events[0].model_dump_json()
        assert "sensitive answer text" not in serialized
        assert recorder.events[0].thread_id == "thread-1"

        subscription.close()
        await service.stop()

    asyncio.run(scenario())


def test_subscription_cancellation_and_service_stop_unblock_consumers() -> None:
    async def scenario() -> None:
        supervisor = FakeSupervisor()
        service = CodexBffService(supervisor, FakeThreads())
        cancelled = service.subscribe("thread-1")
        await service.start()

        pending = asyncio.create_task(cancelled.get())
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert cancelled.closed is True

        closed = service.subscribe("thread-1")
        await service.stop()
        assert closed.closed is True
        with pytest.raises(StopAsyncIteration):
            await closed.__anext__()
        await service.stop()
        assert supervisor.stop_calls == 1

    asyncio.run(scenario())


def test_bff_guard_interrupts_repeated_failures_and_closes_the_turn_stream() -> None:
    async def scenario() -> None:
        supervisor = FakeSupervisor()
        threads = FakeThreads()
        service = CodexBffService(
            supervisor,
            threads,
            turn_limits=TurnLimits(
                turn_timeout_seconds=10,
                tool_call_budget=8,
                repeated_no_progress_limit=2,
                interrupt_timeout_seconds=0.1,
            ),
        )
        subscription = service.subscribe("thread-1")
        await service.start()
        await service.turn_start("thread-1", "retry the failed read")

        for _ in range(2):
            await supervisor.emit(
                "error",
                {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "error": {"code": "provider_timeout"},
                },
            )

        events = []
        while True:
            try:
                events.append(await asyncio.wait_for(subscription.get(), timeout=1))
            except StopAsyncIteration:
                break

        limit_event = next(event for event in events if event.method == "runtime/turn_interrupted")
        assert limit_event.payload["reason"] == TurnInterruptionReason.REPEATED_NO_PROGRESS.value
        assert any(call == ("turn_interrupt", "thread-1", "turn-1") for call in threads.calls)
        await service.stop()

    asyncio.run(scenario())


def test_fastapi_dependency_accepts_fake_runtime_without_starting_it() -> None:
    supervisor = FakeSupervisor()
    threads = FakeThreads()
    service = build_codex_bff_service(supervisor, threads)

    assert service.supervisor is supervisor
    assert service.threads is threads
    assert supervisor.start_calls == 0


def test_turn_limits_from_settings_accepts_optional_runtime_overrides() -> None:
    limits = codex_bff.turn_limits_from_settings(
        SimpleNamespace(
            codex_turn_timeout_seconds=90,
            codex_tool_call_budget=11,
            codex_repeated_no_progress_limit=2,
            codex_interrupt_timeout_seconds=0.25,
            codex_turn_start_timeout_seconds=4,
        )
    )

    assert limits.turn_timeout_seconds == 90
    assert limits.tool_call_budget == 11
    assert limits.repeated_no_progress_limit == 2
    assert limits.interrupt_timeout_seconds == 0.25
    assert limits.turn_start_timeout_seconds == 4


def test_default_supervisor_passes_dotenv_provider_key_only_to_child_environment(
    monkeypatch,
    tmp_path,
) -> None:
    settings = SimpleNamespace(
        codex_home=tmp_path / "codex-home",
        agent_root=tmp_path,
        codex_model="deepseek-v4-pro",
        codex_model_provider_id="deepseek",
        codex_model_base_url="https://api.deepseek.com",
        codex_model_api_key_env="RECRUITOPS_LLM_API_KEY",
        codex_reasoning_effort="high",
        codex_command=("codex", "app-server"),
        codex_startup_timeout_seconds=15.0,
        llm_api_key="dotenv-secret",
        llm_enabled=True,
        job_analysis_enabled=True,
        llm_model="deepseek-v4-pro",
        llm_endpoint="https://api.deepseek.com/anthropic/v1/messages",
        llm_timeout_seconds=25,
        match_max_concurrency=1,
        crawl_max_concurrency=3,
        detail_max_concurrency=5,
        browser_max_concurrency=2,
    )
    monkeypatch.delenv("RECRUITOPS_LLM_API_KEY", raising=False)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    codex_bff.get_codex_supervisor.cache_clear()
    try:
        supervisor = codex_bff.get_codex_supervisor()
        assert supervisor.config.environment["RECRUITOPS_LLM_API_KEY"] == "dotenv-secret"
        rendered = (tmp_path / "codex-home" / "config.toml").read_text(encoding="utf-8")
        assert "dotenv-secret" not in rendered
        import tomllib
        forwarded = tomllib.loads(rendered)["mcp_servers"]["recruitops"]["env_vars"]
        for name in ("ENABLED", "API_KEY", "MODEL", "ENDPOINT", "TIMEOUT_SECONDS"):
            assert f"RECRUITOPS_LLM_{name}" in forwarded
        assert "RECRUITOPS_JOB_ANALYSIS_ENABLED" in forwarded
        assert "RECRUITOPS_MATCH_MAX_CONCURRENCY" in forwarded
        assert supervisor.config.environment["RECRUITOPS_LLM_ENABLED"] == "true"
        assert supervisor.config.environment["RECRUITOPS_JOB_ANALYSIS_ENABLED"] == "true"
        assert supervisor.config.environment["RECRUITOPS_LLM_MODEL"] == "deepseek-v4-pro"
        assert supervisor.config.environment["RECRUITOPS_LLM_TIMEOUT_SECONDS"] == "25"
        assert supervisor.config.environment["RECRUITOPS_MATCH_MAX_CONCURRENCY"] == "1"
        for key, value in (("CRAWL", "3"), ("DETAIL", "5"), ("BROWSER", "2")):
            name = f"RECRUITOPS_{key}_MAX_CONCURRENCY"
            assert name in forwarded
            assert supervisor.config.environment[name] == value
    finally:
        codex_bff.get_codex_supervisor.cache_clear()


def test_desktop_supervisor_separates_installed_code_from_instance(monkeypatch, tmp_path):
    import tomllib

    code_root = tmp_path / "installed app"
    instance = tmp_path / "instance"
    settings = config_module.Settings(
        _env_file=None, env="desktop-isolated", agent_root=instance,
        codex_home=instance / "codex", database_url="sqlite:///:memory:",
        write_enabled=False, llm_api_key="",
    )
    monkeypatch.setattr(codex_bff, "__file__", str(code_root / "apps/api/codex_bff.py"))
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    monkeypatch.delenv("RECRUITOPS_DESKTOP_WRITE_OPTIN", raising=False)
    inherited = {
        "RECRUITOPS_DESKTOP_LAUNCH_MODE": "packaged",
        "RECRUITOPS_DESKTOP_CAPABILITIES": '{"llm_enabled":false}',
        "RECRUITOPS_DESKTOP_INSTANCE_ID": "a" * 32,
        "RECRUITOPS_DESKTOP_RUN_ID": "b" * 32,
        "PLAYWRIGHT_BROWSERS_PATH": str(code_root / "chromium"),
        "PYTHONPATH": str(code_root),
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    codex_bff.get_codex_supervisor.cache_clear()
    try:
        supervisor = codex_bff.get_codex_supervisor()
        rendered = (instance / "codex/config.toml").read_text(encoding="utf-8")
        mcp = tomllib.loads(rendered)["mcp_servers"]["recruitops"]
        assert mcp["args"] == [str(code_root / "scripts/run_mcp_server.py")]
        assert supervisor.config.working_dir == instance
        assert supervisor.config.skill_roots == (code_root / ".agents/skills",)
        assert "RECRUITOPS_DESKTOP_WRITE_OPTIN" not in supervisor.config.environment
        for name in (
            "RECRUITOPS_DESKTOP_LAUNCH_MODE", "RECRUITOPS_DESKTOP_CAPABILITIES",
            "RECRUITOPS_DESKTOP_INSTANCE_ID", "RECRUITOPS_DESKTOP_RUN_ID",
            "RECRUITOPS_DESKTOP_WRITE_OPTIN", "RECRUITOPS_WRITE_ENABLED",
            "RECRUITOPS_AGENT_ROOT", "RECRUITOPS_DATABASE_URL",
            "PLAYWRIGHT_BROWSERS_PATH", "PYTHONPATH", "PYTHONNOUSERSITE",
            "PYTHONDONTWRITEBYTECODE", "PYTHONUTF8", "HOME", "USERPROFILE",
            "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "NO_PROXY", "no_proxy",
            "RECRUITOPS_CODEX_RUNTIME_ENABLED", "RECRUITOPS_AUTOMATION_ENABLED",
            "RECRUITOPS_MAIL_SYNC_ON_STARTUP", "RECRUITOPS_VISION_ENABLED",
        ):
            assert name in mcp["env_vars"]
        assert "RECRUITOPS_API_TOKEN" not in mcp["env_vars"]
        from packages.codex_runtime.client import create_process
        captured = {}

        async def capture_process(*args, **kwargs):
            captured.update(kwargs)
            return object()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_process)
        asyncio.run(create_process(
            supervisor.config.command, supervisor.config.working_dir,
            supervisor.config.environment,
        ))
        forwarded = {key: value for key, value in captured["env"].items()
                     if key in mcp["env_vars"]}
        assert all(forwarded[name] == value for name, value in inherited.items())
        assert "RECRUITOPS_DESKTOP_WRITE_OPTIN" not in forwarded
        assert captured["cwd"] == str(instance)
    finally:
        codex_bff.get_codex_supervisor.cache_clear()
