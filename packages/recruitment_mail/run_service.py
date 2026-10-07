"""Durable, explicitly started mail processing with a frozen single-run scope."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from math import isfinite
import os
from threading import Event, RLock, Thread
from time import monotonic, sleep
from uuid import uuid4

from sqlalchemy import func, or_, select, text

from packages.storage.models import TaskRun, ToolCall
from packages.storage.task_identity import task_identity
from .processing import DONE, _mail_scope, historical_processing_result, process_pending_mail
from .storage import RecruitmentMailRecord
from .binding import BINDING_KEY, confirmed_binding_matches, bound_application_ids
from packages.storage import ApplicationSnapshot


TASK_TYPE = "recruitment_mail_process"
CHECKPOINT_TOOL = "recruitment_mail_process_background"
ACTIVE = {"accepted", "running", "pausing", "cancelling"}
WAITING = {"awaiting_confirmation"}
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


def _confirmation_items(values):
    decisions = values.get("confirmations") or {}
    scope = {item["record_id"]: item["content_digest"] for item in values.get("scope", [])}
    return [{"record_id": identifier, "content_digest": scope[identifier]}
            for identifier, item in (values.get("results") or {}).items()
            if identifier in scope and identifier not in decisions and (
                item.get("state") == "ambiguous_application" or (
                    item.get("state") == "pending_association" and item.get("reason") in {
                        "multiple_candidates", "no_verified_match", "binding_required"}))]


def _reserved_confirmation_ids(session):
    result = set()
    for run_id in session.scalars(select(TaskRun.id).where(
            TaskRun.task_type == TASK_TYPE, TaskRun.status.in_(WAITING))):
        checkpoint = _checkpoint(session, run_id)
        if checkpoint:
            result.update(item["record_id"] for item in _confirmation_items(checkpoint.arguments or {}))
    return result


def _refresh_counts(values):
    results = list(values.get("results", {}).values())
    failed = sum(item.get("state") in {"failed", "failed_terminal"} for item in results)
    blocked = sum(item.get("state") not in DONE | {"user_rejected", "failed", "failed_terminal"} for item in results)
    values["progress"].update(completed=len(results), processed=len(results), total=len(values["scope"]),
        remaining=max(0, len(values["scope"]) - len(results)), failed=failed, blocked=blocked,
        unresolved=blocked, rejected=sum(item.get("state") == "user_rejected" for item in results))


def _finish_run(run, values):
    _refresh_counts(values)
    if values["progress"]["remaining"]:
        # A confirmation may enqueue its record while the worker is finishing.
        run.status = "running"
        return
    if _confirmation_items(values) and values.get("metadata", {}).get("thread_id"):
        run.status = "awaiting_confirmation"
    else:
        progress = values["progress"]
        run.status = "partial" if progress["failed"] or progress["blocked"] or progress.get("sync_warning") else "completed"
    run.current_step = values["progress"]["phase"] = run.status
    values["lease_until"] = None


def _report_pending(run, values):
    version = int(values.get("confirmation_version", 0))
    return bool(version and run.status in {"completed", "partial", "failed"}
                and int(values.get("reported_version", 0)) < version)


def mail_confirmation_queue(storage, thread_id):
    """Only the explicitly selected conversation; no guessing or work on reads."""
    if not thread_id:
        raise ValueError("mail_confirmation_thread_required")
    results = []
    with storage.session() as session:
        statement = select(TaskRun, ToolCall).join(ToolCall, ToolCall.task_id == TaskRun.id).where(
            TaskRun.task_type == TASK_TYPE, ToolCall.tool_name == CHECKPOINT_TOOL,
            ToolCall.arguments["metadata"]["thread_id"].as_string() == thread_id,
            or_(TaskRun.status.in_(ACTIVE | WAITING),
                ToolCall.arguments["confirmation_version"].as_integer()
                > func.coalesce(ToolCall.arguments["reported_version"].as_integer(), 0)),
        ).order_by(TaskRun.updated_at.desc())
        for run, checkpoint in session.execute(statement):
            values = checkpoint.arguments or {}
            if run.status in {"cancelled", "cancelling", "paused", "pausing"}:
                continue
            items = _confirmation_items(values)
            report_pending = _report_pending(run, values)
            if not items and not report_pending and not (run.status in ACTIVE and values.get("confirmations")):
                continue
            visible = []
            for item in items:
                record = session.get(RecruitmentMailRecord, item["record_id"])
                visible.append({**item, "subject": record.subject if record else "邮件已不存在",
                    "source_changed": record is None or record.content_digest != item["content_digest"]})
            results.append({**task_identity(run.id, values.get("metadata"), task_id=TASK_TYPE), "status": run.status,
                "confirmation_version": int(values.get("confirmation_version", 0)),
                "report_pending": report_pending, "items": visible})
    return {"runs": results}


def _projection(run, checkpoint):
    values = checkpoint.arguments or {}
    progress = dict(values.get("progress", {}))
    status = "interrupted" if run.status in ACTIVE and _orphaned(run, values) else run.status
    progress.update(task_identity(run.id, values.get("metadata"), task_id=TASK_TYPE))
    progress.update(task_kind="recruitment_mail", status=status, updated_at=run.updated_at.isoformat(), unit="封")
    progress.update(can_pause=status in {"accepted", "running"},
                    can_cancel=status in ACTIVE | RECOVERABLE | WAITING,
                    can_resume=status in RECOVERABLE
                        and (not values.get("scope_frozen") or progress.get("remaining", 0) > 0))
    failures = [item for item in (values.get("results") or {}).values()
                if item.get("state") in {"failed", "failed_terminal"}]
    # A completed wait must carry safe diagnostics, not merely a generic failure count.
    progress["failure_results"] = [{key: item.get(key) for key in (
        "record_id", "state", "reason", "diagnostic", "analysis_source", "model_attempted",
        "retryable", "next_retry_at", "last_analysis_at", "application_ids", "application_results")} for item in failures[:20]]
    progress["failure_results_truncated"] = len(failures) > 20
    progress["confirmation_required"] = status == "awaiting_confirmation"
    progress["confirmation_count"] = len(_confirmation_items(values))
    progress["confirmation_version"] = int(values.get("confirmation_version", 0))
    results = values.get("results") or {}
    progress["result_counts"] = dict(Counter(str(item.get("state") or "unknown") for item in results.values()))
    schedules = {}
    for item in [*results.values(), *(values.get("confirmation_prior_schedules") or {}).values()]:
        schedule = item.get("schedule_item")
        if isinstance(schedule, dict) and schedule.get("id"):
            previous = schedules.get(schedule["id"], {})
            schedules[schedule["id"]] = {**schedule, "created": bool(previous.get("created") or schedule.get("created"))}
    progress["schedule_items_created"] = sum(bool(item.get("created")) for item in schedules.values())
    progress["schedule_items_reused"] = sum(not item.get("created") for item in schedules.values())
    progress["schedule_items_time_unconfirmed"] = sum(item.get("time_kind") == "unspecified" for item in schedules.values())
    decisions = values.get("confirmations") or {}
    progress["confirmation_results"] = [{"record_id": record_id,
        **{key: decision.get(key) for key in ("subject", "company_name", "job_title", "action", "application_ids", "applications")},
        "state": results.get(record_id, {}).get("state", "processing"),
        "reason": results.get(record_id, {}).get("reason"),
        "schedule_item": results.get(record_id, {}).get("schedule_item"),
        "application_results": results.get(record_id, {}).get("application_results", []),
    } for record_id, decision in list(decisions.items())[:20]]
    progress["confirmation_results_truncated"] = len(decisions) > 20
    return progress


def project_mail_progress(storage, run_id=None, thread_id=None, *, include_recoverable=False):
    """Read only: never synchronize, create a task, or schedule a worker."""
    with storage.session() as session:
        statement = select(TaskRun).where(TaskRun.task_type == TASK_TYPE)
        if run_id:
            statement = statement.where(TaskRun.id == run_id)
        elif include_recoverable:
            statement = statement.where(TaskRun.status.in_(ACTIVE | RECOVERABLE | WAITING))
        elif not thread_id:
            statement = statement.where(TaskRun.status.in_(ACTIVE | WAITING))
        runs = []
        for run in session.scalars(statement.order_by(TaskRun.created_at.desc())):
            checkpoint = _checkpoint(session, run.id)
            if checkpoint is None:
                continue
            if thread_id and task_identity(run.id, (checkpoint.arguments or {}).get("metadata"), task_id=TASK_TYPE)["thread_id"] != thread_id:
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

    def start(self, *, record_ids=None, thread_id=None, turn_id=None, refresh=True, background=True, retry_failed=False):
        if not self.settings.write_enabled:
            raise PermissionError("write_disabled")
        if retry_failed and (not record_ids or len(record_ids) > 50):
            raise ValueError("mail_retry_requires_explicit_records")
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
                    if retry_failed or existing_thread != thread_id:
                        raise ValueError("another_mail_run_is_active")
                    return _projection(active, checkpoint)
                if record_ids and set(record_ids) & _reserved_confirmation_ids(session):
                    raise ValueError("mail_scope_waiting_for_confirmation")
                retry_scope = []
                if retry_failed:
                    identifiers = list(dict.fromkeys(record_ids))
                    records = list(session.scalars(select(RecruitmentMailRecord).where(
                        RecruitmentMailRecord.id.in_(identifiers)).with_for_update()))
                    if len(records) != len(identifiers):
                        raise KeyError("requested_mail_missing")
                    if any(record.processing_status not in {"failed", "failed_terminal"}
                           or (record.raw_metadata or {}).get("model_processing", {}).get("state") == "running"
                           for record in records):
                        raise ValueError("mail_retry_requires_failed_records")
                    retry_scope = [{"record_id": record.id, "content_digest": record.content_digest}
                                   for record in records]
                run_id = uuid4().hex
                metadata = {"task_kind": "recruitment_mail", "thread_id": thread_id, "turn_id": turn_id}
                payload = {"metadata": metadata, "desktop_run_id": os.environ.get("RECRUITOPS_DESKTOP_RUN_ID", ""),
                    "requested_ids": list(dict.fromkeys(record_ids)) if record_ids is not None else None,
                    "refresh": bool(refresh) and not retry_failed, "retry_failed": bool(retry_failed),
                    "scope_frozen": bool(retry_failed), "scope": retry_scope, "results": {}, "control": None,
                    "progress": {"phase": "preparing", "completed": 0, "processed": 0, "total": len(retry_scope),
                                 "remaining": len(retry_scope), "failed": 0, "blocked": 0, "unresolved": 0,
                                 "retry_failed": bool(retry_failed),
                                 "model_attempted_count": 0, "model_call_count": 0,
                                 "historical_failure_count": 0, "history_reused_count": 0,
                                 "retry_pending_count": 0,
                                 "freshness": {"status": "not_requested"}}}
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
                if run.status in WAITING and thread_id and values.get("metadata", {}).get("thread_id") != thread_id:
                    raise ValueError("mail_confirmation_thread_mismatch")
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
                        if thread_id != values["metadata"].get("thread_id"):
                            values["metadata"]["turn_id"] = turn_id
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

    def resolve_confirmation(self, run_id, *, thread_id, record_id, action, content_digest, background=True):
        """Resume only an already-authorized frozen record after a persisted human binding."""
        if not self.settings.write_enabled:
            raise PermissionError("write_disabled")
        if action not in {"confirmed", "rejected"}:
            raise ValueError("invalid_mail_confirmation_action")
        submit = False
        with self._lock, self.storage.write_transaction() as session:
            _lock_scope(session)
            run = session.scalar(select(TaskRun).where(TaskRun.id == run_id, TaskRun.task_type == TASK_TYPE).with_for_update())
            checkpoint = _checkpoint(session, run_id, lock=True)
            if run is None or checkpoint is None:
                raise KeyError("mail_run_not_found")
            values = deepcopy(checkpoint.arguments)
            if not thread_id or values.get("metadata", {}).get("thread_id") != thread_id:
                raise ValueError("mail_confirmation_thread_mismatch")
            if run.status in {"cancelled", "cancelling", "paused", "pausing", "interrupted", "stopped"} or values.get("control"):
                raise ValueError("mail_run_not_accepting_confirmation")
            expected = next((item["content_digest"] for item in values.get("scope", []) if item["record_id"] == record_id), None)
            if not expected or expected != content_digest:
                raise ValueError("mail_confirmation_scope_mismatch")
            record = session.get(RecruitmentMailRecord, record_id)
            if record is None or record.content_digest != expected:
                raise ValueError("mail_changed_since_run_start")
            decision = (values.get("confirmations") or {}).get(record_id)
            if decision:
                if decision["action"] != action:
                    raise ValueError("mail_confirmation_already_resolved")
                return _projection(run, checkpoint)
            if not any(item["record_id"] == record_id for item in _confirmation_items(values)):
                raise ValueError("mail_confirmation_not_pending")
            applications = []
            if action == "confirmed":
                applications = [session.get(ApplicationSnapshot, identifier) for identifier in bound_application_ids(record)]
                if not applications or any(application is None or confirmed_binding_matches(record, application) is not True
                                           for application in applications):
                    raise ValueError("mail_human_binding_required")
                application = applications[0]
                # Binding may already have succeeded; an occupied different worker
                # leaves this receipt pending so the user can retry resolution.
                other = session.scalar(select(TaskRun).where(TaskRun.task_type == TASK_TYPE,
                    TaskRun.id != run_id, TaskRun.status.in_(ACTIVE)).with_for_update())
                if other is not None:
                    raise ValueError("another_mail_run_is_active")
                old_result = values["results"].pop(record_id)
                if old_result.get("schedule_item"):
                    values.setdefault("confirmation_prior_schedules", {})[record_id] = {"schedule_item": old_result["schedule_item"]}
                values["resume_confirmed_ids"] = list(dict.fromkeys([*values.get("resume_confirmed_ids", []), record_id]))
                if run.status not in ACTIVE:
                    run.status = "accepted"
                    values.pop("owner", None)
                    values.pop("lease_until", None)
                    submit = True
            else:
                values["results"][record_id] = {"record_id": record_id, "state": "user_rejected", "reason": "mail_binding_rejected"}
                # Remember this choice for the same evidence version only. It is
                # not deletion or a stage write; explicit selection may revisit it.
                metadata = dict(record.raw_metadata or {})
                metadata["confirmation_dismissal"] = {"content_digest": content_digest,
                    "run_id": run_id, "at": _now().isoformat()}
                record.raw_metadata = metadata
            values.setdefault("confirmations", {})[record_id] = {"action": action, "content_digest": content_digest,
                "subject": str(record.subject or "")[:200],
                "company_name": application.company_name if action == "confirmed" else None,
                "job_title": application.job_title if action == "confirmed" else None,
                "application_ids": [app.id for app in applications],
                "applications": [{"application_id": app.id, "company_name": app.company_name,
                                  "job_title": app.job_title, "stage": app.stage} for app in applications],
                "binding_key": (record.raw_metadata or {}).get(BINDING_KEY, {}).get("approval_key") if action == "confirmed" else None,
                "at": _now().isoformat()}
            values["confirmation_version"] = int(values.get("confirmation_version", 0)) + 1
            values.pop("report_claim", None)
            _refresh_counts(values)
            if run.status not in ACTIVE:
                _finish_run(run, values)
            values["progress"]["status"] = run.status
            checkpoint.arguments = values
            run.updated_at = _now()
        if submit and background:
            self._futures[run_id] = self._executor.submit(self.run, run_id)
        return self.status(run_id)["run"]

    def report_confirmation(self, run_id, *, thread_id, version, action, claim_token=None):
        """Lease only the read-only summary notification; never execute mail here."""
        if not self.settings.write_enabled:
            raise PermissionError("write_disabled")
        if action not in {"claim", "complete", "release"}:
            raise ValueError("invalid_mail_report_action")
        with self._lock, self.storage.write_transaction() as session:
            _lock_scope(session)
            run = session.scalar(select(TaskRun).where(TaskRun.id == run_id, TaskRun.task_type == TASK_TYPE).with_for_update())
            checkpoint = _checkpoint(session, run_id, lock=True)
            if run is None or checkpoint is None:
                raise KeyError("mail_run_not_found")
            values = deepcopy(checkpoint.arguments)
            if not thread_id or values.get("metadata", {}).get("thread_id") != thread_id:
                raise ValueError("mail_confirmation_thread_mismatch")
            if version != int(values.get("confirmation_version", 0)):
                raise ValueError("mail_report_version_changed")
            if action == "complete" and values.get("reported_version") == version and values.get("reported_token") == claim_token:
                return {"completed": True, "version": version}
            if not _report_pending(run, values):
                return {"claimed": False, "report_pending": False, "version": version}
            claim = values.get("report_claim") or {}
            if action == "claim":
                if claim.get("until") and datetime.fromisoformat(claim["until"]) > _now():
                    return {"claimed": False, "report_pending": True, "version": version}
                token = uuid4().hex
                values["report_claim"] = {"token": token, "until": (_now() + timedelta(minutes=5)).isoformat()}
                result = {"claimed": True, "claim_token": token, "version": version, "run": _projection(run, checkpoint)}
            else:
                if not claim_token or claim.get("token") != claim_token:
                    raise ValueError("mail_report_claim_mismatch")
                values.pop("report_claim", None)
                if action == "complete":
                    values.update(reported_version=version, reported_token=claim_token)
                result = {"completed": action == "complete", "version": version}
            checkpoint.arguments = values
        return result

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
            if event.get("model_call"):
                identifiers = set(values.get("model_attempted_ids", []))
                identifiers.update(event.get("record_ids", []))
                values["model_attempted_ids"] = sorted(identifiers)
                progress["model_attempted_count"] = len(identifiers)
                progress["model_call_count"] = progress.get("model_call_count", 0) + 1
            result = event.get("result")
            if result:
                values["results"][result["record_id"]] = result
            results = list(values["results"].values())
            completed = len(results)
            failed = sum(item.get("state") in {"failed", "failed_terminal"} for item in results)
            blocked = sum(item.get("state") not in DONE and item.get("state") not in {"failed", "failed_terminal", "user_rejected"} for item in results)
            progress.update(completed=completed, processed=completed, total=len(values["scope"]),
                            remaining=max(0, len(values["scope"]) - completed), failed=failed,
                            blocked=blocked, unresolved=blocked,
                            history_reused_count=sum(item.get("analysis_source") == "history" for item in results),
                            historical_failure_count=sum(item.get("analysis_source") == "history"
                                and item.get("state") in {"failed", "failed_terminal"} for item in results),
                            retry_pending_count=sum(item.get("retryable") is True for item in results))
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
                    try:
                        freshness = self.sync_mail()
                    except Exception as exc:
                        # Never persist exception text containing credentials or mail bodies.
                        freshness = {"status": "failed", "error_type": type(exc).__name__}
                    if not isinstance(freshness, dict) or freshness.get("status") not in {
                        "synced", "cached", "failed", "disabled", "syncing"
                    }:
                        freshness = {"status": "failed", "error_type": "mail_sync_result_invalid"}
                    def record_sync(run, state):
                        state["progress"]["freshness"] = {
                            key: freshness.get(key) for key in ("status", "synced_at", "error_type")
                        }
                        state["progress"]["sync_warning"] = freshness["status"] not in {"synced", "cached"}
                    self._mutate(run_id, owner, record_sync)
                records = _mail_scope(self.store, values["requested_ids"])
                if values["requested_ids"] is not None and len(records) != len(values["requested_ids"]):
                    raise ValueError("requested_mail_missing")
                with self.storage.session() as session:
                    reserved = _reserved_confirmation_ids(session)
                scope = [{"record_id": record.id, "content_digest": record.content_digest}
                         for record in records if record.processing_status not in DONE and record.id not in reserved
                         and (values["requested_ids"] is not None
                              or (record.raw_metadata or {}).get("confirmation_dismissal", {}).get("content_digest") != record.content_digest
                              or (record.raw_metadata or {}).get(BINDING_KEY, {}).get("state") == "bound")]
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
                        _finish_run(run, state)
                    settled = self._mutate(run_id, owner, finished)
                    if not settled["progress"]["remaining"]:
                        break
                    continue
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
                    should_stop=lambda: self._stopping(run_id, owner),
                    **({"retry_request_id": run_id} if values.get("retry_failed") else {}))
                if outcome.get("status") == "blocked":
                    raise PermissionError(outcome.get("reason", "mail_processing_blocked"))
                if not outcome.get("processed") and not self._stopping(run_id):
                    # Terminal records or records completed by another caller are
                    # checkpointed without reissuing their model calls.
                    waiting = False
                    for identifier in valid:
                        record = self.store.get(record_id=identifier)
                        if record and record.processing_status not in {"pending", "linked"}:
                            self._record_result(run_id, owner, {"result": historical_processing_result(record)})
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
