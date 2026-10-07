from datetime import datetime, timedelta, timezone

import pytest

from packages.orchestration import DailyRecruitmentSync, DailySyncStage, DailySyncStatus
from packages.pipeline.daily import PipelineInterrupted


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.value += timedelta(seconds=1)
        return self.value


class StateStore:
    def __init__(self):
        self.rows = []

    def save_task_run(self, row):
        self.rows.append(row)


def test_complete_sync_runs_deterministic_stages_in_order() -> None:
    calls = []
    state = StateStore()
    sync = DailyRecruitmentSync(
        discovery=lambda: calls.append("discover") or {"lead_count": 3},
        reconcile=lambda value: calls.append("reconcile") or {"existing_count": value["lead_count"]},
        crawl=lambda dry_run: calls.append(("crawl", dry_run)) or {"new_count": 2},
        offline_reconcile=lambda result, dry_run: calls.append(("offline", dry_run)) or {"inactive_count": 0},
        report=lambda pipeline, reconciliation, offline: calls.append("report") or {"issue_count": 0},
        state_store=state,
        clock=Clock(),
    )

    result = sync.run(run_id="run-1")

    assert result.status is DailySyncStatus.SUCCEEDED
    assert calls == ["discover", "reconcile", ("crawl", False), ("offline", False), "report"]
    assert [event.stage for event in result.stages if event.status.value == "succeeded"] == [
        DailySyncStage.DISCOVERY,
        DailySyncStage.RECONCILIATION,
        DailySyncStage.CRAWL,
        DailySyncStage.OFFLINE_RECONCILIATION,
        DailySyncStage.REPORTING,
    ]
    assert state.rows[-1].status.value == "succeeded"
    assert state.rows[-1].current_step == "completed"


def test_source_failure_degrades_but_still_crawls_known_companies() -> None:
    def fail_discovery():
        raise RuntimeError("source unavailable")

    result = DailyRecruitmentSync(
        discovery=fail_discovery,
        reconcile=lambda _value: (_ for _ in ()).throw(AssertionError("must be skipped")),
        crawl=lambda _dry_run: {"new_count": 1},
    ).run(run_id="run-2")

    assert result.status is DailySyncStatus.DEGRADED
    assert result.pipeline == {"new_count": 1}
    assert any(event.stage is DailySyncStage.CRAWL and event.status.value == "succeeded" for event in result.stages)


def test_crawl_failure_stops_all_write_dependent_stages() -> None:
    calls = []

    def fail_crawl(_dry_run):
        raise RuntimeError("crawler registry failed")

    result = DailyRecruitmentSync(
        crawl=fail_crawl,
        offline_reconcile=lambda *_args: calls.append("offline"),
        report=lambda *_args: calls.append("report"),
    ).run(run_id="run-3")

    assert result.status is DailySyncStatus.FAILED
    assert calls == []
    assert result.error == "RuntimeError: crawler registry failed"


def test_crawl_pause_is_recoverable_and_skips_later_stages() -> None:
    calls = []
    state = StateStore()

    def pause(_dry_run):
        raise PipelineInterrupted("time budget reached")

    result = DailyRecruitmentSync(
        crawl=pause,
        offline_reconcile=lambda *_args: calls.append("offline"),
        report=lambda *_args: calls.append("report"),
        state_store=state,
    ).run(run_id="paused-run")

    assert result.status is DailySyncStatus.PAUSED
    assert result.error == "time_budget_reached"
    assert calls == []
    assert state.rows[-1].status.value == "stopped"
    assert state.rows[-1].error_code == "time_budget_reached"


def test_dry_run_propagates_to_crawl_and_offline_reconciliation() -> None:
    observed = []
    result = DailyRecruitmentSync(
        crawl=lambda dry_run: observed.append(("crawl", dry_run)) or {},
        offline_reconcile=lambda _result, dry_run: observed.append(("offline", dry_run)) or {},
    ).run(run_id="run-4", dry_run=True)

    assert result.dry_run is True
    assert observed == [("crawl", True), ("offline", True)]


@pytest.mark.parametrize("payload", [
    {"failed_jobs": 1239},
    {"failed_companies": 335},
    {"scoring_failed": 1},
    {"companies": [{"status": "partial", "list_complete": False}]},
    {"companies": [{"status": "completed", "detail_failure_count": 1}]},
    {"job_write_statistics": {"new_pending_count": 9}},
    {"status": "completed", "failed": 1},
    {"failed_companies": 0, "failed_jobs": 0, "companies": [{
        "status": "partial", "list_complete": True, "detail_failure_count": 0,
        "failure_reason": "detail_capture_failed",
    }]},
])
def test_crawl_business_gaps_degrade_terminal_status_without_discarding_results(payload):
    calls = []
    result = DailyRecruitmentSync(
        crawl=lambda _dry_run: payload,
        offline_reconcile=lambda *_args: calls.append("safe_company_reconcile") or {},
        report=lambda *_args: calls.append("report") or {},
    ).run()
    assert result.status is DailySyncStatus.DEGRADED
    assert result.pipeline == payload
    assert result.warnings
    assert calls == ["safe_company_reconcile", "report"]
    crawl = [event for event in result.stages if event.stage is DailySyncStage.CRAWL][-1]
    assert crawl.status.value == "partial"


def test_filtering_reuse_and_disabled_analysis_do_not_mean_incomplete():
    result = DailyRecruitmentSync(crawl=lambda _: {
        "reused": 15000, "filtered": 70000, "rejected": 20,
        "failed_jobs": 0, "failed_companies": 0, "analysis_enabled": False, "unscored": 15,
        "companies": [{"status": "completed", "list_complete": True}],
    }).run()
    assert result.status is DailySyncStatus.SUCCEEDED


def test_dry_run_predicted_pending_records_are_not_actual_failures():
    result = DailyRecruitmentSync(crawl=lambda _: {
        "dry_run": True, "job_write_statistics": {"new_pending_count": 9},
    }).run(dry_run=True)
    assert result.status is DailySyncStatus.SUCCEEDED
