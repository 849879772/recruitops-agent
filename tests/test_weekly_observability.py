from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from packages.codex_runtime.telemetry import CodexTrace, JsonlTraceRecorder
from packages.observability.weekly import (
    aggregate_weekly_observability,
    render_weekly_markdown,
    report_to_json,
)
from packages.storage import (
    Base,
    BrowserOperation,
    JobAnalysisSnapshot,
    JobSnapshot,
    Storage,
    TaskRun,
    create_storage_engine,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


@pytest.fixture
def sqlite_storage():
    engine = create_storage_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    storage = Storage(engine)
    yield storage
    engine.dispose()


def _timestamp(day_offset: int, hour: int = 8) -> datetime:
    return NOW + timedelta(days=day_offset - 7, hours=hour - 12)


def _task(run_id: str, status: str, timestamp: datetime) -> TaskRun:
    return TaskRun(
        id=run_id,
        idempotency_key=f"task:{run_id}",
        task_type="daily_sync",
        status=status,
        user_request="fixture",
        created_at=timestamp,
        updated_at=timestamp,
        source="fixture",
        source_ref=run_id,
    )


def _browser(
    operation_id: str,
    status: str,
    timestamp: datetime,
    error_code: str | None = None,
) -> BrowserOperation:
    return BrowserOperation(
        operation_id=operation_id,
        idempotency_key=f"browser:{operation_id}",
        operation="observe_application_status_page",
        device_id="edge-fixture",
        status=status,
        command={},
        error_code=error_code,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _job(job_id: str, timestamp: datetime) -> JobSnapshot:
    return JobSnapshot(
        id=job_id,
        company_id="company-fixture",
        title="Software Engineer",
        detail_url=f"https://example.test/jobs/{job_id}",
        cohort=2027,
        cohort_status="confirmed",
        batch="formal",
        source="fixture",
        source_ref=job_id,
        created_at=timestamp,
        updated_at=timestamp,
    )


def _analysis(
    job_id: str,
    timestamp: datetime,
    input_tokens: int,
    output_tokens: int,
) -> JobAnalysisSnapshot:
    return JobAnalysisSnapshot(
        job_id=job_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        analyzed_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
        source="fixture",
        source_ref=job_id,
    )


def _day(report: dict, date_value: str) -> dict:
    return next(item for item in report["daily"] if item["date"] == date_value)


def test_empty_sqlite_data_and_missing_telemetry_are_safe(sqlite_storage, tmp_path: Path) -> None:
    report = aggregate_weekly_observability(
        sqlite_storage,
        days=7,
        now=NOW,
        telemetry_path=tmp_path / "missing-codex-traces.jsonl",
    )

    assert report["period"] == {
        "start_date": "2026-08-24",
        "end_date": "2026-08-30",
        "days": 7,
    }
    assert len(report["daily"]) == 7
    assert all(day["task_runs"] == {"succeeded": 0, "failed": 0} for day in report["daily"])
    assert report["totals"]["browser_operations"] == {
        "failed": 0,
        "failure_types": {},
    }
    assert report["totals"]["job_analysis"] == {
        "count": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost": None,
    }
    assert report["availability"]["codex_telemetry"]["available"] is False
    assert "missing_file:codex_telemetry" in report["warnings"]


def test_missing_tables_are_safe_without_schema_initialization(tmp_path: Path) -> None:
    engine = create_storage_engine("sqlite:///:memory:")
    storage = Storage(engine)
    try:
        report = aggregate_weekly_observability(
            storage,
            days=2,
            now=NOW,
            telemetry_path=tmp_path / "missing-codex-traces.jsonl",
        )
    finally:
        engine.dispose()

    assert len(report["daily"]) == 2
    assert report["availability"]["task_runs"]["available"] is False
    assert report["availability"]["browser_operations"]["available"] is False
    assert report["availability"]["job_analysis"]["available"] is False
    assert report["totals"]["job_analysis"]["input_tokens"] is None


def test_weekly_aggregation_includes_optional_cost_and_codex_stage_timing(
    sqlite_storage,
    tmp_path: Path,
) -> None:
    with sqlite_storage.engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE job_analysis_snapshots "
                "ADD COLUMN cost_usd NUMERIC(12, 6)"
            )
        )

    with sqlite_storage.transaction() as session:
        session.add_all(
            [
                _task("task-success", "succeeded", _timestamp(1)),
                _task("task-failed", "failed", _timestamp(2)),
                _task("task-running", "running", _timestamp(3)),
                _task("task-outside", "succeeded", _timestamp(0)),
                _browser("browser-login", "FAILED", _timestamp(3), "login_required"),
                _browser("browser-unclear", "STATE_UNCLEAR", _timestamp(4)),
                _browser("browser-success", "SUCCEEDED", _timestamp(3)),
                _browser("browser-outside", "FAILED", _timestamp(0), "old_error"),
                _job("job-analysis-1", _timestamp(3)),
                _job("job-analysis-2", _timestamp(4)),
            ]
        )
        session.flush()
        session.add_all(
            [
                _analysis("job-analysis-1", _timestamp(3), 100, 30),
                _analysis("job-analysis-2", _timestamp(4), 50, 20),
            ]
        )

    with sqlite_storage.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE job_analysis_snapshots SET cost_usd = :cost "
                "WHERE job_id = :job_id"
            ),
            [{"cost": "0.25", "job_id": "job-analysis-1"}, {"cost": "0.10", "job_id": "job-analysis-2"}],
        )

    telemetry_path = tmp_path / "codex-traces.jsonl"
    recorder = JsonlTraceRecorder(telemetry_path)
    recorder.record(
        CodexTrace(
            trace_id="trace-in-window-1",
            observed_at=_timestamp(3),
            event_type="item/completed",
            method="item/completed",
            stage="model",
            phase="completed",
            latency_ms=100.5,
        )
    )
    recorder.record(
        CodexTrace(
            trace_id="trace-in-window-2",
            observed_at=_timestamp(4),
            event_type="item/completed",
            method="item/completed",
            stage="model",
            phase="completed",
            latency_ms=200,
        )
    )
    recorder.record(
        CodexTrace(
            trace_id="trace-in-window-3",
            observed_at=_timestamp(4),
            event_type="item/completed",
            method="item/completed",
            stage="tool",
            phase="completed",
            latency_ms=50,
        )
    )
    recorder.record(
        CodexTrace(
            trace_id="trace-outside",
            observed_at=_timestamp(0),
            event_type="item/completed",
            method="item/completed",
            stage="model",
            phase="completed",
            latency_ms=999,
        )
    )

    report = aggregate_weekly_observability(
        sqlite_storage,
        days=7,
        now=NOW,
        telemetry_path=telemetry_path,
    )
    day_26 = _day(report, "2026-08-26")
    day_27 = _day(report, "2026-08-27")

    assert day_26["browser_operations"] == {
        "failed": 1,
        "failure_types": {"login_required": 1},
    }
    assert day_27["browser_operations"] == {
        "failed": 1,
        "failure_types": {"state_unclear": 1},
    }
    assert day_26["job_analysis"] == {
        "count": 1,
        "input_tokens": 100,
        "output_tokens": 30,
        "total_tokens": 130,
        "cost": 0.25,
    }
    assert day_27["job_analysis"] == {
        "count": 1,
        "input_tokens": 50,
        "output_tokens": 20,
        "total_tokens": 70,
        "cost": 0.1,
    }
    assert day_27["stage_duration_ms"] == {
        "model": {
            "count": 1,
            "total_ms": 200,
            "average_ms": 200,
            "max_ms": 200,
        },
        "tool": {
            "count": 1,
            "total_ms": 50,
            "average_ms": 50,
            "max_ms": 50,
        },
    }
    assert report["totals"]["task_runs"] == {"succeeded": 1, "failed": 1}
    assert report["totals"]["browser_operations"]["failure_types"] == {
        "login_required": 1,
        "state_unclear": 1,
    }
    assert report["totals"]["job_analysis"]["total_tokens"] == 200
    assert report["totals"]["job_analysis"]["cost"] == 0.35
    assert report["totals"]["stage_duration_ms"]["model"] == {
        "count": 2,
        "total_ms": 300.5,
        "average_ms": 150.25,
        "max_ms": 200,
    }
    assert report["availability"]["job_analysis"]["fields"]["cost"] == "cost_usd"

    serialized = report_to_json(report)
    assert serialized == report_to_json(report)
    assert json.loads(serialized) == report
    markdown = render_weekly_markdown(report)
    assert "# Weekly Observability Report" in markdown
    assert "| **Total** | **1** | **1** |" in markdown
    assert "login_required: 1" in markdown
    assert "model" in markdown


def test_cli_days_and_output_write_both_formats(tmp_path: Path, capsys) -> None:
    from scripts.render_weekly_observability import main

    database_path = tmp_path / "weekly.sqlite"
    engine = create_storage_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    output_path = tmp_path / "weekly.json"

    assert (
        main(
            [
                "--days",
                "2",
                "--database-url",
                f"sqlite:///{database_path}",
                "--telemetry-path",
                str(tmp_path / "missing.jsonl"),
                "--output",
                str(output_path),
            ]
        )
        == 0
    )

    printed = json.loads(capsys.readouterr().out)
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert printed == written
    assert printed["period"]["days"] == 2
    assert output_path.with_suffix(".md").read_text(encoding="utf-8").startswith(
        "# Weekly Observability Report\n"
    )
