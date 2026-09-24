"""Offline task discovery, fencing, safe-stop, and progress regressions."""

import asyncio
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from apps.api.daily_progress import task_progress
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.scheduler import LocalTaskScheduler, TaskType
from packages.scheduler.lock import LocalInstanceLock
from packages.storage import AgentStateStore, ApplicationSnapshot, Storage, TaskRun, ToolCall
from packages.tools import application_review_run as review
from packages.tools.application_review_tasks import (
    ApplicationReviewControlInput, ApplicationReviewStatusInput, REVIEW_CONTEXT,
    application_review_status, control_application_review, review_write_guard,
)
from packages.tools.batch_browser_operations import (
    ApplicationStatusResult, BatchObserveApplicationStatusInput, BatchObserveApplicationStatusResponse,
)
from packages.tools.operations import OperationalTaskRunInput, OperationalTaskRunner
from packages.tools.task_runtime_control import finish_daily_task, register_daily_task, request_daily_control
from packages.tools.typed import EvidenceSource, ToolStatus


def repository(tmp_path, count=3):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'recovery.db'}", initialize=True)
    with storage.write_transaction() as session:
        for index in range(count):
            session.add(ApplicationSnapshot(id=str(index), company_name="Example", job_title=f"Engineer {index}",
                stage="applied", record_url=f"https://site{index}.example/applications", stage_history=[],
                idempotency_key=f"application:{index}", source="fixture"))
    return PostgresRecruitmentRepository(storage)


def part(request, state="unchanged"):
    return BatchObserveApplicationStatusResponse(
        tool_name="batch_observe_application_status", status=ToolStatus.SUCCESS, success=True,
        total=len(request.application_ids), pages_total=1, evidence=[EvidenceSource(source="fixture")], timeout_ms=1000, elapsed_ms=0,
        **{state: [ApplicationStatusResult(application_id=value, state=state, elapsed_ms=0)
                   for value in request.application_ids]},
    )


def seed_review(storage, suffix, *, status="stopped", thread_id="chat", owner="", lease=0):
    run_id = "status-review-" + suffix * 32
    with storage.write_transaction() as session:
        session.add(TaskRun(id=run_id, task_type="application_status_review", status=status,
                            user_request="fixture", source="fixture"))
        session.flush()
        session.add(ToolCall(id=run_id, task_id=run_id, tool_name="application_review_checkpoint", source="fixture",
            arguments={"ids": ["0"], "results": {}, "attempts": {}, "database_total": 1,
                       "excluded_terminal": 0, "pages_total": 0, "run_status": status,
                       "metadata": {"task_kind": "application_review", "thread_id": thread_id},
                       "owner": {"desktop_run_id": owner}, "claim": "old", "lease_until": lease}))
    return run_id


def test_progress_filters_review_history_before_loading_summaries(tmp_path, monkeypatch):
    from packages.tools import application_review_tasks as tasks
    repo = repository(tmp_path, 0)
    completed = seed_review(repo.storage, "a", status="completed")
    stopped = seed_review(repo.storage, "b", status="stopped")
    active = seed_review(repo.storage, "c", status="running", lease=time.time() + 60)
    stale_boot = seed_review(repo.storage, "d", status="running", owner="old", lease=time.time() + 60)
    monkeypatch.setenv("RECRUITOPS_DESKTOP_RUN_ID", "new")
    observed = []
    original = tasks.review_summary
    def summary(run_id, *args):
        observed.append(run_id)
        return original(run_id, *args)
    monkeypatch.setattr(tasks, "review_summary", summary)
    assert [run["run_id"] for run in task_progress(repo.storage)["runs"]] == [active]
    assert set(observed) == {active, stale_boot}
    observed.clear()
    candidates = task_progress(repo.storage, include_recoverable=True)["runs"]
    assert {run["run_id"] for run in candidates} == {active, stopped, stale_boot}
    assert set(observed) == {active, stopped, stale_boot}
    observed.clear()
    assert task_progress(repo.storage, run_id=completed)["run"]["status"] == "completed"
    assert observed == [completed]


