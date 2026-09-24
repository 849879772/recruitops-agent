from packages.scheduler import LocalTaskScheduler, TaskType
import time

from packages.tools.daily_sync import (
    DailyRecruitmentSyncInput,
    DailyRecruitmentSyncStatusInput,
    get_daily_recruitment_sync_status,
    run_daily_recruitment_sync,
    _compact_status_result,
)
from packages.tools.operations import OperationalTaskRunner
from pydantic import ValidationError
import pytest


def test_background_flow_returns_before_handler_finishes_without_a_schedule(tmp_path):
    from threading import Event

    started, release = Event(), Event()
    calls = []

    def handler(context):
        calls.append(context)
        started.set()
        assert release.wait(5)
        return {"status": "completed", "fixture": True}

    runner = OperationalTaskRunner(LocalTaskScheduler(lock_path=tmp_path / "task.lock"),
        {TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value: handler})
    try:
        response = run_daily_recruitment_sync(DailyRecruitmentSyncInput(mode="full"), runner)
        assert started.wait(2)
        assert response.data.run_status in {"accepted", "running"}
        assert runner.background_status(response.data.run_id)["run_status"] == "running"
    finally:
        release.set()
    for _ in range(100):
        status = get_daily_recruitment_sync_status(
            DailyRecruitmentSyncStatusInput(run_id=response.data.run_id, timeout_ms=5000), runner)
        if status.data.run_status == "success":
            break
        time.sleep(0.01)
    assert status.data.run_status == "success"
    assert status.data.result["fixture"] is True
    assert len(calls) == 1
    assert calls[0].metadata["details"]["mode"] == "full"


def test_daily_sync_status_is_an_immediate_snapshot(tmp_path) -> None:
    class RunningStore:
        def get_task_run(self, run_id):
            return {
                "run_id": run_id,
                "task_id": TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
                "run_status": "running",
                "current_step": "companies:1/10",
                "step_count": 1,
                "error": None,
                "state": {"progress": {
                    "stage": "companies", "scope_total": 10,
                    "attempted_unique": 3, "confirmed_complete": 1,
                    "retry_pending": 2, "remaining": 9,
                }},
            }

    runner = OperationalTaskRunner(
        LocalTaskScheduler(lock_path=tmp_path / "task.lock"),
        {},
        state_store=RunningStore(),
    )
    started = time.monotonic()
    response = get_daily_recruitment_sync_status(
        DailyRecruitmentSyncStatusInput(run_id="running-snapshot", timeout_ms=120_000),
        runner,
    )

    assert time.monotonic() - started < 0.5
    assert response.data.run_status == "running"
    assert response.data.current_step == "companies:1/10"
    assert response.data.progress["attempted_unique"] == 3
    assert response.data.progress["retry_pending"] == 2


def test_company_batch_limit_reaches_runtime_handler(tmp_path) -> None:
    runner = OperationalTaskRunner(
        LocalTaskScheduler(lock_path=tmp_path / "task.lock"),
        {TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value:
         lambda context: {"batch_limit": context.metadata["details"]["company_batch_limit"]}},
    )

    response = run_daily_recruitment_sync(
        DailyRecruitmentSyncInput(mode="full", dry_run=True, company_batch_limit=1500),
        runner,
    )

    assert response.data.company_batch_limit == 1500
    assert response.data.result["batch_limit"] == 1500


def test_daily_sync_tool_has_no_arbitrary_task_selector(tmp_path) -> None:
    runner = OperationalTaskRunner(
        LocalTaskScheduler(lock_path=tmp_path / "task.lock"),
        {
            TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value: lambda context: {
                "run_id": context.run_id,
                "stages": [{"stage": "crawl", "status": "succeeded"}],
            }
        },
    )

    response = run_daily_recruitment_sync(DailyRecruitmentSyncInput(), runner)

    assert response.success is True
    assert response.read_only is False
    assert response.data is not None
    assert response.data.run_status in {"accepted", "running"}
    for _ in range(100):
        status = get_daily_recruitment_sync_status(
            DailyRecruitmentSyncStatusInput(run_id=response.data.run_id), runner
        )
        assert status.data is not None
        if status.data.run_status == "success":
            break
        time.sleep(0.01)
    assert status.data.result["stages"][0]["stage"] == "crawl"


