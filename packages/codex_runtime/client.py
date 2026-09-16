from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeAlias


class AsyncLineReader(Protocol):
    async def readline(self) -> bytes | str: ...


class AsyncLineWriter(Protocol):
    def write(self, data: bytes) -> Any: ...

    async def drain(self) -> Any: ...

    def close(self) -> Any: ...


class ProcessLike(Protocol):
    stdin: AsyncLineWriter | None
    stdout: AsyncLineReader
    returncode: int | None

    async def wait(self) -> int: ...

    def terminate(self) -> Any: ...

    def kill(self) -> Any: ...


ProcessFactory: TypeAlias = Callable[
    [Sequence[str], Path | None, Mapping[str, str] | None], Awaitable[ProcessLike]
]
NotificationHandler: TypeAlias = Callable[["JsonRpcNotification"], Any]
CODEX_STDIO_READER_LIMIT = 4 * 1024 * 1024


@dataclass(frozen=True)
class JsonRpcNotification:
    method: str
    params: Any = None


@dataclass(frozen=True)
class JsonRpcServerRequest:
    request_id: int | str
    method: str
    params: Any = None


class JsonRpcClientError(RuntimeError):
    """Base error for the local JSON-RPC transport."""


class ClientNotStartedError(JsonRpcClientError):
    pass


class ClientClosedError(JsonRpcClientError):
    pass


class JsonRpcProtocolError(JsonRpcClientError):
    pass


class ProcessExitedError(JsonRpcClientError):
    def __init__(
        self,
        returncode: int | None,
        *,
        cause: BaseException | None = None,
    ) -> None:
        self.returncode = returncode
        self.cause = cause
        detail = f"Codex process exited with return code {returncode!r}"
        if cause is not None:
            detail = f"Codex process wait failed: {cause}"
        super().__init__(detail)


