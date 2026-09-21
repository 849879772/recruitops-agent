"""Typed manual operations and inert automation plans for the local Agent."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, time
from time import perf_counter
import threading
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from packages.scheduler.models import TaskCallable
from packages.scheduler.runner import LocalTaskScheduler, _business_failure
from packages.scheduler.tasks import TaskType
from packages.automation import (
    AutomationStore,
    automation_blocked_message,
    automation_blocked_reason,
)

from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolModel, ToolResponse, ToolStatus


_ALLOWED_TASKS = tuple(item.value for item in TaskType)


class OperationalTaskRunInput(ToolInput):
    task_id: str
    dry_run: bool = False
    company_ids: list[str] = Field(default_factory=list, max_length=10)
    source_record_ids: list[str] = Field(default_factory=list, max_length=10)
    mode: Literal["full", "crawl_only", "score_only", "resume"] = "full"
    resume_run_id: str | None = Field(default=None, min_length=8, max_length=128)

    @field_validator("task_id")
    @classmethod
    def task_must_be_allowlisted(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in _ALLOWED_TASKS:
            raise ValueError("operational task is not in the fixed allowlist")
        return normalized

    @field_validator("company_ids", "source_record_ids")
    @classmethod
    def unique_company_ids(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            normalized = value.strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @model_validator(mode="after")
    def scope_only_daily_sync(self) -> "OperationalTaskRunInput":
        is_daily = self.task_id == TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value
        if (self.company_ids or self.source_record_ids or self.mode != "full" or self.resume_run_id) and not is_daily:
            raise ValueError("daily mode options are only valid for the daily recruitment task")
        if self.company_ids and self.source_record_ids:
            raise ValueError("company_ids and source_record_ids cannot be combined")
        if self.mode == "resume" and not self.resume_run_id:
            raise ValueError("resume_run_id is required for resume mode")
        if self.mode != "resume" and self.resume_run_id:
            raise ValueError("resume_run_id is only valid for resume mode")
        return self


class OperationalTaskRunData(ToolModel):
    task_id: str
    run_id: str
    run_status: str
    dry_run: bool
    attempts: int = Field(ge=0)
    agent_write_enabled: bool
    company_ids: list[str] = Field(default_factory=list)
    source_record_ids: list[str] = Field(default_factory=list)
    mode: Literal["full", "crawl_only", "score_only", "resume"] = "full"
    resume_run_id: str | None = None
    result: Any = None
    error: str | None = None


class OperationalTaskRunResponse(ToolResponse[OperationalTaskRunData]):
    pass


class OperationalTaskRunner:
    """Execute only the fixed local scheduler catalog on explicit invocation."""

    def __init__(
        self,
        scheduler: LocalTaskScheduler,
        handlers: Mapping[str, TaskCallable],
        state_store: Any | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.handlers = dict(handlers)
        self.state_store = state_store
        self._background_lock = threading.Lock()
        self._background_runs: dict[str, dict[str, Any]] = {}

    def start(self, request: OperationalTaskRunInput) -> OperationalTaskRunResponse:
        """Start a long-running fixed task without holding the MCP call open."""

        definition = self.scheduler.task_definition(request.task_id)
        run_id = uuid4().hex
        initial = {
            "task_id": request.task_id,
            "run_id": run_id,
            "run_status": "accepted",
            "dry_run": request.dry_run,
            "attempts": 0,
            "agent_write_enabled": definition.agent_write_enabled,
            "company_ids": request.company_ids,
            "source_record_ids": request.source_record_ids,
            "mode": request.mode,
            "resume_run_id": request.resume_run_id,
            "result": None,
            "error": None,
        }
        with self._background_lock:
            self._background_runs[run_id] = initial

        def execute() -> None:
            with self._background_lock:
                self._background_runs[run_id]["run_status"] = "running"
            heartbeat_stop = threading.Event()

            def heartbeat() -> None:
                callback = getattr(self.state_store, "heartbeat_task_run", None)
                while not heartbeat_stop.wait(15.0):
                    if callable(callback):
                        try:
                            callback(run_id)
                        except Exception:
                            pass

            heartbeat_thread = threading.Thread(
                target=heartbeat,
                name=f"recruitops-heartbeat-{run_id[:8]}",
                daemon=True,
            )
            heartbeat_thread.start()
            try:
                result = self.scheduler.run(
                    request.task_id,
                    self.handlers.get(request.task_id),
                    dry_run=request.dry_run,
                    run_id=run_id,
                    metadata={
                        "company_ids": request.company_ids,
                        "source_record_ids": request.source_record_ids,
                        "mode": request.mode,
                        "resume_run_id": request.resume_run_id,
                    },
                )
                payload = {
                    "task_id": request.task_id,
                    "run_id": run_id,
                    "run_status": result.status.value,
                    "dry_run": request.dry_run,
                    "attempts": result.attempts,
                    "agent_write_enabled": definition.agent_write_enabled,
                    "company_ids": request.company_ids,
                    "source_record_ids": request.source_record_ids,
                    "mode": request.mode,
                    "resume_run_id": request.resume_run_id,
                    "result": result.value,
                    "error": result.error,
                }
            except Exception as exc:
                payload = {
                    **initial,
                    "run_status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            finally:
                heartbeat_stop.set()
            with self._background_lock:
                self._background_runs[run_id] = payload

        threading.Thread(
            target=execute,
            name=f"recruitops-{request.task_id}-{run_id[:8]}",
            daemon=True,
        ).start()
        return OperationalTaskRunResponse(
            tool_name="operation_run",
            status=ToolStatus.SUCCESS,
            success=True,
            data=OperationalTaskRunData(**initial),
            evidence=[
                EvidenceSource(
                    source="agent_scheduler",
                    source_ref=f"task:{request.task_id}:run:{run_id}",
                )
            ],
            timeout_ms=request.timeout_ms,
            elapsed_ms=0,
        )

    def background_status(self, run_id: str) -> Mapping[str, Any] | None:
        with self._background_lock:
            value = self._background_runs.get(run_id)
            result = dict(value) if value is not None else None
        persisted = None
        if self.state_store is not None:
            getter = getattr(self.state_store, "get_task_run", None)
            if callable(getter):
                persisted = getter(run_id)
        if result is None:
            if persisted is None:
                return None
            state = persisted.get("state")
            value = state.get("result") if isinstance(state, Mapping) else None
            business_error = _business_failure(value)
            return {
                "task_id": persisted.get("task_id"),
                "run_id": persisted.get("run_id", run_id),
                "run_status": "failed" if business_error else persisted.get("run_status", "unknown"),
                "dry_run": False,
                "attempts": 0,
                "agent_write_enabled": True,
                "result": value,
                "error": business_error or persisted.get("error"),
                "current_step": persisted.get("current_step"),
                "step_count": persisted.get("step_count", 0),
            }
        if persisted:
            result["current_step"] = persisted.get("current_step")
            result["step_count"] = persisted.get("step_count", 0)
            if result.get("run_status") in {"accepted", "running"}:
                result["run_status"] = persisted.get("run_status") or result["run_status"]
        return result

    def run(
        self,
        request: OperationalTaskRunInput,
        *,
        execute_dry_run: bool = False,
    ) -> OperationalTaskRunResponse:
        started = perf_counter()
        definition = self.scheduler.task_definition(request.task_id)
        try:
            result = self.scheduler.run(
                request.task_id,
                self.handlers.get(request.task_id),
                dry_run=request.dry_run and not execute_dry_run,
                metadata={
                    **({"requested_dry_run": True} if request.dry_run else {}),
                    **({"company_ids": request.company_ids} if request.company_ids else {}),
                    **(
                        {"source_record_ids": request.source_record_ids}
                        if request.source_record_ids
                        else {}
                    ),
                    "mode": request.mode,
                    **({"resume_run_id": request.resume_run_id} if request.resume_run_id else {}),
                } or None,
            )
            elapsed_ms = max(0, int((perf_counter() - started) * 1_000))
            success = result.status.value in {"success", "dry_run", "skipped_locked"}
            return OperationalTaskRunResponse(
                tool_name="operation_run",
                status=ToolStatus.SUCCESS if success else ToolStatus.FAILURE,
                success=success,
                data=OperationalTaskRunData(
                    task_id=request.task_id,
                    run_id=result.run_id,
                    run_status=result.status.value,
                    dry_run=request.dry_run,
                    attempts=result.attempts,
                    agent_write_enabled=definition.agent_write_enabled,
                    company_ids=request.company_ids,
                    source_record_ids=request.source_record_ids,
                    mode=request.mode,
                    resume_run_id=request.resume_run_id,
                    result=result.value,
                    error=result.error,
                ),
                evidence=[
                    EvidenceSource(
                        source="agent_scheduler",
                        source_ref=f"task:{request.task_id}:run:{result.run_id}",
                    )
                ],
                error_code=None if success else ToolErrorCode.INTERNAL_ERROR,
                error_message=None if success else (result.error or "operational task failed"),
                timeout_ms=request.timeout_ms,
                timed_out=result.timed_out,
                elapsed_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = max(0, int((perf_counter() - started) * 1_000))
            return OperationalTaskRunResponse(
                tool_name="operation_run",
                status=ToolStatus.FAILURE,
                success=False,
                evidence=[EvidenceSource(source="agent_scheduler", source_ref=request.task_id)],
                error_code=ToolErrorCode.INTERNAL_ERROR,
                error_message=f"{type(exc).__name__}: {exc}",
                timeout_ms=request.timeout_ms,
                elapsed_ms=elapsed_ms,
            )


class AutomationPlanInput(ToolInput):
    task_id: str
    start_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    frequency: Literal["daily"] = "daily"

    @field_validator("task_id")
    @classmethod
    def task_must_be_allowlisted(cls, value: str) -> str:
        normalized = value.strip()
        if normalized not in _ALLOWED_TASKS:
            raise ValueError("automation task is not in the fixed allowlist")
        return normalized


class AutomationPlanData(ToolModel):
    task_id: str
    task_label: str
    frequency: Literal["daily"]
    start_time: str
    active: Literal[False] = False
    activation_status: Literal["not_installed"] = "not_installed"
    activation_requires_human_approval: Literal[True] = True
    command_argv: list[str]
    created_at: datetime


class AutomationPlanResponse(ToolResponse[AutomationPlanData]):
    pass


class AutomationScheduleInput(AutomationPlanInput):
    application_id: str | None = Field(default=None, min_length=1, max_length=128)


class AutomationScheduleData(ToolModel):
    schedule_id: str
    task_id: str
    task_label: str
    frequency: Literal["daily"]
    start_time: str
    timezone: str
    active: bool
    activation_status: str
    runnable: bool = True
    blocked_reason: str | None = None
    target_kind: str
    target_id: str | None = None
    target_label: str | None = None
    next_run_at: datetime
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    latest_execution_id: str | None = None
    latest_thread_id: str | None = None
    latest_turn_id: str | None = None
    latest_result_summary: str | None = None


class AutomationScheduleResponse(ToolResponse[AutomationScheduleData]):
    read_only: Literal[False] = False


class AutomationScheduleListInput(ToolInput):
    active_only: bool = False


class AutomationScheduleListData(ToolModel):
    schedules: list[AutomationScheduleData]
    total: int = Field(ge=0)
    engine_configured: bool = True
    engine_blocked_reason: str | None = None


class AutomationScheduleListResponse(ToolResponse[AutomationScheduleListData]):
    pass


class AutomationScheduleDisableInput(ToolInput):
    schedule_id: str = Field(min_length=1, max_length=128)


class AutomationScheduleDisableResponse(ToolResponse[AutomationScheduleData]):
    read_only: Literal[False] = False


def plan_automation(
    request: AutomationPlanInput,
    scheduler: LocalTaskScheduler,
) -> AutomationPlanResponse:
    started = perf_counter()
    definition = scheduler.task_definition(request.task_id)
    return AutomationPlanResponse(
        tool_name="automation_plan",
        status=ToolStatus.SUCCESS,
        success=True,
        data=AutomationPlanData(
            task_id=request.task_id,
            task_label=definition.label,
            frequency=request.frequency,
            start_time=request.start_time,
            command_argv=[
                ".venv/Scripts/python.exe",
                "scripts/run_local_task.py",
                "--task",
                request.task_id,
            ],
            created_at=datetime.now().astimezone(),
        ),
        evidence=[
            EvidenceSource(
                source="agent_scheduler_catalog",
                source_ref=f"task:{request.task_id}",
            )
        ],
        timeout_ms=request.timeout_ms,
        elapsed_ms=max(0, int((perf_counter() - started) * 1_000)),
    )


def _schedule_data(
    row: Any,
    execution: Any | None = None,
    *,
    blocked_reason: str | None = None,
) -> AutomationScheduleData:
    return AutomationScheduleData(
        schedule_id=row.id,
        task_id=row.task_id,
        task_label=row.task_label,
        frequency=row.frequency,
        start_time=row.start_time.strftime("%H:%M"),
        timezone=row.timezone_name,
        active=bool(row.active),
        activation_status="active" if row.active else "disabled",
        runnable=bool(row.active and blocked_reason is None),
        blocked_reason=blocked_reason,
        target_kind=row.target_kind,
        target_id=row.target_id,
        target_label=row.target_label,
        next_run_at=row.next_run_at,
        last_run_at=row.last_run_at,
        last_status=row.last_status,
        last_error=row.last_error,
        latest_execution_id=execution.id if execution is not None else None,
        latest_thread_id=execution.thread_id if execution is not None else None,
        latest_turn_id=execution.turn_id if execution is not None else None,
        latest_result_summary=execution.result_summary if execution is not None else None,
    )


def _fresh_automation_settings():
    from packages.config import get_settings

    get_settings.cache_clear()
    return get_settings()


def activate_automation(
    request: AutomationScheduleInput,
    scheduler: LocalTaskScheduler,
    store: AutomationStore,
    *,
    target_label: str | None = None,
    runtime_settings: Any | None = None,
) -> AutomationScheduleResponse:
    started = perf_counter()
    settings = runtime_settings or _fresh_automation_settings()
    blocked = automation_blocked_reason(request.task_id, settings)
    if blocked is not None:
        return AutomationScheduleResponse(
            tool_name="automation_schedule",
            status=ToolStatus.FAILURE,
            success=False,
            evidence=[EvidenceSource(
                source="agent_automation_configuration",
                source_ref=blocked,
            )],
            error_code=ToolErrorCode.INVALID_INPUT,
            error_message="计划未创建：" + automation_blocked_message(blocked),
            timeout_ms=request.timeout_ms,
            elapsed_ms=max(0, int((perf_counter() - started) * 1_000)),
        )
    definition = scheduler.task_definition(request.task_id)
    hour, minute = (int(part) for part in request.start_time.split(":"))
    row = store.upsert_daily(
        task_id=request.task_id,
        task_label=definition.label,
        start_time=time(hour, minute),
        target_kind="application" if request.application_id else "all",
        target_id=request.application_id,
        target_label=target_label,
        active=True,
    )
    return AutomationScheduleResponse(
        tool_name="automation_schedule",
        status=ToolStatus.SUCCESS,
        success=True,
        data=_schedule_data(row),
        evidence=[
            EvidenceSource(
                source="agent_automation_store",
                source_ref=f"schedule:{row.id}",
            )
        ],
        timeout_ms=request.timeout_ms,
        elapsed_ms=max(0, int((perf_counter() - started) * 1_000)),
    )


def list_automations(
    request: AutomationScheduleListInput,
    store: AutomationStore,
    *,
    runtime_settings: Any | None = None,
) -> AutomationScheduleListResponse:
    started = perf_counter()
    settings = runtime_settings or _fresh_automation_settings()
    engine_blocked = automation_blocked_reason(None, settings)
    rows = store.list(active_only=request.active_only)
    return AutomationScheduleListResponse(
        tool_name="automation_schedule_list",
        status=ToolStatus.SUCCESS,
        success=True,
        data=AutomationScheduleListData(
            schedules=[
                _schedule_data(
                    row,
                    (store.executions(row.id, limit=1) or [None])[0],
                    blocked_reason=(
                        automation_blocked_message(reason)
                        if row.active
                        and (reason := automation_blocked_reason(row.task_id, settings)) is not None
                        else None
                    ),
                )
                for row in rows
            ],
            total=len(rows),
            engine_configured=engine_blocked is None,
            engine_blocked_reason=(
                automation_blocked_message(engine_blocked)
                if engine_blocked is not None
                else None
            ),
        ),
        evidence=[EvidenceSource(source="agent_automation_store", source_ref="schedules")],
        timeout_ms=request.timeout_ms,
        elapsed_ms=max(0, int((perf_counter() - started) * 1_000)),
    )


def disable_automation(
    request: AutomationScheduleDisableInput,
    store: AutomationStore,
) -> AutomationScheduleDisableResponse:
    started = perf_counter()
    row = store.disable(request.schedule_id)
    if row is None:
        return AutomationScheduleDisableResponse(
            tool_name="automation_schedule_disable",
            status=ToolStatus.NO_RESULTS,
            success=False,
            evidence=[
                EvidenceSource(
                    source="agent_automation_store",
                    source_ref=f"schedule:{request.schedule_id}",
                )
            ],
            error_code=ToolErrorCode.NOT_FOUND,
            error_message="Local automation schedule was not found.",
            timeout_ms=request.timeout_ms,
            elapsed_ms=max(0, int((perf_counter() - started) * 1_000)),
        )
    return AutomationScheduleDisableResponse(
        tool_name="automation_schedule_disable",
        status=ToolStatus.SUCCESS,
        success=True,
        data=_schedule_data(row),
        evidence=[
            EvidenceSource(
                source="agent_automation_store",
                source_ref=f"schedule:{row.id}",
            )
        ],
        timeout_ms=request.timeout_ms,
        elapsed_ms=max(0, int((perf_counter() - started) * 1_000)),
    )


__all__ = [
    "AutomationScheduleData",
    "AutomationScheduleDisableInput",
    "AutomationScheduleDisableResponse",
    "AutomationScheduleInput",
    "AutomationScheduleListData",
    "AutomationScheduleListInput",
    "AutomationScheduleListResponse",
    "AutomationScheduleResponse",
    "AutomationPlanData",
    "AutomationPlanInput",
    "AutomationPlanResponse",
    "OperationalTaskRunData",
    "OperationalTaskRunInput",
    "OperationalTaskRunResponse",
    "OperationalTaskRunner",
    "activate_automation",
    "disable_automation",
    "list_automations",
    "plan_automation",
]
