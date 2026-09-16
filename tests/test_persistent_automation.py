from __future__ import annotations

import asyncio
from datetime import datetime, time, timezone

from packages.automation import (
    AutomationRunResult,
    AutomationStore,
    LocalAutomationWorker,
    next_daily_run,
)
from packages.storage import Storage


def _storage(tmp_path) -> Storage:
    return Storage.from_url(f"sqlite:///{tmp_path / 'agent.db'}", initialize=True)


def test_next_daily_run_uses_asia_shanghai_wall_clock() -> None:
    before = datetime(2026, 8, 23, 18, 59, tzinfo=timezone.utc)  # 02:59 next day
    at = datetime(2026, 8, 23, 19, 0, tzinfo=timezone.utc)  # 03:00 next day

    assert next_daily_run(time(3, 0), now=before) == datetime(
        2026, 8, 23, 19, 0, tzinfo=timezone.utc
    )
    assert next_daily_run(time(3, 0), now=at) == datetime(
        2026, 8, 24, 19, 0, tzinfo=timezone.utc
    )


def test_schedule_is_active_idempotent_and_survives_store_restart(tmp_path) -> None:
    storage = _storage(tmp_path)
    first = AutomationStore(storage).upsert_daily(
        task_id="application_progress",
        task_label="本地投递进度复核",
        start_time=time(3, 0),
        target_kind="application",
        target_id="24",
        target_label="新华三 / 软件开发工程师-C/C++",
        now=datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc),
    )
    updated = AutomationStore(storage).upsert_daily(
        task_id="application_progress",
        task_label="本地投递进度复核",
        start_time=time(3, 15),
        target_kind="application",
        target_id="24",
        target_label="新华三 / 软件开发工程师-C/C++",
        now=datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc),
    )
    restarted = AutomationStore(Storage.from_url(f"sqlite:///{tmp_path / 'agent.db'}"))
    rows = restarted.list(active_only=True)

    assert first.id == updated.id
    assert len(rows) == 1
    assert rows[0].active is True
    assert rows[0].start_time == time(3, 15)
    assert rows[0].target_id == "24"


def test_worker_claims_due_schedule_and_persists_execution(tmp_path) -> None:
    store = AutomationStore(_storage(tmp_path))
    schedule = store.upsert_daily(
        task_id="application_progress",
        task_label="本地投递进度复核",
        start_time=time(3, 0),
        target_kind="application",
        target_id="24",
        now=datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc),
    )
    with store.storage.transaction(write=True) as session:
        session.get(type(schedule), schedule.id).next_run_at = datetime(
            2000, 1, 1, 0, 0, tzinfo=timezone.utc
        )

    observed = []

    async def execute(claimed):
        observed.append(claimed)
        return AutomationRunResult(
            status="succeeded",
            summary="verified unchanged",
            thread_id="thread-1",
            turn_id="turn-1",
        )

    worker = LocalAutomationWorker(store, execute)
    assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is False
    executions = store.executions(schedule.id)

    assert len(observed) == 1
    assert observed[0].target_id == "24"
    assert len(executions) == 1
    assert executions[0].status == "succeeded"
    assert executions[0].result_summary == "verified unchanged"
    persisted = store.list(active_only=True)[0]
    assert persisted.last_status == "succeeded"
    assert persisted.next_run_at.date() >= datetime.now(timezone.utc).date()


def test_previous_failure_is_retained_without_blocking_next_due_claim(tmp_path) -> None:
    store = AutomationStore(_storage(tmp_path))
    schedule = store.upsert_daily(
        task_id="application_progress",
        task_label="本地投递进度复核",
        start_time=time(3, 0),
        target_kind="application",
        target_id="24",
        now=datetime(2026, 9, 6, 0, 0, tzinfo=timezone.utc),
    )
    first_due = datetime(2026, 9, 6, 19, 0, tzinfo=timezone.utc)
    with store.storage.transaction(write=True) as session:
        session.get(type(schedule), schedule.id).next_run_at = first_due

    first = store.claim_due(now=datetime(2026, 9, 7, 0, 0, tzinfo=timezone.utc))
    assert first is not None
    store.complete(
        first.execution_id,
        status="failed",
        error="Reconnecting... waiting for network",
        now=datetime(2026, 9, 7, 0, 0, 1, tzinfo=timezone.utc),
    )

    # Simulate an overdue next occurrence while retaining the prior execution row.
    with store.storage.transaction(write=True) as session:
        session.get(type(schedule), schedule.id).next_run_at = first_due

    assert store.claim_due(now=datetime(2026, 9, 7, 0, 1, tzinfo=timezone.utc)) is None
    advanced = store.list(active_only=True)[0]
    assert advanced.next_run_at.replace(tzinfo=timezone.utc) == datetime(
        2026, 9, 7, 19, 0, tzinfo=timezone.utc
    )

    second = store.claim_due(now=datetime(2026, 9, 8, 0, 1, tzinfo=timezone.utc))
    assert second is not None
    assert second.execution_id != first.execution_id
    assert second.scheduled_for == datetime(2026, 9, 7, 19, 0, tzinfo=timezone.utc)

    executions = store.executions(schedule.id)
    assert len(executions) == 2
    assert any(
        execution.status == "failed"
        and execution.error == "Reconnecting... waiting for network"
        for execution in executions
    )
    persisted = store.list(active_only=True)[0]
    assert persisted.active is True
    assert persisted.last_status == "running"
    assert persisted.last_error is None


def test_restart_marks_interrupted_execution_failed_without_disabling_schedule(tmp_path) -> None:
    store = AutomationStore(_storage(tmp_path))
    schedule = store.upsert_daily(
        task_id="application_progress",
        task_label="本地投递进度复核",
        start_time=time(3, 0),
        target_kind="application",
        target_id="24",
        now=datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc),
    )
    with store.storage.transaction(write=True) as session:
        session.get(type(schedule), schedule.id).next_run_at = datetime(
            2000, 1, 1, 0, 0, tzinfo=timezone.utc
        )
    claimed = store.claim_due()
    assert claimed is not None
    store.mark_running_context(
        claimed.execution_id,
        thread_id="thread-running",
        turn_id="turn-running",
    )

    assert store.recover_interrupted() == 1
    execution = store.executions(schedule.id)[0]
    persisted = store.list(active_only=True)[0]
    assert execution.status == "failed"
    assert execution.thread_id == "thread-running"
    assert "restarted" in (execution.error or "")
    assert persisted.active is True
    assert persisted.last_status == "failed"
