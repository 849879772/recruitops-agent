"""Small, read-only projection of persisted daily recruitment progress for the local UI."""

from __future__ import annotations

from collections.abc import Mapping
import os

from sqlalchemy import or_, select

from packages.scheduler.runner import _business_failure, _business_paused
from packages.storage import AgentStateStore, Storage, TaskRun, ToolCall


_TASK_TYPES = ("daily_recruitment_intelligence", "daily_recruitment_sync")
_STAGES = ("discovery", "reconciliation", "crawl", "offline_reconciliation", "reporting")
_PHASES = {*_STAGES, "companies", "jd", "matching"}
_PROGRESS_FIELDS = {
    "discovery": ("pages_fetched", "pages_total", "records_seen"),
    "companies": ("scope_total", "attempted_unique", "confirmed_complete", "retry_pending", "not_started", "remaining"),
    "jd": ("run_completed", "run_total"),
    "matching": ("run_completed", "run_attempted", "run_total", "confirmed_complete", "scope_total", "retry_pending"),
}


def _daily_resume_available(record: Mapping) -> bool:
    """Mirror the frozen-resume envelope, without reading files during polling.

    The execution boundary still validates file existence, scope digest, and
    company identities. Polling only advertises that a checkpoint was recorded.
    """
    state = record.get("state") or {}
    sections = [section for section in (state, state.get("metadata"), state.get("details"),
                                        record.get("metadata"), record.get("details"))
                if isinstance(section, Mapping)]
    def value(parts, *keys):
        return next((part[key] for part in parts for key in keys if part.get(key) not in (None, "")), None)
    scope = value(sections, "scope")
    scoped = ([scope] if isinstance(scope, Mapping) else []) + sections
    if not value(scoped, "snapshot_ref", "scope_ref", "scope_snapshot", "scope_snapshot_ref", "companies_path"):
        return False
    mode = next((candidate for key in ("effective_mode", "original_mode", "mode", "requested_mode")
                 if (candidate := value(sections, key)) in {"full", "crawl_only", "score_only"}), None)
    if mode is None:
        return False
    stage = str(value(sections, "stage", "current_step") or record.get("current_step") or "").removeprefix("recoverable:")
    return bool(mode == "score_only" or stage.startswith(("matching", "score"))
                or value(sections, "checkpoint_ref", "checkpoint_path", "checkpoint_snapshot"))


def latest_daily_progress(storage: Storage) -> dict[str, object]:
    with storage.session() as session:
        rows = list(session.scalars(
            select(TaskRun).where(TaskRun.task_type.in_(_TASK_TYPES))
            .order_by(TaskRun.created_at.desc(), TaskRun.updated_at.desc()).limit(20)
        ))
    if not rows:
        return {"run": None}
    row = next((item for item in rows if item.status == "running"), rows[0])
    return {"run": _daily_projection(storage, row)}


def _daily_projection(storage, row):
    record = AgentStateStore(storage).get_task_run(row.id)
    if record is None:
        return None
    state = record.get("state") if isinstance(record.get("state"), Mapping) else {}
    result = state.get("result")
    business_error = _business_failure(result)
    status = "failed" if business_error else "paused" if _business_paused(result) else str(record["run_status"])
    step = str(record.get("current_step") or state.get("current_step") or "")
    if step.startswith("recoverable:"):
        step = step.removeprefix("recoverable:")
    phase = step.split(":", 1)[0]
    raw_stages = state.get("stage_statuses")
    stages = {
        stage: raw_stages[stage]
        for stage in _STAGES
        if isinstance(raw_stages, Mapping) and raw_stages.get(stage) in {"running", "succeeded", "failed", "skipped", "paused"}
    }
    if phase not in _PHASES:
        phase = next((stage for stage in reversed(_STAGES) if stage in stages), "starting")
    raw_progress = state.get("progress")
    progress: dict[str, int | str] | None = None
    if isinstance(raw_progress, Mapping):
        stage = raw_progress.get("stage")
        if isinstance(stage, str) and stage in _PROGRESS_FIELDS and (stage == phase or phase == "crawl" and stage in {"companies", "jd", "matching"}):
            progress = {"stage": stage}
            for field in _PROGRESS_FIELDS[stage]:
                value = raw_progress.get(field)
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000_000:
                    progress[field] = value
    metadata = record.get("metadata") or {}
    mode = metadata.get("requested_mode") if isinstance(metadata, Mapping) else None
    return {
        "status": status,
        "mode": mode if mode in {"full", "crawl_only", "score_only", "resume"} else "full",
        "phase": phase,
        "stages": stages,
        "progress": progress,
        "updated_at": record.get("updated_at"),
        "progress_updated_at": state.get("progress_updated_at"),
    }


