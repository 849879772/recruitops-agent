"""WebSocket transport for the persistent Edge browser bridge."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import re
import secrets
from contextlib import asynccontextmanager, suppress
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect

from .models import OperationName, OperationStatus, TERMINAL_STATUSES, normalize_status
from .store import BrowserBridgeStore


BROWSER_BRIDGE_PATH = "/browser-bridge"
BRIDGE_PROTOCOL_VERSION = 1
AUTH_DOMAIN = "recruitops-browser-bridge-v1"
DEFAULT_CHALLENGE_TIMEOUT_SECONDS = 30.0
DEFAULT_DISPATCH_INTERVAL_SECONDS = 0.1
MAX_OUTBOX_BATCH = 500

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{16,256}$")
_SIGNATURE_RE = re.compile(r"^[0-9a-fA-F]{64}$")

TokenProvider = Callable[
    [], str | bytes | None | Awaitable[str | bytes | None]
]


def normalize_device_id(value: Any) -> str:
    """Validate the opaque device identifier used to select an outbox."""

    if not isinstance(value, str):
        raise ValueError("device id is invalid")
    value = value.strip()
    if not _DEVICE_ID_RE.fullmatch(value):
        raise ValueError("device id is invalid")
    return value


def build_auth_message(challenge: str, device_id: str) -> bytes:
    """Build the exact bytes signed by the local API token."""

    if not _CHALLENGE_RE.fullmatch(challenge):
        raise ValueError("challenge is invalid")
    normalized_device_id = normalize_device_id(device_id)
    return f"{AUTH_DOMAIN}\n{normalized_device_id}\n{challenge}".encode("utf-8")


def _token_bytes(api_token: str | bytes | None) -> bytes:
    if isinstance(api_token, bytes):
        value = api_token
    elif isinstance(api_token, str):
        value = api_token.strip().encode("utf-8")
    else:
        value = b""
    if not value or len(value) > 4096:
        raise ValueError("API token is not configured")
    return value


def create_auth_signature(
    api_token: str | bytes,
    challenge: str,
    device_id: str,
) -> str:
    """Return the hexadecimal HMAC-SHA256 proof for one challenge."""

    return hmac.new(
        _token_bytes(api_token),
        build_auth_message(challenge, device_id),
        hashlib.sha256,
    ).hexdigest()


compute_auth_signature = create_auth_signature


def verify_auth_signature(
    api_token: str | bytes | None,
    challenge: str,
    device_id: str,
    signature: Any,
) -> bool:
    """Verify a challenge proof without exposing the expected signature."""

    if not isinstance(signature, str) or not _SIGNATURE_RE.fullmatch(signature):
        return False
    try:
        expected = create_auth_signature(api_token or b"", challenge, device_id)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)


@dataclass(slots=True)
class BrowserBridgeConnection:
    """One authenticated WebSocket and its serialized send path."""

    device_id: str
    websocket: WebSocket
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_dispatched_sequence: int = 0
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserBridgeServer:
    """Authenticate devices and route their durable browser-bridge messages."""

    def __init__(
        self,
        store: BrowserBridgeStore,
        api_token: str | bytes | TokenProvider | Any,
        *,
        challenge_timeout_seconds: float = DEFAULT_CHALLENGE_TIMEOUT_SECONDS,
        dispatch_interval_seconds: float = DEFAULT_DISPATCH_INTERVAL_SECONDS,
    ) -> None:
        if challenge_timeout_seconds <= 0:
            raise ValueError("challenge timeout must be positive")
        if dispatch_interval_seconds <= 0:
            raise ValueError("dispatch interval must be positive")
        self.store = store
        self.api_token = api_token
        self.challenge_timeout_seconds = challenge_timeout_seconds
        self.dispatch_interval_seconds = dispatch_interval_seconds
        self._connections: dict[str, BrowserBridgeConnection] = {}
        self._connection_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._dispatcher_task: asyncio.Task[None] | None = None

    @property
    def connections(self) -> dict[str, BrowserBridgeConnection]:
        """Return a shallow snapshot of currently authenticated connections."""

        return dict(self._connections)

    def is_connected(self, device_id: str) -> bool:
        try:
            normalized = normalize_device_id(device_id)
        except ValueError:
            return False
        return normalized in self._connections

    @property
    def dispatcher_task(self) -> asyncio.Task[None] | None:
        """Expose the task handle for lifecycle tests and operational inspection."""

        return self._dispatcher_task

    async def start(self) -> None:
        """Start the cross-process outbox dispatcher exactly once."""

        async with self._lifecycle_lock:
            if self._dispatcher_task is not None and not self._dispatcher_task.done():
                return
            self.store.storage.initialize()
            self._dispatcher_task = asyncio.create_task(
                self._dispatch_loop(),
                name="browser-bridge-outbox-dispatcher",
            )

    async def stop(self) -> None:
        """Stop background work, close active sockets, and persist disconnects."""

        async with self._lifecycle_lock:
            task = self._dispatcher_task
            self._dispatcher_task = None

        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        async with self._connection_lock:
            connections = list(self._connections.values())
            self._connections.clear()

        for connection in connections:
            self.store.mark_device_disconnected(connection.device_id)
            await self._close_socket(connection.websocket, code=1001)

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                async with self._connection_lock:
                    device_ids = tuple(self._connections)
                for device_id in device_ids:
                    await self.dispatch_pending(device_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A transient database failure must not permanently disable polling.
                pass

            await asyncio.sleep(self.dispatch_interval_seconds)

    async def _resolve_api_token(self) -> str | bytes | None:
        source = self.api_token
        if hasattr(source, "api_token") and not isinstance(source, (str, bytes)):
            source = getattr(source, "api_token")
        if callable(source):
            source = source()
        if inspect.isawaitable(source):
            source = await source
        if source is None or isinstance(source, (str, bytes)):
            return source
        return None

    async def register(self, device_id: str, websocket: WebSocket) -> BrowserBridgeConnection:
        """Register a device, replacing an older connection for the same device."""

        normalized = normalize_device_id(device_id)
        connection = BrowserBridgeConnection(normalized, websocket)
        async with self._connection_lock:
            previous = self._connections.get(normalized)
            self._connections[normalized] = connection
        self.store.mark_device_connected(normalized)
        if previous is not None and previous.websocket is not websocket:
            await self._close_socket(previous.websocket, code=4002)
        return connection

    async def unregister(self, connection: BrowserBridgeConnection) -> None:
        removed = False
        async with self._connection_lock:
            if self._connections.get(connection.device_id) is connection:
                self._connections.pop(connection.device_id, None)
                removed = True
        if removed:
            self.store.mark_device_disconnected(connection.device_id)

    async def close_device(self, device_id: str, *, code: int = 1000) -> None:
        normalized = normalize_device_id(device_id)
        async with self._connection_lock:
            connection = self._connections.pop(normalized, None)
        if connection is not None:
            self.store.mark_device_disconnected(normalized)
            await self._close_socket(connection.websocket, code=code)

    async def _connection_for(self, device_id: str) -> BrowserBridgeConnection | None:
        async with self._connection_lock:
            return self._connections.get(device_id)

    async def _disconnect_connection(
        self,
        connection: BrowserBridgeConnection,
        *,
        code: int = 1011,
    ) -> None:
        await self.unregister(connection)
        await self._close_socket(connection.websocket, code=code)

    async def _send(self, connection: BrowserBridgeConnection, message: Mapping[str, Any]) -> bool:
        async with connection.send_lock:
            try:
                await connection.websocket.send_json(dict(message))
            except Exception:
                return False
        return True

    async def _send_error(
        self,
        connection: BrowserBridgeConnection,
        code: str = "MESSAGE_INVALID",
    ) -> None:
        await self._send(connection, {"type": "error", "code": code})

    @staticmethod
    async def _close_socket(websocket: WebSocket, *, code: int) -> None:
        try:
            await websocket.close(code=code)
        except Exception:
            pass

    @staticmethod
    def _outbox_message(device_id: str, item: Any) -> dict[str, Any]:
        return {
            "protocol_version": BRIDGE_PROTOCOL_VERSION,
            "type": item.message_type,
            "device_id": device_id,
            "sequence": item.sequence,
            "operation_id": item.operation_id,
            "payload": item.payload,
        }

    async def dispatch_pending(self, device_id: str) -> int:
        """Actively send every currently unacknowledged outbox item for a device."""

        normalized = normalize_device_id(device_id)
        async with self._dispatch_lock:
            connection = await self._connection_for(normalized)
            if connection is None:
                return 0
            items = self.store.fetch_unacked_outbox(
                normalized,
                connection.last_dispatched_sequence,
                limit=MAX_OUTBOX_BATCH,
            )
            sent = 0
            for item in items:
                if await self._connection_for(normalized) is not connection:
                    break
                if not await self._send(connection, self._outbox_message(normalized, item)):
                    await self._disconnect_connection(connection)
                    break
                connection.last_dispatched_sequence = item.sequence
                sent += 1
            return sent

    dispatch = dispatch_pending
    notify_device = dispatch_pending

    async def _operation_for_device(
        self,
        connection: BrowserBridgeConnection,
        operation_id: Any,
    ) -> Any:
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("operation is invalid")
        operation = self.store.get_operation(operation_id.strip())
        if operation is None or operation.device_id != connection.device_id:
            raise ValueError("operation is invalid")
        return operation

    @staticmethod
    def _sequence(value: Any, *, required: bool = True) -> int | None:
        if value is None and not required:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("sequence is invalid")
        return value

    async def _handle_ack(
        self,
        connection: BrowserBridgeConnection,
        message: Mapping[str, Any],
    ) -> None:
        sequence = self._sequence(message.get("sequence"))
        operation_id = message.get("operation_id")
        if operation_id is not None:
            await self._operation_for_device(connection, operation_id)
        ack_id = message.get("ack_id")
        if ack_id is not None and (not isinstance(ack_id, str) or not ack_id.strip()):
            raise ValueError("ack is invalid")
        payload = message.get("payload")
        if payload is not None and not isinstance(payload, Mapping):
            raise ValueError("ack is invalid")
        self.store.acknowledge_outbox(
            connection.device_id,
            sequence,
            operation_id=operation_id,
            ack_id=ack_id,
            payload=payload,
        )
        await self.dispatch_pending(connection.device_id)

    async def _handle_progress(
        self,
        connection: BrowserBridgeConnection,
        message: Mapping[str, Any],
    ) -> None:
        operation_id = message.get("operation_id")
        await self._operation_for_device(connection, operation_id)
        event_id = message.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            raise ValueError("progress is invalid")
        status = normalize_status(message.get("status"))
        if status in TERMINAL_STATUSES:
            raise ValueError("progress is invalid")
        payload = message.get("payload")
        if payload is not None and not isinstance(payload, Mapping):
            raise ValueError("progress is invalid")
        sequence = self._sequence(message.get("sequence"), required=False)
        event_type = message.get("event_type", "progress")
        if not isinstance(event_type, str):
            raise ValueError("progress is invalid")
        self.store.append_event(
            operation_id,
            event_id,
            status,
            payload,
            sequence=sequence,
            event_type=event_type,
        )

    async def _handle_result(
        self,
        connection: BrowserBridgeConnection,
        message: Mapping[str, Any],
    ) -> None:
        operation_id = message.get("operation_id")
        operation = await self._operation_for_device(connection, operation_id)
        status = normalize_status(message.get("status"))
        if status not in TERMINAL_STATUSES - {OperationStatus.CANCELLED}:
            raise ValueError("result is invalid")
        event_id = message.get("event_id")
        if event_id is not None and not isinstance(event_id, str):
            raise ValueError("result is invalid")
        result = message.get("result", message.get("payload"))
        if result is not None and not isinstance(result, Mapping):
            raise ValueError("result is invalid")
        error_code = message.get("error_code")
        if error_code is not None and not isinstance(error_code, str):
            raise ValueError("result is invalid")
        sequence = self._sequence(message.get("sequence"), required=False)
        if (
            status is OperationStatus.SUCCEEDED
            and operation.operation != OperationName.CAPTURE_OC_SNAPSHOT.value
        ):
            # A browser read is evidence, not the final business outcome.  Keep
            # the operation open until the MCP validator has matched the
            # application, checked confidence and committed (or rejected) the
            # database change.
            self.store.append_event(
                operation_id,
                event_id or f"observation-{operation_id}",
                OperationStatus.VALIDATING,
                {"result": result or {}},
                sequence=sequence,
                event_type="observation",
            )
        else:
            self.store.terminal_result(
                operation_id,
                result,
                status=status,
                event_id=event_id,
                sequence=sequence,
                error_code=error_code,
            )

    async def _handle_cancel(
        self,
        connection: BrowserBridgeConnection,
        message: Mapping[str, Any],
    ) -> None:
        sequence = self._sequence(message.get("sequence"), required=False)
        operation_id = message.get("operation_id")
        if sequence is not None:
            if operation_id is not None:
                await self._operation_for_device(connection, operation_id)
            outbox = self.store.acknowledge_outbox(
                connection.device_id,
                sequence,
                operation_id=operation_id,
                ack_id=message.get("ack_id"),
                payload=message.get("payload"),
            )
            if outbox.message_type != "operation.cancel":
                raise ValueError("cancel is invalid")
        else:
            await self._operation_for_device(connection, operation_id)
            reason = message.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise ValueError("cancel is invalid")
            self.store.cancel(operation_id, reason=reason)
        await self.dispatch_pending(connection.device_id)

    async def _handle_heartbeat(
        self,
        connection: BrowserBridgeConnection,
    ) -> None:
        await self._send(
            connection,
            {
                "protocol_version": BRIDGE_PROTOCOL_VERSION,
                "type": "heartbeat",
                "ack": True,
            },
        )
        await self.dispatch_pending(connection.device_id)

    async def handle_message(
        self,
        connection: BrowserBridgeConnection,
        message: Mapping[str, Any],
    ) -> None:
        """Process one authenticated device message."""

        if not isinstance(message, Mapping):
            await self._send_error(connection)
            return
        now = datetime.now(timezone.utc)
        connection.last_heartbeat = now
        self.store.mark_device_connected(connection.device_id, seen_at=now)
        message_type = message.get("type")
        try:
            if message_type == "ack":
                await self._handle_ack(connection, message)
            elif message_type == "progress":
                await self._handle_progress(connection, message)
            elif message_type == "result":
                await self._handle_result(connection, message)
            elif message_type == "cancel":
                await self._handle_cancel(connection, message)
            elif message_type == "heartbeat":
                await self._handle_heartbeat(connection)
            else:
                await self._send_error(connection, "MESSAGE_TYPE_UNSUPPORTED")
        except (KeyError, TypeError, ValueError):
            await self._send_error(connection)

    async def handle_websocket(self, websocket: WebSocket) -> None:
        """Run the challenge, authentication, dispatch, and receive loop."""

        await websocket.accept()
        challenge = secrets.token_urlsafe(32)
        connection: BrowserBridgeConnection | None = None
        try:
            await websocket.send_json(
                {
                    "protocol_version": BRIDGE_PROTOCOL_VERSION,
                    "type": "challenge",
                    "challenge": challenge,
                }
            )
            auth = await asyncio.wait_for(
                websocket.receive_json(),
                timeout=self.challenge_timeout_seconds,
            )
            if not isinstance(auth, Mapping):
                raise ValueError("authentication failed")
            allowed_keys = {
                "protocol_version",
                "type",
                "device_id",
                "challenge",
                "signature",
            }
            if set(auth) - allowed_keys or auth.get("type") != "auth":
                raise ValueError("authentication failed")
            device_id = normalize_device_id(auth.get("device_id"))
            if auth.get("challenge") != challenge:
                raise ValueError("authentication failed")
            token = await self._resolve_api_token()
            if not verify_auth_signature(token, challenge, device_id, auth.get("signature")):
                raise ValueError("authentication failed")

            await self.start()
            connection = await self.register(device_id, websocket)
            await self.dispatch_pending(device_id)
            while True:
                message = await websocket.receive_json()
                await self.handle_message(connection, message)
        except (WebSocketDisconnect, asyncio.TimeoutError, ValueError, TypeError):
            await self._close_socket(websocket, code=4401)
        except Exception:
            await self._close_socket(websocket, code=1011)
        finally:
            if connection is not None:
                await self.unregister(connection)

    def router(self, *, path: str = BROWSER_BRIDGE_PATH) -> APIRouter:
        """Create an APIRouter exposing the canonical local WebSocket path."""

        router = APIRouter()

        @router.websocket(path)
        async def browser_bridge_websocket(websocket: WebSocket) -> None:
            await self.handle_websocket(websocket)

        return router


def create_browser_bridge_router(
    store: BrowserBridgeStore | None = None,
    *,
    api_token: str | bytes | TokenProvider | Any = None,
    server: BrowserBridgeServer | None = None,
    path: str = BROWSER_BRIDGE_PATH,
) -> APIRouter:
    """Build the router without coupling the bridge to the main FastAPI module."""

    if server is None:
        if store is None:
            raise TypeError("store is required when server is not provided")
        server = BrowserBridgeServer(store, api_token)
    return server.router(path=path)


def install_browser_bridge(
    app: FastAPI,
    store: BrowserBridgeStore | None = None,
    *,
    api_token: str | bytes | TokenProvider | Any = None,
    server: BrowserBridgeServer | None = None,
    path: str = BROWSER_BRIDGE_PATH,
) -> BrowserBridgeServer:
    """Mount the bridge on an existing FastAPI application and return its server."""

    if server is None:
        if store is None:
            raise TypeError("store is required when server is not provided")
        server = BrowserBridgeServer(store, api_token)
    app.include_router(server.router(path=path))

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def browser_bridge_lifespan(application: FastAPI):
        await server.start()
        try:
            async with original_lifespan(application) as state:
                yield state
        finally:
            await server.stop()

    app.router.lifespan_context = browser_bridge_lifespan
    return server


__all__ = [
    "AUTH_DOMAIN",
    "BROWSER_BRIDGE_PATH",
    "BRIDGE_PROTOCOL_VERSION",
    "DEFAULT_DISPATCH_INTERVAL_SECONDS",
    "BrowserBridgeConnection",
    "BrowserBridgeServer",
    "build_auth_message",
    "compute_auth_signature",
    "create_auth_signature",
    "create_browser_bridge_router",
    "install_browser_bridge",
    "normalize_device_id",
    "verify_auth_signature",
]
