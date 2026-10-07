"""Local scheduled reports survive startup faults; followups never replay the task."""

from datetime import datetime, time, timezone
import asyncio
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api import main
from packages.automation import AutomationStore
from packages.automation.conversations import read_conversation
from packages.codex_runtime.events import CodexEvent, CodexEventType
from packages.codex_runtime import JsonRpcRemoteError
from packages.storage import ConversationMessage, Storage


@pytest.fixture
def task_store(tmp_path, monkeypatch):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'local-tasks.db'}", initialize=True)
    store = AutomationStore(storage)
    schedule = store.upsert_daily(task_id="daily_recruitment_intelligence", task_label="全量抓取",
        start_time=time(5, 14), now=datetime(2000, 1, 1, tzinfo=timezone.utc))
    task = store.claim_due()
    settings = SimpleNamespace(codex_runtime_enabled=True, database_url=str(storage.engine.url))
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "get_storage_engine", lambda: storage.engine)
    return store, schedule, task


def test_local_task_conversation_is_stable_and_idempotent(task_store):
    store, _, task = task_store
    thread_id = store.ensure_task_conversation(task.execution_id, direct=True)
    assert store.ensure_task_conversation(task.execution_id, direct=True) == thread_id
    chat = read_conversation(store.storage, thread_id)
    assert chat["automation"]["local_only"] is True
    assert chat["automation"]["run_id"] == task.execution_id
    assert len(chat["messages"]) == 2
    assert "准备执行" in chat["messages"][1]["text"]
    store.complete(task.execution_id, status="succeeded", result_summary="新增 2 条",
                   result_details={"new": 2})
    assert store.ensure_task_conversation(task.execution_id, direct=True) == thread_id
    chat = read_conversation(store.storage, thread_id)
    assert len(chat["messages"]) == 3
    assert chat["messages"][-1]["result"]["details"] == {"new": 2}


def test_concurrent_workers_atomically_claim_start_once(task_store):
    store, _, task = task_store
    another = AutomationStore(Storage.from_url(str(store.storage.engine.url)))
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(worker.claim_execution_start, task.execution_id)
                   for worker in (store, another)]
        assert sorted(future.result() for future in futures) == [False, True]
    assert store.execution(task.execution_id).result_summary == "正在准备执行定时任务…"
    assert store.claim_execution_start(task.execution_id) is False
    another.storage.engine.dispose()


def test_startup_retries_do_not_create_competing_local_and_runtime_chats(task_store):
    store, schedule, task = task_store
    store.record_startup_retry(task.execution_id, attempt=1, max_attempts=3,
                              error="timeout api_key=sk-fixture-secret recipient@example.com")
    execution = store.execution(task.execution_id)
    assert execution.thread_id is None
    assert "准备重试（1/3）" in execution.result_summary
    assert "sk-fixture-secret" not in execution.error
    assert "recipient@example.com" not in execution.error
    assert store.list()[0].last_error == execution.error
    store.mark_running_context(task.execution_id, thread_id="real-runtime", direct=False)
    store.complete(task.execution_id, status="succeeded", result_summary="复核完成")
    chat = read_conversation(store.storage, "real-runtime")
    assert len(chat["messages"]) == 3
    assert chat["automation"].get("local_only") is not True
    assert store.executions(schedule.id)[0].error is None


@pytest.mark.parametrize("recovery", [False, True])
def test_failure_before_any_thread_is_visible(task_store, recovery):
    store, _, task = task_store
    if recovery:
        assert store.recover_interrupted() == 1
    else:
        store.complete(task.execution_id, status="failed", error="工具连接超时")
    execution = store.execution(task.execution_id)
    chat = read_conversation(store.storage, execution.thread_id)
    assert chat["automation"]["local_only"] is True
    assert chat["automation"]["status"] == "failed"
    assert execution.error in chat["messages"][-1]["text"]


