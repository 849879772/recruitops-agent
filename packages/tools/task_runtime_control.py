"""Durable, cooperative controls for scheduler-owned background daily tasks."""

from __future__ import annotations

import os
from sqlalchemy import select

from packages.storage.models import TaskRun, ToolCall, utc_now

CONTROL_TOOL = "task_runtime_control"
DAILY_TASK_TYPES = {"daily_recruitment_intelligence", "daily_recruitment_sync"}
ACTIVE_STATUSES = {"accepted", "running", "pausing", "cancelling"}
RECOVERABLE_STATUSES = {"stopped", "paused", "failed", "timed_out", "interrupted"}


def register_daily_task(storage, run_id: str, task_type: str, *, thread_id=None, turn_id=None) -> None:
    """Commit an identifiable receipt before the worker (or its reply) can run."""
    with storage.write_transaction() as session:
        session.add(TaskRun(id=run_id, idempotency_key=f"task_run:{run_id}", task_type=task_type,
                            status="accepted", user_request="后台招聘任务", source="agent_scheduler"))
        session.flush()
        session.add(ToolCall(id=f"control:{run_id}", task_id=run_id, tool_name=CONTROL_TOOL,
                            arguments={"metadata": {"task_kind": "daily", "thread_id": thread_id,
                                                   "turn_id": turn_id}, "control_request": None,
                                       "desktop_run_id": os.environ.get("RECRUITOPS_DESKTOP_RUN_ID", ""),
                                       "drained": False}, source="agent_scheduler"))


def daily_control_request(storage, run_id: str) -> str | None:
    with storage.session() as session:
        row = session.scalar(select(ToolCall).where(ToolCall.task_id == run_id, ToolCall.tool_name == CONTROL_TOOL))
        return (row.arguments or {}).get("control_request") if row else None


def request_daily_control(storage, run_id: str, action: str) -> dict:
    if action not in {"pause", "cancel"}:
        return {"success": False, "reason": "unsupported_action", "run_id": run_id}
    with storage.write_transaction() as session:
        row = session.scalar(select(ToolCall).where(
            ToolCall.task_id == run_id, ToolCall.tool_name == CONTROL_TOOL,
        ).with_for_update())
        task = session.scalar(select(TaskRun).where(TaskRun.id == run_id).with_for_update())
        if task is None or task.task_type not in DAILY_TASK_TYPES:
            return {"success": False, "reason": "control_not_available", "run_id": run_id}
        # Old interrupted runs predate control receipts. Locking the task also
        # serializes creation of their cancellation receipt; re-read after lock.
        if row is None:
            row = session.scalar(select(ToolCall).where(
                ToolCall.task_id == run_id, ToolCall.tool_name == CONTROL_TOOL,
            ).with_for_update())
        if task.status == "cancelled" and action == "cancel":
            return {"success": True, "run_id": run_id, "status": "cancelled", "already_cancelled": True}
        state = dict(row.arguments or {}) if row is not None else {}
        previous_boot = state.get("desktop_run_id")
        current_boot = os.environ.get("RECRUITOPS_DESKTOP_RUN_ID", "")
        interrupted = bool(previous_boot and current_boot and previous_boot != current_boot)
        recoverable = task.status in RECOVERABLE_STATUSES or (interrupted and task.status in ACTIVE_STATUSES)
        # No live owner remains after restart, a joined worker, or a legacy
        # terminal receipt. Cancel the continuation, not its historical results.
        if action == "cancel" and recoverable and (row is None or interrupted or state.get("drained")):
            if row is None:
                row = ToolCall(id=f"control:{run_id}", task_id=run_id, tool_name=CONTROL_TOOL,
                               arguments={}, source="agent_scheduler")
                session.add(row)
            state.update(control_request="cancel", drained=True)
            row.arguments = state
            row.updated_at = task.updated_at = utc_now()
            task.status = "cancelled"
            return {"success": True, "run_id": run_id, "status": "cancelled",
                    "message": "已取消该中断任务的后续续跑；已保存的岗位、评分和断点历史均保留。"}
        if row is None:
            return {"success": False, "reason": "control_not_available", "run_id": run_id}
        # A same-boot worker can be draining even after writing a stopped state.
        # Keep cancellation cooperative until finish_daily_task confirms it.
        if action == "cancel" and recoverable and not state.get("drained"):
            state["control_request"] = "cancel"
            row.arguments = state
            row.updated_at = task.updated_at = utc_now()
            task.status = "cancelling"
            return {"success": True, "run_id": run_id, "status": "cancelling",
                    "message": "已请求取消，等待本次启动中的执行者安全结束。"}
        if task.status not in ACTIVE_STATUSES or state.get("drained") or interrupted:
            return {"success": False, "reason": "not_running", "run_id": run_id}
        if state.get("control_request") == "cancel":
            action = "cancel"  # Cancellation cannot be weakened back to pause.
        state["control_request"] = action
        row.arguments = state
        row.updated_at = task.updated_at = utc_now()
        task.status = "pausing" if action == "pause" else "cancelling"
        return {"success": True, "run_id": run_id, "status": task.status,
                "message": "已请求停止派发，正在保存并等待正在执行的工作安全结束。"}


def finish_daily_task(storage, run_id: str, status: str) -> str:
    """Only the caller that has joined the worker may confirm a terminal control."""
    with storage.write_transaction() as session:
        row = session.scalar(select(ToolCall).where(
            ToolCall.task_id == run_id, ToolCall.tool_name == CONTROL_TOOL,
        ).with_for_update())
        task = session.get(TaskRun, run_id)
        state = dict(row.arguments or {})
        control = state.get("control_request")
        final = "cancelled" if control == "cancel" else "paused" if control == "pause" else status
        state["drained"] = True
        row.arguments = state
        task.status = final
        row.updated_at = task.updated_at = utc_now()
        return final
