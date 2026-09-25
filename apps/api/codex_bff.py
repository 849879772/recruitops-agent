"""FastAPI-friendly facade for the local Codex app-server runtime."""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from packages.codex_runtime import (
    CodexEvent,
    CodexEventType,
    CodexHomeConfig,
    CodexRuntimeConfig,
    CodexSupervisor,
    CodexThreads,
    NormalizedEvent,
    RuntimeHealth,
    TurnLimits,
    TurnLoopResult,
    TurnStreamLoop,
    runtime_health,
)
from packages.codex_runtime.telemetry import CodexTelemetry, JsonlTraceRecorder


class SupervisorDependency(Protocol):
    async def start(self) -> Any: ...

    async def stop(self) -> Any: ...

    async def health(self) -> Any: ...

    async def next_event(self) -> Any: ...

    async def request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any: ...


class ThreadsDependency(Protocol):
    async def start(self, **params: Any) -> Any: ...

    async def resume(self, thread_id: str, **params: Any) -> Any: ...

    async def read(self, thread_id: str, *, include_turns: bool = True) -> Any: ...

    async def list(
        self,
        *,
        cursor: str | None = None,
        limit: int = 20,
        archived: bool = False,
    ) -> Any: ...

    async def start_turn(self, thread_id: str, text: str, **params: Any) -> Any: ...

    async def interrupt(self, thread_id: str, turn_id: str) -> Any: ...

    async def delete(self, thread_id: str) -> Any: ...


_T = TypeVar("_T")
_CLOSED = object()


async def _await_if_needed(value: _T | Awaitable[_T]) -> _T:
    if inspect.isawaitable(value):
        return await value
    return value


class CodexEventSubscription:
    """One fan-out queue that can be consumed with ``get`` or ``async for``."""

    def __init__(self, service: CodexBffService, thread_id: str) -> None:
        self.thread_id = thread_id
        self._service: CodexBffService | None = service
        self._queue: asyncio.Queue[CodexEvent | object] = asyncio.Queue()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def queue(self) -> asyncio.Queue[CodexEvent | object]:
        """Expose the underlying queue for adapters that require queue semantics."""

        return self._queue

    def __aiter__(self) -> CodexEventSubscription:
        return self

    async def __anext__(self) -> NormalizedEvent:
        return await self.get()

    async def get(self) -> NormalizedEvent:
        try:
            value = await self._queue.get()
        except asyncio.CancelledError:
            self.close()
            raise
        return self._take(value)

    def get_nowait(self) -> NormalizedEvent:
        return self._take(self._queue.get_nowait())

    async def aclose(self) -> None:
        self.close()

    async def __aenter__(self) -> CodexEventSubscription:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        service = self._service
        self._service = None
        if service is not None:
            service._remove_subscription(self)
        self._queue.put_nowait(_CLOSED)

    def _put(self, event: CodexEvent) -> None:
        if not self._closed:
            self._queue.put_nowait(event)

    def _take(self, value: CodexEvent | object) -> NormalizedEvent:
        if value is _CLOSED:
            self.close()
            raise StopAsyncIteration
        return cast(NormalizedEvent, value)


@dataclass
class _ActiveTurnGuard:
    loop: TurnStreamLoop
    subscription: CodexEventSubscription
    task: asyncio.Task[None] | None = None


