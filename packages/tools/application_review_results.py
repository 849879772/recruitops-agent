"""Read-only, bounded result receipts; raw browser evidence never leaves this API."""

from __future__ import annotations

import base64
from collections import Counter
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator
from sqlalchemy import select

from packages.domain.urls import normalize_http_page_url
from packages.storage.models import ApplicationSnapshot, TaskRun, ToolCall
from packages.storage.application_reviews import safe_review_receipt_diagnostics
from .application_review_summary import review_result_presentation
from .application_review_tasks import STATE_TOOL, review_summary
from .batch_browser_operations import ApplicationStatusResult, _storage
from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolResponse, ToolStatus

STATES = ("updated", "unchanged", "excluded", "blocked", "unresolved", "failed")
CATEGORIES = Literal["all", "attention", "failed", "blocked", "unresolved", "retained", "updated", "unchanged", "excluded"]


class ApplicationReviewResultsInput(ToolInput):
    scope: Literal["run", "latest"] = "run"
    run_id: str | None = Field(default=None, pattern=r"^status-review-[0-9a-f]{32}$")
    thread_id: str | None = Field(default=None, min_length=1, max_length=255)
    category: CATEGORIES = "attention"
    reason: str | None = Field(default=None, min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def scope_is_explicit(self):
        if self.scope == "latest" and (self.run_id or self.thread_id):
            raise ValueError("latest receipts cannot be filtered as a historical run")
        return self


class ApplicationReviewResultsResponse(ToolResponse[dict[str, Any]]):
    timeout_ms: int = 5_000
    elapsed_ms: int = 0
    evidence: list[EvidenceSource] = Field(default_factory=lambda: [EvidenceSource(source="agent.application_review_checkpoint")])


def compact_summary(summary):
    """Do not repeat an unbounded identity list in every wave/status receipt."""
    result = dict(summary)
    confirmations = result.pop("identity_confirmation_items", []) or []
    result["identity_confirmation_count"] = result.get("identity_confirmation_required_count", len(confirmations))
    result.setdefault("identity_confirmation_required_count", len(confirmations))
    breakdown = result.get("reason_breakdown")
    if isinstance(breakdown, dict):
        result["reason_breakdown"] = {
            state: {str(reason)[:200]: count for reason, count in list(reasons.items())[:12]}
            for state, reasons in breakdown.items() if state in STATES and isinstance(reasons, dict)
        }
        result["reason_breakdown_has_more"] = any(len(reasons) > 12 for reasons in breakdown.values() if isinstance(reasons, dict))
    result["details_tool"] = "application_review_results"
    result["details_note"] = "明细须按 run_id 和 category 分页查询；不能把预览当作完整名单。"
    # Internal unresolved includes retained rows. Give consumers a disjoint,
    # display-ready breakdown instead of inviting them to call all of it failed.
    retained = int(result.get("retained_count", 0))
    result["display_counts"] = {
        "已更新": int(result.get("updated", 0)),
        "官网已核验无变化": int(result.get("unchanged", 0)),
        "保留原阶段（不等于已核验无变化）": retained,
        "仅邮件更新，已跳过": int(result.get("excluded", 0)),
        "需要登录或验证": int(result.get("blocked", 0)),
        "无法确认，需处理": int(result.get("attention_required_count", max(0, int(result.get("unresolved", 0)) - retained))),
        "执行失败": int(result.get("failed", 0)),
    }
    return result


def _result(row):
    # Old checkpoints can have additional keys; only known receipt fields are used.
    known = {key: value for key, value in row.items() if key in ApplicationStatusResult.model_fields}
    known.setdefault("elapsed_ms", 0)
    return review_result_presentation(ApplicationStatusResult.model_validate(known))


def _matches(row, category):
    if category == "all":
        return True
    if category == "attention":
        return row.state in {"failed", "blocked"} or (row.state == "unresolved" and row.presentation_state != "retained")
    if category == "retained":
        return row.presentation_state == "retained"
    return row.state == category


def _safe_url(value):
    value = str(value or "")
    return (normalize_http_page_url(value) or None) if len(value) <= 2048 else None


def _code(value):
    return str(value)[:200] if value is not None else None


def _receipt(row, application=None, *, scope="run", fallback_time=None):
    company = row.company_name or (application.company_name if application else None)
    title = row.job_title or (application.job_title if application else None)
    observed_page = (row.observation or {}).get("page")
    page = observed_page if isinstance(observed_page, dict) else {}
    historical_url = _safe_url(page.get("url") or page.get("page_url"))
    url = historical_url or _safe_url(application.record_url if application else None)
    time = row.checked_at.isoformat() if row.checked_at else fallback_time
    navigation = safe_review_receipt_diagnostics(row.model_dump()).get("navigation_diagnostics") or {}
    # Schema-sized strings bound transport size; no DOM, quotations or model traces.
    return {
        "application_id": row.application_id,
        "company_name": str(company)[:255] if company else None,
        "job_title": str(title)[:512] if title else None,
        "name_source": "application_snapshot" if scope == "latest" else (
            "review_result" if row.company_name and row.job_title else "application_snapshot_fallback" if application else "unavailable"),
        "record_url": url,
        "url_source": "review_result" if historical_url else "application_snapshot" if url else "unavailable",
        "state": row.state, "presentation_state": row.presentation_state,
        "reason": str(row.reason)[:200] if row.reason else None,
        "saved_stage": _code(row.saved_stage), "observed_status": _code(row.observed_status),
        "wrote": row.wrote, "checked_at": time,
        "checked_at_source": "review_result" if row.checked_at else "checkpoint_updated_at" if fallback_time else "unavailable",
        "model_disposition": _code(row.model_disposition), "vision_disposition": _code(row.vision_disposition),
        "navigation_reason": navigation.get("restriction") or navigation.get("reason"),
        "navigation_diagnostics": navigation or None,
    }


def compact_batch_response(response):
    """MCP-only projection. Internal waves and persisted evidence stay untouched."""
    all_rows = [row for state in STATES for row in getattr(response, state)]
    # Explicit batches have at most 50 records and no shared checkpoint. Keep
    # their names complete; checkpointed full batches expose five sample rows.
    limit = 5 if response.summary.get("run_id") else 50
    ranked = sorted(all_rows, key=lambda row: (not _matches(row, "attention"), row.application_id))
    visible = ranked[:limit]
    buckets = {state: [] for state in STATES}
    for row in visible:
        buckets[row.state].append(row.model_copy(update={
            "company_name": str(row.company_name)[:255] if row.company_name else None,
            "job_title": str(row.job_title)[:512] if row.job_title else None,
            "reason": _code(row.reason), "saved_stage": _code(row.saved_stage),
            "observed_status": _code(row.observed_status), "model_disposition": _code(row.model_disposition),
            "vision_disposition": _code(row.vision_disposition),
            "observation": None, "diagnostics": safe_review_receipt_diagnostics(row.model_dump()) or None,
            "model_digest": None, "model_event_id": None,
        }))
    summary = compact_summary(response.summary)
    summary["result_preview_count"] = len(visible)
    summary["result_total"] = len(all_rows)
    summary["results_has_more"] = len(visible) < len(all_rows)
    summary["result_selection"] = "attention_first_preview" if summary["results_has_more"] else "complete_compact_receipts"
    return response.model_copy(update={**buckets, "succeeded": [], "skipped": [], "summary": summary, "data": summary})


def _fingerprint(items, scope, run_id, category, reason):
    return sha256(json.dumps([scope, run_id, category, reason, items], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _cursor(value):
    if not value:
        return None
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
        if not isinstance(decoded, dict) or type(decoded.get("offset")) is not int or decoded["offset"] < 0:
            raise ValueError
        run_id = decoded.get("run_id")
        if run_id is not None and (not isinstance(run_id, str) or not re.fullmatch(r"status-review-[0-9a-f]{32}", run_id)):
            raise ValueError
        if not isinstance(decoded.get("fingerprint"), str) or not re.fullmatch(r"[0-9a-f]{24}", decoded["fingerprint"]):
            raise ValueError
        return decoded
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("分页游标无效，请从第一页重新查询。") from exc


def application_review_results(request, repository):
    storage = _storage(repository)
    if storage is None:
        return _error(ToolErrorCode.SOURCE_UNAVAILABLE, "投递记录存储不可用。")
    try:
        cursor = _cursor(request.cursor)
        # A continuation must keep its original run even if another run finishes.
        run_id = request.run_id or (cursor.get("run_id") if cursor else None)
        rows = []
        summary = {}
        status = None
        fallback_time = None
        with storage.session() as session:
            if request.scope == "run":
                statement = select(TaskRun, ToolCall).join(ToolCall, ToolCall.task_id == TaskRun.id).where(
                    TaskRun.task_type == "application_status_review", ToolCall.tool_name == STATE_TOOL)
                if run_id:
                    statement = statement.where(TaskRun.id == run_id)
                if request.thread_id:
                    statement = statement.where(ToolCall.arguments["metadata"]["thread_id"].as_string() == request.thread_id)
                selected = session.execute(statement.order_by(TaskRun.updated_at.desc(), TaskRun.id.desc()).limit(1)).first()
                if not selected:
                    return _error(ToolErrorCode.NOT_FOUND, "没有找到相应复核任务。")
                task, checkpoint = selected
                run_id, status = task.id, task.status
                state = dict(checkpoint.arguments or {})
                summary = compact_summary(review_summary(run_id, state, status))
                status = summary.get("run_status", status)
                if state.get("details_expired"):
                    return ApplicationReviewResultsResponse(tool_name="application_review_results", status=ToolStatus.SUCCESS,
                        success=True, data={"scope": "run", "run_id": run_id, "run_status": status,
                            "details_expired": True, "items": [], "total": None, "has_more": False, "next_cursor": None,
                            "summary": summary, "note": "本轮明细已过保留期；不会用各投递的最新结果替代历史结果。可另查 scope=latest。"})
                fallback_time = checkpoint.updated_at.isoformat() if checkpoint.updated_at else None
                saved = state.get("results") or {}
                rows = [_result({**saved[item], "application_id": item}) for item in dict.fromkeys(state.get("ids") or [])
                        if item in saved and isinstance(saved[item], dict)]
                ids = [row.application_id for row in rows]
                apps = {app.id: app for app in session.execute(select(
                    ApplicationSnapshot.id, ApplicationSnapshot.company_name, ApplicationSnapshot.job_title,
                    ApplicationSnapshot.record_url).where(ApplicationSnapshot.id.in_(ids)))} if ids else {}
            else:
                apps = {app.id: app for app in session.execute(select(
                    ApplicationSnapshot.id, ApplicationSnapshot.company_name, ApplicationSnapshot.job_title,
                    ApplicationSnapshot.record_url, ApplicationSnapshot.last_review).where(ApplicationSnapshot.last_review.is_not(None)))}
                rows = [_result({**app.last_review, "application_id": app.id}) for app in apps.values()
                        if isinstance(app.last_review, dict) and app.last_review.get("state") in STATES]
                summary = {"total": len(rows), **dict(Counter(row.state for row in rows)),
                    "note": "每条投递最近一次复核，可能来自不同任务；不是单轮运行结果。"}
            items = [_receipt(row, apps.get(row.application_id), scope=request.scope, fallback_time=fallback_time)
                     for row in sorted(rows, key=lambda item: item.application_id)
                     if _matches(row, request.category) and (not request.reason or row.reason == request.reason)]
        fingerprint = _fingerprint(items, request.scope, run_id, request.category, request.reason)
        if cursor and cursor.get("fingerprint") != fingerprint:
            return _error(ToolErrorCode.INVALID_INPUT, "复核结果或查询条件已变化，请从第一页重新查询。")
        offset = cursor["offset"] if cursor else 0
        if offset > len(items):
            return _error(ToolErrorCode.INVALID_INPUT, "分页游标超出结果范围，请从第一页重新查询。")
        page = []
        page_chars = 0
        for item in items[offset:offset + request.limit]:
            size = len(json.dumps(item, ensure_ascii=False))
            if page and page_chars + size > 14_000:
                break
            page.append(item)
            page_chars += size
        next_offset = offset + len(page)
        has_more = next_offset < len(items)
        next_cursor = base64.urlsafe_b64encode(json.dumps({"offset": next_offset, "run_id": run_id,
            "fingerprint": fingerprint}).encode()).decode() if has_more else None
        return ApplicationReviewResultsResponse(tool_name="application_review_results", status=ToolStatus.SUCCESS, success=True,
            data={"scope": request.scope, "run_id": run_id, "run_status": status, "category": request.category,
                "details_expired": False, "items": page, "total": len(items), "has_more": has_more,
                "next_cursor": next_cursor, "summary": summary},
            evidence=[EvidenceSource(source="agent.application_review_checkpoint" if request.scope == "run" else "agent.application_snapshot",
                                     source_ref=run_id)])
    except ValidationError:
        return _error(ToolErrorCode.INTERNAL_ERROR, "持久化复核明细格式异常；未用不完整或推测结果替代。")
    except ValueError as exc:
        return _error(ToolErrorCode.INVALID_INPUT, str(exc))


def _error(code, message):
    return ApplicationReviewResultsResponse(tool_name="application_review_results", status=ToolStatus.FAILURE,
        success=False, error_code=code, error_message=message)
