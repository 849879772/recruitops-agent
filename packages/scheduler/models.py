"""Data contracts for the local scheduler with a read-only legacy boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Any, Callable, Mapping, TypeAlias


TaskCallable: TypeAlias = Callable[["TaskContext"], Any]


class RunStatus(str, Enum):
    """The terminal state of one local task invocation."""

    SUCCESS = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED_LOCKED = "skipped_locked"
    DRY_RUN = "dry_run"


@dataclass(frozen=True)
class DailySchedule:
    """A local-time daily schedule used by Windows Task Scheduler."""

    start_time: time

    def __post_init__(self) -> None:
        if self.start_time.tzinfo is not None:
            raise ValueError("daily schedule times must be local and timezone-naive")

    def scheduled_for(self, now: datetime) -> datetime:
        """Return the most recent occurrence at or before ``now``."""

        candidate = now.replace(
            hour=self.start_time.hour,
            minute=self.start_time.minute,
            second=self.start_time.second,
            microsecond=self.start_time.microsecond,
        )
        if candidate > now:
            candidate -= timedelta(days=1)
        return candidate

    def to_dict(self) -> dict[str, str]:
        return {"frequency": "daily", "start_time": self.start_time.strftime("%H:%M:%S")}


@dataclass(frozen=True)
class TaskDefinition:
    """A fixed task contract.

    ``read_only`` protects the legacy autumn-recruitment source.  A task may
    separately write Agent-owned PostgreSQL state when ``agent_write_enabled``
    is true.
    """

    task_id: str
    label: str
    schedule: DailySchedule
    timeout_seconds: float = 300.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.0
    misfire_grace_seconds: float = 900.0
    read_only: bool = True
    agent_write_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.task_id or self.task_id.strip() != self.task_id:
            raise ValueError("task_id must be a non-empty, trimmed string")
        if not self.label.strip():
            raise ValueError("task label must be non-empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds cannot be negative")
        if self.misfire_grace_seconds < 0:
            raise ValueError("misfire_grace_seconds cannot be negative")
        if not self.read_only:
            raise ValueError("legacy recruitment source must remain read-only")


@dataclass(frozen=True)
class TaskContext:
    """The only input supplied to an injected task callable."""

    task_id: str
    task_label: str
    scheduled_for: datetime
    run_id: str
    attempt: int
    read_only: bool = True
    write_enabled: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunMetadata:
    """Scheduling evidence retained with a result, including misfire data."""

    scheduled_for: datetime
    observed_at: datetime
    lateness_seconds: float
    missed: bool
    catch_up: bool
    reason: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheduled_for": self.scheduled_for.isoformat(),
            "observed_at": self.observed_at.isoformat(),
            "lateness_seconds": self.lateness_seconds,
            "missed": self.missed,
            "catch_up": self.catch_up,
            "reason": self.reason,
            "details": _json_safe(self.details),
        }


@dataclass(frozen=True)
class TaskRunResult:
    """A serializable summary of a task run without persisting business data."""

    task_id: str
    task_label: str
    run_id: str
    status: RunStatus
    attempts: int
    read_only: bool
    run_metadata: RunMetadata
    started_at: datetime
    finished_at: datetime
    value: Any = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.status is RunStatus.SUCCESS

    @property
    def retry_count(self) -> int:
        return max(0, self.attempts - 1)

    @property
    def timed_out(self) -> bool:
        return self.status is RunStatus.TIMED_OUT

    @property
    def metadata(self) -> dict[str, Any]:
        """Return catch-up metadata in the convenient mapping form."""

        return self.run_metadata.to_dict()

    @property
    def result(self) -> Any:
        """Alias for callers that prefer the word ``result`` to ``value``."""

        return self.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_label": self.task_label,
            "run_id": self.run_id,
            "status": self.status.value,
            "ok": self.success,
            "attempts": self.attempts,
            "retry_count": self.retry_count,
            "read_only": self.read_only,
            "timed_out": self.timed_out,
            "scheduled_for": self.run_metadata.scheduled_for.isoformat(),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "metadata": self.metadata,
            "result": _json_safe(self.value),
            "error": self.error,
        }


def _json_safe(value: Any) -> Any:
    """Keep CLI output serializable without retaining arbitrary object state."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return repr(value)