def task_progress(storage: Storage, run_id: str | None = None, thread_id: str | None = None,
                  *, include_recoverable: bool = False) -> dict:
    """Project current tasks without executing work or falling back to history.

    A caller may explicitly bind a run to inspect its terminal receipt. Merely
    switching chats must never attach the latest completed task to a new turn.
    """
    from packages.tools.application_review_tasks import review_runs
    from packages.tools.task_runtime_control import ACTIVE_STATUSES, CONTROL_TOOL, RECOVERABLE_STATUSES
    from packages.recruitment_mail.run_service import project_mail_progress

    visible_statuses = ACTIVE_STATUSES | {"awaiting_continuation"} | ({"stopped", "paused", "failed", "timed_out", "interrupted"} if include_recoverable else set())
    runs = []
    with storage.session() as session:
        statement = select(TaskRun).where(TaskRun.task_type.in_(_TASK_TYPES))
        if run_id:
            statement = statement.where(TaskRun.id == run_id)
        else:
            undrained = select(ToolCall.task_id).where(
                ToolCall.tool_name == CONTROL_TOOL,
                ToolCall.arguments["control_request"].as_string().in_(["pause", "cancel"]),
                ToolCall.arguments["drained"].as_boolean().is_(False),
            )
            statement = statement.where(or_(TaskRun.status.in_(visible_statuses), TaskRun.id.in_(undrained)))
        rows = list(session.scalars(statement.order_by(TaskRun.updated_at.desc())))
        controls = {
            row.task_id: dict(row.arguments or {}) for row in session.scalars(
                select(ToolCall).where(ToolCall.tool_name == CONTROL_TOOL,
                                       ToolCall.task_id.in_([item.id for item in rows]))
            )
        } if rows else {}
    for row in rows:
        value = _daily_projection(storage, row)
        if value is None:
            continue
        control = controls.get(row.id, {})
        record = AgentStateStore(storage).get_task_run(row.id) or {}
        metadata = {**(record.get("metadata") or {}), **(control.get("metadata") or {})}
        if thread_id and metadata.get("thread_id") != thread_id:
            continue
        previous_boot = control.get("desktop_run_id")
        current_boot = os.environ.get("RECRUITOPS_DESKTOP_RUN_ID", "")
        if previous_boot and current_boot and previous_boot != current_boot and not control.get("drained"):
            value["status"] = "stopped"
        elif control.get("control_request"):
            if control.get("drained"):
                value["status"] = "paused" if control["control_request"] == "pause" else "cancelled"
            else:
                value["status"] = "pausing" if control["control_request"] == "pause" else "cancelling"
        progress = value.get("progress") or {}
        stage = progress.get("stage") or value["phase"]
        # Company progress counts unique persisted crawl outcomes (including
        # partial/failed outcomes), not successful companies. Scoring still
        # counts confirmed completion; checkpoint retry semantics stay intact.
        complete_key, total_key, unit = {
            "discovery": ("pages_fetched", "pages_total", "页"),
            "companies": ("attempted_unique", "scope_total", "家公司"),
            "matching": ("confirmed_complete", "scope_total", "个岗位"),
        }.get(stage, ("run_completed", "run_total", "个岗位"))
        fallback_key = "confirmed_complete" if stage == "companies" else "run_completed"
        completed = progress.get(complete_key, progress.get(fallback_key, 0))
        total = progress.get(total_key, progress.get("run_total", 0))
        value.update(run_id=row.id, task_kind="daily", thread_id=metadata.get("thread_id"),
                     completed=completed, total=total, unit=unit, phase=stage,
                     failed=progress.get("retry_pending", 0), blocked=0,
                     can_pause=bool(control) and value["status"] in {"accepted", "running"},
                     can_cancel=(value["status"] in RECOVERABLE_STATUSES
                                 or bool(control) and value["status"] in {"accepted", "running", "pausing"}),
                     can_resume=value["status"] in {"stopped", "paused", "failed", "timed_out"} and _daily_resume_available(record))
        runs.append(value)
    for summary in review_runs(storage, run_id=run_id, thread_id=thread_id, statuses=visible_statuses):
        runs.append({
            "run_id": summary["run_id"], "task_kind": "application_review", "thread_id": summary["thread_id"],
            "status": summary["run_status"], "phase": "application_review",
            "completed": summary["completed_count"], "total": summary["scope_total"], "unit": "条记录",
            "processed": summary["processed_count"], "verified": summary["verification_success_count"],
            "failed": summary["failed"], "blocked": summary["blocked"], "unresolved": summary["unresolved"],
            "remaining": summary["remaining_count"], "database_total": summary["database_total"],
            "retry_pending": summary["retryable_count"],
            "excluded_terminal": summary["excluded_terminal"], "updated_at": summary["updated_at"],
            **{key: summary[key] for key in ("can_pause", "can_resume", "can_cancel")},
        })
    runs.extend(project_mail_progress(storage, run_id=run_id, thread_id=thread_id,
                                      include_recoverable=include_recoverable)["runs"])
    if not run_id:
        runs = [value for value in runs if value["status"] in visible_statuses
                or include_recoverable and value.get("can_resume")]
    for value in runs:
        value["actions"] = [action for action in ("pause", "resume", "cancel") if value.get("can_" + action)]
    runs.sort(key=lambda value: value.get("updated_at") or "", reverse=True)
    return {"runs": runs, "run": runs[0] if len(runs) == 1 else None}
