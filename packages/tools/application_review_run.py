"""Checkpointed application reviews within the MCP transport time budget."""

from __future__ import annotations

import asyncio
from collections import defaultdict
import os
from time import perf_counter, time
from uuid import uuid4

from sqlalchemy import select

from packages.domain.urls import normalize_http_page_url
from packages.storage.models import TaskRun, ToolCall, utc_now
from .application_review_tasks import REVIEW_CONTEXT, lease_active, lock_review_scope, review_summary
from .batch_browser_operations import (
    ApplicationStatusResult, BatchObserveApplicationStatusInput,
    BatchObserveApplicationStatusResponse, _error_response, _origin_concurrency_key,
    _storage, _value, batch_observe_application_status,
)
from .typed import EvidenceSource, ToolErrorCode, ToolStatus

_STATE_TOOL = "application_review_checkpoint"
_STATES = ("updated", "unchanged", "excluded", "blocked", "unresolved", "failed")
# Bound each call independently of batch size; unfinished pages resume next call.
_WAVE_PAGES = 10
_WAVE_TIMEOUT_SECONDS = 105
_LEASE_SECONDS = 115
_CONTINUATION_SECONDS = 120
_WAVE_MAX_CONCURRENCY = 4
_MAX_APPLICATION_ATTEMPTS = 3
_TRANSIENT_FAILURE_MARKERS = (
    "busy", "timeout", "timed out", "temporar", "operation failed", "connection reset",
    "connection refused", "connection lost", "transport closed", "transport error",
    "bridge disconnected", "bridge unavailable", "service unavailable", "queue full",
    "capacity", "review batch exception", "review wave cancelled",
    "review batch connection", "review batch incomplete", "review wave timeout",
    "review wave interrupted",
)


def _is_transient_failure(result):
    if not isinstance(result, dict) or result.get("state") != "failed":
        return False
    reason = str(result.get("reason") or "").casefold().replace("_", " ").replace("-", " ")
    return any(marker in reason for marker in _TRANSIENT_FAILURE_MARKERS)


def _batch_exception_reason(error):
    message = f"{type(error).__name__}: {error}".casefold().replace("_", " ").replace("-", " ")
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)) or "timeout" in message or "timed out" in message:
        return "review_batch_timeout"
    if isinstance(error, ConnectionError) or any(
        marker in message for marker in ("connection reset", "connection refused", "connection lost")
    ):
        return "review_batch_connection"
    if any(marker in message for marker in ("busy", "queue full", "capacity")):
        return "review_batch_busy"
    return "review_batch_exception"


def _attempt_count(state, application_id):
    try:
        return max(0, int((state.get("attempts") or {}).get(application_id, 0)))
    except (TypeError, ValueError):
        return 0


def _retryable_ids(state):
    results = state.get("results") or {}
    return {
        application_id
        for application_id in state.get("ids", [])
        if _is_transient_failure(results.get(application_id))
        and _attempt_count(state, application_id) < _MAX_APPLICATION_ATTEMPTS
    }


def _pending_ids(state):
    results = state.get("results") or {}
    retryable = _retryable_ids(state)
    return [
        application_id for application_id in state.get("ids", [])
        if application_id not in results or application_id in retryable
    ]


def _update_task_progress(task, state):
    # One review is one task operation. Application counts belong to the
    # checkpoint, not the scheduler's 1..50 step budget (including empty scopes).
    total = len(state.get("ids", []))
    remaining = len(_pending_ids(state))
    task.max_steps = 1
    task.step_count = int(remaining == 0)
    task.current_step = f"{total - remaining}/{total}"


