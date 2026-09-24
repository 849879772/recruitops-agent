"""Durable, explicitly started mail processing with a frozen single-run scope."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import isfinite
import os
from threading import Event, RLock, Thread
from time import monotonic, sleep
from uuid import uuid4

from sqlalchemy import select, text

from packages.storage.models import TaskRun, ToolCall
from .processing import DONE, _mail_scope, process_pending_mail


TASK_TYPE = "recruitment_mail_process"
CHECKPOINT_TOOL = "recruitment_mail_process_background"
ACTIVE = {"accepted", "running", "pausing", "cancelling"}
RECOVERABLE = {"paused", "cancelled", "interrupted", "stopped", "failed"}
_SCOPE_LOCK = RLock()
_LEASE_SECONDS = 180


def _now():
    return datetime.now(timezone.utc)


def _lock_scope(session):
    if session.bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(718202610)"))


def _boot_changed(values):
    current = os.environ.get("RECRUITOPS_DESKTOP_RUN_ID", "")
    previous = values.get("desktop_run_id") or ""
    return bool(previous and current and previous != current)


def _lease_active(values):
    lease = values.get("lease_until")
    return bool(not _boot_changed(values) and lease and datetime.fromisoformat(lease) > _now())


def _orphaned(run, values):
    if _boot_changed(values):
        return True
    if values.get("owner"):
        return not _lease_active(values)
    created = run.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return run.status in ACTIVE and (_now() - created).total_seconds() > _LEASE_SECONDS


def _checkpoint(session, run_id, *, lock=False):
    statement = select(ToolCall).where(ToolCall.task_id == run_id, ToolCall.tool_name == CHECKPOINT_TOOL)
    return session.scalar(statement.with_for_update() if lock else statement)


def _projection(run, checkpoint):
    values = checkpoint.arguments or {}
    progress = dict(values.get("progress", {}))
    status = "interrupted" if run.status in ACTIVE and _orphaned(run, values) else run.status
    progress.update(run_id=run.id, task_kind="recruitment_mail", thread_id=values.get("metadata", {}).get("thread_id"),
                    status=status, updated_at=run.updated_at.isoformat(), unit="封")
    progress.update(can_pause=status in {"accepted", "running"},
                    can_cancel=status in ACTIVE | RECOVERABLE,
                    can_resume=status in RECOVERABLE
                        and (not values.get("scope_frozen") or progress.get("remaining", 0) > 0))
    return progress


def project_mail_progress(storage, run_id=None, thread_id=None, *, include_recoverable=False):
    """Read only: never synchronize, create a task, or schedule a worker."""
    with storage.session() as session:
        statement = select(TaskRun).where(TaskRun.task_type == TASK_TYPE)
        if run_id:
            statement = statement.where(TaskRun.id == run_id)
        elif include_recoverable:
            statement = statement.where(TaskRun.status.in_(ACTIVE | RECOVERABLE))
        elif not thread_id:
            statement = statement.where(TaskRun.status.in_(ACTIVE))
        runs = []
        for run in session.scalars(statement.order_by(TaskRun.created_at.desc())):
            checkpoint = _checkpoint(session, run.id)
            if checkpoint is None:
                continue
            if thread_id and (checkpoint.arguments or {}).get("metadata", {}).get("thread_id") != thread_id:
                continue
            runs.append(_projection(run, checkpoint))
            if thread_id and not run_id and not include_recoverable:
                break
    return {"runs": runs, "run": runs[0] if len(runs) == 1 else None}


def wait_mail_progress(storage, run_id=None, thread_id=None, *, timeout_seconds=20):
    """Bounded, read-only waiting for one existing run; never claim or resume it.

    Callers can keep the assistant turn open using successive short waits instead
    of holding one tool connection for the entire mailbox. Once selected, the run
    is fixed so a later task cannot silently replace the result being awaited.
    """
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 20):
        raise ValueError("mail_wait_timeout_out_of_range")
    deadline = monotonic() + timeout_seconds
    result = project_mail_progress(storage, run_id, thread_id)
    selected = result["run"]
    if selected is None:
        return result
    selected_id = selected["run_id"]
    while result["run"] is not None and result["run"]["status"] in ACTIVE:
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(0.5, remaining))
        result = project_mail_progress(storage, selected_id, thread_id)
    return result


class MailProcessingRunService:
    def __init__(self, store, repository, settings, *, sync_mail=None, client=None):
        self.store, self.repository, self.settings = store, repository, settings
        self.storage = store.storage
        self.sync_mail, self.client = sync_mail, client
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recruitment-mail")
        self._lock = _SCOPE_LOCK
        self._futures = {}

    def status(self, run_id=None, thread_id=None):
        return project_mail_progress(self.storage, run_id, thread_id)

    def wait(self, run_id, *, timeout_seconds=20):
        """Wait for persisted progress without creating work or hiding worker errors."""
        future = self._futures.get(run_id)
        if future is not None and future.done():
            future.result()
        result = wait_mail_progress(self.storage, run_id, timeout_seconds=timeout_seconds)["run"]
        if future is not None and future.done():
            future.result()
        if result is None:
            raise KeyError("mail_run_not_found")
        return result

    def start(self, *, record_ids=None, thread_id=None, turn_id=None, refresh=True, background=True):
        if not self.settings.write_enabled:
            raise PermissionError("write_disabled")
        with self._lock:
            with self.storage.write_transaction() as session:
                _lock_scope(session)
                for old_run in session.scalars(select(TaskRun).where(
                        TaskRun.task_type == TASK_TYPE, TaskRun.status.in_(ACTIVE)).with_for_update()):
                    old_checkpoint = _checkpoint(session, old_run.id, lock=True)
                    if old_checkpoint is not None and _orphaned(old_run, old_checkpoint.arguments or {}):
                        old_run.status = "interrupted"
                session.flush()
                active = session.scalar(select(TaskRun).where(
                    TaskRun.task_type == TASK_TYPE, TaskRun.status.in_(ACTIVE)).with_for_update())
                if active is not None:
                    checkpoint = _checkpoint(session, active.id)
                    existing_thread = (checkpoint.arguments or {}).get("metadata", {}).get("thread_id") if checkpoint else None
                    if existing_thread != thread_id:
                        raise ValueError("another_mail_run_is_active")
                    return _projection(active, checkpoint)
                run_id = uuid4().hex
                metadata = {"task_kind": "recruitment_mail", "thread_id": thread_id, "turn_id": turn_id}
                payload = {"metadata": metadata, "desktop_run_id": os.environ.get("RECRUITOPS_DESKTOP_RUN_ID", ""),
                    "requested_ids": list(dict.fromkeys(record_ids)) if record_ids is not None else None,
                    "refresh": bool(refresh), "scope_frozen": False, "scope": [], "results": {}, "control": None,
                    "progress": {"phase": "preparing", "completed": 0, "processed": 0, "total": 0,
                                 "remaining": 0, "failed": 0, "blocked": 0, "unresolved": 0}}
                session.add(TaskRun(id=run_id, task_type=TASK_TYPE, status="accepted", user_request="处理招聘邮件",
                                    current_step="preparing", source="mail_processing_run", source_ref=run_id))
                session.flush()
                session.add(ToolCall(id="mail-checkpoint-" + run_id, task_id=run_id,
                    tool_name=CHECKPOINT_TOOL, arguments=payload, source="mail_processing_run", source_ref=run_id))
            if background:
                self._futures[run_id] = self._executor.submit(self.run, run_id)
        return self.status(run_id)["run"]

    def control(self, run_id, action, *, thread_id=None, turn_id=None, background=True):
        if action not in {"pause", "cancel", "resume"}:
            raise ValueError("invalid_mail_control")
        if not self.settings.write_enabled:
            raise PermissionError("write_disabled")
        with self._lock:
            with self.storage.write_transaction() as session:
                _lock_scope(session)
                run = session.scalar(select(TaskRun).where(TaskRun.id == run_id, TaskRun.task_type == TASK_TYPE).with_for_update())
                checkpoint = _checkpoint(session, run_id, lock=True)
                if run is None or checkpoint is None:
                    raise KeyError("mail_run_not_found")
                values = deepcopy(checkpoint.arguments)
                live = self._futures.get(run_id)
                live = live is not None and not live.done()
                lease_active = _lease_active(values)
                if action == "resume":
                    if live or (run.status in ACTIVE and lease_active):
                        return _projection(run, checkpoint)
                    if values.get("scope_frozen") and not values["progress"].get("remaining"):
                        raise ValueError("mail_run_scope_already_processed")
                    other = session.scalars(select(TaskRun).where(TaskRun.task_type == TASK_TYPE,
                        TaskRun.id != run_id, TaskRun.status.in_(ACTIVE)).with_for_update()).all()
                    for active in other:
                        active_checkpoint = _checkpoint(session, active.id, lock=True)
                        if active_checkpoint is not None and not _orphaned(active, active_checkpoint.arguments or {}):
                            raise ValueError("another_mail_run_is_active")
                        active.status = "interrupted"
                    values["control"] = None
                    values["desktop_run_id"] = os.environ.get("RECRUITOPS_DESKTOP_RUN_ID", "")
                    if thread_id is not None:
                        values["metadata"]["thread_id"] = thread_id
                    if turn_id is not None:
                        values["metadata"]["turn_id"] = turn_id
                    run.status = "accepted"
                    values.pop("owner", None)
                    values.pop("lease_until", None)
                else:
                    values["control"] = action
                    run.status = ("pausing" if action == "pause" else "cancelling") if live or lease_active else (
                        "paused" if action == "pause" else "cancelled")
                checkpoint.arguments = values
                run.updated_at = _now()
            if action == "resume" and background:
                self._futures[run_id] = self._executor.submit(self.run, run_id)
        return self.status(run_id)["run"]

    def _mutate(self, run_id, owner, change):
        with self.storage.write_transaction() as session:
            run = session.scalar(select(TaskRun).where(TaskRun.id == run_id).with_for_update())
            checkpoint = _checkpoint(session, run_id, lock=True)
            if run is None or checkpoint is None:
                raise KeyError("mail_run_not_found")
            values = deepcopy(checkpoint.arguments)
            if values.get("owner") not in {None, owner}:
                raise ValueError("mail_run_claim_lost")
            change(run, values)
            values["owner"] = owner
            values["lease_until"] = (_now() + timedelta(seconds=_LEASE_SECONDS)).isoformat() if run.status in ACTIVE else None
            values["progress"]["status"] = run.status
            values["progress"]["updated_at"] = _now().isoformat()
            checkpoint.arguments = values
            run.updated_at = _now()
            return values

    def _stopping(self, run_id, owner=None):
        with self.storage.session() as session:
            checkpoint = _checkpoint(session, run_id)
            values = checkpoint.arguments or {} if checkpoint else {}
            return checkpoint is None or bool(values.get("control")) or (owner is not None and values.get("owner") != owner)

    def _heartbeat(self, run_id, owner, stopped):
        while not stopped.wait(30):
            try:
                self._mutate(run_id, owner, lambda run, values: None)
            except Exception:
                return

    def _record_result(self, run_id, owner, event):
        def update(run, values):
            progress = values["progress"]
            progress["phase"] = run.current_step = event.get("phase", "analysis")
            result = event.get("result")
            if result:
                values["results"][result["record_id"]] = result
            results = list(values["results"].values())
            completed = len(results)
            failed = sum(item.get("state") in {"failed", "failed_terminal"} for item in results)
            blocked = sum(item.get("state") not in DONE and item.get("state") not in {"failed", "failed_terminal"} for item in results)
            progress.update(completed=completed, processed=completed, total=len(values["scope"]),
                            remaining=max(0, len(values["scope"]) - completed), failed=failed,
                            blocked=blocked, unresolved=blocked)
            run.step_count = completed
        return self._mutate(run_id, owner, update)

    def run(self, run_id):
        owner = uuid4().hex
        # Explicit runner entry only. Status projections never invoke this method.
        with self._lock, self.storage.write_transaction() as session:
            _lock_scope(session)
            run = session.scalar(select(TaskRun).where(TaskRun.id == run_id, TaskRun.task_type == TASK_TYPE).with_for_update())
            checkpoint = _checkpoint(session, run_id, lock=True)
            if run is None or checkpoint is None:
                raise KeyError("mail_run_not_found")
            values = deepcopy(checkpoint.arguments)
            if values.get("control") and run.status in ACTIVE and not values.get("owner"):
                run.status = "cancelled" if values["control"] == "cancel" else "paused"
                values["lease_until"] = None
                checkpoint.arguments = values
                return
            if (run.status not in ACTIVE or values.get("control")
                    or (values.get("owner") and _lease_active(values))):
                return
            values.update(owner=owner, desktop_run_id=os.environ.get("RECRUITOPS_DESKTOP_RUN_ID", ""),
                          lease_until=(_now() + timedelta(seconds=_LEASE_SECONDS)).isoformat())
            checkpoint.arguments = values
            run.status = "running"
        heartbeat_stop = Event()
        heartbeat = Thread(target=self._heartbeat, args=(run_id, owner, heartbeat_stop), daemon=True)
        heartbeat.start()
        try:
            if not values["scope_frozen"]:
                if values["refresh"] and self.sync_mail:
                    self._record_result(run_id, owner, {"phase": "sync"})
                    self.sync_mail()
                records = _mail_scope(self.store, values["requested_ids"])
                if values["requested_ids"] is not None and len(records) != len(values["requested_ids"]):
                    raise ValueError("requested_mail_missing")
                scope = [{"record_id": record.id, "content_digest": record.content_digest}
                         for record in records if record.processing_status not in DONE]
                def freeze(run, state):
                    state.update(scope=scope, scope_frozen=True)
                    state["progress"].update(total=len(scope), remaining=len(scope), phase="analysis")
                    run.current_step = "analysis"
                values = self._mutate(run_id, owner, freeze)
            while True:
                if self._stopping(run_id, owner):
                    def stopped(run, state):
                        run.status = "cancelled" if state.get("control") == "cancel" else "paused"
                    self._mutate(run_id, owner, stopped)
                    break
                with self.storage.session() as session:
                    values = deepcopy(_checkpoint(session, run_id).arguments)
                pending = [item for item in values["scope"] if item["record_id"] not in values["results"]]
                if not pending:
                    def finished(run, state):
                        run.status = "partial" if state["progress"]["failed"] or state["progress"]["blocked"] else "completed"
                        run.current_step = state["progress"]["phase"] = "completed"
                    self._mutate(run_id, owner, finished)
                    break
                wave = pending[:10]
                valid = []
                for item in wave:
                    record = self.store.get(record_id=item["record_id"])
                    if record is None or record.content_digest != item["content_digest"]:
                        self._record_result(run_id, owner, {"result": {"record_id": item["record_id"], "state": "source_changed", "reason": "mail_changed_since_run_start"}})
                    else:
                        valid.append(record.id)
                if not valid:
                    continue
                outcome = process_pending_mail(self.store, self.repository, self.settings,
                    limit=10, record_ids=valid, client=self.client,
                    expected_digests={item["record_id"]: item["content_digest"] for item in wave},
                    progress=lambda event: self._record_result(run_id, owner, event),
                    should_stop=lambda: self._stopping(run_id, owner))
                if outcome.get("status") == "blocked":
                    raise PermissionError(outcome.get("reason", "mail_processing_blocked"))
                if not outcome.get("processed") and not self._stopping(run_id):
                    # Terminal records or records completed by another caller are
                    # checkpointed without reissuing their model calls.
                    waiting = False
                    for identifier in valid:
                        record = self.store.get(record_id=identifier)
                        if record and record.processing_status not in {"pending", "linked"}:
                            self._record_result(run_id, owner, {"result": {"record_id": identifier, "state": record.processing_status,
                                "reason": record.processing_error}})
                        else:
                            waiting = True
                    if waiting:
                        self._mutate(run_id, owner, lambda run, state: setattr(run, "status", "interrupted"))
                        break
        except Exception as exc:
            def failed(run, state):
                run.status = "failed"
                run.error_code = type(exc).__name__
                state["progress"]["phase"] = "failed"
            self._mutate(run_id, owner, failed)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1)
        return self.status(run_id)["run"]

    def close(self):
        for run_id, future in list(self._futures.items()):
            if not future.done():
                try:
                    self.control(run_id, "pause")
                except (KeyError, ValueError, PermissionError):
                    pass
        self._executor.shutdown(wait=False)
