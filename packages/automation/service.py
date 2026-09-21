from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
from zoneinfo import ZoneInfo
from uuid import uuid4

from sqlalchemy import select

from packages.storage import AutomationExecution, AutomationSchedule, Storage


DEFAULT_TIMEZONE = "Asia/Shanghai"
_BLOCKED_MESSAGES = {
    "codex_runtime_disabled": "本地助理尚未在当前桌面实例启用；启用模型/助理后需重启，再创建计划。",
    "automation_disabled": "全局定时任务开关已关闭，请先在配置中启用。",
    "mail_disabled": "招聘邮箱任务需要启用并完成邮箱连接配置。",
    "write_disabled": "此任务需要当前实例具备本地写入权限。",
}


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def next_daily_run(
    start_time: time,
    timezone_name: str = DEFAULT_TIMEZONE,
    *,
    now: datetime | None = None,
) -> datetime:
    zone = ZoneInfo(timezone_name)
    local_now = _utc(now).astimezone(zone)
    candidate = datetime.combine(local_now.date(), start_time, tzinfo=zone)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def automation_blocked_reason(task_id: str | None, settings) -> str | None:
    """Return the capability gate relevant to this engine or task."""
    if not getattr(settings, "codex_runtime_enabled", False):
        return "codex_runtime_disabled"
    if not getattr(settings, "automation_enabled", False):
        return "automation_disabled"
    if task_id == "recruitment_mailbox" and not getattr(settings, "mail_enabled", False):
        return "mail_disabled"
    if task_id in {"daily_recruitment_intelligence", "application_progress"} and not getattr(
        settings, "write_enabled", False
    ):
        return "write_disabled"
    return None


def automation_blocked_message(reason: str) -> str:
    return _BLOCKED_MESSAGES.get(reason, "计划所需能力当前不可用。")


@dataclass(frozen=True)
class ClaimedAutomation:
    execution_id: str
    schedule_id: str
    task_id: str
    task_label: str
    target_kind: str
    target_id: str | None
    target_label: str | None
    scheduled_for: datetime


