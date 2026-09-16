"""Read-only weekly observability aggregation for the Agent database and Codex traces."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, and_, func, inspect, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from packages.codex_runtime.telemetry import CodexTrace, JsonlTraceRecorder
from packages.storage import (
    AutomationExecution,
    BrowserOperation,
    JobAnalysisSnapshot,
    Storage,
    TaskRun,
)


WEEKLY_REPORT_SCHEMA_VERSION = "recruitops.observability.weekly.v1"
DEFAULT_DAYS = 7
UTC = timezone.utc

_TASK_SUCCESS_STATUSES = frozenset({"succeeded", "success", "completed", "complete", "ok"})
_TASK_FAILURE_STATUSES = frozenset(
    {"failed", "failure", "error", "timed_out", "timeout", "stopped", "cancelled"}
)
_BROWSER_FAILURE_STATUSES = frozenset(
    {"failed", "failure", "error", "timed_out", "timeout", "state_unclear", "cancelled"}
)

_TASK_TIME_FIELDS = ("completed_at", "finished_at", "ended_at", "updated_at", "created_at")
_BROWSER_TIME_FIELDS = ("updated_at", "completed_at", "finished_at", "created_at")
_ANALYSIS_TIME_FIELDS = ("analyzed_at", "completed_at", "updated_at", "created_at")

_ANALYSIS_FIELD_ALIASES = {
    "input_tokens": ("input_tokens", "prompt_tokens", "token_input"),
    "output_tokens": ("output_tokens", "completion_tokens", "token_output"),
    "total_tokens": ("total_tokens", "token_total", "tokens"),
    "cost": (
        "cost",
        "cost_usd",
        "total_cost",
        "total_cost_usd",
        "estimated_cost",
        "estimated_cost_usd",
        "fee",
        "fee_usd",
        "price",
        "amount",
    ),
    "input_cost": ("input_cost", "input_cost_usd"),
    "output_cost": ("output_cost", "output_cost_usd"),
}

_MAX_TELEMETRY_RECORDS = 10_000_000


@dataclass(frozen=True)
class _Window:
    start_date: date
    end_date: date
    start_at: datetime
    end_at: datetime
    days: int


@dataclass(frozen=True)
class _AnalysisLayout:
    table: Table | None
    time_fields: tuple[str, ...]
    input_tokens: str | None
    output_tokens: str | None
    total_tokens: str | None
    cost: str | None
    input_cost: str | None
    output_cost: str | None

    @property
    def available(self) -> bool:
        return self.table is not None

    @property
    def token_fields_available(self) -> bool:
        return bool(self.input_tokens or self.output_tokens or self.total_tokens)

    @property
    def total_tokens_available(self) -> bool:
        return bool(
            self.total_tokens
            or self.input_tokens
            or self.output_tokens
        )

    @property
    def cost_available(self) -> bool:
        return bool(self.cost or self.input_cost or self.output_cost)


def aggregate_weekly_observability(
    bind: Storage | Engine | Connection | Session,
    *,
    days: int = DEFAULT_DAYS,
    now: datetime | date | None = None,
    telemetry_path: Path | str | None = None,
) -> dict[str, Any]:
    """Aggregate recent task, browser, analysis, and Codex timing metrics.

    The database is accessed through SELECT statements only. Missing tables,
    optional analysis columns, empty tables, and an absent telemetry file are
    represented in ``availability``/``warnings`` instead of raising.
    """

    window = _make_window(days, now)
    dates = [window.start_date + timedelta(days=offset) for offset in range(days)]
    warnings: list[str] = []

    engine = _engine_for(bind)
    task_table = _reflect_model_table(engine, TaskRun, warnings)
    automation_table = _reflect_model_table(engine, AutomationExecution, warnings)
    browser_table = _reflect_model_table(engine, BrowserOperation, warnings)
    analysis_table = _reflect_model_table(engine, JobAnalysisSnapshot, warnings)
    analysis_layout = _analysis_layout(analysis_table)

    states = [
        _new_day(day, analysis_layout)
        for day in dates
    ]
    buckets = {day: state for day, state in zip(dates, states)}

    with _session_scope(bind) as session:
        task_availability = _collect_task_runs(
            session,
            task_table,
            window,
            buckets,
            warnings,
        )
        automation_availability = _collect_task_runs(
            session,
            automation_table,
            window,
            buckets,
            warnings,
        )
        browser_availability = _collect_browser_operations(
            session,
            browser_table,
            window,
            buckets,
            warnings,
        )
        analysis_availability = _collect_job_analysis(
            session,
            analysis_layout,
            window,
            buckets,
            warnings,
        )

    resolved_telemetry_path = _resolve_telemetry_path(telemetry_path)
    telemetry_availability = _collect_codex_timing(
        resolved_telemetry_path,
        window,
        buckets,
        warnings,
    )

    daily = [_serialize_day(state) for state in states]
    totals = _serialize_totals(states, analysis_layout)
    availability = {
        "task_runs": task_availability,
        "automation_executions": automation_availability,
        "browser_operations": browser_availability,
        "job_analysis": analysis_availability,
        "codex_telemetry": telemetry_availability,
    }

    return {
        "schema_version": WEEKLY_REPORT_SCHEMA_VERSION,
        "report_type": "weekly_observability",
        "period": {
            "start_date": window.start_date.isoformat(),
            "end_date": window.end_date.isoformat(),
            "days": window.days,
        },
        "daily": daily,
        "totals": totals,
        "availability": availability,
        "warnings": sorted(set(warnings)),
    }


def render_weekly_markdown(report: Mapping[str, Any]) -> str:
    """Render a deterministic Markdown report from an aggregated report."""

    period = report.get("period") if isinstance(report.get("period"), Mapping) else {}
    start_date = str(period.get("start_date", "unknown"))
    end_date = str(period.get("end_date", "unknown"))
    period_days = period.get("days", "?")
    daily = report.get("daily") if isinstance(report.get("daily"), list) else []
    totals = report.get("totals") if isinstance(report.get("totals"), Mapping) else {}

    lines = [
        "# Weekly Observability Report",
        "",
        f"Period: `{start_date}` to `{end_date}` ({period_days} days)",
        "",
        "## Task Runs",
        "",
        "| Date | Succeeded | Failed |",
        "| --- | ---: | ---: |",
    ]
    for day in daily:
        task_runs = day.get("task_runs") if isinstance(day, Mapping) else {}
        task_runs = task_runs if isinstance(task_runs, Mapping) else {}
        lines.append(
            f"| {_cell(day.get('date'))} | {_cell(task_runs.get('succeeded', 0))} | "
            f"{_cell(task_runs.get('failed', 0))} |"
        )
    task_total = totals.get("task_runs") if isinstance(totals.get("task_runs"), Mapping) else {}
    lines.extend(
        [
            f"| **Total** | **{_cell(task_total.get('succeeded', 0))}** | "
            f"**{_cell(task_total.get('failed', 0))}** |",
            "",
            "## Browser Operation Failures",
            "",
            "| Date | Failed | Failure Types |",
            "| --- | ---: | --- |",
        ]
    )
    for day in daily:
        browser = day.get("browser_operations") if isinstance(day, Mapping) else {}
        browser = browser if isinstance(browser, Mapping) else {}
        lines.append(
            f"| {_cell(day.get('date'))} | {_cell(browser.get('failed', 0))} | "
            f"{_failure_types_cell(browser.get('failure_types'))} |"
        )
    browser_total = (
        totals.get("browser_operations")
        if isinstance(totals.get("browser_operations"), Mapping)
        else {}
    )
    lines.extend(
        [
            f"| **Total** | **{_cell(browser_total.get('failed', 0))}** | "
            f"{_failure_types_cell(browser_total.get('failure_types'))} |",
            "",
            "## Job Analysis Usage",
            "",
            "| Date | Analyses | Input Tokens | Output Tokens | Total Tokens | Cost |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for day in daily:
        analysis = day.get("job_analysis") if isinstance(day, Mapping) else {}
        analysis = analysis if isinstance(analysis, Mapping) else {}
        lines.append(
            f"| {_cell(day.get('date'))} | {_cell(analysis.get('count', 0))} | "
            f"{_metric_cell(analysis.get('input_tokens'))} | "
            f"{_metric_cell(analysis.get('output_tokens'))} | "
            f"{_metric_cell(analysis.get('total_tokens'))} | "
            f"{_metric_cell(analysis.get('cost'))} |"
        )
    analysis_total = (
        totals.get("job_analysis")
        if isinstance(totals.get("job_analysis"), Mapping)
        else {}
    )
    lines.extend(
        [
            f"| **Total** | **{_cell(analysis_total.get('count', 0))}** | "
            f"**{_metric_cell(analysis_total.get('input_tokens'))}** | "
            f"**{_metric_cell(analysis_total.get('output_tokens'))}** | "
            f"**{_metric_cell(analysis_total.get('total_tokens'))}** | "
            f"**{_metric_cell(analysis_total.get('cost'))}** |",
            "",
            "## Stage Duration",
            "",
            "| Date | Stage | Count | Total ms | Average ms | Max ms |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for day in daily:
        durations = day.get("stage_duration_ms") if isinstance(day, Mapping) else {}
        if not isinstance(durations, Mapping) or not durations:
            continue
        for stage in sorted(durations):
            metric = durations[stage]
            metric = metric if isinstance(metric, Mapping) else {}
            lines.append(
                f"| {_cell(day.get('date'))} | {_cell(stage)} | "
                f"{_cell(metric.get('count', 0))} | {_metric_cell(metric.get('total_ms'))} | "
                f"{_metric_cell(metric.get('average_ms'))} | {_metric_cell(metric.get('max_ms'))} |"
            )
    duration_totals = (
        totals.get("stage_duration_ms")
        if isinstance(totals.get("stage_duration_ms"), Mapping)
        else {}
    )
    for stage in sorted(duration_totals):
        metric = duration_totals[stage]
        metric = metric if isinstance(metric, Mapping) else {}
        lines.append(
            f"| **Total** | **{_cell(stage)}** | **{_cell(metric.get('count', 0))}** | "
            f"**{_metric_cell(metric.get('total_ms'))}** | "
            f"**{_metric_cell(metric.get('average_ms'))}** | "
            f"**{_metric_cell(metric.get('max_ms'))}** |"
        )

    lines.extend(["", "## Data Availability", "", "| Source | Available | Details |", "| --- | --- | --- |"])
    availability = report.get("availability")
    if isinstance(availability, Mapping):
        for source in sorted(availability):
            details = availability[source]
            details = details if isinstance(details, Mapping) else {}
            available = bool(details.get("available", False))
            detail_text = _availability_cell(source, details)
            lines.append(f"| {_cell(source)} | {_cell(str(available).lower())} | {detail_text} |")

    warnings = report.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {_cell(item)}" for item in warnings)

    return "\n".join(lines) + "\n"


def report_to_json(report: Mapping[str, Any]) -> str:
    """Serialize a report deterministically for stdout or a JSON file."""

    return json.dumps(
        _json_safe(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _make_window(days: int, now: datetime | date | None) -> _Window:
    if days < 1:
        raise ValueError("days must be at least 1")
    if now is None:
        end_date = datetime.now(UTC).date()
    elif isinstance(now, datetime):
        end_date = _as_utc(now).date()
    elif isinstance(now, date):
        end_date = now
    else:
        raise TypeError("now must be a date, datetime, or None")
    start_date = end_date - timedelta(days=days - 1)
    start_at = datetime.combine(start_date, time.min, tzinfo=UTC)
    end_at = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)
    return _Window(start_date, end_date, start_at, end_at, days)


def _engine_for(bind: Storage | Engine | Connection | Session) -> Engine:
    if isinstance(bind, Storage):
        return bind.engine
    if isinstance(bind, Engine):
        return bind
    if isinstance(bind, Connection):
        return bind.engine
    if isinstance(bind, Session):
        engine = bind.get_bind()
        if isinstance(engine, Engine):
            return engine
        if isinstance(engine, Connection):
            return engine.engine
    engine = getattr(bind, "engine", None)
    if isinstance(engine, Engine):
        return engine
    raise TypeError("bind must be a Storage, SQLAlchemy Engine, Connection, or Session")


@contextmanager
def _session_scope(bind: Storage | Engine | Connection | Session) -> Iterator[Session]:
    if isinstance(bind, Storage):
        with bind.session() as session:
            yield session
        return
    if isinstance(bind, Session):
        yield bind
        return
    with Session(bind=bind) as session:
        yield session


def _reflect_model_table(engine: Engine, model: Any, warnings: list[str]) -> Table | None:
    table_name = model.__tablename__
    try:
        if not inspect(engine).has_table(table_name):
            _warn(warnings, f"missing_table:{table_name}")
            return None
        return Table(table_name, MetaData(), autoload_with=engine)
    except SQLAlchemyError as exc:
        _warn(warnings, f"table_unavailable:{table_name}:{type(exc).__name__}")
        return None


def _analysis_layout(table: Table | None) -> _AnalysisLayout:
    if table is None:
        return _AnalysisLayout(None, (), None, None, None, None, None, None)
    columns = {column.name.casefold(): column.name for column in table.columns}

    def choose(names: tuple[str, ...]) -> str | None:
        for name in names:
            actual = columns.get(name.casefold())
            if actual is not None:
                return actual
        return None

    return _AnalysisLayout(
        table=table,
        time_fields=tuple(
            actual
            for name in _ANALYSIS_TIME_FIELDS
            if (actual := columns.get(name.casefold())) is not None
        ),
        input_tokens=choose(_ANALYSIS_FIELD_ALIASES["input_tokens"]),
        output_tokens=choose(_ANALYSIS_FIELD_ALIASES["output_tokens"]),
        total_tokens=choose(_ANALYSIS_FIELD_ALIASES["total_tokens"]),
        cost=choose(_ANALYSIS_FIELD_ALIASES["cost"]),
        input_cost=choose(_ANALYSIS_FIELD_ALIASES["input_cost"]),
        output_cost=choose(_ANALYSIS_FIELD_ALIASES["output_cost"]),
    )


def _collect_task_runs(
    session: Session,
    table: Table | None,
    window: _Window,
    buckets: Mapping[date, dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    if table is None:
        return {"available": False, "timestamp_field": None, "records": 0}
    columns = _column_names(table)
    status_name = columns.get("status")
    time_names = _ordered_existing(columns, _TASK_TIME_FIELDS)
    if status_name is None or not time_names:
        _warn(warnings, "task_runs:required_fields_unavailable")
        return {
            "available": True,
            "timestamp_field": time_names[0] if time_names else None,
            "records": 0,
        }
    try:
        rows = _query_rows(session, table, status_name, time_names, window)
    except SQLAlchemyError as exc:
        _warn(warnings, f"query_unavailable:task_runs:{type(exc).__name__}")
        return {"available": False, "timestamp_field": time_names[0], "records": 0}

    record_count = 0
    for row in rows:
        day = _row_day(row.get("_observed_at"))
        if day not in buckets:
            continue
        status = _normalised_text(row.get("_status"))
        if status in _TASK_SUCCESS_STATUSES:
            buckets[day]["task_runs"]["succeeded"] += 1
            record_count += 1
        elif status in _TASK_FAILURE_STATUSES:
            buckets[day]["task_runs"]["failed"] += 1
            record_count += 1
    return {"available": True, "timestamp_field": time_names[0], "records": record_count}


def _collect_browser_operations(
    session: Session,
    table: Table | None,
    window: _Window,
    buckets: Mapping[date, dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    if table is None:
        return {"available": False, "timestamp_field": None, "records": 0}
    columns = _column_names(table)
    status_name = columns.get("status")
    error_name = columns.get("error_code")
    time_names = _ordered_existing(columns, _BROWSER_TIME_FIELDS)
    if status_name is None or not time_names:
        _warn(warnings, "browser_operations:required_fields_unavailable")
        return {
            "available": True,
            "timestamp_field": time_names[0] if time_names else None,
            "records": 0,
        }
    try:
        selected = [
            table.c[status_name].label("_status"),
            table.c[time_names[0]].label("_observed_at"),
        ]
        if len(time_names) > 1:
            selected[1] = func.coalesce(
                *(table.c[name] for name in time_names)
            ).label("_observed_at")
        if error_name is not None:
            selected.append(table.c[error_name].label("_error_code"))
        timestamp = selected[1]
        rows = session.execute(
            select(*selected).where(
                and_(timestamp >= window.start_at, timestamp < window.end_at)
            )
        ).mappings()
    except SQLAlchemyError as exc:
        _warn(warnings, f"query_unavailable:browser_operations:{type(exc).__name__}")
        return {"available": False, "timestamp_field": time_names[0], "records": 0}

    record_count = 0
    for row in rows:
        day = _row_day(row.get("_observed_at"))
        if day not in buckets:
            continue
        status = _normalised_text(row.get("_status"))
        if status not in _BROWSER_FAILURE_STATUSES:
            continue
        record_count += 1
        failure_type = _normalised_text(row.get("_error_code")) or status or "unknown"
        browser = buckets[day]["browser_operations"]
        browser["failed"] += 1
        failure_types = browser["failure_types"]
        failure_types[failure_type] = failure_types.get(failure_type, 0) + 1
    return {"available": True, "timestamp_field": time_names[0], "records": record_count}


def _collect_job_analysis(
    session: Session,
    layout: _AnalysisLayout,
    window: _Window,
    buckets: Mapping[date, dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    fields = {
        "input_tokens": layout.input_tokens,
        "output_tokens": layout.output_tokens,
        "total_tokens": layout.total_tokens,
        "cost": layout.cost,
        "input_cost": layout.input_cost,
        "output_cost": layout.output_cost,
    }
    availability = {
        "available": layout.available,
        "timestamp_field": layout.time_fields[0] if layout.time_fields else None,
        "records": 0,
        "fields": fields,
        "token_fields_available": layout.token_fields_available,
        "cost_fields_available": layout.cost_available,
    }
    if layout.table is None:
        return availability
    if not layout.time_fields:
        _warn(warnings, "job_analysis:timestamp_field_unavailable")
        return availability

    table = layout.table
    selected: list[Any] = []
    timestamp = _coalesced_column(table, layout.time_fields)
    selected.append(timestamp.label("_observed_at"))
    for key, actual in fields.items():
        if actual is not None:
            selected.append(table.c[actual].label(f"_{key}"))
    try:
        rows = session.execute(
            select(*selected).where(
                and_(timestamp >= window.start_at, timestamp < window.end_at)
            )
        ).mappings()
    except SQLAlchemyError as exc:
        _warn(warnings, f"query_unavailable:job_analysis:{type(exc).__name__}")
        availability["available"] = False
        return availability

    record_count = 0
    for row in rows:
        day = _row_day(row.get("_observed_at"))
        if day not in buckets:
            continue
        record_count += 1
        analysis = buckets[day]["job_analysis"]
        analysis["count"] += 1

        input_tokens = _nonnegative_int(row.get("_input_tokens"))
        output_tokens = _nonnegative_int(row.get("_output_tokens"))
        total_tokens = _nonnegative_int(row.get("_total_tokens"))
        if input_tokens is not None and layout.input_tokens:
            analysis["input_tokens"] += input_tokens
        if output_tokens is not None and layout.output_tokens:
            analysis["output_tokens"] += output_tokens
        if layout.total_tokens_available:
            if total_tokens is None:
                total_tokens = (input_tokens or 0) + (output_tokens or 0)
            analysis["total_tokens"] += total_tokens

        cost = _nonnegative_decimal(row.get("_cost")) if layout.cost else None
        if cost is None and layout.cost is None:
            input_cost = _nonnegative_decimal(row.get("_input_cost"))
            output_cost = _nonnegative_decimal(row.get("_output_cost"))
            if input_cost is not None or output_cost is not None:
                cost = (input_cost or Decimal("0")) + (output_cost or Decimal("0"))
        if cost is not None and layout.cost_available:
            analysis["cost"] += cost
    availability["records"] = record_count
    return availability


def _collect_codex_timing(
    path: Path,
    window: _Window,
    buckets: Mapping[date, dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    if not path.is_file():
        _warn(warnings, "missing_file:codex_telemetry")
        return {"available": False, "records": 0, "timing_records": 0}
    try:
        traces = JsonlTraceRecorder(path).read(limit=_MAX_TELEMETRY_RECORDS)
    except (OSError, ValueError) as exc:
        _warn(warnings, f"telemetry_unavailable:{type(exc).__name__}")
        return {"available": False, "records": 0, "timing_records": 0}

    timing_records = 0
    for trace in traces:
        if not isinstance(trace, CodexTrace):
            continue
        day = _row_day(trace.observed_at)
        if day not in buckets:
            continue
        duration = _nonnegative_decimal(trace.latency_ms)
        if duration is None:
            continue
        stage = _normalised_text(trace.stage) or "unknown"
        stage_metric = buckets[day]["stage_duration_ms"].setdefault(
            stage,
            {"count": 0, "total_ms": Decimal("0"), "max_ms": Decimal("0")},
        )
        stage_metric["count"] += 1
        stage_metric["total_ms"] += duration
        stage_metric["max_ms"] = max(stage_metric["max_ms"], duration)
        timing_records += 1
    return {
        "available": True,
        "records": len(traces),
        "timing_records": timing_records,
    }


def _query_rows(
    session: Session,
    table: Table,
    status_name: str,
    time_names: tuple[str, ...],
    window: _Window,
) -> Any:
    timestamp = _coalesced_column(table, time_names)
    return session.execute(
        select(
            table.c[status_name].label("_status"),
            timestamp.label("_observed_at"),
        ).where(and_(timestamp >= window.start_at, timestamp < window.end_at))
    ).mappings()


def _coalesced_column(table: Table, names: tuple[str, ...]) -> Any:
    columns = [table.c[name] for name in names]
    if len(columns) == 1:
        return columns[0]
    return func.coalesce(*columns)


def _new_day(day: date, layout: _AnalysisLayout) -> dict[str, Any]:
    return {
        "date": day.isoformat(),
        "task_runs": {"succeeded": 0, "failed": 0},
        "browser_operations": {"failed": 0, "failure_types": {}},
        "job_analysis": {
            "count": 0,
            "input_tokens": 0 if layout.input_tokens else None,
            "output_tokens": 0 if layout.output_tokens else None,
            "total_tokens": 0 if layout.total_tokens_available else None,
            "cost": Decimal("0") if layout.cost_available else None,
        },
        "stage_duration_ms": {},
    }


def _serialize_day(state: Mapping[str, Any]) -> dict[str, Any]:
    analysis = state["job_analysis"]
    return {
        "date": state["date"],
        "task_runs": dict(state["task_runs"]),
        "browser_operations": {
            "failed": state["browser_operations"]["failed"],
            "failure_types": dict(sorted(state["browser_operations"]["failure_types"].items())),
        },
        "job_analysis": {
            "count": analysis["count"],
            "input_tokens": analysis["input_tokens"],
            "output_tokens": analysis["output_tokens"],
            "total_tokens": analysis["total_tokens"],
            "cost": _json_number(analysis["cost"]),
        },
        "stage_duration_ms": _serialize_stage_metrics(state["stage_duration_ms"]),
    }


def _serialize_totals(
    states: list[Mapping[str, Any]],
    layout: _AnalysisLayout,
) -> dict[str, Any]:
    task_runs = {"succeeded": 0, "failed": 0}
    browser = {"failed": 0, "failure_types": {}}
    analysis = {
        "count": 0,
        "input_tokens": 0 if layout.input_tokens else None,
        "output_tokens": 0 if layout.output_tokens else None,
        "total_tokens": 0 if layout.total_tokens_available else None,
        "cost": Decimal("0") if layout.cost_available else None,
    }
    stages: dict[str, dict[str, Any]] = {}
    for state in states:
        for key in task_runs:
            task_runs[key] += state["task_runs"][key]
        browser["failed"] += state["browser_operations"]["failed"]
        for key, value in state["browser_operations"]["failure_types"].items():
            browser["failure_types"][key] = browser["failure_types"].get(key, 0) + value
        state_analysis = state["job_analysis"]
        analysis["count"] += state_analysis["count"]
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            if analysis[key] is not None and state_analysis[key] is not None:
                analysis[key] += state_analysis[key]
        if analysis["cost"] is not None and state_analysis["cost"] is not None:
            analysis["cost"] += state_analysis["cost"]
        for stage, metric in state["stage_duration_ms"].items():
            target = stages.setdefault(
                stage,
                {"count": 0, "total_ms": Decimal("0"), "max_ms": Decimal("0")},
            )
            target["count"] += metric["count"]
            target["total_ms"] += metric["total_ms"]
            target["max_ms"] = max(target["max_ms"], metric["max_ms"])
    return {
        "task_runs": task_runs,
        "browser_operations": {
            "failed": browser["failed"],
            "failure_types": dict(sorted(browser["failure_types"].items())),
        },
        "job_analysis": {
            "count": analysis["count"],
            "input_tokens": analysis["input_tokens"],
            "output_tokens": analysis["output_tokens"],
            "total_tokens": analysis["total_tokens"],
            "cost": _json_number(analysis["cost"]),
        },
        "stage_duration_ms": _serialize_stage_metrics(stages),
    }


def _serialize_stage_metrics(metrics: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    serialized: dict[str, Any] = {}
    for stage in sorted(metrics):
        metric = metrics[stage]
        count = int(metric["count"])
        total = _json_number(metric["total_ms"])
        average = _json_number(metric["total_ms"] / count) if count else 0
        serialized[stage] = {
            "count": count,
            "total_ms": total,
            "average_ms": average,
            "max_ms": _json_number(metric["max_ms"]),
        }
    return serialized


def _column_names(table: Table) -> dict[str, str]:
    return {column.name.casefold(): column.name for column in table.columns}


def _ordered_existing(columns: Mapping[str, str], names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(columns[name.casefold()] for name in names if name.casefold() in columns)


def _row_day(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
        return _as_utc(parsed).date()
    return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalised_text(value: Any) -> str:
    return "" if value is None else str(value).strip().casefold()


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not decimal.is_finite() or decimal < 0 or decimal != decimal.to_integral_value():
        return None
    return int(decimal)


def _nonnegative_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not decimal.is_finite() or decimal < 0:
        return None
    return decimal


def _json_number(value: Any) -> int | float | None:
    if value is None:
        return None
    decimal = _nonnegative_decimal(value)
    if decimal is None:
        return None
    rounded = decimal.quantize(Decimal("0.000001"))
    if rounded == rounded.to_integral_value():
        return int(rounded)
    return float(rounded)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _json_number(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _resolve_telemetry_path(path: Path | str | None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    try:
        from packages.config import get_settings

        settings = get_settings()
        configured = Path(settings.codex_trace_path).expanduser()
        return configured if configured.is_absolute() else settings.agent_root / configured
    except Exception:
        return Path(".data/codex-traces.jsonl")


def _warn(warnings: list[str], value: str) -> None:
    if value not in warnings:
        warnings.append(value)


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _metric_cell(value: Any) -> str:
    return "n/a" if value is None else _cell(value)


def _failure_types_cell(value: Any) -> str:
    if not isinstance(value, Mapping) or not value:
        return "-"
    return ", ".join(f"{_cell(key)}: {_cell(value[key])}" for key in sorted(value))


def _availability_cell(source: str, details: Mapping[str, Any]) -> str:
    if source == "job_analysis":
        fields = details.get("fields")
        if isinstance(fields, Mapping):
            present = sorted(key for key, value in fields.items() if value)
            return _cell(", ".join(present) if present else "no optional metric fields")
    if "records" in details:
        return _cell(f"records={details['records']}")
    return "-"


# Short aliases keep the module convenient for callers that use the wording
# used by the CLI and by the existing reporting package.
build_weekly_observability_report = aggregate_weekly_observability
build_weekly_report = aggregate_weekly_observability
render_markdown = render_weekly_markdown
serialize_report = report_to_json


__all__ = [
    "DEFAULT_DAYS",
    "WEEKLY_REPORT_SCHEMA_VERSION",
    "aggregate_weekly_observability",
    "build_weekly_observability_report",
    "build_weekly_report",
    "render_markdown",
    "render_weekly_markdown",
    "report_to_json",
    "serialize_report",
]