def test_daily_sync_tool_forwards_dry_run(tmp_path) -> None:
    runner = OperationalTaskRunner(
        LocalTaskScheduler(lock_path=tmp_path / "task.lock"),
        {
            TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value: lambda context: {
                "requested_dry_run": context.metadata["details"]["requested_dry_run"]
            }
        },
    )

    response = run_daily_recruitment_sync(
        DailyRecruitmentSyncInput(dry_run=True),
        runner,
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.dry_run is True
    assert response.data.run_status == "success"
    assert response.data.result["requested_dry_run"] is True


def test_daily_sync_tool_forwards_bounded_company_scope(tmp_path) -> None:
    runner = OperationalTaskRunner(
        LocalTaskScheduler(lock_path=tmp_path / "task.lock"),
        {
            TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value: lambda context: {
                "company_ids": context.metadata["details"]["company_ids"]
            }
        },
    )

    response = run_daily_recruitment_sync(
        DailyRecruitmentSyncInput(dry_run=True, company_ids=["company-a", "company-a", "company-b"]),
        runner,
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.result["company_ids"] == ["company-a", "company-b"]


def test_daily_sync_tool_forwards_offerbiu_source_scope(tmp_path) -> None:
    runner = OperationalTaskRunner(
        LocalTaskScheduler(lock_path=tmp_path / "task.lock"),
        {
            TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value: lambda context: {
                "source_record_ids": context.metadata["details"]["source_record_ids"]
            }
        },
    )

    response = run_daily_recruitment_sync(
        DailyRecruitmentSyncInput(
            dry_run=True,
            source_record_ids=["source-a", "source-a", "source-b"],
        ),
        runner,
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.source_record_ids == ["source-a", "source-b"]
    assert response.data.result["source_record_ids"] == ["source-a", "source-b"]


def test_daily_sync_rejects_mixed_company_and_source_scopes() -> None:
    with pytest.raises(ValidationError, match="cannot be combined"):
        DailyRecruitmentSyncInput(
            company_ids=["company-a"],
            source_record_ids=["source-a"],
        )


def test_daily_sync_status_can_read_persisted_stage_after_runtime_restart(tmp_path) -> None:
    class PersistedStore:
        def get_task_run(self, run_id):
            return {
                "run_id": run_id,
                "task_id": TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
                "run_status": "running",
                "current_step": "crawl:running",
                "step_count": 5,
                "error": None,
            }

    runner = OperationalTaskRunner(
        LocalTaskScheduler(lock_path=tmp_path / "task.lock"),
        {},
        state_store=PersistedStore(),
    )
    response = get_daily_recruitment_sync_status(
        DailyRecruitmentSyncStatusInput(run_id="persisted-run-123"), runner
    )

    assert response.success is True
    assert response.data is not None
    assert response.data.current_step == "crawl:running"
    assert response.data.step_count == 5


def test_daily_sync_status_compacts_large_pipeline_payload() -> None:
    compact = _compact_status_result({
        "pipeline": {
            "selected_companies": 1,
            "analysis_enabled": True,
            "scoring_candidates": 11,
            "scored": 7,
            "scoring_failed": 4,
            "unscored": 4,
            "rejected_job_ids": [f"job-{index}" for index in range(500)],
            "companies": [{
                "company_id": "company-a",
                "status": "partial",
                "rejected_job_ids": [f"job-{index}" for index in range(500)],
                "crawl_evidence": {"html": "x" * 100_000},
            }],
        },
        "status": "success",
    })

    assert compact["analysis_enabled"] is True
    assert compact["scoring_candidates"] == 11
    assert compact["scored"] == 7
    assert "rejected_job_ids" not in compact
    assert compact["companies"] == [{"company_id": "company-a", "status": "partial"}]


@pytest.mark.parametrize("mode", ["full", "crawl_only", "score_only"])
def test_daily_sync_tool_forwards_stage_mode(tmp_path, mode) -> None:
    runner = OperationalTaskRunner(
        LocalTaskScheduler(lock_path=tmp_path / "task.lock"),
        {
            TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value: lambda context: {
                "mode": context.metadata["details"]["mode"]
            }
        },
    )
    response = run_daily_recruitment_sync(
        DailyRecruitmentSyncInput(dry_run=True, mode=mode), runner
    )
    assert response.success is True
    assert response.data is not None
    assert response.data.result["mode"] == mode


def test_resume_mode_requires_a_run_id() -> None:
    with pytest.raises(ValidationError, match="resume_run_id"):
        DailyRecruitmentSyncInput(mode="resume")