class AutomationStore:
    """Persistence and atomic claiming for local recurring automations."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def upsert_daily(
        self,
        *,
        task_id: str,
        task_label: str,
        start_time: time,
        target_kind: str = "all",
        target_id: str | None = None,
        target_label: str | None = None,
        timezone_name: str = DEFAULT_TIMEZONE,
        active: bool = True,
        now: datetime | None = None,
    ) -> AutomationSchedule:
        timestamp = _utc(now)
        target_key = target_id or "*"
        schedule_key = (
            f"{task_id}:{target_key}:{timezone_name}:"
            f"{start_time.strftime('%H:%M:%S')}"
        )
        schedule_id = f"automation-{sha256(schedule_key.encode('utf-8')).hexdigest()[:24]}"
        with self.storage.transaction(write=True) as session:
            row = session.scalar(
                select(AutomationSchedule).where(
                    AutomationSchedule.task_id == task_id,
                    AutomationSchedule.target_key == target_key,
                    AutomationSchedule.start_time == start_time,
                    AutomationSchedule.timezone_name == timezone_name,
                )
            )
            if row is None:
                row = AutomationSchedule(
                    id=schedule_id,
                    task_id=task_id,
                    task_label=task_label,
                    target_kind=target_kind,
                    target_key=target_key,
                    target_id=target_id,
                    target_label=target_label,
                    frequency="daily",
                    start_time=start_time,
                    timezone_name=timezone_name,
                    active=active,
                    next_run_at=next_daily_run(start_time, timezone_name, now=timestamp),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                session.add(row)
            else:
                row.task_label = task_label
                row.target_kind = target_kind
                row.target_id = target_id
                row.target_label = target_label
                row.active = active
                row.next_run_at = next_daily_run(start_time, timezone_name, now=timestamp)
                row.updated_at = timestamp
            session.flush()
            session.refresh(row)
            return row

    def list(self, *, active_only: bool = False) -> list[AutomationSchedule]:
        statement = select(AutomationSchedule)
        if active_only:
            statement = statement.where(AutomationSchedule.active.is_(True))
        statement = statement.order_by(
            AutomationSchedule.active.desc(), AutomationSchedule.next_run_at
        )
        with self.storage.session() as session:
            return list(session.scalars(statement))

    def disable(self, schedule_id: str, *, now: datetime | None = None) -> AutomationSchedule | None:
        timestamp = _utc(now)
        with self.storage.transaction(write=True) as session:
            row = session.get(AutomationSchedule, schedule_id)
            if row is None:
                return None
            row.active = False
            row.updated_at = timestamp
            session.flush()
            session.refresh(row)
            return row

    def skip_missed_occurrences(self, *, now: datetime | None = None) -> int:
        """Advance overdue schedules at worker startup without replaying them."""
        timestamp = _utc(now)
        with self.storage.transaction(write=True) as session:
            statement = (
                select(AutomationSchedule)
                .where(
                    AutomationSchedule.active.is_(True),
                    AutomationSchedule.next_run_at <= timestamp,
                )
                .order_by(AutomationSchedule.next_run_at)
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            rows = list(session.scalars(statement))
            for row in rows:
                row.next_run_at = next_daily_run(
                    row.start_time,
                    row.timezone_name,
                    now=timestamp,
                )
                row.updated_at = timestamp
            session.flush()
            return len(rows)

    def claim_due(self, *, now: datetime | None = None) -> ClaimedAutomation | None:
        timestamp = _utc(now)
        with self.storage.transaction(write=True) as session:
            statement = (
                select(AutomationSchedule)
                .where(
                    AutomationSchedule.active.is_(True),
                    AutomationSchedule.next_run_at <= timestamp,
                )
                .order_by(AutomationSchedule.next_run_at)
                .limit(1)
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            row = session.scalar(statement)
            if row is None:
                return None
            scheduled_for = _utc(row.next_run_at)
            recorded_occurrences = {
                _utc(value)
                for value in session.scalars(
                    select(AutomationExecution.scheduled_for).where(
                        AutomationExecution.schedule_id == row.id
                    )
                )
            }
            # A pre-v17 run may have persisted the failure but left next_run_at
            # on the same occurrence. Preserve that history and move forward.
            while scheduled_for in recorded_occurrences:
                scheduled_for = next_daily_run(
                    row.start_time,
                    row.timezone_name,
                    now=scheduled_for,
                )
                row.next_run_at = scheduled_for
                if scheduled_for > timestamp:
                    session.flush()
                    return None
            execution = AutomationExecution(
                id=f"automation-run-{uuid4().hex}",
                schedule_id=row.id,
                scheduled_for=scheduled_for,
                status="running",
                started_at=timestamp,
            )
            session.add(execution)
            row.next_run_at = next_daily_run(
                row.start_time,
                row.timezone_name,
                now=max(timestamp, scheduled_for),
            )
            row.last_run_at = timestamp
            row.last_status = "running"
            row.last_error = None
            row.updated_at = timestamp
            session.flush()
            return ClaimedAutomation(
                execution_id=execution.id,
                schedule_id=row.id,
                task_id=row.task_id,
                task_label=row.task_label,
                target_kind=row.target_kind,
                target_id=row.target_id,
                target_label=row.target_label,
                scheduled_for=scheduled_for,
            )

    def complete(
        self,
        execution_id: str,
        *,
        status: str,
        result_summary: str | None = None,
        error: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        now: datetime | None = None,
    ) -> AutomationExecution:
        if status not in {"succeeded", "failed", "blocked"}:
            raise ValueError("invalid terminal automation status")
        timestamp = _utc(now)
        with self.storage.transaction(write=True) as session:
            execution = session.get(AutomationExecution, execution_id)
            if execution is None:
                raise KeyError(f"automation execution not found: {execution_id}")
            execution.status = status
            execution.result_summary = result_summary
            execution.error = error
            execution.thread_id = thread_id
            execution.turn_id = turn_id
            execution.completed_at = timestamp
            schedule = session.get(AutomationSchedule, execution.schedule_id)
            if schedule is not None:
                schedule.last_status = status
                schedule.last_error = error
                schedule.updated_at = timestamp
            session.flush()
            session.refresh(execution)
            return execution

    def mark_running_context(
        self,
        execution_id: str,
        *,
        thread_id: str,
        turn_id: str,
    ) -> None:
        with self.storage.transaction(write=True) as session:
            execution = session.get(AutomationExecution, execution_id)
            if execution is None:
                raise KeyError(f"automation execution not found: {execution_id}")
            if execution.status != "running":
                return
            execution.thread_id = thread_id
            execution.turn_id = turn_id

    def executions(self, schedule_id: str, *, limit: int = 20) -> list[AutomationExecution]:
        with self.storage.session() as session:
            return list(
                session.scalars(
                    select(AutomationExecution)
                    .where(AutomationExecution.schedule_id == schedule_id)
                    .order_by(AutomationExecution.started_at.desc())
                    .limit(limit)
                )
            )

    def recover_interrupted(self, *, now: datetime | None = None) -> int:
        """Close executions left running by a previous local API process."""

        timestamp = _utc(now)
        recovered = 0
        with self.storage.transaction(write=True) as session:
            rows = list(
                session.scalars(
                    select(AutomationExecution).where(
                        AutomationExecution.status == "running"
                    )
                )
            )
            for execution in rows:
                execution.status = "failed"
                execution.error = "local API restarted before automation completed"
                execution.completed_at = timestamp
                schedule = session.get(AutomationSchedule, execution.schedule_id)
                if schedule is not None:
                    schedule.last_status = "failed"
                    schedule.last_error = execution.error
                    schedule.updated_at = timestamp
                recovered += 1
        return recovered


__all__ = [
    "AutomationStore",
    "ClaimedAutomation",
    "DEFAULT_TIMEZONE",
    "automation_blocked_message",
    "automation_blocked_reason",
    "next_daily_run",
]
