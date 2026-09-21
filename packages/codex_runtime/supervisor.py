from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from .client import (
    JsonRpcNotification,
    JsonRpcServerRequest,
    JsonRpcStdioClient,
    ProcessFactory,
    ProcessLike,
    create_process,
)
from .config import CodexRuntimeConfig, RestartPolicy
from .events import CodexEvent, normalize_event


class SupervisorState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class SupervisorStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: SupervisorState
    restart_count: int = 0
    last_error: str | None = None


class HealthStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    healthy: bool
    state: SupervisorState
    provider: str
    model: str
    response: Any = None
    error: str | None = None


class SupervisorStartupError(RuntimeError):
    pass


EventHandler = Callable[[CodexEvent], Any]
ClientFactory = Callable[
    [ProcessLike, Callable[[JsonRpcNotification], Awaitable[None]]],
    JsonRpcStdioClient,
]


class CodexSupervisor:
    """Lifecycle shell around the stdio client with bounded crash recovery."""

    def __init__(
        self,
        config: CodexRuntimeConfig,
        *,
        process_factory: ProcessFactory = create_process,
        client_factory: ClientFactory | None = None,
        event_handler: EventHandler | None = None,
    ) -> None:
        self.config = config
        self._process_factory = process_factory
        self._client_factory = client_factory or self._build_client
        self._event_handler = event_handler
        self._events: asyncio.Queue[CodexEvent] = asyncio.Queue()
        self._lifecycle_lock = asyncio.Lock()
        self._client: JsonRpcStdioClient | None = None
        self._process: ProcessLike | None = None
        self._state = SupervisorState.STOPPED
        self._restart_count = 0
        self._last_error: str | None = None
        self._initialize_response: Any = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._server_request_task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def state(self) -> SupervisorState:
        return self._state

    @property
    def client(self) -> JsonRpcStdioClient | None:
        return self._client

    @property
    def process(self) -> ProcessLike | None:
        return self._process

    @property
    def status(self) -> SupervisorStatus:
        return SupervisorStatus(
            state=self._state,
            restart_count=self._restart_count,
            last_error=self._last_error,
        )

    async def start(self, *, probe: bool = True) -> SupervisorStatus:
        async with self._lifecycle_lock:
            if self._state is SupervisorState.RUNNING:
                return self.status
            self._state = SupervisorState.STARTING
            self._stopping = False
            self._last_error = None
            client: JsonRpcStdioClient | None = None
            try:
                start_deadline = (
                    asyncio.get_running_loop().time()
                    + self.config.startup_timeout_seconds
                )
                try:
                    process = await asyncio.wait_for(
                        self._process_factory(
                            self.config.command,
                            self.config.working_dir,
                            self.config.environment,
                        ),
                        timeout=self.config.startup_timeout_seconds,
                    )
                except asyncio.TimeoutError as exc:
                    raise SupervisorStartupError("Codex process startup timed out") from exc
                client = self._client_factory(process, self._on_notification)
                await client.start()
                self._process = process
                self._client = client
                if probe:
                    remaining = max(0.0, start_deadline - asyncio.get_running_loop().time())
                    try:
                        self._initialize_response = await client.request(
                            "initialize",
                            {
                                "clientInfo": {
                                    "name": self.config.client_name,
                                    "title": self.config.client_title,
                                    "version": self.config.client_version,
                                },
                                "capabilities": {
                                    "experimentalApi": self.config.experimental_api,
                                },
                            },
                            timeout=remaining,
                        )
                    except asyncio.TimeoutError as exc:
                        raise SupervisorStartupError(
                            "Codex initialize handshake timed out"
                        ) from exc
                    await client.notify("initialized")
                    if self.config.skill_roots:
                        remaining = max(0.0, start_deadline - asyncio.get_running_loop().time())
                        try:
                            await client.request(
                                "skills/extraRoots/set",
                                {"extraRoots": [
                                    str(root.resolve()) for root in self.config.skill_roots
                                ]},
                                timeout=remaining,
                            )
                        except asyncio.TimeoutError as exc:
                            raise SupervisorStartupError(
                                "Codex bundled skills registration timed out"
                            ) from exc
                self._state = SupervisorState.RUNNING
                self._server_request_task = asyncio.create_task(
                    self._handle_server_requests(client),
                    name="codex-supervisor-server-requests",
                )
                self._monitor_task = asyncio.create_task(
                    self._monitor_client(client),
                    name="codex-supervisor-monitor",
                )
                return self.status
            except BaseException as exc:
                self._last_error = str(exc)
                self._state = SupervisorState.FAILED
                if client is not None:
                    await client.close()
                raise

    async def stop(self) -> SupervisorStatus:
        async with self._lifecycle_lock:
            if self._state is SupervisorState.STOPPED:
                return self.status
            self._state = SupervisorState.STOPPING
            self._stopping = True
            monitor = self._monitor_task
            self._monitor_task = None
            server_requests = self._server_request_task
            self._server_request_task = None
            client = self._client
            if client is not None:
                await client.close()
            self._client = None
            self._process = None
            self._initialize_response = None
            self._state = SupervisorState.STOPPED
            status = self.status
        current = asyncio.current_task()
        for task in (monitor, server_requests):
            if task is not None and task is not current and not task.done():
                task.cancel()
        tasks = [
            task
            for task in (monitor, server_requests)
            if task is not None and task is not current
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return status

    async def restart(self, *, probe: bool = True) -> SupervisorStatus:
        await self.stop()
        self._restart_count += 1
        if self.config.restart_backoff_seconds:
            await asyncio.sleep(self.config.restart_backoff_seconds)
        return await self.start(probe=probe)

    async def request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        client = self._client
        if self._state is not SupervisorState.RUNNING or client is None:
            raise RuntimeError("Codex supervisor is not running")
        return await client.request(method, params, timeout=timeout)

    async def health(self, *, timeout: float | None = None) -> HealthStatus:
        client = self._client
        if client is None or self._state in (SupervisorState.STOPPED, SupervisorState.FAILED):
            return HealthStatus(
                healthy=False,
                state=self._state,
                provider=self.config.provider,
                model=self.config.model,
                error="Codex process is not started",
            )
        return HealthStatus(
            healthy=True,
            state=self._state,
            provider=self.config.provider,
            model=self.config.model,
            response=self._initialize_response,
        )

    healthcheck = health

    def should_restart(self, *, failed: bool = True) -> bool:
        policy = self.config.restart_policy
        eligible = (
            policy is RestartPolicy.ALWAYS
            or (policy is RestartPolicy.ON_FAILURE and failed)
        )
        return eligible and self._restart_count < self.config.max_restarts

    async def next_event(self) -> CodexEvent:
        return await self._events.get()

    async def _on_notification(self, notification: JsonRpcNotification) -> None:
        event = normalize_event(notification)
        self._events.put_nowait(event)
        if self._event_handler is not None:
            result = self._event_handler(event)
            if inspect.isawaitable(result):
                await result

    async def _monitor_client(self, client: JsonRpcStdioClient) -> None:
        """Restart only an unexpectedly closed active client, within policy limits."""

        await client.wait_closed()
        if self._stopping or self._client is not client:
            return
        failed = client.failure is not None
        if not self.should_restart(failed=failed):
            self._state = SupervisorState.FAILED if failed else SupervisorState.STOPPED
            self._last_error = str(client.failure) if client.failure is not None else None
            return

        self._restart_count += 1
        if self.config.restart_backoff_seconds:
            await asyncio.sleep(self.config.restart_backoff_seconds)
        async with self._lifecycle_lock:
            if self._stopping or self._client is not client:
                return
            self._client = None
            self._process = None
            self._initialize_response = None
            self._state = SupervisorState.FAILED
            self._monitor_task = None
            request_task = self._server_request_task
            self._server_request_task = None
            if request_task is not None and not request_task.done():
                request_task.cancel()
        try:
            await self.start()
        except BaseException as exc:
            self._last_error = str(exc)

    async def _handle_server_requests(self, client: JsonRpcStdioClient) -> None:
        """Resolve Harness approvals without exposing a second, competing UI loop."""

        try:
            while True:
                request = await client.next_server_request()
                result, decision = _server_request_result(request)
                if result is None:
                    await client.respond_error(
                        request.request_id,
                        code=-32601,
                        message="RecruitOps does not support this App Server request",
                    )
                else:
                    await client.respond(request.request_id, result=result)
                params = request.params if isinstance(request.params, Mapping) else {}
                event = normalize_event(
                    "server/request/handled",
                    {
                        "threadId": params.get("threadId"),
                        "turnId": params.get("turnId"),
                        "requestMethod": request.method,
                        "decision": decision,
                    },
                )
                self._events.put_nowait(event)
                if self._event_handler is not None:
                    callback_result = self._event_handler(event)
                    if inspect.isawaitable(callback_result):
                        await callback_result
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if self._client is client and not self._stopping:
                self._last_error = f"Codex server request handler failed: {exc}"

    @staticmethod
    def _build_client(
        process: ProcessLike,
        handler: Callable[[JsonRpcNotification], Awaitable[None]],
    ) -> JsonRpcStdioClient:
        return JsonRpcStdioClient(process, notification_handler=handler)


def _server_request_result(
    request: JsonRpcServerRequest,
) -> tuple[dict[str, Any] | None, str]:
    """Apply the local Harness policy to App Server initiated requests."""

    params = request.params if isinstance(request.params, Mapping) else {}
    if request.method == "mcpServer/elicitation/request":
        server_name = str(params.get("serverName") or "").strip().casefold()
        form = params.get("request")
        meta = form.get("meta") if isinstance(form, Mapping) else None
        approval_kind = meta.get("codex_approval_kind") if isinstance(meta, Mapping) else None
        if server_name in {"recruitops", "recruitops agent"} and approval_kind == "mcp_tool_call":
            return {
                "action": "accept",
                "content": {"decision": "approve"},
            }, "trusted_recruitops_mcp"
        return {"action": "decline", "content": None}, "declined_untrusted_mcp"
    if request.method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }:
        return {"decision": "decline"}, "declined_non_mcp_write"
    if request.method == "item/permissions/requestApproval":
        return {"permissions": {}}, "declined_additional_permissions"
    return None, "unsupported"


__all__ = [
    "CodexSupervisor",
    "ClientFactory",
    "EventHandler",
    "HealthStatus",
    "SupervisorStartupError",
    "SupervisorState",
    "SupervisorStatus",
]