class CodexBffService:
    """Injectable BFF facade over a supervisor and its thread/turn adapter."""

    def __init__(
        self,
        supervisor: SupervisorDependency,
        threads: ThreadsDependency | None = None,
        *,
        telemetry: CodexTelemetry | None = None,
        turn_limits: TurnLimits | None = None,
    ) -> None:
        self.supervisor = supervisor
        self.threads = (
            threads
            if threads is not None
            else CodexThreads(cast(CodexSupervisor, supervisor))
        )
        self._subscriptions: dict[str, set[CodexEventSubscription]] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._event_task: asyncio.Task[None] | None = None
        self._start_result: Any = None
        self._stop_result: Any = None
        self._started = False
        self._stopping = False
        self.telemetry = telemetry
        self.turn_limits = turn_limits or TurnLimits()
        self._turn_guards: dict[tuple[str, str], _ActiveTurnGuard] = {}
        self._completed_turns: set[tuple[str, str]] = set()

    async def start(self) -> Any:
        async with self._lifecycle_lock:
            if self._event_task is not None and not self._event_task.done():
                return self._start_result

            self._stopping = False
            self._start_result = await _await_if_needed(self.supervisor.start())
            self._started = True
            self._event_task = asyncio.create_task(
                self._event_loop(),
                name="codex-bff-events",
            )
            return self._start_result

    async def stop(self) -> Any:
        async with self._lifecycle_lock:
            if not self._started and self._event_task is None:
                self._close_subscriptions()
                return self._stop_result

            self._stopping = True
            self._started = False
            event_task = self._event_task
            self._event_task = None
            await self._cancel_turn_guards()
            self._close_subscriptions()

            if event_task is not None and not event_task.done():
                event_task.cancel()
            if event_task is not None:
                await asyncio.gather(event_task, return_exceptions=True)

            try:
                self._stop_result = await _await_if_needed(self.supervisor.stop())
                return self._stop_result
            finally:
                self._stopping = False

    async def health(self) -> RuntimeHealth:
        return await runtime_health(cast(CodexSupervisor, self.supervisor))

    async def mcp_status(self) -> Any:
        return await _await_if_needed(
            self.supervisor.request(
                "mcpServerStatus/list",
                {"detail": "full", "limit": 100},
                timeout=10.0,
            )
        )

    async def thread_start(self, **params: Any) -> Any:
        return await _await_if_needed(self.threads.start(**params))

    async def thread_resume(self, thread_id: str, **params: Any) -> Any:
        return await _await_if_needed(self.threads.resume(thread_id, **params))

    async def thread_read(self, thread_id: str, *, include_turns: bool = True) -> Any:
        return await _await_if_needed(
            self.threads.read(thread_id, include_turns=include_turns)
        )

    async def thread_list(
        self,
        *,
        cursor: str | None = None,
        limit: int = 20,
        archived: bool = False,
    ) -> Any:
        return await _await_if_needed(
            self.threads.list(cursor=cursor, limit=limit, archived=archived)
        )

    async def turn_start(self, thread_id: str, text: str, **params: Any) -> Any:
        key = _require_thread_id(thread_id)
        try:
            turn = await asyncio.wait_for(
                _await_if_needed(self.threads.start_turn(key, text, **params)),
                timeout=self.turn_limits.turn_start_timeout_seconds,
            )
        except TimeoutError as exc:
            raise TimeoutError(
                "Codex turn start exceeded the runtime start-time limit"
            ) from exc
        turn_id = _turn_id(turn)
        if turn_id:
            self._register_turn_guard(key, turn_id)
        return turn

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> Any:
        thread_key = _require_thread_id(thread_id)
        turn_key = _require_thread_id(turn_id)
        await self._cancel_turn_guard((thread_key, turn_key))
        return await _await_if_needed(self.threads.interrupt(thread_key, turn_key))

    async def thread_delete(self, thread_id: str) -> Any:
        key = _require_thread_id(thread_id)
        await self._cancel_thread_guards(key)
        return await _await_if_needed(self.threads.delete(key))

    def subscribe(self, thread_id: str) -> CodexEventSubscription:
        key = _require_thread_id(thread_id)
        subscription = CodexEventSubscription(self, key)
        self._subscriptions.setdefault(key, set()).add(subscription)
        return subscription

    def subscribe_events(self, thread_id: str) -> CodexEventSubscription:
        return self.subscribe(thread_id)

    def events(self, thread_id: str) -> AsyncIterator[NormalizedEvent]:
        return self.event_stream(thread_id)

    async def event_stream(self, thread_id: str) -> AsyncIterator[NormalizedEvent]:
        subscription = self.subscribe(thread_id)
        try:
            async for event in subscription:
                yield event
        finally:
            subscription.close()

    async def _event_loop(self) -> None:
        try:
            while True:
                raw_event = await _await_if_needed(self.supervisor.next_event())
                if raw_event is None:
                    return
                event = (
                    raw_event
                    if isinstance(raw_event, CodexEvent)
                    else CodexEvent.model_validate(raw_event)
                    if isinstance(raw_event, Mapping)
                    and {"event_type", "method"}.issubset(raw_event)
                    else _normalize_raw_event(raw_event)
                )
                self._publish(event)
        except asyncio.CancelledError:
            raise
        except (EOFError, StopAsyncIteration):
            return
        finally:
            if not self._stopping:
                self._close_subscriptions()

    def _publish(self, event: CodexEvent) -> None:
        if self.telemetry is not None:
            self.telemetry.record(event)
        if event.thread_id is None:
            return
        if event.event_type.value == "turn_completed" and event.turn_id:
            self._completed_turns.add((event.thread_id, event.turn_id))
            if len(self._completed_turns) > 256:
                self._completed_turns.pop()
        for subscription in tuple(self._subscriptions.get(event.thread_id, ())):
            subscription._put(event)

    def _register_turn_guard(self, thread_id: str, turn_id: str) -> None:
        key = (thread_id, turn_id)
        if key in self._completed_turns:
            self._completed_turns.discard(key)
            return
        previous = self._turn_guards.pop(key, None)
        if previous is not None:
            previous.subscription.close()
            if previous.task is not None and not previous.task.done():
                previous.task.cancel()
        subscription = self.subscribe(thread_id)

        async def interrupt() -> Any:
            return await _await_if_needed(self.threads.interrupt(thread_id, turn_id))

        loop = TurnStreamLoop(
            subscription,
            thread_id=thread_id,
            turn_id=turn_id,
            limits=self.turn_limits,
            interrupt=interrupt,
        )
        guard = _ActiveTurnGuard(loop=loop, subscription=subscription)
        guard.task = asyncio.create_task(
            self._run_turn_guard(key, guard),
            name=f"codex-turn-guard-{thread_id}-{turn_id}",
        )
        self._turn_guards[key] = guard

    async def _run_turn_guard(
        self,
        key: tuple[str, str],
        guard: _ActiveTurnGuard,
    ) -> None:
        try:
            result = await guard.loop.run()
            if result.interruption is not None:
                self._publish(_limit_event(key, result))
                self._close_thread_subscriptions(key[0])
        except asyncio.CancelledError:
            raise
        finally:
            guard.subscription.close()
            if self._turn_guards.get(key) is guard:
                self._turn_guards.pop(key, None)

    async def _cancel_turn_guard(self, key: tuple[str, str]) -> None:
        guard = self._turn_guards.pop(key, None)
        if guard is None:
            return
        guard.subscription.close()
        task = guard.task
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _cancel_turn_guards(self) -> None:
        keys = tuple(self._turn_guards)
        for key in keys:
            await self._cancel_turn_guard(key)

    async def _cancel_thread_guards(self, thread_id: str) -> None:
        keys = tuple(key for key in self._turn_guards if key[0] == thread_id)
        for key in keys:
            await self._cancel_turn_guard(key)

    def _close_thread_subscriptions(self, thread_id: str) -> None:
        for subscription in tuple(self._subscriptions.get(thread_id, ())):
            subscription.close()

    def _remove_subscription(self, subscription: CodexEventSubscription) -> None:
        subscriptions = self._subscriptions.get(subscription.thread_id)
        if subscriptions is None:
            return
        subscriptions.discard(subscription)
        if not subscriptions:
            self._subscriptions.pop(subscription.thread_id, None)

    def _close_subscriptions(self) -> None:
        for subscriptions in tuple(self._subscriptions.values()):
            for subscription in tuple(subscriptions):
                subscription.close()
        self._subscriptions.clear()


