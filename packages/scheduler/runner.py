"""Execution engine for fixed, local, read-only recruitment tasks."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
import threading
import time as time_module
from typing import Any, Callable, Mapping
from uuid import uuid4

from .lock import LocalInstanceLock
from .models import RunMetadata, RunStatus, TaskCallable, TaskContext, TaskDefinition, TaskRunResult
from .tasks import default_task_definitions


DEFAULT_LOCK_PATH = Path(".data") / "scheduler" / "recruitops.lock"
Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


@dataclass
class _AttemptState:
    done: threading.Event = field(default_factory=threading.Event)
    state_lock: threading.Lock = field(default_factory=threading.Lock)
    release_on_finish: bool = False
    value: Any = None
    error: BaseException | None = None


class TaskNotFoundError(KeyError):
    """Raised when a caller asks for a task outside the fixed catalog."""


class LocalTaskScheduler:
    """Run fixed task callables with local safety controls only.

    The scheduler owns no business repository and persists no task result. A
    callable receives a read-only :class:`TaskContext`; any business read or
    observation must be supplied by the caller through that callable.
    """

    def __init__(
        self,
        *,
        tasks: Mapping[str, TaskDefinition] | None = None,
        lock_path: str | Path = DEFAULT_LOCK_PATH,
        clock: Clock | None = None,
        sleeper: Sleeper = time_module.sleep,
    ) -> None:
        configured = dict(tasks or default_task_definitions())
        if not configured:
            raise ValueError("at least one task definition is required")
        if set(configured) != {definition.task_id for definition in configured.values()}:
            raise ValueError("task mapping keys must match task_id values")
        self._tasks = configured
        self.lock_path = Path(lock_path)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._sleeper = sleeper

    @property
    def tasks(self) -> Mapping[str, TaskDefinition]:
        return self._tasks.copy()

    def task_definition(self, task_id: str) -> TaskDefinition:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise TaskNotFoundError(task_id) from exc

    def scheduled_for(self, task_id: str, *, now: datetime | None = None) -> datetime:
        definition = self.task_definition(task_id)
        observed_at = now or self._clock()
        _require_datetime(observed_at, "now")
        return definition.schedule.scheduled_for(observed_at)

    def run(
        self,
        task_id: str,
        handler: TaskCallable | None = None,
        *,
        now: datetime | None = None,
        scheduled_for: datetime | None = None,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        dry_run: bool = False,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        stop_requested: threading.Event | None = None,
    ) -> TaskRunResult:
        """Run one task, or return a plan when ``dry_run`` is true.

        Retries apply to completed callable failures. A timed-out callable is
        not retried because it may still be running; its lock stays held until
        that callable exits, preventing overlapping attempts.
        """

        definition = self.task_definition(task_id)
        observed_at = now or self._clock()
        _require_datetime(observed_at, "now")
        scheduled = scheduled_for or definition.schedule.scheduled_for(observed_at)
        _require_datetime(scheduled, "scheduled_for")
        try:
            delta_seconds = (observed_at - scheduled).total_seconds()
        except TypeError as exc:
            raise ValueError("now and scheduled_for must use compatible timezone awareness") from exc

        lateness_seconds = max(0.0, delta_seconds)
        missed = delta_seconds > definition.misfire_grace_seconds
        run_metadata = RunMetadata(
            scheduled_for=scheduled,
            observed_at=observed_at,
            lateness_seconds=lateness_seconds,
            missed=missed,
            catch_up=missed,
            reason=(
                "missed_schedule_catch_up"
                if missed
                else "scheduled_ahead"
                if delta_seconds < 0
                else "within_schedule_window"
            ),
            details=dict(metadata or {}),
        )
        actual_run_id = run_id or uuid4().hex
        started_at = observed_at

        if dry_run:
            return TaskRunResult(
                task_id=definition.task_id,
                task_label=definition.label,
                run_id=actual_run_id,
                status=RunStatus.DRY_RUN,
                attempts=0,
                read_only=True,
                run_metadata=run_metadata,
                started_at=started_at,
                finished_at=started_at,
                value={"planned": True, "handler_injected": handler is not None},
            )

        if handler is None or not callable(handler):
            raise ValueError("a callable handler is required unless dry_run is true")

        timeout = definition.timeout_seconds if timeout_seconds is None else timeout_seconds
        retries = definition.max_retries if max_retries is None else max_retries
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        if retries < 0:
            raise ValueError("max_retries cannot be negative")

        lock = LocalInstanceLock(self.lock_path)
        if not lock.acquire():
            return TaskRunResult(
                task_id=definition.task_id,
                task_label=definition.label,
                run_id=actual_run_id,
                status=RunStatus.SKIPPED_LOCKED,
                attempts=0,
                read_only=True,
                run_metadata=run_metadata,
                started_at=started_at,
                finished_at=self._not_before(started_at),
                error="another local task instance holds the scheduler lock",
            )

        deferred_lock_release = False
        try:
            max_attempts = retries + 1
            for attempt in range(1, max_attempts + 1):
                context = TaskContext(
                    task_id=definition.task_id,
                    task_label=definition.label,
                    scheduled_for=scheduled,
                    run_id=actual_run_id,
                    attempt=attempt,
                    read_only=True,
                    write_enabled=definition.agent_write_enabled,
                    metadata=run_metadata.to_dict(),
                    stop_requested=(stop_requested if stop_requested is not None and not definition.auto_continue_on_timeout else threading.Event()),
                )
                last_progress = None
                while True:
                    state, timed_out, lock_deferred = self._invoke_with_timeout(
                        handler, context, timeout, lock,
                        cooperative_timeout=definition.cooperative_timeout,
                        external_stop=stop_requested if definition.auto_continue_on_timeout else None,
                    )
                    continuation = state.value.get("continuation") if isinstance(state.value, Mapping) else None
                    if not (definition.auto_continue_on_timeout and context.budget_expired.is_set()
                            and state.error is None and not _business_failure(state.value)
                            and _business_paused(state.value) and isinstance(continuation, Mapping)
                            and (stop_requested is None or not stop_requested.is_set())):
                        break
                    progress = continuation.get("progress")
                    if not progress or progress == last_progress:
                        state.value = {**state.value, "continuation_blocked": "no_progress"}
                        break  # Do not loop forever over a stalled checkpoint.
                    last_progress = progress
                    details = dict(context.metadata.get("details") or {})
                    details.update(mode="resume", resume_run_id=context.run_id,
                                   company_ids=[], source_record_ids=[])
                    context = replace(context, segment=context.segment + 1,
                                      metadata={**context.metadata, "details": details},
                                      stop_requested=threading.Event(), budget_expired=threading.Event())
                if isinstance(state.value, Mapping) and definition.auto_continue_on_timeout:
                    state.value = {key: value for key, value in state.value.items() if key != "continuation"}
                    state.value["execution_segments"] = context.segment
                if timed_out:
                    deferred_lock_release = lock_deferred
                    return TaskRunResult(
                        task_id=definition.task_id,
                        task_label=definition.label,
                        run_id=actual_run_id,
                        status=RunStatus.TIMED_OUT,
                        attempts=attempt,
                        read_only=True,
                        run_metadata=run_metadata,
                        started_at=started_at,
                        finished_at=self._not_before(started_at),
                        error=f"task exceeded timeout of {timeout:g} seconds",
                    )

                if state.error is None:
                    business_error = _business_failure(state.value)
                    paused = _business_paused(state.value) or context.stop_requested.is_set()
                    return TaskRunResult(
                        task_id=definition.task_id,
                        task_label=definition.label,
                        run_id=actual_run_id,
                        status=(RunStatus.FAILED if business_error else RunStatus.PAUSED if paused else RunStatus.SUCCESS),
                        attempts=attempt,
                        read_only=True,
                        run_metadata=run_metadata,
                        started_at=started_at,
                        finished_at=self._not_before(started_at),
                        value=state.value,
                        error=business_error,
                    )

                if context.stop_requested.is_set():
                    return TaskRunResult(
                        task_id=definition.task_id, task_label=definition.label,
                        run_id=actual_run_id, status=RunStatus.PAUSED, attempts=attempt,
                        read_only=True, run_metadata=run_metadata, started_at=started_at,
                        finished_at=self._not_before(started_at), error=_format_exception(state.error),
                    )

                if attempt < max_attempts and definition.retry_backoff_seconds:
                    self._sleeper(definition.retry_backoff_seconds)

                if attempt == max_attempts:
                    return TaskRunResult(
                        task_id=definition.task_id,
                        task_label=definition.label,
                        run_id=actual_run_id,
                        status=RunStatus.FAILED,
                        attempts=attempt,
                        read_only=True,
                        run_metadata=run_metadata,
                        started_at=started_at,
                        finished_at=self._not_before(started_at),
                        error=_format_exception(state.error),
                    )

            raise AssertionError("unreachable task execution state")
        finally:
            if not deferred_lock_release:
                lock.release()

    def run_task(self, *args: Any, **kwargs: Any) -> TaskRunResult:
        """Compatibility spelling for callers that prefer an explicit verb."""

        return self.run(*args, **kwargs)

    def run_all(
        self,
        handlers: Mapping[str, TaskCallable],
        *,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> tuple[TaskRunResult, ...]:
        """Run the fixed catalog in declaration order with one shared clock value."""

        observed_at = now or self._clock()
        return tuple(
            self.run(
                task_id,
                handlers.get(task_id),
                now=observed_at,
                dry_run=dry_run,
            )
            for task_id in self._tasks
        )

    def _invoke_with_timeout(
        self,
        handler: TaskCallable,
        context: TaskContext,
        timeout_seconds: float,
        lock: LocalInstanceLock,
        *,
        cooperative_timeout: bool = False,
        external_stop: threading.Event | None = None,
    ) -> tuple[_AttemptState, bool, bool]:
        state = _AttemptState()

        def invoke() -> None:
            try:
                state.value = handler(context)
            except BaseException as exc:  # Keep the worker responsible for releasing state.
                state.error = exc
            finally:
                with state.state_lock:
                    state.done.set()
                    release_lock = state.release_on_finish
                if release_lock:
                    lock.release()

        worker = threading.Thread(
            target=invoke,
            name=f"recruitops-task-{context.task_id}",
            daemon=True,
        )
        if external_stop is not None and external_stop.is_set():
            context.stop_requested.set()
        worker.start()
        deadline = time_module.monotonic() + timeout_seconds
        while external_stop is not None and not external_stop.is_set() and not state.done.is_set():
            remaining = deadline - time_module.monotonic()
            if remaining <= 0:
                break
            state.done.wait(min(0.05, remaining))
        if external_stop is not None and external_stop.is_set():
            context.stop_requested.set()
            state.done.wait()  # Cooperative daily worker owns lock until drained.
            return state, False, False
        if state.done.wait(max(0, deadline - time_module.monotonic())):
            return state, False, False

        context.budget_expired.set()
        context.stop_requested.set()
        if cooperative_timeout:
            # The result remains running until the handler has drained and
            # saved its in-flight work. Reporting a terminal timeout earlier
            # would expose an inconsistent checkpoint and release the lock.
            state.done.wait()
            return state, False, False

        with state.state_lock:
            state.release_on_finish = True
            completed_during_timeout_boundary = state.done.is_set()
        if completed_during_timeout_boundary:
            lock.release()
            return state, False, False
        return state, True, True

    def _not_before(self, started_at: datetime) -> datetime:
        finished_at = self._clock()
        try:
            return max(started_at, finished_at)
        except TypeError as exc:
            raise ValueError("scheduler clock returned incompatible timezone awareness") from exc


def _require_datetime(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")


def _format_exception(error: BaseException | None) -> str:
    if error is None:
        return "task failed without an exception"
    message = str(error).strip()
    return f"{type(error).__name__}: {message}" if message else type(error).__name__


def _business_failure(value: Any) -> str | None:
    """Returned failures are terminal receipts, not permission to replay writes."""
    if not isinstance(value, Mapping):
        return None
    daily_sync = value.get("daily_sync")
    receipts = [value]
    if isinstance(daily_sync, Mapping):
        receipts.append(daily_sync)
    for receipt in receipts:
        for key in ("status", "sync_status"):
            status = receipt.get(key)
            if isinstance(status, str) and status in {"failed", "failure", "configuration_required"}:
                return str(
                    receipt.get("error") or receipt.get("reason")
                    or receipt.get("message")
                    or (daily_sync.get("error") if isinstance(daily_sync, Mapping) else None)
                    or f"task returned {key}={status}"
                )
    return None


def _business_paused(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    daily_sync = value.get("daily_sync")
    return value.get("status") == "paused" or (
        isinstance(daily_sync, Mapping) and daily_sync.get("status") == "paused"
    )