def test_two_tasks_keep_distinct_local_reports(task_store):
    store, _, first = task_store
    store.upsert_daily(task_id="crawler_health", task_label="网站健康检查", start_time=time(6),
                       now=datetime(2000, 1, 1, tzinfo=timezone.utc))
    second = store.claim_due()
    first_id = store.ensure_task_conversation(first.execution_id, direct=True)
    second_id = store.ensure_task_conversation(second.execution_id, direct=True)
    assert first_id != second_id
    store.complete(first.execution_id, status="succeeded", result_summary="抓取报告")
    store.complete(second.execution_id, status="failed", result_summary="健康检查报告")
    assert read_conversation(store.storage, first_id)["messages"][-1]["text"] == "抓取报告"
    assert read_conversation(store.storage, second_id)["messages"][-1]["text"] == "健康检查报告"


def test_local_list_open_resume_delete_work_during_runtime_outage(task_store, monkeypatch):
    store, _, task = task_store
    store.complete(task.execution_id, status="failed", error="工具连接超时")
    thread_id = store.execution(task.execution_id).thread_id

    class OfflineService:
        async def thread_list(self, **_):
            raise ConnectionError("MCP unavailable")

        def __getattr__(self, name):
            pytest.fail(f"local task report must not call runtime {name}")

    monkeypatch.setattr(main, "get_codex_bff_service", lambda: OfflineService())
    client = TestClient(main.app)
    assert client.get("/api/codex/threads").json()["data"][0]["id"] == thread_id
    detail = client.get(f"/api/codex/threads/{thread_id}")
    assert detail.status_code == 200
    assert detail.json()["messages"][-1]["text"].endswith("工具连接超时")
    assert client.post(f"/api/codex/threads/{thread_id}/resume").status_code == 200
    assert client.delete(f"/api/codex/threads/{thread_id}").status_code == 200
    assert read_conversation(store.storage, thread_id) is None


class FollowupService:
    def __init__(self):
        self.created = 0
        self.prompts = []
        self.subscription_closed = False
        self.resumed = []

    async def thread_start(self):
        self.created += 1
        return {"id": "runtime-followup"}

    async def thread_resume(self, thread_id):
        self.resumed.append(thread_id)
        return {"id": thread_id}

    async def turn_start(self, thread_id, prompt):
        assert thread_id == "runtime-followup"
        self.prompts.append(prompt)
        return {"id": "followup-turn"}

    def subscribe(self, thread_id):
        assert thread_id == "runtime-followup"
        owner = self

        class Subscription:
            def close(self):
                owner.subscription_closed = True

            async def __aiter__(self):
                for kind, text in [(CodexEventType.TEXT_DELTA, "本轮有两条新增。"),
                                   (CodexEventType.TURN_COMPLETED, None)]:
                    yield CodexEvent(event_type=kind, method=kind.value, thread_id=thread_id,
                                     turn_id="followup-turn", text=text)

        return Subscription()


@pytest.mark.parametrize("streaming", [False, True])
def test_local_report_followup_creates_real_runtime_once_with_report_context(task_store, monkeypatch, streaming):
    store, _, task = task_store
    store.complete(task.execution_id, status="succeeded", result_summary="新增 2 条岗位")
    thread_id = store.execution(task.execution_id).thread_id
    before = read_conversation(store.storage, thread_id)["messages"]
    service = FollowupService()
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)
    client = TestClient(main.app)
    suffix = "/stream" if streaming else ""
    response = client.post(f"/api/codex/threads/{thread_id}/turns{suffix}", json={"text": "解释本次结果"})
    assert response.status_code == 200
    if streaming:
        assert response.headers["X-RecruitOps-Thread-ID"] == "runtime-followup"
        assert '"source_thread_id": "' + thread_id + '"' in response.text
        assert "本轮有两条新增" in response.text
        assert service.subscription_closed
    else:
        assert response.json()["thread_id"] == "runtime-followup"
        assert response.json()["source_thread_id"] == thread_id
    assert "新增 2 条岗位" in service.prompts[0]
    assert "不得因为加载记录再次启动任务" in service.prompts[0]
    assert "执行定时任务：" not in service.prompts[0]
    assert task.execution_id in service.prompts[0]
    assert '"thread_id": "runtime-followup"' in service.prompts[0]
    assert service.created == 1
    client.post(f"/api/codex/threads/{thread_id}/turns", json={"text": "还有失败吗"})
    assert service.created == 1
    assert read_conversation(store.storage, thread_id)["messages"] == before
    with store.storage.session() as session:
        assert len(list(session.scalars(select(ConversationMessage)))) == 3