def _response(run_id, state, started, *, busy=False):
    ids = list(dict.fromkeys(str(item) for item in state.get("ids", [])))
    scope_ids = set(ids)
    saved_results = {
        str(application_id): row
        for application_id, row in (state.get("results") or {}).items()
        if str(application_id) in scope_ids and isinstance(row, dict)
    }
    rows = [
        ApplicationStatusResult.model_validate({**saved_results[item], "application_id": item})
        for item in ids if item in saved_results
    ]
    buckets = {key: [row for row in rows if row.state == key] for key in _STATES}
    total = len(ids)
    retryable = _retryable_ids({**state, "ids": ids, "results": saved_results})
    remaining = len(_pending_ids({**state, "ids": ids, "results": saved_results}))
    verified = len(buckets["updated"]) + len(buckets["unchanged"])
    settled = verified + len(buckets["excluded"])
    retry_exhausted = sum(
        1 for item in ids
        if _is_transient_failure(saved_results.get(item))
        and _attempt_count(state, item) >= _MAX_APPLICATION_ATTEMPTS
    )
    completed_count = total - remaining
    run_status = state.get("run_status") or (("stopped" if state.get("wave_error") else "awaiting_continuation") if remaining else "completed")
    continuation_required = bool(
        remaining and not busy and run_status == "awaiting_continuation"
        and not state.get("control_request")
        and state.get("wave_error") in {None, "review_wave_timeout"}
    )
    if not remaining:
        next_action = "范围处理完毕不代表核验成功；按更新、登录、证据不足和失败分类报告。"
    elif state.get("control_request") == "cancel" or run_status in {"cancelled", "cancelling"}:
        next_action = "任务已取消或正在安全结束当前页面；不得续跑这轮任务，若用户需要重新执行须明确新建。"
    elif state.get("control_request") == "pause" or run_status in {"paused", "pausing"}:
        next_action = "用户已请求暂停；等待在途页面保存后停止，不得自动续跑，只有用户明确恢复才调用恢复操作。"
    elif busy:
        next_action = "复核正在执行，本次不是完成结果；不得重复新建或抢占，等待当前调用返回真实结果。"
    elif continuation_required:
        next_action = (
            "保持当前助理回合，用同一个 run_id 继续调用本工具，直到范围结束；不要只返回已启动，也不需要再次询问用户。"
            "进度使用 completed_count/total；processed_count 包含仍在 remaining_count 中的可重试记录，不得将二者相加。"
        )
    else:
        next_action = "任务意外中断；如实说明已保存进度和中断原因，不得宣称安全暂停或完成，需显式恢复才可继续。"
    summary = {
        "run_id": run_id,
        "run_status": run_status,
        "selection": "all_non_terminal", "scope_total": total, "total": total,
        "database_total": state["database_total"],
        "excluded_terminal": state["excluded_terminal"],
        "unique_record_count": total,
        "processed_count": len(rows), "completed_count": completed_count,
        "remaining_count": remaining, "retryable_count": len(retryable),
        "pending_unattempted_count": total - len(rows),
        "continuation_required": continuation_required,
        "background": False,
        "retry_exhausted_count": retry_exhausted,
        "scope_complete": remaining == 0, "in_progress": busy,
        "control_request": state.get("control_request"),
        "wave_error": state.get("wave_error"),
        "interruption_reason": state.get("interruption_reason"),
        "pages_total": state["pages_total"],
        "write_count": sum(row.wrote for row in rows),
        "completion_rate": round(completed_count / total, 4) if total else 1.0,
        "verification_success_count": verified,
        "verification_rate": round(verified / total, 4) if total else 1.0,
        **{key: len(value) for key, value in buckets.items()},
        "next_action": next_action,
    }
    status = (
        ToolStatus.SUCCESS
        if not remaining and settled == total and not state.get("wave_error")
        else ToolStatus.AMBIGUOUS
    )
    return BatchObserveApplicationStatusResponse(
        tool_name="batch_observe_application_status", status=status,
        success=status == ToolStatus.SUCCESS, total=total, pages_total=state["pages_total"],
        data=summary, summary=summary, **buckets,
        error_code=ToolErrorCode.AMBIGUOUS_MATCH if status != ToolStatus.SUCCESS else None,
        error_message=next_action if status != ToolStatus.SUCCESS else None,
        evidence=[EvidenceSource(source="agent.application_snapshot"),
                  EvidenceSource(source="agent.application_review_checkpoint", source_ref=run_id)],
        timeout_ms=110_000, elapsed_ms=int((perf_counter() - started) * 1000),
    )


async def continue_application_review(request, bridge, repository, *, resume_control=False):
    # Old clients may still send background=True. Keep the input compatible,
    # but never detach browser work from the assistant call that reports it.
    return await _continue_review_wave(request, bridge, repository, resume_control=resume_control)


