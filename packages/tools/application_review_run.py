"""Checkpointed application reviews within the MCP transport time budget."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from time import perf_counter, time
from uuid import uuid4

from sqlalchemy import select

from packages.domain.urls import normalize_http_page_url
from packages.storage.models import TaskRun, ToolCall
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
    summary = {
        "run_id": run_id,
        "run_status": ("stopped" if state.get("wave_error") else "running") if remaining else "completed",
        "selection": "all_non_terminal", "scope_total": total, "total": total,
        "database_total": state["database_total"],
        "excluded_terminal": state["excluded_terminal"],
        "unique_record_count": total,
        "processed_count": len(rows), "completed_count": completed_count,
        "remaining_count": remaining, "retryable_count": len(retryable),
        "retry_exhausted_count": retry_exhausted,
        "scope_complete": remaining == 0, "in_progress": busy,
        "wave_error": state.get("wave_error"),
        "pages_total": state["pages_total"],
        "write_count": sum(row.wrote for row in rows),
        "completion_rate": round(completed_count / total, 4) if total else 1.0,
        "verification_success_count": verified,
        "verification_rate": round(verified / total, 4) if total else 1.0,
        **{key: len(value) for key, value in buckets.items()},
        "next_action": (
            "用同一个 run_id 继续调用本工具；暂时失败仅在预算内重试，累计统计无需相加。"
            if remaining else "范围处理完毕不代表核验成功；按更新、登录、证据不足和失败分类报告。"
        ),
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
        error_message=("复核范围尚有待处理记录，按 run_id 继续。" if remaining else
                       "范围已处理，但部分记录未能核验成功。") if status != ToolStatus.SUCCESS else None,
        evidence=[EvidenceSource(source="agent.application_snapshot"),
                  EvidenceSource(source="agent.application_review_checkpoint", source_ref=run_id)],
        timeout_ms=110_000, elapsed_ms=int((perf_counter() - started) * 1000),
    )


async def continue_application_review(request, bridge, repository):
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
        # Serializes creation across MCP sessions on PostgreSQL without a schema migration.
        if session.bind.dialect.name == "postgresql":
            from sqlalchemy import text
            session.execute(text("SELECT pg_advisory_xact_lock(718202609)"))
        if run_id:
            row = session.scalar(select(ToolCall).where(
                ToolCall.task_id == run_id, ToolCall.tool_name == _STATE_TOOL,
            ).with_for_update())
        else:
            row = session.scalar(select(ToolCall).join(TaskRun, ToolCall.task_id == TaskRun.id)
                                 .where(ToolCall.tool_name == _STATE_TOOL,
                                        TaskRun.status.in_(["running", "stopped"]))
                                 .order_by(ToolCall.created_at.desc()).with_for_update())
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
                     "excluded_terminal": len(applications) - len(ids), "pages_total": 0}
            session.add(TaskRun(id=run_id, task_type="application_status_review",
                                status="running", user_request="复核全部未挂投递", source="application_review"))
            session.flush()
            row = ToolCall(id=run_id, task_id=run_id, tool_name=_STATE_TOOL,
                           arguments=state, source="application_review")
            session.add(row)
        else:
            run_id = row.task_id
            state = dict(row.arguments or {})
            state["ids"] = list(dict.fromkeys(str(item) for item in state.get("ids", [])))
            state["results"] = dict(state.get("results") or {})
            attempts = dict(state.get("attempts") or {})
            for application_id in state["results"]:
                attempts.setdefault(str(application_id), 1)
            state["attempts"] = attempts
            state.setdefault("database_total", len(applications))
            state.setdefault("excluded_terminal", max(0, len(applications) - len(state["ids"])))
            state.setdefault("pages_total", 0)
        if state.get("lease_until", 0) > time():
            return _response(run_id, state, started, busy=True)
        state.update(lease_until=time() + _LEASE_SECONDS, claim=claim, wave_error=None)
        row.arguments = state
        task = session.get(TaskRun, run_id)
        if _pending_ids(state):
            task.status = "running"
            task.error_code = None

    async def save_result(result_rows, page_count, expected_ids):
        with storage.write_transaction() as session:
            row = session.scalar(select(ToolCall).where(ToolCall.id == run_id).with_for_update())
            current = dict(row.arguments)
            if current.get("claim") != claim:
                return
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
            row.arguments = current
            return saved_ids

    page_tasks = []
    inflight_ids = set()
    checkpointed_this_wave = set()
    wave_error = None
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
                row.arguments = state
                task = session.get(TaskRun, run_id)
                remaining = len(_pending_ids(state))
                task.step_count = len(state["ids"]) - remaining
                task.max_steps = len(state["ids"])
                task.current_step = f"{task.step_count}/{task.max_steps}"
                task.error_code = wave_error
                task.status = ("completed" if remaining == 0 else
                               "stopped" if wave_error else "running")
    return _response(run_id, state, started)
