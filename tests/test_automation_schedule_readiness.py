from __future__ import annotations

from datetime import datetime, time, timezone
from types import SimpleNamespace

from packages.tools.operations import (
    AutomationScheduleInput,
    AutomationScheduleListInput,
    activate_automation,
    list_automations,
)
from packages.tools.typed import ToolStatus


class FakeScheduler:
    def task_definition(self, task_id):
        return SimpleNamespace(label=f"fixture:{task_id}")


class FakeStore:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.created = []

    def upsert_daily(self, **values):
        self.created.append(values)
        row = _row(task_id=values["task_id"], active=values["active"])
        row.start_time = values["start_time"]
        self.rows.append(row)
        return row

    def list(self, *, active_only=False):
        return [row for row in self.rows if not active_only or row.active]

    def executions(self, _schedule_id, *, limit=20):
        return []


def _row(*, task_id, active=True):
    return SimpleNamespace(
        id=f"schedule-{task_id}",
        task_id=task_id,
        task_label=f"fixture:{task_id}",
        frequency="daily",
        start_time=time(5, 0),
        timezone_name="Asia/Shanghai",
        active=active,
        target_kind="all",
        target_id=None,
        target_label=None,
        next_run_at=datetime(2026, 9, 20, 21, 0, tzinfo=timezone.utc),
        last_run_at=None,
        last_status=None,
        last_error=None,
    )


def _settings(**overrides):
    values = {
        "codex_runtime_enabled": True,
        "automation_enabled": True,
        "mail_enabled": True,
        "write_enabled": True,
        "first_run_complete": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_mail_schedule_creation_reports_mail_capability_instead_of_saving_active_row():
    store = FakeStore()
    response = activate_automation(
        AutomationScheduleInput(task_id="recruitment_mailbox", start_time="05:00"),
        FakeScheduler(),
        store,
        runtime_settings=_settings(mail_enabled=False),
    )

    assert response.status == ToolStatus.FAILURE
    assert response.success is False
    assert response.data is None
    assert "邮箱" in response.error_message
    assert store.created == []


def test_first_run_completion_and_job_preferences_do_not_block_authorized_task():
    store = FakeStore()
    response = activate_automation(
        AutomationScheduleInput(task_id="application_progress", start_time="05:00"),
        FakeScheduler(),
        store,
        runtime_settings=_settings(mail_enabled=False, first_run_complete=False),
    )

    assert response.status == ToolStatus.SUCCESS
    assert response.success is True
    assert response.data is not None and response.data.runnable is True
    assert store.created[0]["active"] is True

    mail_response = activate_automation(
        AutomationScheduleInput(task_id="recruitment_mailbox", start_time="05:00"),
        FakeScheduler(),
        FakeStore(),
        runtime_settings=_settings(first_run_complete=False, mail_enabled=True),
    )
    assert mail_response.status == ToolStatus.SUCCESS
    assert mail_response.data is not None and mail_response.data.runnable is True


def test_schedule_query_explains_engine_and_task_specific_readiness():
    store = FakeStore([_row(task_id="recruitment_mailbox"), _row(task_id="application_progress")])
    response = list_automations(
        AutomationScheduleListInput(),
        store,
        runtime_settings=_settings(mail_enabled=False, first_run_complete=False),
    )

    assert response.data is not None
    assert response.data.engine_configured is True
    by_task = {row.task_id: row for row in response.data.schedules}
    assert by_task["recruitment_mailbox"].runnable is False
    assert "邮箱" in by_task["recruitment_mailbox"].blocked_reason
    assert by_task["application_progress"].runnable is True

    blocked = list_automations(
        AutomationScheduleListInput(),
        store,
        runtime_settings=_settings(automation_enabled=False),
    )
    assert blocked.data is not None
    assert blocked.data.engine_configured is False
    assert "定时任务开关" in blocked.data.engine_blocked_reason
    assert all(not row.runnable for row in blocked.data.schedules)
