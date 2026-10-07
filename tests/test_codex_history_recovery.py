import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api import main
from packages.codex_runtime.client import (
    CODEX_STDIO_READER_LIMIT, JsonRpcClientError, JsonRpcProtocolError,
    JsonRpcRemoteError, JsonRpcStdioClient,
)


class Service:
    def __init__(self, error):
        self.error = error
        self.calls = []

    async def thread_read(self, thread_id, *, include_turns=True):
        self.calls.append("read")
        raise self.error

    async def thread_resume(self, thread_id):
        self.calls.append("resume")
        raise self.error


@pytest.mark.parametrize("suffix", ["", "/resume"])
@pytest.mark.parametrize("error,status,code", [
    (TimeoutError("secret-body"), 504, "history_timeout"),
    (JsonRpcRemoteError(code=-32602, message="no rollout found at D:/private/secret-body"), 404, "history_not_found"),
    (JsonRpcRemoteError(code=-32603, message="failed to open rollout: os error 2 secret-body"), 404, "history_not_found"),
    (JsonRpcClientError("process closed secret-body"), 503, "history_runtime_unavailable"),
    (JsonRpcProtocolError("JSON-RPC message exceeds reader limit secret-body"), 413, "history_too_large"),
    (JsonRpcRemoteError(code=-32602, message="bad state secret-body"), 502, "history_runtime_error"),
    (ValueError("secret-body"), 500, "history_internal_error"),
])
def test_read_and_resume_failures_are_classified_and_redacted(monkeypatch, caplog, suffix, error, status, code):
    service = Service(error)
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(codex_runtime_enabled=True))
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)
    # No lifespan: no runtime, database, or model is started by this regression.
    client = TestClient(main.app)
    response = client.post(f"/api/codex/threads/thread-private{suffix}") if suffix else client.get("/api/codex/threads/thread-private")
    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail["code"] == code
    assert len(detail["request_id"]) == 12
    assert detail["request_id"] in caplog.text
    assert "secret-body" not in caplog.text + response.text
    assert "thread-private" not in caplog.text
    assert service.calls == ["resume" if suffix else "read"]


def test_history_timeout_is_bounded_and_does_not_start_turn(monkeypatch):
    class WaitingService:
        async def thread_read(self, *_args, **_kwargs):
            await asyncio.Event().wait()
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(codex_runtime_enabled=True))
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: WaitingService())
    monkeypatch.setattr(main, "HISTORY_REQUEST_TIMEOUT_SECONDS", 0.01)
    response = TestClient(main.app).get("/api/codex/threads/waiting")
    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "history_timeout"


def test_history_transport_accepts_six_megabytes_and_still_has_a_finite_limit():
    async def scenario():
        reader = asyncio.StreamReader(limit=CODEX_STDIO_READER_LIMIT)
        client = JsonRpcStdioClient(SimpleNamespace(stdout=reader))
        future = asyncio.get_running_loop().create_future()
        client._pending[1] = future
        payload = {"id": 1, "result": {"history": "x" * (6 * 1024 * 1024)}}
        reader.feed_data((json.dumps(payload) + "\n").encode())
        reader.feed_eof()
        await client._read_loop()
        assert (await future)["history"] == payload["result"]["history"]
        assert client.failure is None
    asyncio.run(scenario())
    assert 6 * 1024 * 1024 < CODEX_STDIO_READER_LIMIT <= 32 * 1024 * 1024


def test_oversized_history_has_explicit_protocol_error(monkeypatch):
    from packages.codex_runtime import client as transport
    async def scenario():
        reader = asyncio.StreamReader(limit=64)
        client = JsonRpcStdioClient(SimpleNamespace(stdout=reader))
        future = asyncio.get_running_loop().create_future()
        client._pending[1] = future
        reader.feed_data(b"x" * 200)
        reader.feed_eof()
        await client._read_loop()
        with pytest.raises(JsonRpcProtocolError, match="exceeds reader limit"):
            await future
        assert client.pending_count == 0
    monkeypatch.setattr(transport, "CODEX_STDIO_READER_LIMIT", 64)
    asyncio.run(scenario())
