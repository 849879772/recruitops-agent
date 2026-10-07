"""Concurrent task ownership contracts; synthetic SQLite and fake SSE only."""

import asyncio
import json
from datetime import datetime, timezone
from time import sleep, time as now
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api import main
from apps.api.daily_progress import latest_daily_progress, task_progress
from packages.codex_runtime import normalize_event
from packages.storage import AgentStateStore, Storage, TaskRun, ToolCall
from packages.storage.task_identity import task_identity


HEADERS = {"X-RecruitOps-Local-UI": "1", "Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def storage(tmp_path):
    value = Storage.from_url(f"sqlite:///{tmp_path / 'ownership.db'}", initialize=True)
    yield value
    value.engine.dispose()


def daily(storage, run_id="daily-run-a", *, thread="crawl-chat", turn="crawl-turn", control=False):
    owner = {"thread_id": thread, "turn_id": turn}
    AgentStateStore(storage).save_task_state(run_id, {
        "metadata": {} if control else owner,
        "current_step": "companies:1/3",
        "progress": {"stage": "companies", "attempted_unique": 1, "scope_total": 3},
    }, ensure_task_run=True)
    with storage.write_transaction() as session:
        session.get(TaskRun, run_id).status = "running"
        if control:
            session.add(ToolCall(id=f"control:{run_id}", task_id=run_id, tool_name="task_runtime_control",
                                source="fixture", arguments={"metadata": owner, "drained": False}))
    return run_id


def review(storage, *, thread="review-chat", turn="review-turn"):
    run_id = "status-review-" + "a" * 32
    with storage.write_transaction() as session:
        session.add(TaskRun(id=run_id, task_type="application_status_review", status="running",
                            user_request="fixture", source="fixture"))
        session.flush()
        session.add(ToolCall(id=run_id, task_id=run_id, tool_name="application_review_checkpoint", source="fixture",
            arguments={"ids": ["application-a"], "results": {}, "database_total": 1,
                       "excluded_terminal": 0, "pages_total": 0, "run_status": "running",
                       "metadata": {"thread_id": thread, "turn_id": turn}, "lease_until": now() + 60}))
    return run_id


def mail(storage, *, thread="mail-chat", turn="mail-turn"):
    run_id = "mail-run-a"
    with storage.write_transaction() as session:
        session.add(TaskRun(id=run_id, task_type="recruitment_mail_process", status="running",
                            user_request="fixture", source="fixture"))
        session.flush()
        session.add(ToolCall(id=run_id, task_id=run_id, tool_name="recruitment_mail_process_background",
            source="fixture", arguments={"metadata": {"thread_id": thread, "turn_id": turn},
                "scope": [], "results": {}, "progress": {"phase": "analysis", "completed": 0,
                    "total": 1, "remaining": 1, "failed": 0, "blocked": 0}}))
    return run_id


def test_parallel_tracks_keep_distinct_run_thread_and_turn_ids(storage):
    expected = {daily(storage): ("crawl-chat", "crawl-turn"),
                review(storage): ("review-chat", "review-turn"), mail(storage): ("mail-chat", "mail-turn")}
    runs = task_progress(storage)["runs"]
    assert len(runs) == 3 and task_progress(storage)["run"] is None
    for run in runs:
        assert (run["thread_id"], run["turn_id"]) == expected[run["run_id"]]
        assert run["task_id"]
        owned = task_progress(storage, thread_id=run["thread_id"])
        assert owned["run"]["run_id"] == run["run_id"] and len(owned["runs"]) == 1
        assert task_progress(storage, run_id=run["run_id"], thread_id="unrelated") == {"run": None, "runs": []}


def test_daily_control_owner_survives_old_state_and_latest_endpoint(storage):
    run_id = daily(storage, control=True)
    for projected in (latest_daily_progress(storage)["run"], task_progress(storage)["run"]):
        assert projected["run_id"] == run_id
        assert projected["thread_id"] == "crawl-chat" and projected["turn_id"] == "crawl-turn"
    assert latest_daily_progress(storage, thread_id="unrelated") == {"run": None}
    with storage.write_transaction() as session:
        receipt = session.get(ToolCall, f"control:{run_id}")
        receipt.arguments = {"metadata": {"thread_id": "resumed-chat", "turn_id": "resumed-turn"}}
    assert task_progress(storage, thread_id="crawl-chat")["runs"] == []
    assert latest_daily_progress(storage, thread_id="resumed-chat")["run"]["turn_id"] == "resumed-turn"


def test_unknown_owner_does_not_attach_to_any_selected_chat(storage):
    daily(storage, thread=None, turn=None)
    projected = task_progress(storage)["run"]
    assert projected["thread_id"] is projected["turn_id"] is None
    assert task_progress(storage, thread_id="new-chat")["runs"] == []
    assert latest_daily_progress(storage, thread_id="new-chat")["run"] is None


def test_latest_daily_filters_owner_before_global_recent_limit(storage):
    owned = daily(storage, "older-owned-run", thread="old-chat")
    for index in range(21):
        daily(storage, f"new-run-{index}", thread="new-chat")
    assert latest_daily_progress(storage, thread_id="old-chat")["run"]["run_id"] == owned


@pytest.mark.parametrize("route", ["tasks", "daily-recruitment"])
def test_api_thread_query_is_bounded_same_origin_and_read_only(storage, monkeypatch, route):
    owned = daily(storage)
    daily(storage, "foreign-daily-run", thread="foreign-chat")
    monkeypatch.setattr(main, "get_storage_engine", lambda: storage.engine)
    client = TestClient(main.app, base_url="http://localhost")
    url = f"/api/local-ui/{route}/progress"
    assert client.get(url, params={"thread_id": "crawl-chat"}).status_code == 403
    response = client.get(url, params={"thread_id": "crawl-chat"}, headers=HEADERS)
    assert response.status_code == 200 and response.json()["run"]["run_id"] == owned
    assert client.get(url, params={"thread_id": ""}, headers=HEADERS).status_code == 422
    assert client.get(url, params={"thread_id": "x" * 256}, headers=HEADERS).status_code == 422
    assert client.get(url, params={"thread_id": "unrelated"}, headers=HEADERS).json()["run"] is None


def test_direct_automation_progress_uses_exact_saved_execution_without_model(tmp_path):
    from tests.test_automation_chat_20261006 import setup, DirectService
    from apps.api.automation import CodexAutomationExecutor
    from packages.automation import LocalAutomationWorker

    store, schedule, settings = setup(tmp_path)
    receipts = []
    def handler(context):
        # Simulate an older state that omitted ids; execution/thread is durable.
        daily(store.storage, context.run_id, thread=None, turn=None)
        run = task_progress(store.storage, thread_id=context.metadata["thread_id"])["run"]
        assert run["run_id"] == context.run_id and run["thread_id"] == context.metadata["thread_id"]
        assert run["turn_id"] is None and run["report_persisted"] is True
        assert run["automation"] == {"direct": True, "execution_id": context.run_id,
            "run_id": context.run_id, "schedule_id": schedule.id, "task_id": context.task_id}
        receipts.append(run)
        return {"status": "completed"}
    worker = LocalAutomationWorker(store, CodexAutomationExecutor(DirectService(), store, settings=settings,
        task_handlers={"daily_recruitment_intelligence": handler}))
    assert asyncio.run(worker.run_once()) and len(receipts) == 1
    assert task_progress(store.storage, thread_id="foreign-chat")["runs"] == []
    saved = task_progress(store.storage, run_id=receipts[0]["run_id"])["run"]
    assert saved["status"] == "success" and saved["report_persisted"] is True
    assert task_progress(store.storage)["runs"] == []
    store.storage.engine.dispose()


def test_direct_marker_is_not_assumed_from_run_name_or_metadata(storage):
    daily(storage, "automation-run-unproven", thread="manual-chat")
    run = task_progress(storage)["run"]
    assert run["automation"] is None and run["report_persisted"] is False


@pytest.mark.parametrize("metadata", [None, [], {"thread_id": "  ", "turn_id": 7},
                                        {"thread_id": {"secret": "private"}, "turn_id": "x" * 256}])
def test_task_identity_projects_only_explicit_valid_scalar_ids(metadata):
    assert task_identity("run-a", metadata, task_id="daily") == {
        "run_id": "run-a", "task_id": "daily", "thread_id": None, "turn_id": None}


def test_review_tool_receipt_preserves_same_owner_as_progress(storage):
    from packages.tools.application_review_tasks import review_runs
    from packages.tools.application_review_run import _response
    run_id = review(storage)
    with storage.session() as session:
        state = session.get(ToolCall, run_id).arguments
    receipt = _response(run_id, state, 0, busy=True).summary
    summary = review_runs(storage, run_id=run_id)[0]
    for value in (receipt, summary, task_progress(storage, run_id=run_id)["run"]):
        assert value["thread_id"] == "review-chat" and value["turn_id"] == "review-turn"
        assert value["run_id"] == run_id and value["task_id"] == "application_status_review"


def test_waiting_mail_confirmation_keeps_run_thread_turn_binding(storage):
    from packages.recruitment_mail.run_service import mail_confirmation_queue
    run_id = mail(storage)
    with storage.write_transaction() as session:
        session.get(TaskRun, run_id).status = "awaiting_confirmation"
        checkpoint = session.get(ToolCall, run_id)
        checkpoint.arguments = {**checkpoint.arguments,
            "scope": [{"record_id": "record-a", "content_digest": "synthetic-digest"}],
            "results": {"record-a": {"state": "ambiguous_application"}}}
    row = mail_confirmation_queue(storage, "mail-chat")["runs"][0]
    assert row["run_id"] == run_id and row["thread_id"] == "mail-chat"
    assert row["turn_id"] == "mail-turn" and row["task_id"] == "recruitment_mail_process"
    assert mail_confirmation_queue(storage, "foreign-chat") == {"runs": []}


@pytest.mark.parametrize("new_turn", [None, "new-turn"])
def test_review_resume_in_another_chat_never_carries_old_turn(storage, monkeypatch, new_turn):
    from packages.tools import application_review_run
    from packages.tools.application_review_tasks import ApplicationReviewControlInput, control_application_review
    from packages.repositories.postgres import PostgresRecruitmentRepository
    from packages.storage import ApplicationSnapshot
    from tests.test_task_recovery_controls import part
    run_id = review(storage, thread="old-chat", turn="old-turn")
    with storage.write_transaction() as session:
        session.get(TaskRun, run_id).status = "stopped"
        checkpoint = session.get(ToolCall, run_id)
        checkpoint.arguments = {**checkpoint.arguments, "run_status": "stopped", "lease_until": 0}
        session.add(ApplicationSnapshot(id="application-a", company_name="示例公司", job_title="开发工程师",
            stage="applied", record_url="https://example.test/applications", stage_history=[],
            source="fixture", idempotency_key="application-a"))
    async def observe(request, *_args):
        return part(request)
    monkeypatch.setattr(application_review_run, "batch_observe_application_status", observe)
    result = asyncio.run(control_application_review(ApplicationReviewControlInput(action="resume", run_id=run_id,
        thread_id="new-chat", turn_id=new_turn), object(), PostgresRecruitmentRepository(storage)))
    assert result.success
    row = task_progress(storage, run_id=run_id)["run"]
    assert row["thread_id"] == "new-chat" and row["turn_id"] == new_turn


@pytest.mark.parametrize("new_turn", [None, "new-turn"])
def test_mail_resume_in_another_chat_never_carries_old_turn(storage, new_turn):
    from packages.recruitment_mail import RecruitmentMailStore
    from packages.recruitment_mail.run_service import MailProcessingRunService
    run_id = mail(storage, thread="old-chat", turn="old-turn")
    with storage.write_transaction() as session:
        session.get(TaskRun, run_id).status = "paused"
    service = MailProcessingRunService(RecruitmentMailStore(storage), SimpleNamespace(), SimpleNamespace(write_enabled=True))
    try:
        row = service.control(run_id, "resume", thread_id="new-chat", turn_id=new_turn, background=False)
        assert row["thread_id"] == "new-chat" and row["turn_id"] == new_turn
    finally:
        service.close()


def test_sse_turn_stream_drops_foreign_and_unowned_turn_messages(monkeypatch):
    events = [
        normalize_event("item/agentMessage/delta", {"threadId": "foreign", "turnId": "turn-a", "delta": "foreign"}),
        normalize_event("item/agentMessage/delta", {"threadId": "chat-a", "turnId": "old-turn", "delta": "old"}),
        normalize_event("item/agentMessage/delta", {"threadId": "chat-a", "delta": "unowned"}),
        normalize_event("turn/completed", {"threadId": "chat-a", "turnId": "old-turn"}),
        normalize_event("item/agentMessage/delta", {"threadId": "chat-a", "turnId": "turn-a", "delta": "actual", "run_id": "daily-run-a"}),
        normalize_event("turn/completed", {"threadId": "chat-a", "turnId": "turn-a"}),
    ]
    class Subscription:
        closed = False
        def __aiter__(self):
            async def iterator():
                for event in events:
                    yield event
            return iterator()
        def close(self):
            self.closed = True
    subscription = Subscription()
    class Service:
        def subscribe(self, thread):
            assert thread == "chat-a"
            return subscription
        async def turn_start(self, thread, _prompt):
            assert thread == "chat-a"
            return {"id": "turn-a"}
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(codex_runtime_enabled=True))
    monkeypatch.setattr(main, "get_codex_bff_service", Service)
    async def scenario():
        response = await main.codex_turn_stream("chat-a", main.CodexTurnStartRequest(text="fixture"))
        return [part async for part in response.body_iterator]
    frames = asyncio.run(scenario())
    assert subscription.closed and len(frames) == 3
    payloads = [json.loads(frame.split("data: ", 1)[1]) for frame in frames]
    assert payloads[0]["thread_id"] == "chat-a" and payloads[0]["turn_id"] == "turn-a"
    assert payloads[1]["text"] == "actual" and payloads[1]["run_id"] == "daily-run-a"
    assert payloads[2]["turn_id"] == "turn-a" and payloads[2]["run_id"] is None
    assert all(token not in "".join(frames) for token in ("foreign", "old-turn", "unowned"))


