"""Persisted discovery and cooperative control for application-review runs."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os
from time import perf_counter, time
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import select, text

from packages.storage.models import TaskRun, ToolCall, utc_now
from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolResponse, ToolStatus

STATE_TOOL = "application_review_checkpoint"
ACTIVE_STATUSES = {"accepted", "running", "awaiting_continuation", "pausing", "cancelling"}
RECOVERABLE_STATUSES = {"stopped", "paused", "failed"}
REVIEW_CONTEXT: ContextVar[tuple[Any, str, str] | None] = ContextVar("application_review_owner", default=None)


class ApplicationReviewStatusInput(ToolInput):
    run_id: str | None = Field(default=None, pattern=r"^status-review-[0-9a-f]{32}$")
    thread_id: str | None = Field(default=None, min_length=1, max_length=255)


class ApplicationReviewControlInput(ApplicationReviewStatusInput):
    timeout_ms: int = Field(default=110_000, ge=1, le=120_000)
    action: Literal["resume", "pause", "cancel"]
    turn_id: str | None = Field(default=None, min_length=1, max_length=255)


class ApplicationReviewStatusResponse(ToolResponse[dict[str, Any]]):
    evidence: list[EvidenceSource] = Field(default_factory=lambda: [EvidenceSource(source="agent.application_review_checkpoint")])
    timeout_ms: int = 5_000
    elapsed_ms: int = 0


class ApplicationReviewControlResponse(ApplicationReviewStatusResponse):
    read_only: Literal[False] = False


def lock_review_scope(session) -> None:
    if session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(718202609)"))


def owner_interrupted(state: dict) -> bool:
    previous = str((state.get("owner") or {}).get("desktop_run_id") or "")
    current = os.environ.get("RECRUITOPS_DESKTOP_RUN_ID", "")
    # Desktop startup holds the exclusive instance lock. A different boot ID
    # proves the old worker cannot survive; a mere stale status does not.
    return bool(previous and current and previous != current)


def lease_active(state: dict) -> bool:
    return not owner_interrupted(state) and float(state.get("lease_until") or 0) > time()


def review_summary(run_id: str, state: dict, status: str | None = None) -> dict:
    from .application_review_run import _response

    saved = dict(state)
    if status:
        saved["run_status"] = status
    if owner_interrupted(saved) and saved.get("run_status") in ACTIVE_STATUSES | {"stopped"}:
        saved["run_status"] = "stopped"
        saved["interruption_reason"] = "desktop_restarted"
    elif saved.get("run_status") == "awaiting_continuation" and float(saved.get("continuation_until") or 0) <= time():
        saved["run_status"] = "stopped"
        saved["interruption_reason"] = "assistant_continuation_expired"
    elif saved.get("run_status") == "running" and not lease_active(saved):
        # Legacy synchronous waves also used "running" between invocations.
        # A resumable receipt is not evidence of an active browser worker.
        saved["run_status"] = "stopped"
        saved["interruption_reason"] = "worker_heartbeat_lost"
    result = dict(_response(run_id, saved, perf_counter(), busy=lease_active(saved)).summary)
    metadata = saved.get("metadata") or {}
    result.update(task_kind="application_review", thread_id=metadata.get("thread_id"))
    result["can_resume"] = (
        result["remaining_count"] > 0 and not result["in_progress"]
        and result["run_status"] not in {"cancelled", "cancelling", "pausing"}
        and result.get("control_request") != "cancel"
    )
    result["can_pause"] = result["run_status"] in {"accepted", "running", "awaiting_continuation"}
    result["can_cancel"] = result["run_status"] not in {"completed", "cancelled", "cancelling"}
    result["actions"] = [name for name in ("resume", "pause", "cancel") if result.get("can_" + name)]
    return result


def review_runs(storage, *, run_id: str | None = None, thread_id: str | None = None,
                statuses: set[str] | None = None) -> list[dict]:
    with storage.session() as session:
        statement = select(TaskRun, ToolCall).join(ToolCall, ToolCall.task_id == TaskRun.id).where(
            TaskRun.task_type == "application_status_review", ToolCall.tool_name == STATE_TOOL,
        )
        if run_id:
            statement = statement.where(TaskRun.id == run_id)
        else:
            allowed = ACTIVE_STATUSES | RECOVERABLE_STATUSES if statuses is None else statuses
            statement = statement.where(TaskRun.status.in_(allowed))
        rows = session.execute(statement.order_by(TaskRun.updated_at.desc())).all()
        runs = []
        for task, checkpoint in rows:
            state = dict(checkpoint.arguments or {})
            if thread_id and (state.get("metadata") or {}).get("thread_id") != thread_id:
                continue
            summary = review_summary(task.id, state, task.status)
            summary["updated_at"] = checkpoint.updated_at.isoformat() if checkpoint.updated_at else None
            runs.append(summary)
        return runs


def application_review_status(request: ApplicationReviewStatusInput, repository) -> ApplicationReviewStatusResponse:
    from .batch_browser_operations import _storage

    storage = _storage(repository)
    if storage is None:
        return ApplicationReviewStatusResponse(
            tool_name="application_review_status", status=ToolStatus.FAILURE, success=False,
            error_code=ToolErrorCode.SOURCE_UNAVAILABLE, error_message="投递记录存储不可用。",
        )
    runs = review_runs(storage, run_id=request.run_id, thread_id=request.thread_id)
    return ApplicationReviewStatusResponse(
        tool_name="application_review_status",
        status=ToolStatus.SUCCESS if len(runs) == 1 else ToolStatus.AMBIGUOUS if runs else ToolStatus.NO_RESULTS,
        success=len(runs) == 1,
        data={"run": runs[0] if len(runs) == 1 else None, "runs": runs,
              "selection": "selected" if len(runs) == 1 else "ambiguous" if runs else "not_found"},
        error_code=ToolErrorCode.AMBIGUOUS_MATCH if len(runs) > 1 else ToolErrorCode.NOT_FOUND if not runs else None,
        error_message="存在多个可恢复任务，请选择后继续。" if len(runs) > 1 else "没有找到相应任务。" if not runs else None,
        evidence=[EvidenceSource(source="agent.application_review_checkpoint")],
    )


async def control_application_review(request: ApplicationReviewControlInput, bridge, repository) -> ApplicationReviewControlResponse:
    from .batch_browser_operations import BatchObserveApplicationStatusInput, _storage
    from .application_review_run import continue_application_review

    selection = application_review_status(
        ApplicationReviewStatusInput(run_id=request.run_id, thread_id=None if request.run_id else request.thread_id), repository,
    )
    if not selection.success:
        return ApplicationReviewControlResponse(**{
            **selection.model_dump(), "tool_name": "application_review_control", "read_only": False,
        })
    selected = selection.data["run"]
    run_id = selected["run_id"]
    if not selected.get("can_" + request.action):
        return ApplicationReviewControlResponse(
            tool_name="application_review_control", status=ToolStatus.FAILURE, success=False,
            data=selection.data, error_code=ToolErrorCode.INVALID_INPUT,
            error_message="当前任务状态不支持此操作。", read_only=False,
        )
    if request.action == "resume":
        result = await continue_application_review(
            BatchObserveApplicationStatusInput(run_id=run_id,
                                              thread_id=request.thread_id, turn_id=request.turn_id,
                                              timeout_ms=request.timeout_ms),
            bridge, repository, resume_control=True,
        )
        return ApplicationReviewControlResponse(
            tool_name="application_review_control", status=result.status, success=result.success,
            data={"run": result.summary, "runs": [result.summary]}, read_only=False,
            error_code=result.error_code, error_message=result.error_message,
            timeout_ms=result.timeout_ms, elapsed_ms=result.elapsed_ms, timed_out=result.timed_out,
        )
    storage = _storage(repository)
    with storage.write_transaction() as session:
        lock_review_scope(session)
        row = session.scalar(select(ToolCall).where(
            ToolCall.task_id == run_id, ToolCall.tool_name == STATE_TOOL,
        ).with_for_update())
        task = session.get(TaskRun, run_id)
        state = dict(row.arguments or {})
        current = review_summary(run_id, state, task.status)
        if not current.get("can_" + request.action):
            return ApplicationReviewControlResponse(
                tool_name="application_review_control", status=ToolStatus.FAILURE, success=False,
                data={"run": current, "runs": [current]}, read_only=False,
                error_code=ToolErrorCode.INVALID_INPUT, error_message="任务状态已变化，请刷新后重试。",
            )
        state["control_request"] = request.action
        # Do not release an active owner's lease. It confirms the final state
        # after its in-flight pages are drained and their receipts are saved.
        task.status = ("pausing" if request.action == "pause" else "cancelling") if lease_active(state) else (
            "paused" if request.action == "pause" else "cancelled"
        )
        state["run_status"] = task.status
        if not lease_active(state):
            state["lease_until"] = 0
        row.arguments = state
        row.updated_at = task.updated_at = utc_now()
        summary = review_summary(run_id, state, task.status)
    return ApplicationReviewControlResponse(
        tool_name="application_review_control", status=ToolStatus.SUCCESS, success=True,
        data={"run": summary, "runs": [summary]}, read_only=False,
    )


@contextmanager
def review_write_guard():
    """Fence business writes from a worker whose checkpoint claim was replaced."""
    owner = REVIEW_CONTEXT.get()
    if owner is None:
        yield True
        return
    storage, run_id, claim = owner
    with storage.write_transaction() as session:
        row = session.scalar(select(ToolCall).where(
            ToolCall.task_id == run_id, ToolCall.tool_name == STATE_TOOL,
        ).with_for_update())
        state = dict(row.arguments or {}) if row is not None else {}
        yield state.get("claim") == claim and lease_active(state)
