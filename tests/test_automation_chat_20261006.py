"""Scheduled chats use temporary SQLite and fake runtime events only."""

import asyncio
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api import main
from apps.api.automation import CodexAutomationExecutor
from packages.automation import AutomationStore, LocalAutomationWorker
from packages.automation.conversations import read_conversation, thread_updated_at, _payload
from packages.storage import Storage, TaskRun
from tests.test_automation_executor import _Service, _event
from packages.codex_runtime.events import CodexEventType
from packages.codex_runtime import JsonRpcRemoteError


def setup(tmp_path, task_id="daily_recruitment_intelligence"):
    database_url = f"sqlite:///{tmp_path / 'scheduled-chat.db'}"
    store = AutomationStore(Storage.from_url(database_url, initialize=True))
    schedule = store.upsert_daily(task_id=task_id, task_label="测试定时任务",
        start_time=time(3), now=datetime(2000, 1, 1, tzinfo=timezone.utc))
    settings = SimpleNamespace(database_url=database_url, agent_root=tmp_path,
                               codex_runtime_enabled=True, write_enabled=True)
    return store, schedule, settings


class DirectService:
    def __init__(self):
        self.created = []

    async def thread_start(self):
        thread = SimpleNamespace(id=f"scheduled-thread-{len(self.created) + 1}")
        self.created.append(thread.id)
        return thread

    def subscribe(self, *_):
        pytest.fail("A direct handler must not create a model turn")

    async def thread_list(self, **_kwargs):
        return {"data": [], "next_cursor": None}

    async def thread_read(self, *_args, **_kwargs):
        return {"id": _args[0], "turns": []}


@pytest.mark.parametrize("task_id", ["daily_recruitment_intelligence", "crawler_health"])
def test_direct_task_gets_visible_started_and_completed_chat_without_model(tmp_path, monkeypatch, task_id):
    store, schedule, settings = setup(tmp_path, task_id)
    service, calls = DirectService(), []

    def handler(context):
        calls.append(context)
        execution = store.executions(schedule.id)[0]
        assert execution.thread_id == context.metadata["thread_id"]
        assert not service.created
        assert context.metadata["task_id"] == task_id
        assert context.metadata["automation_execution_id"] == context.run_id == execution.id
        started = read_conversation(store.storage, execution.thread_id)
        assert [message["role"] for message in started["messages"]] == ["user", "assistant"]
        assert started["automation"]["status"] == "running"
        return {"status": "completed" if task_id == "daily_recruitment_intelligence" else "observed",
                "company_total": 2, "integration_status_counts": {"connected": 2}}

    executor = CodexAutomationExecutor(service, store, settings=settings, task_handlers={task_id: handler})
    worker = LocalAutomationWorker(store, executor)
    assert asyncio.run(worker.run_once())
    assert not asyncio.run(worker.run_once())
    assert len(calls) == 1 and not service.created
    execution = store.executions(schedule.id)[0]
    chat = read_conversation(store.storage, execution.thread_id)
    assert chat["automation"]["status"] == "succeeded"
    assert chat["automation"]["run_id"] == execution.id
    assert len(chat["messages"]) == 3
    assert chat["messages"][-1]["result"]["details"] is not None
    assert execution.turn_id is None

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_storage_engine", lambda: store.storage.engine)
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)
    client = TestClient(main.app)
    listed = client.get("/api/codex/threads").json()["data"]
    assert [item["id"] for item in listed] == [execution.thread_id]
    assert client.get(f"/api/codex/threads/{execution.thread_id}").json()["messages"] == chat["messages"]
    assert client.get(f"/api/codex/threads/{execution.thread_id}?include_turns=false").json()["messages"] == []


def test_reentering_a_claim_cannot_create_another_thread_or_repeat_handler(tmp_path):
    store, schedule, settings = setup(tmp_path)
    task = store.claim_due()
    service, calls = DirectService(), []
    def handler(context):
        calls.append(context)
        return {"status": "completed"}
    executor = CodexAutomationExecutor(service, store, settings=settings,
        task_handlers={task.task_id: handler})
    first = asyncio.run(executor(task))
    store.complete(task.execution_id, status=first.status, result_summary=first.summary,
                   thread_id=first.thread_id, result_details=first.details)
    repeated = asyncio.run(executor(task))
    assert repeated.thread_id == first.thread_id and repeated.status == first.status
    assert len(calls) == 1 and not service.created
    assert len(read_conversation(store.storage, first.thread_id)["messages"]) == 3


def test_handler_failure_keeps_thread_and_persists_failure_message(tmp_path):
    store, schedule, settings = setup(tmp_path, "crawler_health")
    service = DirectService()
    def handler(_context):
        raise RuntimeError("fixture handler failed")
    executor = CodexAutomationExecutor(service, store, settings=settings,
        task_handlers={"crawler_health": handler})
    assert asyncio.run(LocalAutomationWorker(store, executor).run_once())
    execution = store.executions(schedule.id)[0]
    assert execution.status == "failed" and execution.thread_id
    assert not service.created
    chat = read_conversation(store.storage, execution.thread_id)
    assert chat["automation"]["status"] == "failed"
    assert "fixture handler failed" in chat["messages"][-1]["text"]


