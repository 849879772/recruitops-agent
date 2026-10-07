"""Scheduled startup failures use fake tools and an isolated database."""

import asyncio
import json
import threading
from datetime import datetime, time, timezone
from types import SimpleNamespace

import pytest

from apps.api.automation import CodexAutomationExecutor
from packages.automation import AutomationStore, LocalAutomationWorker
from packages.automation.conversations import read_conversation
from packages.automation.latest_report import report_path
from packages.automation.startup import start_automation_thread
from packages.codex_runtime import JsonRpcRemoteError
from packages.codex_runtime.events import CodexEventType
from packages.storage import Storage
from tests.test_automation_executor import _Service, _event


def _store(tmp_path, task_id):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'schedule.db'}", initialize=True)
    store = AutomationStore(storage)
    schedule = store.upsert_daily(
        task_id=task_id, task_label="测试任务", start_time=time(5, 14),
        now=datetime(2000, 1, 1, tzinfo=timezone.utc))
    settings = SimpleNamespace(agent_root=tmp_path, write_enabled=True)
    return store, schedule, settings


def _handshake_error():
    return JsonRpcRemoteError(
        code=-32603,
        message="error creating thread: Fatal error: Failed to initialize session: "
                "required MCP servers failed to initialize: recruitops: "
                "timed out handshaking with MCP server after 19.9999988s")


async def _no_sleep(_seconds):
    pass


def _fast_retries(monkeypatch):
    async def fast(service, **kwargs):
        return await start_automation_thread(service, sleeper=_no_sleep, **kwargs)
    monkeypatch.setattr("apps.api.automation.start_automation_thread", fast)


def test_full_capture_runs_once_even_when_model_thread_cannot_initialize(tmp_path):
    store, schedule, settings = _store(tmp_path, "daily_recruitment_intelligence")
    calls = []

    class UnavailableRuntime:
        async def thread_start(self):
            pytest.fail("A full capture must start without initializing the model thread")

    def capture(context):
        calls.append(context.run_id)
        chat = read_conversation(store.storage, context.metadata["thread_id"])
        assert chat["automation"]["status"] == "running"
        return {
            "status": "partial", "sync_status": "degraded",
            "new": 149, "reused": 18198, "scored": 2773,
            "scoring_candidates": 2817, "scoring_failed": 44,
            "write_statistics": {"job_snapshot_update_count": 18198},
            "daily_sync": {"status": "succeeded", "pipeline": {
                "new": 149, "reused": 18198, "scored": 0,
                "companies": [{"status": "partial", "detail_failure_count": 14}]}},
        }

    executor = CodexAutomationExecutor(UnavailableRuntime(), store, settings=settings,
                                      task_handlers={schedule.task_id: capture})
    worker = LocalAutomationWorker(store, executor)
    assert asyncio.run(worker.run_once())
    assert not asyncio.run(worker.run_once())
    execution = store.executions(schedule.id)[0]
    assert calls == [execution.id]
    assert execution.status == "succeeded" and execution.error is None
    assert "部分完成" in execution.result_summary
    assert "2773" in execution.result_summary and "18198" in execution.result_summary
    chat = read_conversation(store.storage, execution.thread_id)
    assert chat["automation"]["local_only"]
    report = chat["messages"][-1]["result"]["details"]
    assert report["status"] == "partial" and report["scoring_failed"] == 44
    assert report["scoring_candidates"] == 2817
    assert json.loads(report_path(settings).read_text(encoding="utf-8"))["scored_jobs"] == 2773
    assert store.list()[0].active and store.list()[0].next_run_at > execution.started_at


@pytest.mark.parametrize("failures", [1, 3])
def test_model_startup_retries_same_occurrence_and_preserves_visible_result(tmp_path, monkeypatch, failures):
    _fast_retries(monkeypatch)
    store, schedule, settings = _store(tmp_path, "application_progress")

    class FlakyRuntime(_Service):
        attempts = 0
        turns = 0

        async def thread_start(self):
            self.attempts += 1
            if self.attempts <= failures:
                raise _handshake_error()
            execution = store.executions(schedule.id)[0]
            assert "重试" in execution.result_summary
            return SimpleNamespace(id="thread-1")

        async def turn_start(self, thread_id, prompt):
            self.turns += 1
            return await super().turn_start(thread_id, prompt)

    service = FlakyRuntime([
        _event(CodexEventType.TEXT_DELTA, text="复核完成，状态未变化 1。"),
        _event(CodexEventType.TURN_COMPLETED),
    ])
    worker = LocalAutomationWorker(
        store, CodexAutomationExecutor(service, store, settings=settings))
    assert asyncio.run(worker.run_once())
    assert not asyncio.run(worker.run_once())
    assert len(store.executions(schedule.id)) == 1
    execution = store.executions(schedule.id)[0]
    chat = read_conversation(store.storage, execution.thread_id)
    assert chat and len(chat["messages"]) == 3
    if failures == 1:
        assert service.attempts == 2 and service.turns == 1
        assert execution.status == "succeeded" and execution.error is None
    else:
        assert service.attempts == 3 and service.turns == 0
        assert execution.status == "failed"
        assert "未启动" in chat["messages"][-1]["text"]
        assert chat["messages"][-1]["result"]["details"]["stage"] == "startup"
        assert chat["messages"][-1]["result"]["details"]["attempts"] == 3