def test_lost_receipt_can_be_found_paused_and_resumed_without_id(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    monkeypatch.setattr(review, "_WAVE_MAX_CONCURRENCY", 1)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        visited = []

        async def observe(request, *_):
            visited.extend(request.application_ids)
            if len(visited) == 1:
                entered.set()
                await release.wait()
            return part(request)

        monkeypatch.setattr(review, "batch_observe_application_status", observe)
        # The foreground caller is still waiting; progress/control use storage.
        pending = asyncio.create_task(review.continue_application_review(BatchObserveApplicationStatusInput(
            all_non_terminal=True, background=True, thread_id="chat"), object(), repo))
        await entered.wait()
        assert not pending.done()  # Legacy background=True no longer returns a receipt early.
        found = application_review_status(ApplicationReviewStatusInput(thread_id="chat"), repo)
        run_id = found.data["run"]["run_id"]
        assert found.data["run"]["can_pause"]
        assert task_progress(repo.storage)["run"]["run_id"] == run_id
        response = await control_application_review(ApplicationReviewControlInput(action="pause", thread_id="chat"), None, repo)
        assert response.data["run"]["run_status"] == "pausing"
        assert response.data["run"]["in_progress"] is True
        conflict = await review.continue_application_review(BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repo)
        assert conflict.summary["active_run_id"] == run_id
        assert conflict.summary["can_resume"] is False
        release.set()
        paused = await pending
        assert paused.summary["run_status"] == "paused"
        assert not paused.summary["continuation_required"]
        assert visited == ["0"]
        assert application_review_status(ApplicationReviewStatusInput(run_id=run_id), repo).data["run"]["run_status"] == "paused"
        assert task_progress(repo.storage) == {"runs": [], "run": None}
        with repo.storage.write_transaction() as session:
            session.add(ApplicationSnapshot(id="new", company_name="New", job_title="New",
                stage="applied", record_url="https://new.example", stage_history=[],
                idempotency_key="new", source="fixture"))
        resumed = await control_application_review(ApplicationReviewControlInput(action="resume", thread_id="chat"), object(), repo)
        assert resumed.success
        assert sorted(visited) == ["0", "1", "2"]  # Frozen scope, no rescans/no newly added record.
        final = task_progress(repo.storage, run_id=run_id)["run"]
        assert final["completed"] == final["verified"] == 3
        assert final["status"] == "completed"
        assert task_progress(repo.storage, thread_id="chat")["runs"] == []

    asyncio.run(scenario())


def test_ambiguous_recoverable_runs_require_selection_and_explicit_resume_rebinds(tmp_path, monkeypatch):
    repo = repository(tmp_path, 1)
    first = seed_review(repo.storage, "a")
    seed_review(repo.storage, "b")
    found = application_review_status(ApplicationReviewStatusInput(), repo)
    assert found.data["selection"] == "ambiguous"
    assert len(found.data["runs"]) == 2

    async def scenario():
        conflict = await control_application_review(ApplicationReviewControlInput(action="resume"), object(), repo)
        assert not conflict.success
        async def observe(request, *_):
            return part(request)
        monkeypatch.setattr(review, "batch_observe_application_status", observe)
        result = await control_application_review(ApplicationReviewControlInput(action="resume", run_id=first,
            thread_id="new-chat"), object(), repo)
        assert result.success
        assert task_progress(repo.storage, run_id=first)["run"]["thread_id"] == "new-chat"
    asyncio.run(scenario())


def test_cancel_waits_for_inflight_page_and_does_not_undo_its_receipt(tmp_path, monkeypatch):
    repo = repository(tmp_path, 2)
    monkeypatch.setattr(review, "_WAVE_MAX_CONCURRENCY", 1)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def observe(request, *_):
            entered.set()
            await release.wait()
            return part(request)
        monkeypatch.setattr(review, "batch_observe_application_status", observe)
        pending = asyncio.create_task(review.continue_application_review(BatchObserveApplicationStatusInput(
            all_non_terminal=True, background=True), object(), repo))
        await entered.wait()
        assert not pending.done()
        run_id = application_review_status(ApplicationReviewStatusInput(), repo).data["run"]["run_id"]
        result = await control_application_review(ApplicationReviewControlInput(action="cancel", run_id=run_id), None, repo)
        assert result.data["run"]["run_status"] == "cancelling"
        release.set()
        cancelled = await pending
        assert not cancelled.summary["continuation_required"]
        final = task_progress(repo.storage, run_id=run_id)["run"]
        assert final["status"] == "cancelled" and final["completed"] == 1
        rejected = await control_application_review(ApplicationReviewControlInput(action="resume", run_id=run_id), object(), repo)
        assert not rejected.success
    asyncio.run(scenario())


def test_restart_reclaims_prior_boot_but_old_claim_cannot_write(tmp_path, monkeypatch):
    repo = repository(tmp_path, 1)
    run_id = seed_review(repo.storage, "a", status="running", owner="old-boot", lease=time.time() + 3600)
    monkeypatch.setenv("RECRUITOPS_DESKTOP_RUN_ID", "new-boot")
    found = application_review_status(ApplicationReviewStatusInput(), repo).data["run"]
    assert found["run_status"] == "stopped" and found["can_resume"]
    token = REVIEW_CONTEXT.set((repo.storage, run_id, "old"))
    try:
        with review_write_guard() as allowed:
            assert not allowed
    finally:
        REVIEW_CONTEXT.reset(token)
    async def observe(request, *_):
        return part(request, "blocked")
    monkeypatch.setattr(review, "batch_observe_application_status", observe)
    result = asyncio.run(review.continue_application_review(BatchObserveApplicationStatusInput(run_id=run_id), object(), repo))
    assert result.summary["completed_count"] == 1
    assert result.summary["verification_success_count"] == 0
    token = REVIEW_CONTEXT.set((repo.storage, run_id, "old"))
    try:
        with review_write_guard() as allowed:
            assert not allowed
    finally:
        REVIEW_CONTEXT.reset(token)


def test_foreground_continuation_respects_pause_until_explicit_resume(tmp_path, monkeypatch):
    repo = repository(tmp_path, 12)
    visited = []
    async def observe(request, *_):
        visited.extend(request.application_ids)
        return part(request)
    monkeypatch.setattr(review, "batch_observe_application_status", observe)
    async def scenario():
        first = await review.continue_application_review(BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repo)
        run_id = first.summary["run_id"]
        assert first.summary["continuation_required"]
        paused = await control_application_review(ApplicationReviewControlInput(action="pause", run_id=run_id), None, repo)
        assert paused.data["run"]["run_status"] == "paused"
        automatic = await review.continue_application_review(BatchObserveApplicationStatusInput(run_id=run_id), object(), repo)
        assert automatic.summary["run_status"] == "paused"
        assert not automatic.summary["continuation_required"]
        assert len(visited) == 10
        resumed = await control_application_review(ApplicationReviewControlInput(action="resume", run_id=run_id), object(), repo)
        assert resumed.data["run"]["run_status"] == "completed"
        assert len(visited) == len(set(visited)) == 12
    asyncio.run(scenario())


def test_foreground_continuation_window_is_not_a_worker_lease(tmp_path, monkeypatch):
    from packages.tools import application_review_tasks as tasks
    repo = repository(tmp_path, 12)
    async def observe(request, *_):
        return part(request)
    monkeypatch.setattr(review, "batch_observe_application_status", observe)
    first = asyncio.run(review.continue_application_review(BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repo))
    run_id = first.summary["run_id"]
    active = application_review_status(ApplicationReviewStatusInput(run_id=run_id), repo).data["run"]
    assert active["run_status"] == "awaiting_continuation"
    assert active["continuation_required"] and not active["in_progress"]
    assert active["can_pause"] and active["can_cancel"]
    with repo.storage.session() as session:
        state = dict(session.get(ToolCall, run_id).arguments)
    assert state["lease_until"] == 0
    monkeypatch.setattr(tasks, "time", lambda: state["continuation_until"] + 1)
    expired = application_review_status(ApplicationReviewStatusInput(run_id=run_id), repo).data["run"]
    assert expired["run_status"] == "stopped"
    assert expired["interruption_reason"] == "assistant_continuation_expired"
    assert not expired["continuation_required"]
    assert expired["can_resume"]


def test_stale_running_checkpoint_is_interrupted_not_safely_paused(tmp_path, monkeypatch):
    repo = repository(tmp_path, 1)
    run_id = seed_review(repo.storage, "a", status="running", lease=time.time() - 1)
    found = application_review_status(ApplicationReviewStatusInput(run_id=run_id), repo).data["run"]
    assert found["run_status"] == "stopped"
    assert found["interruption_reason"] == "worker_heartbeat_lost"
    assert not found["continuation_required"]
    assert found["can_resume"]


def test_daily_projection_uses_company_processed_counts_without_showing_history(tmp_path):
    repo = repository(tmp_path, 0)
    storage = repo.storage
    register_daily_task(storage, "daily", "daily_recruitment_intelligence", thread_id="chat")
    AgentStateStore(storage).save_task_state("daily", {"current_step": "companies:3/10", "progress": {
        "stage": "companies", "scope_total": 10, "attempted_unique": 8, "confirmed_complete": 3}})
    current = task_progress(storage)["run"]
    assert current["completed"] == 8 and current["total"] == 10
    assert current["progress"]["confirmed_complete"] == 3
    assert current["thread_id"] == "chat"
    assert task_progress(storage, thread_id="unrelated")["runs"] == []
    with storage.write_transaction() as session:
        session.get(TaskRun, "daily").status = "completed"
    assert task_progress(storage) == {"runs": [], "run": None}
    assert task_progress(storage, run_id="daily")["run"]["status"] == "completed"


def test_daily_cancel_terminal_receipt_wins_over_partial_business_result(tmp_path):
    storage = repository(tmp_path, 0).storage
    register_daily_task(storage, "daily", "daily_recruitment_intelligence")
    AgentStateStore(storage).save_task_state("daily", {"result": {"status": "paused"}})
    assert request_daily_control(storage, "daily", "cancel")["success"]
    # Pipeline checkpointing can finish before the scheduler joins its worker.
    with storage.write_transaction() as session:
        session.get(TaskRun, "daily").status = "stopped"
    assert task_progress(storage)["run"]["status"] == "cancelling"
    finish_daily_task(storage, "daily", "paused")
    assert task_progress(storage)["runs"] == []
    assert task_progress(storage, run_id="daily")["run"]["status"] == "cancelled"


@pytest.mark.parametrize("status", ["stopped", "failed", "timed_out", "paused", "interrupted"])
@pytest.mark.parametrize("has_control", [False, True])
def test_cancel_interrupted_daily_receipt_preserves_results_and_disables_resume(tmp_path, status, has_control):
    storage = repository(tmp_path, 1).storage
    store = AgentStateStore(storage)
    saved = {"progress": {"stage": "companies", "scope_total": 86, "attempted_unique": 16},
             "checkpoint_ref": "saved-checkpoint.json", "result": {"status": "paused"}}
    if has_control:
        register_daily_task(storage, "old-daily", "daily_recruitment_intelligence")
        finish_daily_task(storage, "old-daily", status)
    store.save_task_state("old-daily", saved, ensure_task_run=True)
    with storage.write_transaction() as session:
        session.get(TaskRun, "old-daily").status = status
    assert task_progress(storage, include_recoverable=True)["run"]["can_cancel"]
    result = request_daily_control(storage, "old-daily", "cancel")
    assert result["success"] and result["status"] == "cancelled"
    assert store.get_task_run("old-daily")["state"] == saved
    assert task_progress(storage, include_recoverable=True)["runs"] == []
    found = task_progress(storage, run_id="old-daily")["run"]
    assert found["status"] == "cancelled" and not found["can_resume"]
    assert request_daily_control(storage, "old-daily", "cancel")["already_cancelled"]
    assert len(PostgresRecruitmentRepository(storage).list_applications()) == 1


def test_cancel_previous_boot_daily_receipt_is_terminal_without_waiting_for_dead_worker(tmp_path, monkeypatch):
    storage = repository(tmp_path, 0).storage
    monkeypatch.setenv("RECRUITOPS_DESKTOP_RUN_ID", "old-boot")
    register_daily_task(storage, "old-boot-run", "daily_recruitment_intelligence")
    monkeypatch.setenv("RECRUITOPS_DESKTOP_RUN_ID", "new-boot")
    assert task_progress(storage, include_recoverable=True)["run"]["can_cancel"]
    result = request_daily_control(storage, "old-boot-run", "cancel")
    assert result["status"] == "cancelled"


def test_legacy_running_daily_without_control_is_not_falsely_cancelled(tmp_path):
    storage = repository(tmp_path, 0).storage
    AgentStateStore(storage).save_task_state("legacy-running", {}, ensure_task_run=True)
    result = request_daily_control(storage, "legacy-running", "cancel")
    assert not result["success"] and result["reason"] == "control_not_available"
    assert AgentStateStore(storage).get_task_run("legacy-running")["run_status"] == "running"


@pytest.mark.parametrize("mode", ["full", "crawl_only", "score_only"])
def test_resume_projection_accepts_real_runtime_state_writer(tmp_path, monkeypatch, mode):
    import packages.scheduler.runtime as runtime
    from packages.config import Settings
    from packages.scheduler import TaskContext
    config = tmp_path / "config"
    config.mkdir()
    (config / "candidate_profile.yaml").write_text("profile: {}\n", encoding="utf-8")
    (config / "companies.yaml").write_text(
        "companies:\n  - id: fixture\n    name: Fixture\n    careers_url: https://fixture.example/campus\n"
        "    crawler: render\n    integration_status: connected\n", encoding="utf-8")
    settings = Settings(agent_root=tmp_path, database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
                        discovery_enabled=False, offline_reconciliation_enabled=False, llm_enabled=False)
    storage = Storage.from_url(settings.database_url, initialize=True)
    class Pipeline:
        def __init__(self, *, checkpoint_path=None, **kwargs):
            self.checkpoint_path = checkpoint_path
        def run(self, **kwargs):
            if self.checkpoint_path:
                self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                self.checkpoint_path.write_text('{"company_ids":["fixture"],"companies":{}}', encoding="utf-8")
            return SimpleNamespace(written=False, to_dict=lambda: {"selected_companies": 1, "companies": [], "written": False})
    monkeypatch.setattr(runtime, "DailyRecruitmentPipeline", Pipeline)
    monkeypatch.setattr(runtime, "build_reporting_summary", lambda *args, **kwargs: {})
    handler = runtime.build_runtime_task_handlers(settings=settings)[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value]
    handler(TaskContext(task_id=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value, task_label="fixture",
                        scheduled_for=datetime.now(timezone.utc), run_id="runtime-fixture", attempt=1,
                        write_enabled=True, metadata={"details": {"mode": mode, "company_ids": ["fixture"]}}))
    store = AgentStateStore(storage)
    record = store.get_task_run("runtime-fixture")
    assert runtime._load_frozen_resume(settings, record)["company_ids"] == ("fixture",)
    # Simulate restart after the real runtime wrote the scope/checkpoint envelope.
    with storage.write_transaction() as session:
        session.get(TaskRun, "runtime-fixture").status = "stopped"
    projected = task_progress(storage, include_recoverable=True)["run"]
    assert projected["can_resume"] is True
    assert "resume" in projected["actions"]


@pytest.mark.parametrize("action,terminal", [("pause", "paused"), ("cancel", "cancelled")])
def test_daily_stop_is_cooperative_and_retains_lock_until_worker_drains(tmp_path, action, terminal):
    repo = repository(tmp_path, 0)
    entered, stop_seen, release = threading.Event(), threading.Event(), threading.Event()
    lock_path = tmp_path / "daily.lock"
    def handler(context):
        entered.set()
        assert context.stop_requested.wait(4)
        stop_seen.set()
        assert release.wait(4)
        return {"status": "paused"}
    runner = OperationalTaskRunner(LocalTaskScheduler(lock_path=lock_path),
        {TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value: handler}, AgentStateStore(repo.storage))
    started = runner.start(OperationalTaskRunInput(task_id=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
                                                 thread_id="chat"))
    run_id = started.data.run_id
    try:
        assert entered.wait(2)
        assert request_daily_control(repo.storage, run_id, action)["success"]
        assert stop_seen.wait(3)
        second_lock = LocalInstanceLock(lock_path)
        assert not second_lock.acquire()
        assert task_progress(repo.storage)["run"]["status"] == ("pausing" if action == "pause" else "cancelling")
    finally:
        release.set()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and runner.background_status(run_id)["run_status"] != terminal:
        time.sleep(0.01)
    assert runner.background_status(run_id)["run_status"] == terminal
    assert task_progress(repo.storage)["runs"] == []
    free_lock = LocalInstanceLock(lock_path)
    assert free_lock.acquire()
    free_lock.release()
