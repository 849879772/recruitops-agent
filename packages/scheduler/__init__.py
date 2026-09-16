"""Pure-local fixed task orchestration for RecruitOps."""

from .lock import LocalInstanceLock, LockAcquisitionError
from .models import (
    DailySchedule,
    RunMetadata,
    RunStatus,
    TaskCallable,
    TaskContext,
    TaskDefinition,
    TaskRunResult,
)
from .runner import DEFAULT_LOCK_PATH, LocalTaskScheduler, TaskNotFoundError
from .tasks import (
    DEFAULT_TASK_HANDLERS,
    DEFAULT_TASKS,
    TASK_DEFINITIONS,
    TaskType,
    default_task_definitions,
    default_task_handlers,
    read_only_task_handler,
)


def build_runtime_task_handlers(*args, **kwargs):
    """Load runtime wiring lazily so pure pipeline imports stay acyclic."""

    from .runtime import build_runtime_task_handlers as build

    return build(*args, **kwargs)

__all__ = [
    "DEFAULT_LOCK_PATH",
    "DEFAULT_TASK_HANDLERS",
    "DEFAULT_TASKS",
    "TASK_DEFINITIONS",
    "DailySchedule",
    "LocalInstanceLock",
    "LocalTaskScheduler",
    "LockAcquisitionError",
    "RunMetadata",
    "RunStatus",
    "TaskCallable",
    "TaskContext",
    "TaskDefinition",
    "TaskNotFoundError",
    "TaskRunResult",
    "TaskType",
    "default_task_definitions",
    "default_task_handlers",
    "build_runtime_task_handlers",
    "read_only_task_handler",
]
