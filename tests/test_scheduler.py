from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import time as time_module
from threading import Event

import pytest

from packages.scheduler import (
    DailySchedule,
    LocalInstanceLock,
    LocalTaskScheduler,
    RunStatus,
    TaskContext,
    TaskDefinition,
    TaskType,
    default_task_definitions,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 20, 20, 30, tzinfo=timezone.utc)


def _demo_task(**updates: object) -> TaskDefinition:
    values: dict[str, object] = {
        "task_id": "demo",
        "label": "Deterministic demo",
        "schedule": DailySchedule(time(8, 0)),
        "timeout_seconds": 1.0,
        "max_retries": 0,
        "misfire_grace_seconds": 60.0,
    }
    values.update(updates)
    return TaskDefinition(**values)


def _scheduler(tmp_path: Path, task: TaskDefinition | None = None) -> LocalTaskScheduler:
    tasks = {task.task_id: task} if task is not None else default_task_definitions()
    return LocalTaskScheduler(tasks=tasks, lock_path=tmp_path / "scheduler.lock")


def test_fixed_catalog_contains_four_read_only_daily_tasks() -> None:
    definitions = default_task_definitions()

    assert set(definitions) == {
        TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
        TaskType.CRAWLER_HEALTH.value,
        TaskType.APPLICATION_PROGRESS.value,
        TaskType.RECRUITMENT_MAILBOX.value,
    }
    assert [definition.schedule.start_time for definition in definitions.values()] == [
        time(8, 0),
        time(8, 15),
        time(20, 0),
        time(9, 0),
    ]
    assert all(definition.read_only for definition in definitions.values())
    assert definitions[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value].agent_write_enabled is True
    assert all(
        not definition.agent_write_enabled
        for task_id, definition in definitions.items()
        if task_id != TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value
    )


def test_daily_task_allows_only_agent_owned_writes(tmp_path: Path) -> None:
    received: list[TaskContext] = []

    result = _scheduler(tmp_path).run(
        TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
        lambda context: received.append(context) or "written",
        now=NOW,
        scheduled_for=NOW,
    )

    assert result.status is RunStatus.SUCCESS
    assert result.read_only is True
    assert received[0].read_only is True
    assert received[0].write_enabled is True


def test_cooperative_timeout_waits_for_checkpoint_drain_before_terminal_result(tmp_path: Path) -> None:
    task = _demo_task(timeout_seconds=0.01, cooperative_timeout=True)
    scheduler = _scheduler(tmp_path, task)
    drained = Event()

    def handler(context: TaskContext) -> dict[str, str]:
        assert context.stop_requested.wait(1)
        time_module.sleep(0.02)
        drained.set()
        return {"status": "paused"}

    result = scheduler.run("demo", handler, now=NOW, scheduled_for=NOW)

    assert result.status is RunStatus.PAUSED
    assert drained.is_set()
    assert scheduler.run("demo", lambda _: {"status": "completed"}, now=NOW, scheduled_for=NOW).status is RunStatus.SUCCESS


def test_injected_handler_receives_read_only_context_and_no_store(tmp_path: Path) -> None:
    received: list[TaskContext] = []

    def handler(context: TaskContext) -> dict[str, object]:
        received.append(context)
        return {
            "task_id": context.task_id,
            "read_only": context.read_only,
            "write_enabled": context.write_enabled,
        }

    result = _scheduler(tmp_path, _demo_task()).run(
        "demo",
        handler,
        now=NOW,
        scheduled_for=NOW,
        run_id="injected-1",
    )

    assert result.status is RunStatus.SUCCESS
    assert result.read_only is True
    assert result.value == {
        "task_id": "demo",
        "read_only": True,
        "write_enabled": False,
    }
    assert len(received) == 1
    assert received[0].attempt == 1
    assert not (tmp_path / "formal.db").exists()


def test_failures_are_retried_only_within_the_configured_budget(tmp_path: Path) -> None:
    attempts: list[int] = []

    def flaky(context: TaskContext) -> str:
        attempts.append(context.attempt)
        if context.attempt < 3:
            raise RuntimeError("temporary read failure")
        return "observed"

    result = _scheduler(tmp_path, _demo_task(max_retries=2)).run(
        "demo",
        flaky,
        now=NOW,
        scheduled_for=NOW,
        run_id="retry-1",
    )

    assert result.status is RunStatus.SUCCESS
    assert result.attempts == 3
    assert result.retry_count == 2
    assert attempts == [1, 2, 3]


def test_retry_budget_exhaustion_is_a_failed_result(tmp_path: Path) -> None:
    attempts: list[int] = []

    def always_fails(context: TaskContext) -> None:
        attempts.append(context.attempt)
        raise ValueError("fixed failure")

    result = _scheduler(tmp_path, _demo_task(max_retries=1)).run(
        "demo",
        always_fails,
        now=NOW,
        scheduled_for=NOW,
        run_id="retry-2",
    )

    assert result.status is RunStatus.FAILED
    assert result.attempts == 2
    assert attempts == [1, 2]
    assert result.error == "ValueError: fixed failure"


