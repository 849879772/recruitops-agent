"""The four fixed local recruitment task definitions."""

from __future__ import annotations

from datetime import time
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from .models import DailySchedule, TaskCallable, TaskContext, TaskDefinition


class TaskType(str, Enum):
    """Stable task identifiers shared by the runner and Windows scripts."""

    DAILY_RECRUITMENT_INTELLIGENCE = "daily_recruitment_intelligence"
    CRAWLER_HEALTH = "crawler_health"
    APPLICATION_PROGRESS = "application_progress"
    RECRUITMENT_MAILBOX = "recruitment_mailbox"


def default_task_definitions() -> dict[str, TaskDefinition]:
    """Build fresh definitions so callers cannot mutate the fixed catalog."""

    return {
        TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value: TaskDefinition(
            task_id=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
            label="本地每日招聘情报",
            schedule=DailySchedule(time(8, 0)),
            timeout_seconds=21_600.0,
            max_retries=0,
            misfire_grace_seconds=7_200.0,
            agent_write_enabled=True,
        ),
        TaskType.CRAWLER_HEALTH.value: TaskDefinition(
            task_id=TaskType.CRAWLER_HEALTH.value,
            label="本地爬虫健康检查",
            schedule=DailySchedule(time(8, 15)),
        ),
        TaskType.APPLICATION_PROGRESS.value: TaskDefinition(
            task_id=TaskType.APPLICATION_PROGRESS.value,
            label="本地投递进度复核",
            schedule=DailySchedule(time(20, 0)),
        ),
        TaskType.RECRUITMENT_MAILBOX.value: TaskDefinition(
            task_id=TaskType.RECRUITMENT_MAILBOX.value,
            label="本地招聘邮箱检查",
            schedule=DailySchedule(time(9, 0)),
        ),
    }


def read_only_task_handler(context: TaskContext) -> dict[str, object]:
    """Return an observation marker until a caller injects a real read callable."""

    return {
        "task_id": context.task_id,
        "mode": "read_only",
        "action": "observation_only",
        "attempt": context.attempt,
    }


def default_task_handlers() -> dict[str, TaskCallable]:
    """Return independent handlers for the CLI's safe, no-op default path."""

    return {
        task_id: read_only_task_handler
        for task_id in default_task_definitions()
    }


DEFAULT_TASKS: Mapping[str, TaskDefinition] = MappingProxyType(default_task_definitions())
TASK_DEFINITIONS = DEFAULT_TASKS
DEFAULT_TASK_HANDLERS: Mapping[str, TaskCallable] = MappingProxyType(default_task_handlers())