def test_turn_start_failure_keeps_already_created_chat(tmp_path):
    store, schedule, settings = setup(tmp_path, "application_progress")
    class FailingService(_Service):
        async def turn_start(self, thread_id, prompt):
            assert store.executions(schedule.id)[0].thread_id == thread_id
            assert f'thread_id="{thread_id}"' in prompt
            raise RuntimeError("fixture turn failed")
    service = FailingService([])
    executor = CodexAutomationExecutor(service, store, settings=settings)
    assert asyncio.run(LocalAutomationWorker(store, executor).run_once())
    execution = store.executions(schedule.id)[0]
    assert execution.status == "failed" and execution.thread_id == "thread-1"
    assert execution.turn_id is None and service.subscription.closed
    assert "fixture turn failed" in read_conversation(store.storage, "thread-1")["messages"][-1]["text"]


@pytest.mark.parametrize("task_id", ["application_progress", "recruitment_mailbox"])
def test_chat_tasks_create_one_turn_with_thread_routing_context(tmp_path, task_id):
    store, schedule, settings = setup(tmp_path, task_id)
    class RoutedService(_Service):
        async def turn_start(self, thread_id, prompt):
            assert f'thread_id="{thread_id}"' in prompt
            assert store.executions(schedule.id)[0].thread_id == thread_id
            return await super().turn_start(thread_id, prompt)
    service = RoutedService([_event(CodexEventType.TEXT_DELTA, text="执行已完成"),
                             _event(CodexEventType.TURN_COMPLETED)])
    executor = CodexAutomationExecutor(service, store, settings=settings)
    assert asyncio.run(LocalAutomationWorker(store, executor).run_once())
    execution = store.executions(schedule.id)[0]
    assert execution.status == "succeeded"
    assert execution.thread_id == "thread-1" and execution.turn_id == "turn-1"
    assert read_conversation(store.storage, execution.thread_id)["automation"]["direct"] is False


def test_conversation_timestamps_preserve_aware_offsets():
    instant = datetime(2026, 10, 6, 16, 30, tzinfo=timezone(timedelta(hours=8)))
    thread = SimpleNamespace(id="thread", title="task", created_at=instant,
                             updated_at=instant, context={"automation": {}})
    payload = _payload(thread)
    assert payload["createdAt"] == payload["updatedAt"] == instant.astimezone(timezone.utc).timestamp()
    thread.updated_at = instant.astimezone(timezone.utc).replace(tzinfo=None)
    assert _payload(thread)["updatedAt"] == payload["updatedAt"]
    message = SimpleNamespace(id="message", role="assistant", body="fixture",
        created_at=thread.updated_at, task_id=None, result=None)
    assert _payload(thread, [message])["messages"][0]["createdAt"] == payload["updatedAt"]


def test_restart_closes_local_task_chat_without_reexecuting_it(tmp_path):
    store, _, settings = setup(tmp_path)
    task = store.claim_due()
    store.mark_running_context(task.execution_id, thread_id="interrupted-thread", direct=True)
    assert store.recover_interrupted() == 1
    chat = read_conversation(store.storage, "interrupted-thread")
    assert chat["automation"]["status"] == "failed"
    assert "restarted" in chat["messages"][-1]["text"]
    service = DirectService()
    result = asyncio.run(CodexAutomationExecutor(service, store, settings=settings,
        task_handlers={})(task))
    assert result.status == "failed" and result.thread_id == "interrupted-thread"
    assert service.created == []


def test_direct_history_merges_real_followup_turns_and_deduplicates_list(tmp_path, monkeypatch):
    store, _, settings = setup(tmp_path)
    task = store.claim_due()
    store.mark_running_context(task.execution_id, thread_id="direct-thread", direct=True)
    class HistoryService(DirectService):
        async def thread_list(self, **_kwargs):
            return {"data": [{"id": "direct-thread", "preview": "runtime preview"}], "next_cursor": None}
        async def thread_read(self, thread_id, **_kwargs):
            return {"id": thread_id, "turns": [{"id": "followup", "items": []}],
                    "updatedAt": "2099-01-01T00:00:00Z"}
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_storage_engine", lambda: store.storage.engine)
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: HistoryService())
    client = TestClient(main.app)
    listed = client.get("/api/codex/threads").json()["data"]
    assert [item["id"] for item in listed] == ["direct-thread"]
    assert listed[0]["preview"] == "runtime preview"
    history = client.get("/api/codex/threads/direct-thread").json()
    assert history["turns"][0]["id"] == "followup" and len(history["messages"]) == 2
    assert history["updatedAt"] == datetime(2099, 1, 1, tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize("direct", [False, True])
def test_persisted_failure_is_visible_when_runtime_history_is_missing(tmp_path, monkeypatch, direct):
    store, _, settings = setup(tmp_path)
    task = store.claim_due()
    store.mark_running_context(task.execution_id, thread_id="failed-thread", direct=direct)
    store.complete(task.execution_id, status="failed", error="fixture startup failed")
    class MissingHistoryService(DirectService):
        async def thread_read(self, *_args, **_kwargs):
            raise RuntimeError("runtime history unavailable")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_storage_engine", lambda: store.storage.engine)
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: MissingHistoryService())
    response = TestClient(main.app).get("/api/codex/threads/failed-thread")
    assert response.status_code == 200
    assert "fixture startup failed" in response.json()["messages"][-1]["text"]