@pytest.mark.parametrize("error", [TimeoutError("thread start timeout"), RuntimeError("MCP connection failed")])
def test_followup_startup_fault_is_actionable_and_keeps_original_report(task_store, monkeypatch, error):
    store, _, task = task_store
    store.complete(task.execution_id, status="failed", result_summary="本轮尚未启动")
    thread_id = store.execution(task.execution_id).thread_id
    before = read_conversation(store.storage, thread_id)

    class OfflineFollowup:
        async def thread_start(self):
            raise error

    monkeypatch.setattr(main, "get_codex_bff_service", lambda: OfflineFollowup())
    response = TestClient(main.app).post(f"/api/codex/threads/{thread_id}/turns/stream", json={"text": "解释失败原因"})
    assert response.status_code in {503, 504}
    assert response.json()["detail"]["message"]
    assert read_conversation(store.storage, thread_id) == before


def test_followup_deleted_before_runtime_creation_returns_not_found(task_store, monkeypatch):
    from packages.automation import conversations

    store, _, task = task_store
    thread_id = store.ensure_task_conversation(task.execution_id, direct=True)
    original = conversations.read_conversation
    calls = 0

    def disappearing(storage, selected, **kwargs):
        nonlocal calls
        calls += 1
        return original(storage, selected, **kwargs) if calls == 1 else None

    monkeypatch.setattr(conversations, "read_conversation", disappearing)
    service = FollowupService()
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)
    response = TestClient(main.app).post(f"/api/codex/threads/{thread_id}/turns", json={"text": "解释结果"})
    assert response.status_code == 404
    assert service.created == 0


def test_followup_resumes_bound_runtime_after_restart_before_starting_turn(task_store, monkeypatch):
    from packages.automation.conversations import bind_followup_thread

    store, _, task = task_store
    thread_id = store.ensure_task_conversation(task.execution_id, direct=True)
    bind_followup_thread(store.storage, thread_id, "runtime-followup")

    class RestartedService(FollowupService):
        loaded = False

        async def thread_resume(self, selected):
            self.resumed.append(selected)
            self.loaded = True
            return {"id": selected}

        async def turn_start(self, selected, prompt):
            assert self.loaded, "the old runtime thread starts unloaded after restart"
            return await super().turn_start(selected, prompt)

    service = RestartedService()
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)
    response = TestClient(main.app).post(f"/api/codex/threads/{thread_id}/turns", json={"text": "结果怎么样"})
    assert response.status_code == 200
    assert service.resumed == ["runtime-followup"]
    assert service.created == 0 and len(service.prompts) == 1


@pytest.mark.parametrize("missing", ["thread not found", "no rollout found", "unknown thread"])
def test_deleted_bound_runtime_creates_new_followup_without_replaying_old_turn(task_store, monkeypatch, missing):
    from packages.automation.conversations import bind_followup_thread

    store, _, task = task_store
    thread_id = store.ensure_task_conversation(task.execution_id, direct=True)
    bind_followup_thread(store.storage, thread_id, "deleted-runtime")
    before = read_conversation(store.storage, thread_id)["messages"]

    class DeletedService(FollowupService):
        async def thread_resume(self, selected):
            self.resumed.append(selected)
            raise JsonRpcRemoteError(code=-32602, message=f"{missing}: {selected}")

    service = DeletedService()
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)
    response = TestClient(main.app).post(f"/api/codex/threads/{thread_id}/turns", json={"text": "解释本次结果"})
    assert response.status_code == 200
    assert service.resumed == ["deleted-runtime"]
    assert service.created == 1 and len(service.prompts) == 1
    assert "执行定时任务：" not in service.prompts[0]
    assert read_conversation(store.storage, thread_id)["messages"] == before
    assert read_conversation(store.storage, thread_id)["automation"]["followup_thread_id"] == "runtime-followup"