def test_missed_schedule_is_executed_with_catch_up_metadata(tmp_path: Path) -> None:
    scheduled = NOW - timedelta(minutes=20)
    result = _scheduler(tmp_path, _demo_task()).run(
        "demo",
        lambda _context: "caught up",
        now=NOW,
        scheduled_for=scheduled,
        run_id="catch-up-1",
    )

    assert result.status is RunStatus.SUCCESS
    assert result.metadata["missed"] is True
    assert result.metadata["catch_up"] is True
    assert result.metadata["reason"] == "missed_schedule_catch_up"
    assert result.metadata["lateness_seconds"] == 1200.0


def test_dry_run_does_not_invoke_handler_or_acquire_lock(tmp_path: Path) -> None:
    called = False

    def handler(_context: TaskContext) -> None:
        nonlocal called
        called = True

    lock_path = tmp_path / "scheduler.lock"
    result = _scheduler(tmp_path, _demo_task()).run(
        "demo",
        handler,
        now=NOW,
        scheduled_for=NOW,
        run_id="dry-run-1",
        dry_run=True,
    )

    assert result.status is RunStatus.DRY_RUN
    assert result.value == {"planned": True, "handler_injected": True}
    assert called is False
    assert lock_path.exists() is False


def test_local_instance_lock_is_non_blocking_and_process_local(tmp_path: Path) -> None:
    path = tmp_path / "instance.lock"
    first = LocalInstanceLock(path)
    second = LocalInstanceLock(path)

    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_timeout_does_not_start_an_overlapping_retry_and_lock_is_released_after_exit(
    tmp_path: Path,
) -> None:
    entered = Event()
    release_handler = Event()
    finished = Event()

    def slow_handler(_context: TaskContext) -> str:
        entered.set()
        release_handler.wait(2.0)
        finished.set()
        return "finished"

    scheduler = _scheduler(tmp_path, _demo_task(timeout_seconds=0.03, max_retries=2))
    result = scheduler.run(
        "demo",
        slow_handler,
        now=NOW,
        scheduled_for=NOW,
        run_id="timeout-1",
    )

    assert entered.wait(1.0)
    assert result.status is RunStatus.TIMED_OUT
    assert result.attempts == 1

    locked = scheduler.run(
        "demo",
        lambda _context: "overlap",
        now=NOW,
        scheduled_for=NOW,
        run_id="timeout-overlap",
    )
    assert locked.status is RunStatus.SKIPPED_LOCKED

    release_handler.set()
    assert finished.wait(1.0)
    for _ in range(20):
        after = scheduler.run(
            "demo",
            lambda _context: "after",
            now=NOW,
            scheduled_for=NOW,
            run_id="timeout-after",
        )
        if after.status is RunStatus.SUCCESS:
            break
        time_module.sleep(0.01)
    assert after.status is RunStatus.SUCCESS


def test_cli_dry_run_is_offline_and_json_serializable() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_local_task.py",
            "--task",
            TaskType.RECRUITMENT_MAILBOX.value,
            "--now",
            "2026-08-20T09:30:00+00:00",
            "--scheduled-for",
            "2026-08-20T09:25:00+00:00",
            "--run-id",
            "cli-dry-run-1",
            "--dry-run",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == RunStatus.DRY_RUN.value
    assert payload["read_only"] is True
    assert payload["metadata"]["catch_up"] is False


def test_windows_scripts_use_schtasks_safe_arguments_and_dry_run_switch() -> None:
    install = (ROOT / "scripts" / "install_windows_tasks.ps1").read_text(encoding="utf-8")
    uninstall = (ROOT / "scripts" / "uninstall_windows_tasks.ps1").read_text(encoding="utf-8")

    assert "schtasks.exe" in install
    assert "/Create" in install
    assert "/TR" in install
    assert "ConvertTo-WindowsCommandLineArgument" in install
    assert "[switch]$DryRun" in install
    assert "schtasks.exe" in uninstall
    assert "/Delete" in uninstall
    assert "ConvertTo-WindowsCommandLineArgument" in uninstall
    assert "[switch]$DryRun" in uninstall
    for task_id in default_task_definitions():
        assert task_id in install
        assert task_id in uninstall


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell task scripts are Windows-only")
def test_windows_task_install_and_uninstall_dry_runs_execute() -> None:
    for script in ("install_windows_tasks.ps1", "uninstall_windows_tasks.ps1"):
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                f"scripts/{script}",
                "-DryRun",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.count("DRY-RUN schtasks.exe") == 4


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell service scripts are Windows-only")
def test_local_service_start_dry_run_executes_without_starting_a_process() -> None:
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "scripts/start_local.ps1",
            "-DryRun",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert "DRY-RUN start RecruitOps API" in completed.stdout


def test_task_definitions_reject_write_mode() -> None:
    with pytest.raises(ValueError, match="read-only"):
        TaskDefinition(
            task_id="write-task",
            label="Write task",
            schedule=DailySchedule(time(8, 0)),
            read_only=False,
        )