def test_local_task_list_survives_runtime_outage_using_shared_pool(tmp_path, monkeypatch):
    store, _, settings = setup(tmp_path)
    task = store.claim_due()
    store.mark_running_context(task.execution_id, thread_id="offline-thread", direct=True)
    class OfflineService(DirectService):
        async def thread_list(self, **_kwargs):
            raise RuntimeError("runtime unavailable")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_storage_engine", lambda: store.storage.engine)
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: OfflineService())
    monkeypatch.setattr(Storage, "from_url", lambda *_args, **_kwargs: pytest.fail("reuse the shared engine"))
    client = TestClient(main.app)
    for _ in range(3):
        response = client.get("/api/codex/threads?limit=1")
        assert response.status_code == 200
        assert response.json()["history_status"] == "local_automation"
        assert [row["id"] for row in response.json()["data"]] == ["offline-thread"]
        assert client.get("/api/codex/threads/offline-thread").status_code == 200


def test_mixed_runtime_timestamp_formats_sort_by_the_same_instant():
    instant = datetime(2026, 10, 6, 16, 30, tzinfo=timezone(timedelta(hours=8)))
    for value in [instant.isoformat(), instant.timestamp(), str(instant.timestamp()), instant.timestamp() * 1000]:
        assert thread_updated_at({"updatedAt": value}) == instant.timestamp()


def test_first_page_local_merge_keeps_runtime_cursor_and_all_remote_rows(tmp_path, monkeypatch):
    store, _, settings = setup(tmp_path)
    task = store.claim_due()
    store.mark_running_context(task.execution_id, thread_id="local-only-thread", direct=True)
    class PagedService(DirectService):
        async def thread_list(self, *, cursor, limit, archived):
            assert limit == 1
            return {"data": [{"id": "second-runtime" if cursor else "first-runtime", "updatedAt": 1}],
                    "next_cursor": None if cursor else "runtime-next"}
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_storage_engine", lambda: store.storage.engine)
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: PagedService())
    client = TestClient(main.app)
    first = client.get("/api/codex/threads?limit=1").json()
    assert first["next_cursor"] == "runtime-next"
    assert {row["id"] for row in first["data"]} == {"local-only-thread", "first-runtime"}
    second = client.get("/api/codex/threads?limit=1&cursor=runtime-next").json()
    assert [row["id"] for row in second["data"]] == ["second-runtime"]


@pytest.mark.parametrize("missing_runtime", [False, True])
def test_deleted_local_task_chat_does_not_reappear_after_completion(tmp_path, monkeypatch, missing_runtime):
    store, _, settings = setup(tmp_path)
    task = store.claim_due()
    store.mark_running_context(task.execution_id, thread_id="deleted-thread", direct=True)
    class DeletionService(DirectService):
        async def thread_list(self, **_kwargs):
            # A cached runtime row must not resurrect the local tombstone.
            return {"data": [{"id": "deleted-thread"}], "next_cursor": None}
        async def thread_delete(self, thread_id):
            assert thread_id == "deleted-thread"
            if missing_runtime:
                raise JsonRpcRemoteError(code=-32602, message="no rollout found")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_storage_engine", lambda: store.storage.engine)
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: DeletionService())
    client = TestClient(main.app)
    assert client.delete("/api/codex/threads/deleted-thread").status_code == 200
    store.complete(task.execution_id, status="succeeded", result_summary="完成")
    assert read_conversation(store.storage, "deleted-thread") is None
    assert client.get("/api/codex/threads").json()["data"] == []
    assert client.get("/api/codex/threads?cursor=next").json()["data"] == []


@pytest.mark.parametrize("task_id", ["daily_recruitment_intelligence", "application_progress"])
@pytest.mark.parametrize("status", ["succeeded", "failed", "blocked", "interrupted"])
def test_completion_settles_only_the_matching_direct_task_run(tmp_path, task_id, status):
    store, _, _ = setup(tmp_path, task_id)
    task = store.claim_due()
    with store.storage.transaction(write=True) as session:
        for run_id in [task.execution_id, "unrelated-child-run"]:
            session.add(TaskRun(id=run_id, task_type=task_id, status="running",
                               user_request="fixture", source="fixture"))
    if status == "interrupted":
        assert store.recover_interrupted() == 1
    else:
        store.complete(task.execution_id, status=status)
    with store.storage.session() as session:
        run = session.get(TaskRun, task.execution_id)
        expected = "running" if task_id == "application_progress" else "success" if status == "succeeded" else "failed"
        assert run.status == expected
        assert session.get(TaskRun, "unrelated-child-run").status == "running"