def test_daily_tool_receipt_and_restarted_status_keep_owner(tmp_path):
    from threading import Event
    from packages.scheduler import LocalTaskScheduler, TaskType
    from packages.tools.operations import OperationalTaskRunner
    from packages.tools.daily_sync import DailyRecruitmentSyncInput, run_daily_recruitment_sync
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'runtime.db'}", initialize=True)
    entered, release = Event(), Event()
    def handler(_context):
        entered.set()
        assert release.wait(5)
        return {"status": "completed"}
    runner = OperationalTaskRunner(LocalTaskScheduler(lock_path=tmp_path / "task.lock"),
        {TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value: handler}, state_store=AgentStateStore(storage))
    try:
        receipt = run_daily_recruitment_sync(DailyRecruitmentSyncInput(thread_id="chat-a", turn_id="turn-a"), runner).data
        assert entered.wait(2)
        assert receipt.thread_id == "chat-a" and receipt.turn_id == "turn-a"
        restarted = OperationalTaskRunner(LocalTaskScheduler(lock_path=tmp_path / "other.lock"), {},
                                          state_store=AgentStateStore(storage))
        status = restarted.background_status(receipt.run_id)
        assert status["thread_id"] == "chat-a" and status["turn_id"] == "turn-a"
        assert status["run_id"] == receipt.run_id
    finally:
        release.set()
        for _ in range(100):
            if runner.background_status(receipt.run_id)["run_status"] not in {"accepted", "running"}:
                break
            sleep(0.01)
        storage.engine.dispose()