def _normalize_raw_event(raw_event: Any) -> CodexEvent:
    if isinstance(raw_event, CodexEvent):
        return raw_event
    return normalize_event(raw_event)


def _turn_id(value: Any) -> str | None:
    candidate = getattr(value, "id", None)
    if isinstance(candidate, str) and candidate.strip():
        return candidate
    if isinstance(value, Mapping):
        candidate = value.get("id")
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        nested = value.get("turn")
        if isinstance(nested, Mapping):
            candidate = nested.get("id")
            if isinstance(candidate, str) and candidate.strip():
                return candidate
    return None


def _limit_event(key: tuple[str, str], result: TurnLoopResult) -> CodexEvent:
    interruption = result.interruption
    assert interruption is not None
    payload: dict[str, Any] = {
        "status": "interrupted",
        "reason": interruption.reason.value,
        "tool_calls": result.tool_calls,
        "no_progress_events": result.no_progress_events,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "interrupt_requested": interruption.interrupt_requested,
    }
    if interruption.interrupt_error:
        payload["interrupt_error"] = interruption.interrupt_error
    return CodexEvent(
        event_type=CodexEventType.ERROR,
        method="runtime/turn_interrupted",
        thread_id=key[0],
        turn_id=key[1],
        text=interruption.message,
        payload=payload,
    )


def _require_thread_id(thread_id: str) -> str:
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("thread_id must not be blank")
    return thread_id