def test_startup_failure_redacts_configured_credentials_in_persisted_report(tmp_path, monkeypatch):
    _fast_retries(monkeypatch)
    store, schedule, settings = _store(tmp_path, "recruitment_mailbox")
    settings.llm_api_key = "fixture-custom-secret"

    class RejectedRuntime:
        async def thread_start(self):
            raise RuntimeError(f"401 Unauthorized: {settings.llm_api_key}")

    worker = LocalAutomationWorker(
        store, CodexAutomationExecutor(RejectedRuntime(), store, settings=settings))
    assert asyncio.run(worker.run_once())
    execution = store.executions(schedule.id)[0]
    chat = read_conversation(store.storage, execution.thread_id)
    assert execution.status == "failed"
    assert settings.llm_api_key not in json.dumps(chat)
    assert settings.llm_api_key not in execution.error


def test_concurrent_attempts_cannot_launch_the_same_capture_twice(tmp_path):
    store, schedule, settings = _store(tmp_path, "daily_recruitment_intelligence")
    task = store.claim_due()
    calls = []

    def capture(context):
        calls.append(context.run_id)
        return {"status": "completed"}

    executor = CodexAutomationExecutor(None, store, settings=settings,
                                      task_handlers={schedule.task_id: capture})

    async def simultaneous_starts():
        return await asyncio.gather(executor(task), executor(task))

    results = asyncio.run(simultaneous_starts())
    assert calls == [task.execution_id]
    assert sorted(result.status for result in results) == ["blocked", "succeeded"]
    assert next(result for result in results if result.status == "blocked").error == \
        "automation_execution_already_started"
    assert next(result for result in results if result.status == "blocked").skip_completion


def test_duplicate_worker_callback_cannot_finalize_the_active_owner(tmp_path, monkeypatch):
    store, schedule, settings = _store(tmp_path, "daily_recruitment_intelligence")
    task = store.claim_due()
    monkeypatch.setattr(store, "claim_due", lambda: task)
    release = threading.Event()
    calls = []

    def capture(context):
        calls.append(context.run_id)
        assert release.wait(5), "duplicate observer did not release the owner"
        return {"status": "completed", "new": 9}

    executor = CodexAutomationExecutor(None, store, settings=settings,
        task_handlers={schedule.task_id: capture})

    async def execute(claimed):
        result = await executor(claimed)
        if result.skip_completion:
            assert store.execution(task.execution_id).status == "running"
            release.set()
        return result

    async def duplicate_callbacks():
        return await asyncio.gather(
            LocalAutomationWorker(store, execute).run_once(),
            LocalAutomationWorker(store, execute).run_once())

    assert asyncio.run(duplicate_callbacks()) == [True, True]
    execution = store.execution(task.execution_id)
    chat = read_conversation(store.storage, execution.thread_id)
    assert calls == [task.execution_id]
    assert execution.status == "succeeded"
    assert chat["automation"]["status"] == "succeeded"
    assert chat["messages"][-1]["result"]["details"]["new_jobs"] == 9


def test_duplicate_completion_and_terminal_worker_replay_preserve_the_report(tmp_path, monkeypatch):
    store, schedule, settings = _store(tmp_path, "daily_recruitment_intelligence")
    task = store.claim_due()
    executor = CodexAutomationExecutor(None, store, settings=settings,
        task_handlers={schedule.task_id: lambda _: pytest.fail("terminal occurrence reran")})
    store.ensure_task_conversation(task.execution_id, direct=True)
    first = store.complete(task.execution_id, status="succeeded",
        result_summary="完整成果", result_details={"status": "partial", "new_jobs": 9})
    before = read_conversation(store.storage, first.thread_id)
    store.complete(task.execution_id, status="blocked", result_summary="旧回调", error="old error")
    monkeypatch.setattr(store, "claim_due", lambda: task)
    assert asyncio.run(LocalAutomationWorker(store, executor).run_once())
    after = store.execution(task.execution_id)
    assert after.status == "succeeded" and after.result_summary == "完整成果"
    assert after.completed_at == first.completed_at
    assert read_conversation(store.storage, first.thread_id) == before