def test_sse_conversation_stream_retains_explicit_turn_ids_but_not_foreign_thread(monkeypatch):
    events = [normalize_event("turn/completed", {"threadId": thread, "turnId": turn})
              for thread, turn in [("chat-a", "turn-old"), ("foreign", "turn-foreign"), ("chat-a", "turn-current")]]
    class Service:
        async def event_stream(self, thread):
            assert thread == "chat-a"
            for event in events:
                yield event
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(codex_runtime_enabled=True))
    monkeypatch.setattr(main, "get_codex_bff_service", Service)
    async def scenario():
        response = await main.codex_thread_events("chat-a")
        return [part async for part in response.body_iterator]
    payloads = [json.loads(frame.split("data: ", 1)[1]) for frame in asyncio.run(scenario())]
    assert [item["turn_id"] for item in payloads] == ["turn-old", "turn-current"]
    assert all(item["thread_id"] == "chat-a" and item["run_id"] is None for item in payloads)


@pytest.mark.parametrize("nested", [False, True])
def test_daily_runtime_persists_context_owner_in_all_stage_states(tmp_path, monkeypatch, nested):
    import packages.scheduler.runtime as runtime
    from packages.scheduler import TaskContext
    from packages.orchestration import DailySyncStatus
    database_url = f"sqlite:///{tmp_path / 'stage-state.db'}"
    storage = Storage.from_url(database_url, initialize=True)
    profile = tmp_path / "synthetic-profile.yaml"
    profile.write_text("profile: {}", encoding="utf-8")
    settings = SimpleNamespace(database_url=database_url, mail_enabled=False,
        candidate_profile_config=profile, companies_config=tmp_path / "unused-companies.yaml",
        agent_root=tmp_path, crawl_max_concurrency=1, crawl_company_timeout_seconds=1)
    monkeypatch.setattr(runtime, "DailyRecruitmentPipeline", lambda **_kwargs: SimpleNamespace())
    class Sync:
        def __init__(self, **_kwargs):
            pass
        def run(self, **_kwargs):
            return SimpleNamespace(status=DailySyncStatus.SUCCEEDED, warnings=[],
                model_dump=lambda **_kwargs: {"status": "succeeded", "pipeline": {}})
    monkeypatch.setattr(runtime, "DailyRecruitmentSync", Sync)
    owner = {"thread_id": "chat-stage", "turn_id": "turn-stage"}
    metadata = {"details": {**owner, "mode": "full"}} if nested else {**owner, "details": {"mode": "full"}}
    context = TaskContext(task_id="daily_recruitment_intelligence", task_label="fixture",
        scheduled_for=datetime.now(timezone.utc), run_id="runtime-stage-run", attempt=1,
        write_enabled=True, read_only=False, metadata=metadata)
    result = runtime.build_runtime_task_handlers(settings=settings)[context.task_id](context)
    assert result["status"] == "completed"
    persisted = AgentStateStore(storage).get_task_run(context.run_id)
    assert persisted["metadata"]["thread_id"] == "chat-stage"
    assert persisted["metadata"]["turn_id"] == "turn-stage"
    assert persisted["metadata"]["run_id"] == context.run_id
    assert persisted["metadata"]["task_id"] == context.task_id
    assert task_progress(storage)["run"]["thread_id"] == "chat-stage"
    storage.engine.dispose()