@lru_cache
def get_codex_supervisor() -> CodexSupervisor:
    """Build the default supervisor lazily so importing this module is inert."""

    from packages.config import get_settings

    settings = get_settings()
    desktop = getattr(settings, "env", None) == "desktop-isolated"
    code_root = Path(__file__).resolve().parents[2] if desktop else settings.agent_root
    codex_home = settings.codex_home
    if not codex_home.is_absolute():
        codex_home = settings.agent_root / codex_home
    mcp_env_vars = (
        "RECRUITOPS_ENV",
        "RECRUITOPS_AGENT_ROOT",
        "RECRUITOPS_SOURCE_ROOT",
        "RECRUITOPS_SOURCE_PYTHON_EXECUTABLE",
        "RECRUITOPS_DATABASE_URL",
        "RECRUITOPS_WRITE_ENABLED",
        "RECRUITOPS_LLM_ENABLED",
        "RECRUITOPS_JOB_ANALYSIS_ENABLED",
        "RECRUITOPS_LLM_API_KEY",
        "RECRUITOPS_MODEL_API_BASE_URL",
        "RECRUITOPS_MODEL_API_STYLE",
        "RECRUITOPS_MODEL_NAME",
        "RECRUITOPS_MODEL_PROVIDER_NAME",
        "RECRUITOPS_LLM_MODEL",
        "RECRUITOPS_LLM_ENDPOINT",
        "RECRUITOPS_LLM_TIMEOUT_SECONDS",
        "RECRUITOPS_MATCH_MAX_CONCURRENCY",
        "RECRUITOPS_BACKUP_ROOT",
        "RECRUITOPS_CHECKPOINT_MODE",
        "RECRUITOPS_TRACE_PATH",
        "RECRUITOPS_CRAWL_MAX_CONCURRENCY",
        "RECRUITOPS_DETAIL_MAX_CONCURRENCY",
        "RECRUITOPS_BROWSER_MAX_CONCURRENCY",
        "RECRUITOPS_CRAWL_RESOURCE_ROOT",
        "RECRUITOPS_BROWSER_CHANNEL",
        "RECRUITOPS_BROWSER_EXECUTABLE_PATH",
        "RECRUITOPS_EMBEDDING_ENDPOINT",
        "RECRUITOPS_EMBEDDING_API_KEY",
        "RECRUITOPS_EMBEDDING_MODEL",
        "RECRUITOPS_EMBEDDING_DIMENSION",
        "RECRUITOPS_MAIL_ENABLED",
        "RECRUITOPS_MAIL_IMAP_HOST",
        "RECRUITOPS_MAIL_IMAP_PORT",
        "RECRUITOPS_MAIL_IMAP_USERNAME",
        "RECRUITOPS_MAIL_IMAP_PASSWORD",
        "RECRUITOPS_MAIL_IMAP_MAILBOX",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    )
    if desktop:
        # Forward the native supervisor's isolation contract, never synthesize opt-in.
        mcp_env_vars += (
            "RECRUITOPS_DESKTOP_LAUNCH_MODE",
            "RECRUITOPS_DESKTOP_CAPABILITIES",
            "RECRUITOPS_DESKTOP_INSTANCE_ID",
            "RECRUITOPS_DESKTOP_RUN_ID",
            "RECRUITOPS_DESKTOP_WRITE_OPTIN",
            "RECRUITOPS_CODEX_RUNTIME_ENABLED",
            "RECRUITOPS_AUTOMATION_ENABLED",
            "RECRUITOPS_MAIL_SYNC_ON_STARTUP",
            "RECRUITOPS_VISION_ENABLED",
            "RECRUITOPS_CODEX_HOME",
            "PLAYWRIGHT_BROWSERS_PATH",
            "PYTHONPATH",
            "PYTHONNOUSERSITE",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONUTF8",
            "HOME",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "TEMP",
            "TMP",
            "no_proxy",
        )
    CodexHomeConfig(
        model=settings.codex_model,
        provider_id=settings.codex_model_provider_id,
        provider_name=getattr(settings, "model_provider_name", "DeepSeek"),
        base_url=settings.codex_model_base_url,
        api_key_env=settings.codex_model_api_key_env,
        reasoning_effort=settings.codex_reasoning_effort,
        model_context_window=getattr(settings, "codex_model_context_window", 1_000_000),
        model_auto_compact_token_limit=getattr(
            settings,
            "codex_model_auto_compact_token_limit",
            96_000,
        ),
        mcp_command=sys.executable,
        mcp_args=(str(code_root / "scripts" / "run_mcp_server.py"),),
        mcp_env_vars=mcp_env_vars,
    ).write(codex_home)
    api_key = (settings.llm_api_key if settings.codex_model_api_key_env == "RECRUITOPS_LLM_API_KEY"
               else os.environ.get(settings.codex_model_api_key_env, ""))
    if (
        not api_key
        and settings.codex_model_api_key_env == "RECRUITOPS_LLM_API_KEY"
    ):
        api_key = settings.llm_api_key
    runtime_environment = {"CODEX_HOME": str(codex_home.resolve(strict=False))}
    if api_key:
        runtime_environment[settings.codex_model_api_key_env] = api_key
    # Settings may come from .env rather than the parent's process environment.
    for field in (
        "model_api_base_url",
        "model_api_style",
        "model_name",
        "model_provider_name",
        "llm_enabled",
        "job_analysis_enabled",
        "llm_api_key",
        "llm_model",
        "llm_endpoint",
        "llm_timeout_seconds",
        "match_max_concurrency",
        "crawl_max_concurrency",
        "detail_max_concurrency",
        "browser_max_concurrency",
    ):
        value = getattr(settings, field, None)
        if value is not None:
            runtime_environment[f"RECRUITOPS_{field.upper()}"] = (
                str(value).lower() if isinstance(value, bool) else str(value)
            )
    return CodexSupervisor(
        CodexRuntimeConfig(
            command=settings.codex_command,
            working_dir=settings.agent_root,
            skill_roots=(code_root / ".agents" / "skills",) if desktop else (),
            startup_timeout_seconds=settings.codex_startup_timeout_seconds,
            provider=settings.codex_model_provider_id,
            model=settings.codex_model,
            environment=runtime_environment,
        )
    )


