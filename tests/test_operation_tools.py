from datetime import datetime

import pytest

from packages.scheduler import LocalTaskScheduler, TaskType
from packages.tools.operations import (
    AutomationPlanInput,
    OperationalTaskRunInput,
    OperationalTaskRunner,
    plan_automation,
)
from packages.domain.models import TaskRun, TaskStatus
from packages.storage import AgentStateStore, Storage


def test_manual_operation_runs_only_a_fixed_task(tmp_path) -> None:
    observed = []
    scheduler = LocalTaskScheduler(lock_path=tmp_path / "task.lock")
    runner = OperationalTaskRunner(
        scheduler,
        {
            TaskType.CRAWLER_HEALTH.value: lambda context: observed.append(context.task_id)
            or {"status": "observed"}
        },
    )

    response = runner.run(
        OperationalTaskRunInput(task_id=TaskType.CRAWLER_HEALTH.value)
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.run_status == "success"
    assert response.data.agent_write_enabled is False
    assert observed == [TaskType.CRAWLER_HEALTH.value]


def test_manual_daily_operation_reports_agent_write_capability_without_legacy_write(tmp_path) -> None:
    scheduler = LocalTaskScheduler(lock_path=tmp_path / "task.lock")
    runner = OperationalTaskRunner(
        scheduler,
        {
            TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value: lambda context: {
                "agent_write_performed": context.write_enabled,
                "source_write_attempted": False,
            }
        },
    )

    response = runner.run(
        OperationalTaskRunInput(
            task_id=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value
        )
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.agent_write_enabled is True
    assert response.data.result["source_write_attempted"] is False


def test_automation_plan_is_inert_and_never_installs_a_task(tmp_path) -> None:
    scheduler = LocalTaskScheduler(lock_path=tmp_path / "task.lock")

    response = plan_automation(
        AutomationPlanInput(
            task_id=TaskType.RECRUITMENT_MAILBOX.value,
            start_time="09:30",
        ),
        scheduler,
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.active is False
    assert response.data.activation_status == "not_installed"
    assert response.data.activation_requires_human_approval is True
    assert "schtasks" not in " ".join(response.data.command_argv).casefold()


def test_unknown_operational_task_is_rejected_before_execution() -> None:
    with pytest.raises(ValueError, match="fixed allowlist"):
        OperationalTaskRunInput(task_id="arbitrary_shell_task")


def test_company_scope_is_rejected_for_non_daily_operation() -> None:
    with pytest.raises(ValueError, match="only valid for the daily"):
        OperationalTaskRunInput(
            task_id=TaskType.CRAWLER_HEALTH.value,
            company_ids=["company-1"],
        )


def test_invalid_automation_time_is_rejected() -> None:
    with pytest.raises(ValueError):
        AutomationPlanInput(
            task_id=TaskType.CRAWLER_HEALTH.value,
            start_time="25:00",
        )


def test_startup_recovery_marks_running_task_as_recoverable() -> None:
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    store = AgentStateStore(storage)
    store.save_task_run(TaskRun(
        id="interrupted-run",
        task_type=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
        status=TaskStatus.RUNNING,
        user_request="daily sync",
        current_step="matching:12/40",
        source="test",
    ))

    assert store.heartbeat_task_run("interrupted-run") is True
    assert store.recover_interrupted_task_runs() == 1
    recovered = store.get_task_run("interrupted-run")
    assert recovered is not None
    assert recovered["run_status"] == "stopped"
    assert recovered["current_step"] == "recoverable:matching:12/40"
    assert recovered["error"] == "process_interrupted"


def test_restarted_status_preserves_resume_lineage_and_durable_progress() -> None:
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    store = AgentStateStore(storage)
    store.save_task_run(TaskRun(
        id="paused-resume-run",
        task_type=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
        status=TaskStatus.STOPPED,
        user_request="daily sync",
        current_step="paused",
        source="test",
    ))
    store.save_task_state("paused-resume-run", {
        "metadata": {
            "requested_mode": "full",
            "resumed_from": "original-run",
            "company_ids": ["a", "b"],
        },
        "progress": {
            "stage": "companies", "scope_total": 2,
            "attempted_unique": 1, "confirmed_complete": 1,
            "retry_pending": 0, "remaining": 1,
        },
        "result": {"status": "paused"},
    })
    runner = OperationalTaskRunner(LocalTaskScheduler(), {}, state_store=store)

    observed = runner.background_status("paused-resume-run")

    assert observed is not None
    assert observed["run_status"] == "paused"
    assert observed["mode"] == "resume"
    assert observed["resume_run_id"] == "original-run"
    assert observed["progress"]["confirmed_complete"] == 1