async def _continue_review_wave(request, bridge, repository, *, resume_control=False):
    started = perf_counter()
    if bridge is None:
        return _error_response(request, started=started, reason="Browser bridge unavailable; scope was not consumed.",
                               error_code=ToolErrorCode.SOURCE_UNAVAILABLE)
    storage = _storage(repository)
    if storage is None:
        return _error_response(request, started=started, reason="Application storage unavailable.",
                               error_code=ToolErrorCode.SOURCE_UNAVAILABLE)
    try:
        application_rows = list(repository.list_applications())
    except Exception:
        return _error_response(request, started=started, reason="Application snapshot unavailable.",
                               error_code=ToolErrorCode.SOURCE_UNAVAILABLE)
    applications_by_id = {}
    for application in application_rows:
        applications_by_id.setdefault(str(application.id), application)
    applications = list(applications_by_id.values())
    run_id = request.run_id
    claim = uuid4().hex
    with storage.write_transaction() as session:
        lock_review_scope(session)
        active = session.scalars(select(ToolCall).join(TaskRun, ToolCall.task_id == TaskRun.id)
                                 .where(ToolCall.tool_name == _STATE_TOOL,
                                        TaskRun.status.in_(["accepted", "running", "awaiting_continuation", "stopped", "pausing", "cancelling"]))
                                 .with_for_update()).all()
        conflicting = next((item for item in active if lease_active(dict(item.arguments or {}))), None)
        if conflicting is not None:
            result = _response(conflicting.task_id, dict(conflicting.arguments or {}), started, busy=True)
            summary = review_summary(conflicting.task_id, dict(conflicting.arguments or {}),
                                     session.get(TaskRun, conflicting.task_id).status)
            result.summary.update(summary, active_run_id=conflicting.task_id)
            result.summary["actions"] = ["status", *summary["actions"]]
            result.data = dict(result.summary)
            result.error_code = ToolErrorCode.SOURCE_UNAVAILABLE
            result.error_message = "Review already in progress; inspect or control the returned active_run_id."
            result.status = ToolStatus.FAILURE
            result.success = False
            return result
        if run_id:
            row = session.scalar(select(ToolCall).where(
                ToolCall.task_id == run_id, ToolCall.tool_name == _STATE_TOOL,
            ).with_for_update())
        else:
            row = None
        if row is None and run_id:
            return _error_response(request, started=started, reason="Review checkpoint not found; scope was not restarted.",
                                   error_code=ToolErrorCode.NOT_FOUND)
        if row is None:
            run_id = "status-review-" + uuid4().hex
            ids = list(dict.fromkeys(
                str(app.id) for app in applications
                if _value(app.stage) not in {"rejected", "withdrawn"}
            ))
            state = {"ids": ids, "results": {}, "attempts": {}, "database_total": len(applications),
                     "excluded_terminal": len(applications) - len(ids), "pages_total": 0,
                     "metadata": {"task_kind": "application_review", "thread_id": request.thread_id,
                                  "turn_id": request.turn_id}}
            session.add(TaskRun(id=run_id, task_type="application_status_review",
                                status="running", max_steps=1, user_request="复核全部未挂投递", source="application_review"))
            session.flush()
            row = ToolCall(id=run_id, task_id=run_id, tool_name=_STATE_TOOL,
                           arguments=state, source="application_review")
            session.add(row)
        else:
            run_id = row.task_id
            state = dict(row.arguments or {})
            task = session.get(TaskRun, run_id)
            if task.status in {"cancelled", "cancelling"}:
                result = _response(run_id, {**state, "run_status": task.status}, started)
                result.status, result.success = ToolStatus.FAILURE, False
                result.error_code, result.error_message = ToolErrorCode.INVALID_INPUT, "已取消的任务不能续跑，请明确新建任务。"
                return result
            if not resume_control and (state.get("control_request") or task.status in {"paused", "pausing"}):
                return _response(run_id, {**state, "run_status": task.status}, started)
            state["ids"] = list(dict.fromkeys(str(item) for item in state.get("ids", [])))
            state["results"] = dict(state.get("results") or {})
            attempts = dict(state.get("attempts") or {})
            for application_id in state["results"]:
                attempts.setdefault(str(application_id), 1)
            state["attempts"] = attempts
            state.setdefault("database_total", len(applications))
            state.setdefault("excluded_terminal", max(0, len(applications) - len(state["ids"])))
            state.setdefault("pages_total", 0)
        metadata = dict(state.get("metadata") or {})
        metadata.update(task_kind="application_review")
        if request.thread_id:
            metadata["thread_id"] = request.thread_id
        if request.turn_id:
            metadata["turn_id"] = request.turn_id
        state.update(lease_until=time() + _LEASE_SECONDS, claim=claim, wave_error=None,
                     continuation_until=0, interruption_reason=None,
                     control_request=None, run_status="running", metadata=metadata,
                     owner={"pid": os.getpid(), "desktop_run_id": os.environ.get("RECRUITOPS_DESKTOP_RUN_ID", "")})
        row.arguments = state
        task = session.get(TaskRun, run_id)
        _update_task_progress(task, state)
        if _pending_ids(state):
            task.status = "running"
            task.error_code = None

    owner_context = REVIEW_CONTEXT.set((storage, run_id, claim))

    def can_dispatch():
        with storage.session() as session:
            row = session.get(ToolCall, run_id)
            current = dict(row.arguments or {}) if row else {}
            return current.get("claim") == claim and lease_active(current) and not current.get("control_request")

    async def heartbeat():
        while True:
            await asyncio.sleep(min(10, _LEASE_SECONDS / 3))
            with storage.write_transaction() as session:
                row = session.scalar(select(ToolCall).where(ToolCall.id == run_id).with_for_update())
                current = dict(row.arguments or {}) if row else {}
                if current.get("claim") != claim or not lease_active(current):
                    return
                current["lease_until"] = time() + _LEASE_SECONDS
                row.arguments = current
                row.updated_at = utc_now()

    async def save_result(result_rows, page_count, expected_ids):
        with storage.write_transaction() as session:
            row = session.scalar(select(ToolCall).where(ToolCall.id == run_id).with_for_update())
            current = dict(row.arguments)
            if current.get("claim") != claim:
                return set()
            results = dict(current["results"])
            attempts = dict(current.get("attempts") or {})
            saved_ids = set()
            for result in result_rows:
                application_id = str(result.application_id)
                if application_id not in expected_ids or application_id in saved_ids:
                    continue
                saved_ids.add(application_id)
                saved = result.model_dump(mode="json", exclude={"observation"})
                application = lookup.get(application_id)
                if application:
                    saved.update(company_name=application.company_name, job_title=application.job_title)
                results[application_id] = saved
                attempts[application_id] = _attempt_count(current, application_id) + 1
            current.update(results=results, attempts=attempts,
                           pages_total=current["pages_total"] + page_count)
            task = session.get(TaskRun, run_id)
            _update_task_progress(task, current)
            row.arguments = current
            row.updated_at = task.updated_at = utc_now()
            return saved_ids

    page_tasks = []
    inflight_ids = set()
    checkpointed_this_wave = set()
    wave_error = None
    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        lookup = {str(app.id): app for app in applications}
        groups = defaultdict(list)
        for app_id in _pending_ids(state):
            app = lookup.get(app_id)
            url = normalize_http_page_url(app.record_url or "") if app else None
            groups[url or ""].append(app_id)
        selected = []
        for url, ids in groups.items():
            selected.append((url, ids))
            if len(selected) >= _WAVE_PAGES:
                break

        origin_locks = defaultdict(asyncio.Lock)
        wave_semaphore = asyncio.Semaphore(_WAVE_MAX_CONCURRENCY)

        async def process_page_records(url, ids):
            # The parent wave owns the global budget; each child batch must stay serial.
            for offset in range(0, len(ids), 50):
                page_ids = ids[offset:offset + 50]
                async with wave_semaphore:
                    if not can_dispatch():
                        return
                    inflight_ids.update(page_ids)
                    try:
                        part = await batch_observe_application_status(
                            BatchObserveApplicationStatusInput(
                                application_ids=page_ids, timeout_per_application_ms=40_000,
                                max_concurrent=1, timeout_ms=110_000,
                            ), bridge, repository,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        reason = _batch_exception_reason(exc)
                        failed = [
                            ApplicationStatusResult(
                                application_id=application_id,
                                state="failed",
                                reason=reason,
                                elapsed_ms=0,
                            )
                            for application_id in page_ids
                        ]
                        saved_ids = await save_result(failed, int(bool(url)), set(page_ids))
                        checkpointed_this_wave.update(saved_ids)
                        inflight_ids.difference_update(page_ids)
                        if reason == "review_batch_exception":
                            raise
                        continue
                    result_rows = [
                        result for key in _STATES for result in getattr(part, key)
                    ]
                    saved_ids = await save_result(result_rows, int(bool(url)), set(page_ids))
                    checkpointed_this_wave.update(saved_ids)
                    missing_ids = set(page_ids) - saved_ids
                    if missing_ids:
                        missing_results = [
                            ApplicationStatusResult(
                                application_id=application_id,
                                state="failed",
                                reason="review_batch_incomplete",
                                elapsed_ms=0,
                            )
                            for application_id in missing_ids
                        ]
                        saved_ids = await save_result(missing_results, 0, missing_ids)
                        checkpointed_this_wave.update(saved_ids)
                    inflight_ids.difference_update(page_ids)

        async def process_page(url, ids):
            origin = _origin_concurrency_key(url) if url else "missing"
            async with origin_locks[origin]:
                await process_page_records(url, ids)

        page_tasks = [asyncio.create_task(process_page(url, ids)) for url, ids in selected]
        async def wait_for_page_tasks():
            pending = set(page_tasks)
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_EXCEPTION,
                )
                for page_task in done:
                    if page_task.cancelled():
                        continue
                    error = page_task.exception()
                    if error is not None:
                        raise error

        await asyncio.wait_for(wait_for_page_tasks(), timeout=_WAVE_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        # Finished pages are already checkpointed; only unfinished IDs remain on resume.
        wave_error = "review_wave_timeout"
    except asyncio.CancelledError:
        wave_error = "review_wave_cancelled"
        raise
    except Exception:
        wave_error = "review_wave_failed"
    finally:
        # gather propagates a failure without cancelling siblings. Drain them before
        # releasing the lease so a resumed wave cannot overlap old browser writes.
        for page_task in page_tasks:
            if not page_task.done():
                page_task.cancel()
        await asyncio.gather(*page_tasks, return_exceptions=True)
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        REVIEW_CONTEXT.reset(owner_context)
        with storage.write_transaction() as session:
            row = session.scalar(select(ToolCall).where(ToolCall.id == run_id).with_for_update())
            state = dict(row.arguments)
            if state.get("claim") == claim:
                interrupted_ids = inflight_ids - checkpointed_this_wave
                if interrupted_ids:
                    results = dict(state.get("results") or {})
                    attempts = dict(state.get("attempts") or {})
                    reason = {
                        "review_wave_cancelled": "review_wave_cancelled",
                        "review_wave_timeout": "review_wave_timeout",
                    }.get(wave_error, "review_wave_interrupted")
                    for application_id in interrupted_ids:
                        existing = results.get(application_id)
                        if existing and not _is_transient_failure(existing):
                            continue
                        interrupted = ApplicationStatusResult(
                            application_id=application_id,
                            state="failed",
                            reason=reason,
                            elapsed_ms=0,
                        ).model_dump(mode="json", exclude={"observation"})
                        application = applications_by_id.get(application_id)
                        if application:
                            interrupted.update(
                                company_name=application.company_name,
                                job_title=application.job_title,
                            )
                        results[application_id] = interrupted
                        attempts[application_id] = _attempt_count(
                            {"attempts": attempts}, application_id
                        ) + 1
                    state.update(results=results, attempts=attempts)
                state.update(lease_until=0, wave_error=wave_error)
                task = session.get(TaskRun, run_id)
                remaining = len(_pending_ids(state))
                _update_task_progress(task, state)
                task.error_code = wave_error
                control = state.get("control_request")
                task.status = ("cancelled" if control == "cancel" else
                               "completed" if remaining == 0 else "paused" if control == "pause" else
                               "stopped" if wave_error not in {None, "review_wave_timeout"} else "awaiting_continuation")
                state["run_status"] = task.status
                # This is an assistant continuation window, not a browser lease.
                # Expiry is projected as interrupted rather than permanently active.
                state["continuation_until"] = time() + _CONTINUATION_SECONDS if task.status == "awaiting_continuation" else 0
                row.arguments = state
                row.updated_at = task.updated_at = utc_now()
    return _response(run_id, state, started)