class JsonRpcRemoteError(JsonRpcClientError):
    def __init__(
        self,
        *,
        code: int | str | None,
        message: str,
        data: Any = None,
        request_id: int | str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.data = data
        self.request_id = request_id
        super().__init__(message)


async def create_process(
    command: Sequence[str],
    working_dir: Path | None,
    environment: Mapping[str, str] | None = None,
) -> ProcessLike:
    """Create the real subprocess; tests can replace this factory entirely."""

    return await asyncio.create_subprocess_exec(
        *command,
        cwd=str(working_dir) if working_dir is not None else None,
        env={**os.environ, **dict(environment or {})},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=CODEX_STDIO_READER_LIMIT,
    )


class JsonRpcStdioClient:
    """Line-delimited JSON-RPC 2.0 client over an injected process."""

    def __init__(
        self,
        process: ProcessLike,
        *,
        notification_handler: NotificationHandler | None = None,
    ) -> None:
        self.process = process
        self._next_request_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._notification_queue: asyncio.Queue[JsonRpcNotification] = asyncio.Queue()
        self._server_request_queue: asyncio.Queue[JsonRpcServerRequest] = asyncio.Queue()
        self._notification_handlers: list[NotificationHandler] = []
        if notification_handler is not None:
            self._notification_handlers.append(notification_handler)
        self._reader_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[None] | None = None
        self._closed_event = asyncio.Event()
        self._started = False
        self._closing = False
        self._failure: JsonRpcClientError | None = None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def failure(self) -> JsonRpcClientError | None:
        return self._failure

    @property
    def is_running(self) -> bool:
        return self._started and not self._closing and self._failure is None

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self._notification_handlers.append(handler)

    async def start(self) -> None:
        if self._started and not self._closing:
            return
        if self._closing:
            raise ClientClosedError("JSON-RPC client is closed")
        self._started = True
        self._reader_task = asyncio.create_task(self._read_loop())
        self._wait_task = asyncio.create_task(self._watch_process())

    async def request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        self._ensure_usable()
        if not method.strip():
            raise ValueError("JSON-RPC method must not be blank")

        async with self._write_lock:
            self._ensure_usable()
            request_id = self._next_request_id
            self._next_request_id += 1
            future = asyncio.get_running_loop().create_future()
            self._pending[request_id] = future
            try:
                await self._write_message(
                    {"id": request_id, "method": method, "params": params}
                )
            except BaseException:
                self._pending.pop(request_id, None)
                if not future.done():
                    future.cancel()
                raise

        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout)
        except BaseException:
            if self._pending.get(request_id) is future:
                self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise

    async def notify(self, method: str, params: Any = None) -> None:
        self._ensure_usable()
        if not method.strip():
            raise ValueError("JSON-RPC method must not be blank")
        async with self._write_lock:
            self._ensure_usable()
            await self._write_message(
                {"method": method, "params": params}
            )

    async def next_notification(self) -> JsonRpcNotification:
        return await self._notification_queue.get()

    async def next_server_request(self) -> JsonRpcServerRequest:
        return await self._server_request_queue.get()

    async def respond(self, request_id: int | str, *, result: Any = None) -> None:
        self._ensure_usable()
        async with self._write_lock:
            await self._write_message({"id": request_id, "result": result})

    async def respond_error(
        self,
        request_id: int | str,
        *,
        code: int,
        message: str,
        data: Any = None,
    ) -> None:
        self._ensure_usable()
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        async with self._write_lock:
            await self._write_message({"id": request_id, "error": error})

    async def wait_closed(self) -> None:
        await self._closed_event.wait()

    async def close(self, *, wait_timeout: float = 1.0) -> None:
        if self._closing and self._closed_event.is_set():
            return
        self._closing = True
        self._fail_pending(ClientClosedError("JSON-RPC client closed"))

        writer = self.process.stdin
        if writer is not None:
            close = getattr(writer, "close", None)
            if close is not None:
                result = close()
                if inspect.isawaitable(result):
                    await result
            wait_closed = getattr(writer, "wait_closed", None)
            if wait_closed is not None:
                try:
                    result = wait_closed()
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=wait_timeout)
                except (asyncio.TimeoutError, ConnectionError, OSError):
                    pass

        if getattr(self.process, "returncode", None) is None:
            terminate = getattr(self.process, "terminate", None)
            if terminate is not None:
                result = terminate()
                if inspect.isawaitable(result):
                    await result

        wait_task = self._wait_task
        if wait_task is not None and not wait_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(wait_task), timeout=wait_timeout)
            except (asyncio.TimeoutError, ProcessExitedError):
                pass

        reader_task = self._reader_task
        if reader_task is not None and not reader_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(reader_task), timeout=wait_timeout)
            except (asyncio.TimeoutError, JsonRpcClientError):
                pass

        for task in (reader_task, self._wait_task):
            if task is not None and not task.done():
                task.cancel()
        tasks = [task for task in (reader_task, self._wait_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._started = False
        self._closed_event.set()

    def _ensure_usable(self) -> None:
        if not self._started:
            raise ClientNotStartedError("JSON-RPC client has not been started")
        if self._closing:
            raise ClientClosedError("JSON-RPC client is closed")
        if self._failure is not None:
            raise self._failure

    async def _write_message(self, message: Mapping[str, Any]) -> None:
        writer = self.process.stdin
        if writer is None:
            raise ClientClosedError("JSON-RPC process stdin is unavailable")
        payload = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        result = writer.write(payload)
        if inspect.isawaitable(result):
            await result
        drain = getattr(writer, "drain", None)
        if drain is not None:
            result = drain()
            if inspect.isawaitable(result):
                await result

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self.process.stdout.readline()
                if line in (b"", ""):
                    return
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise JsonRpcProtocolError("invalid JSON-RPC response") from exc
                if not isinstance(message, dict):
                    raise JsonRpcProtocolError("JSON-RPC message must be an object")
                await self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failure = exc if isinstance(exc, JsonRpcClientError) else JsonRpcClientError(str(exc))
            self._fail_pending(failure)

    async def _watch_process(self) -> None:
        try:
            returncode = await self.process.wait()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if not self._closing:
                self._fail_pending(ProcessExitedError(None, cause=exc))
            return
        if not self._closing:
            self._fail_pending(ProcessExitedError(returncode))
            reader_task = self._reader_task
            if reader_task is not None and not reader_task.done():
                reader_task.cancel()
        self._closed_event.set()

    async def _handle_message(self, message: Mapping[str, Any]) -> None:
        if "method" in message:
            method = message.get("method")
            if not isinstance(method, str) or not method.strip():
                raise JsonRpcProtocolError("JSON-RPC notification method is invalid")
            if "id" in message:
                request_id = message["id"]
                if not isinstance(request_id, (int, str)):
                    raise JsonRpcProtocolError("JSON-RPC server request id is invalid")
                self._server_request_queue.put_nowait(
                    JsonRpcServerRequest(
                        request_id=request_id,
                        method=method,
                        params=message.get("params"),
                    )
                )
                return
            notification = JsonRpcNotification(method=method, params=message.get("params"))
            self._notification_queue.put_nowait(notification)
            for handler in tuple(self._notification_handlers):
                result = handler(notification)
                if inspect.isawaitable(result):
                    await result
            return

        if "id" not in message:
            raise JsonRpcProtocolError("JSON-RPC message has neither method nor id")
        request_id = message["id"]
        if not isinstance(request_id, int):
            raise JsonRpcProtocolError("JSON-RPC response id is not an integer")
        future = self._pending.pop(request_id, None)
        if future is None:
            return
        if "error" in message:
            error = message["error"]
            if not isinstance(error, Mapping):
                raise JsonRpcProtocolError("JSON-RPC error must be an object")
            future.set_exception(
                JsonRpcRemoteError(
                    code=error.get("code"),
                    message=str(error.get("message", "remote JSON-RPC error")),
                    data=error.get("data"),
                    request_id=request_id,
                )
            )
            return
        if "result" not in message:
            raise JsonRpcProtocolError("JSON-RPC response has neither result nor error")
        future.set_result(message["result"])

    def _fail_pending(self, failure: JsonRpcClientError) -> None:
        if self._failure is None:
            self._failure = failure
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(self._failure)
        self._pending.clear()
        self._closed_event.set()


JsonRpcClient = JsonRpcStdioClient
default_process_factory = create_process


__all__ = [
    "AsyncLineReader",
    "AsyncLineWriter",
    "ClientClosedError",
    "ClientNotStartedError",
    "JsonRpcClientError",
    "JsonRpcClient",
    "JsonRpcNotification",
    "JsonRpcProtocolError",
    "JsonRpcRemoteError",
    "JsonRpcServerRequest",
    "JsonRpcStdioClient",
    "NotificationHandler",
    "ProcessExitedError",
    "ProcessFactory",
    "ProcessLike",
    "create_process",
    "default_process_factory",
]
