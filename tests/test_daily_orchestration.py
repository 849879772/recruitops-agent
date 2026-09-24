from datetime import datetime, timedelta, timezone

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
