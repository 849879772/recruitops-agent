from types import SimpleNamespace

from fastapi.testclient import TestClient

from apps.api import main
from packages.codex_runtime import JsonRpcRemoteError


class FakeCodexService:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.thread_ids = {"thread-1"}

    async def start(self):
        self.started += 1

    async def stop(self):
        self.stopped += 1

    async def health(self):
        return SimpleNamespace(
            model_dump=lambda **_kwargs: {
                "ready": True,
                "state": "running",
                "detail": "initialized",
            }
        )

    async def mcp_status(self):
        return {
            "data": [
                {
                    "name": "recruitops",
                    "startupState": "ready",
                    "tools": {"capabilities": {}},
                }
            ],
            "nextCursor": None,
        }

    async def thread_start(self, **_params):
        return {"id": "thread-1"}

    async def thread_list(self, **_params):
        return {
            "data": [{"id": thread_id} for thread_id in sorted(self.thread_ids)],
            "next_cursor": None,
        }

    async def thread_read(self, thread_id: str, *, include_turns: bool = True):
        assert thread_id == "thread-1"
        assert include_turns is True
        return {"id": thread_id, "turns": []}

    async def thread_resume(self, thread_id: str):
        return {"id": thread_id}

    async def turn_start(self, thread_id: str, text: str):
        assert thread_id == "thread-1"
        assert text == "hello"
        return {"id": "turn-1"}

    async def turn_interrupt(self, thread_id: str, turn_id: str):
        assert (thread_id, turn_id) == ("thread-1", "turn-1")

    async def thread_delete(self, thread_id: str):
        assert thread_id == "thread-1"
        self.thread_ids.discard(thread_id)


class UnmaterializedThreadService(FakeCodexService):
    def __init__(self) -> None:
        super().__init__()
        self.include_turns_calls: list[bool] = []

    async def thread_read(self, thread_id: str, *, include_turns: bool = True):
        self.include_turns_calls.append(include_turns)
        if include_turns:
            raise JsonRpcRemoteError(
                code=-32602,
                message=(
                    f"thread {thread_id} is not materialized yet; "
                    "includeTurns is unavailable before first user message"
                ),
            )
        return {"id": thread_id}


class UnloadedThreadService(FakeCodexService):
    def __init__(self) -> None:
        super().__init__()
        self.loaded = False
        self.read_calls = 0
        self.resume_calls = 0

    async def thread_read(self, thread_id: str, *, include_turns: bool = True):
        self.read_calls += 1
        if not self.loaded:
            raise JsonRpcRemoteError(code=-32602, message=f"thread not loaded: {thread_id}")
        return {"id": thread_id, "turns": [{"id": "turn-1", "items": []}]}

    async def thread_resume(self, thread_id: str):
        self.resume_calls += 1
        self.loaded = True
        return {"id": thread_id}


def test_codex_routes_are_explicitly_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(codex_runtime_enabled=False),
    )

    with TestClient(main.app) as client:
        assert client.get("/api/codex/health").json() == {
            "enabled": False,
            "ready": False,
            "state": "disabled",
        }
        response = client.post("/api/codex/threads", json={})
        assert response.status_code == 503
        assert client.post(
            "/api/codex/threads/thread-1/turns/stream",
            json={"text": "hello"},
        ).status_code == 503


def test_codex_lifecycle_and_thread_turn_routes_use_one_service(monkeypatch) -> None:
    service = FakeCodexService()
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(codex_runtime_enabled=True),
    )
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)

    with TestClient(main.app) as client:
        assert service.started == 1
        assert client.get("/api/codex/health").json()["ready"] is True
        assert client.get("/api/codex/mcp-status").json()["data"][0]["name"] == "recruitops"
        assert client.post("/api/codex/threads", json={}).json()["id"] == "thread-1"
        assert client.get("/api/codex/threads?limit=10").json()["data"][0]["id"] == "thread-1"
        assert client.get("/api/codex/threads/thread-1").json()["id"] == "thread-1"
        assert client.post("/api/codex/threads/thread-1/resume").json()["id"] == "thread-1"
        assert client.post(
            "/api/codex/threads/thread-1/turns",
            json={"text": "hello"},
        ).json()["id"] == "turn-1"
        assert client.post(
            "/api/codex/threads/thread-1/interrupt",
            json={"turn_id": "turn-1"},
        ).json()["status"] == "interrupt_requested"
        assert client.delete("/api/codex/threads/thread-1").json() == {
            "status": "deleted",
            "thread_id": "thread-1",
        }
        assert client.get("/api/codex/threads?limit=10").json()["data"] == []

    assert service.stopped == 1


def test_codex_thread_read_retries_unmaterialized_thread_without_turns(monkeypatch) -> None:
    service = UnmaterializedThreadService()
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(codex_runtime_enabled=True),
    )
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)

    with TestClient(main.app) as client:
        response = client.get("/api/codex/threads/thread-1")

    assert response.status_code == 200
    assert response.json() == {"id": "thread-1"}
    assert service.include_turns_calls == [True, False]


def test_codex_thread_read_resumes_threads_unloaded_after_api_restart(monkeypatch) -> None:
    service = UnloadedThreadService()
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(codex_runtime_enabled=True),
    )
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)

    with TestClient(main.app) as client:
        response = client.get("/api/codex/threads/thread-1")

    assert response.status_code == 200
    assert response.json()["turns"][0]["id"] == "turn-1"
    assert service.resume_calls == 1
    assert service.read_calls == 2


def test_codex_health_exposes_a_strict_auto_compaction_boundary(monkeypatch) -> None:
    service = FakeCodexService()
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            codex_runtime_enabled=True,
            codex_model_context_window=1_000_000,
            codex_model_auto_compact_token_limit=96_000,
        ),
    )
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)

    with TestClient(main.app) as client:
        context = client.get("/api/codex/health").json()["context_management"]

    assert context == {
        "context_window_tokens": 1_000_000,
        "auto_compact_token_limit": 96_000,
    }
    assert 0 < context["auto_compact_token_limit"] < context["context_window_tokens"]