def test_bound_runtime_resume_fault_does_not_start_another_thread_or_turn(task_store, monkeypatch):
    from packages.automation.conversations import bind_followup_thread

    store, _, task = task_store
    thread_id = store.ensure_task_conversation(task.execution_id, direct=True)
    bind_followup_thread(store.storage, thread_id, "runtime-followup")

    class OfflineService(FollowupService):
        async def thread_resume(self, _):
            raise ConnectionError("runtime transport offline")

    service = OfflineService()
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)
    response = TestClient(main.app).post(f"/api/codex/threads/{thread_id}/turns", json={"text": "解释结果"})
    assert response.status_code == 503
    assert service.created == 0 and service.prompts == []


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("error", [TimeoutError("turn start timeout"), ConnectionError("turn transport disconnected")])
def test_followup_turn_start_fault_is_not_acknowledged_or_retried(task_store, monkeypatch, streaming, error):
    store, _, task = task_store
    thread_id = store.ensure_task_conversation(task.execution_id, direct=True)
    before = read_conversation(store.storage, thread_id)["messages"]

    class FailedTurn(FollowupService):
        subscribed = False
        attempts = 0

        def subscribe(self, selected):
            self.subscribed = True
            return super().subscribe(selected)

        async def turn_start(self, selected, prompt):
            assert self.subscribed is streaming, "stream events must be subscribed before turn_start"
            self.attempts += 1
            raise error

    service = FailedTurn()
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)
    suffix = "/stream" if streaming else ""
    response = TestClient(main.app).post(f"/api/codex/threads/{thread_id}/turns{suffix}", json={"text": "解释結果"})
    assert response.status_code in {503, 504}
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"]["code"] == "followup_start_unconfirmed"
    assert "先查看会话运行状态" in response.json()["detail"]["message"]
    assert service.attempts == 1
    assert service.subscription_closed is streaming
    assert read_conversation(store.storage, thread_id)["messages"] == before


def test_followup_resume_has_bounded_wait(task_store, monkeypatch):
    from packages.automation.conversations import bind_followup_thread

    store, _, task = task_store
    thread_id = store.ensure_task_conversation(task.execution_id, direct=True)
    bind_followup_thread(store.storage, thread_id, "runtime-followup")

    class HungResume(FollowupService):
        async def thread_resume(self, _):
            await asyncio.Event().wait()

    service = HungResume()
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)
    monkeypatch.setattr(main, "HISTORY_REQUEST_TIMEOUT_SECONDS", 0.01)
    response = TestClient(main.app).post(f"/api/codex/threads/{thread_id}/turns", json={"text": "解释结果"})
    assert response.status_code == 504
    assert service.created == 0 and service.prompts == []


def test_followup_turn_start_has_bounded_wait_and_closes_subscription(task_store, monkeypatch):
    store, _, task = task_store
    thread_id = store.ensure_task_conversation(task.execution_id, direct=True)

    class HungTurn(FollowupService):
        attempts = 0

        async def turn_start(self, selected, prompt):
            self.attempts += 1
            await asyncio.Event().wait()

    service = HungTurn()
    monkeypatch.setattr(main, "get_codex_bff_service", lambda: service)
    monkeypatch.setattr(main, "HISTORY_REQUEST_TIMEOUT_SECONDS", 0.01)
    response = TestClient(main.app).post(f"/api/codex/threads/{thread_id}/turns/stream", json={"text": "解释结果"})
    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "followup_start_unconfirmed"
    assert service.attempts == 1 and service.subscription_closed