@lru_cache
def get_codex_threads() -> CodexThreads:
    return CodexThreads(get_codex_supervisor())


def build_codex_bff_service(
    supervisor: SupervisorDependency,
    threads: ThreadsDependency,
    *,
    telemetry: CodexTelemetry | None = None,
    turn_limits: TurnLimits | None = None,
) -> CodexBffService:
    return CodexBffService(
        supervisor,
        threads,
        telemetry=telemetry,
        turn_limits=turn_limits,
    )


DEFAULT_CODEX_TURN_TIMEOUT_SECONDS = 600.0
DEFAULT_CODEX_TOOL_CALL_BUDGET = 64
DEFAULT_CODEX_REPEATED_NO_PROGRESS_LIMIT = 4
DEFAULT_CODEX_INTERRUPT_TIMEOUT_SECONDS = 5.0
DEFAULT_CODEX_TURN_START_TIMEOUT_SECONDS = 30.0


def _first_setting(settings: Any, names: tuple[str, ...], default: Any) -> Any:
    for name in names:
        value = getattr(settings, name, None)
        if value is not None:
            return value
    return default


def turn_limits_from_settings(settings: Any) -> TurnLimits:
    """Read optional runtime limits without requiring a config schema change."""

    return TurnLimits(
        turn_timeout_seconds=_first_setting(
            settings,
            ("codex_turn_timeout_seconds", "codex_max_duration_seconds"),
            DEFAULT_CODEX_TURN_TIMEOUT_SECONDS,
        ),
        tool_call_budget=_first_setting(
            settings,
            ("codex_tool_call_budget", "codex_max_tool_calls"),
            DEFAULT_CODEX_TOOL_CALL_BUDGET,
        ),
        repeated_no_progress_limit=_first_setting(
            settings,
            (
                "codex_repeated_no_progress_limit",
                "codex_max_repeated_no_progress",
            ),
            DEFAULT_CODEX_REPEATED_NO_PROGRESS_LIMIT,
        ),
        interrupt_timeout_seconds=_first_setting(
            settings,
            ("codex_interrupt_timeout_seconds",),
            DEFAULT_CODEX_INTERRUPT_TIMEOUT_SECONDS,
        ),
        turn_start_timeout_seconds=_first_setting(
            settings,
            ("codex_turn_start_timeout_seconds",),
            DEFAULT_CODEX_TURN_START_TIMEOUT_SECONDS,
        ),
    )


@lru_cache
def get_codex_bff_service() -> CodexBffService:
    from packages.config import get_settings

    settings = get_settings()
    trace_path = settings.codex_trace_path
    if not trace_path.is_absolute():
        trace_path = settings.agent_root / trace_path
    telemetry = CodexTelemetry(JsonlTraceRecorder(trace_path))
    return build_codex_bff_service(
        get_codex_supervisor(),
        get_codex_threads(),
        telemetry=telemetry,
        turn_limits=turn_limits_from_settings(settings),
    )


__all__ = [
    "CodexBffService",
    "CodexEventSubscription",
    "build_codex_bff_service",
    "get_codex_bff_service",
    "get_codex_supervisor",
    "get_codex_threads",
    "turn_limits_from_settings",
]
