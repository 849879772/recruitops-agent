"""Thin MCP boundaries for durable task receipts and human-approved mail binding."""

from __future__ import annotations

from threading import RLock
from time import perf_counter
from typing import Any, Literal

from pydantic import Field, model_validator

from packages.config import get_settings
from packages.tools.typed import EvidenceSource, ToolErrorCode, ToolInput, ToolResponse, ToolStatus


class BackgroundTaskStatusInput(ToolInput):
    run_id: str | None = Field(default=None, min_length=1, max_length=255)
    thread_id: str | None = Field(default=None, min_length=1, max_length=255)
    include_recoverable: bool = True


class BackgroundTaskResponse(ToolResponse[dict[str, Any]]):
    evidence: list[EvidenceSource] = Field(default_factory=lambda: [EvidenceSource(source="agent.task_runs")])
    timeout_ms: int = 5_000
    elapsed_ms: int = 0


class BackgroundTaskActionResponse(BackgroundTaskResponse):
    read_only: Literal[False] = False


class DailyRecruitmentControlInput(ToolInput):
    run_id: str = Field(min_length=1, max_length=255)
    action: Literal["pause", "cancel"]


class RecruitmentMailRunStartInput(ToolInput):
    timeout_ms: int = Field(default=30_000, ge=1, le=120_000)
    wait_ms: int = Field(default=20_000, ge=0, le=20_000)
    record_ids: list[str] | None = Field(default=None, min_length=1, max_length=10_000)
    thread_id: str | None = Field(default=None, min_length=1, max_length=255)
    turn_id: str | None = Field(default=None, min_length=1, max_length=255)
    refresh: bool = True
    retry_failed: bool = Field(default=False, description=(
        "Only after the user explicitly requests retrying selected failed mail. Requires 1-50 record_ids; "
        "grants one bounded processing round, never repeats successful mail or refreshes the mailbox."))

    @model_validator(mode="after")
    def bounded_explicit_retry(self):
        if self.retry_failed and (not self.record_ids or len(self.record_ids) > 50):
            raise ValueError("mail_retry_requires_explicit_records")
        return self


class RecruitmentMailRunStatusInput(ToolInput):
    timeout_ms: int = Field(default=30_000, ge=1, le=120_000)
    wait_ms: int = Field(default=20_000, ge=0, le=20_000)
    run_id: str | None = Field(default=None, min_length=1, max_length=255)
    thread_id: str | None = Field(default=None, min_length=1, max_length=255)


class RecruitmentMailRunControlInput(ToolInput):
    timeout_ms: int = Field(default=30_000, ge=1, le=120_000)
    wait_ms: int = Field(default=20_000, ge=0, le=20_000)
    run_id: str = Field(min_length=1, max_length=255)
    action: Literal["pause", "cancel", "resume"]
    thread_id: str | None = Field(default=None, min_length=1, max_length=255)
    turn_id: str | None = Field(default=None, min_length=1, max_length=255)


_SERVICE_LOCK = RLock()


def _response(request, tool_name, data, *, action=False):
    model = BackgroundTaskActionResponse if action else BackgroundTaskResponse
    success = data.get("success", True)
    return model(tool_name=tool_name, status=ToolStatus.SUCCESS if success else ToolStatus.FAILURE,
                 success=success, data=data, timeout_ms=request.timeout_ms,
                 error_code=None if success else ToolErrorCode.INVALID_INPUT,
                 error_message=None if success else str(data.get("reason", "task_control_failed")))


def background_task_status(request, dependencies):
    from apps.api.daily_progress import task_progress

    storage = getattr(dependencies.repository, "storage", None) or dependencies.mail_store.storage
    data = task_progress(storage, run_id=request.run_id, thread_id=request.thread_id,
                         include_recoverable=request.include_recoverable)
    data["selection"] = "selected" if data.get("run") else "ambiguous" if data.get("runs") else "not_found"
    return _response(request, "background_task_status", data)


def daily_recruitment_sync_control(request, dependencies):
    from packages.tools.task_runtime_control import request_daily_control

    storage = getattr(dependencies.repository, "storage", None) or dependencies.mail_store.storage
    return _response(request, "daily_recruitment_sync_control",
                     request_daily_control(storage, request.run_id, request.action), action=True)


def mail_run_service(dependencies):
    """One worker pool per adapter mail store; state remains in durable storage."""
    from packages.recruitment_mail.run_service import MailProcessingRunService
    from packages.recruitment_mail.freshness import ensure_mail_fresh

    store = dependencies.mail_store
    with _SERVICE_LOCK:
        service = getattr(store, "_mcp_mail_run_service", None)
        if service is None:
            service = MailProcessingRunService(store, dependencies.repository, get_settings(),
                sync_mail=lambda: ensure_mail_fresh(get_settings(), store, limit=500, force=True))
            store._mcp_mail_run_service = service
        else:
            service.settings = get_settings()
        return service


def recruitment_mail_run_start(request, dependencies):
    started = perf_counter()
    if not request.thread_id:
        return _response(request, "recruitment_mail_run_start", {
            "success": False, "reason": "mail_conversation_thread_required",
            "next_action": "使用当前会话上下文中的 thread_id 重试；不要猜测或认领其他会话的任务。",
        }, action=True)
    service = mail_run_service(dependencies)
    result = service.start(record_ids=request.record_ids,
        thread_id=request.thread_id, turn_id=request.turn_id, refresh=request.refresh,
        **({"retry_failed": True} if request.retry_failed else {}))
    if result.get("run_id") and _mail_wait_seconds(request):
        result = service.wait(result["run_id"], timeout_seconds=_mail_wait_seconds(request))
    return _response(request, "recruitment_mail_run_start", _mail_turn_data({"run": result}), action=True).model_copy(
        update={"elapsed_ms": int((perf_counter() - started) * 1000)})


def recruitment_mail_run_status(request, dependencies):
    # Do not construct a service, sync the mailbox, or create a worker for reads.
    from packages.recruitment_mail.run_service import wait_mail_progress

    started = perf_counter()
    result = wait_mail_progress(dependencies.mail_store.storage,
        run_id=request.run_id, thread_id=request.thread_id, timeout_seconds=_mail_wait_seconds(request))
    return _response(request, "recruitment_mail_run_status", _mail_turn_data(result)).model_copy(
        update={"elapsed_ms": int((perf_counter() - started) * 1000)})


def recruitment_mail_run_control(request, dependencies):
    started = perf_counter()
    service = mail_run_service(dependencies)
    result = service.control(request.run_id, request.action,
        thread_id=request.thread_id, turn_id=request.turn_id)
    if request.action == "resume" and _mail_wait_seconds(request):
        result = service.wait(request.run_id, timeout_seconds=_mail_wait_seconds(request))
    return _response(request, "recruitment_mail_run_control", _mail_turn_data({"run": result}), action=True).model_copy(
        update={"elapsed_ms": int((perf_counter() - started) * 1000)})


def _mail_wait_seconds(request):
    return min(request.wait_ms, max(0, request.timeout_ms - 1_000)) / 1_000


def _mail_turn_data(data):
    run = data.get("run") or {}
    waiting = run.get("status") == "awaiting_confirmation"
    return {**data, "execution_mode": "waiting_for_user" if waiting else "foreground",
            **({"confirmation_required": True,
                "next_action": "用户在当前会话关联弹窗中选择岗位。任务尚未完成；确认后系统继续原范围，不要轮询等待或要求用户输入继续。"} if waiting else {}),
            "continuation_required": run.get("status") in {"accepted", "running", "pausing", "cancelling"}}


def recruitment_mail_binding_candidates_operation(request, dependencies):
    from packages.tools.recruitment_mail import recruitment_mail_binding_candidates

    return recruitment_mail_binding_candidates(request, dependencies.mail_store)


def application_identity_candidates_operation(request, dependencies):
    from packages.tools.application_identity_binding import application_identity_candidates
    return application_identity_candidates(request, dependencies.mail_store.storage)


def application_identity_propose_operation(request, dependencies):
    from packages.approval import ApprovalRegistry, SqlAlchemyApprovalPersistence
    from packages.tools.application_identity_binding import application_identity_propose
    store = dependencies.mail_store
    with _SERVICE_LOCK:
        registry = getattr(store, "_mcp_binding_approval_registry", None)
        if registry is None:
            registry = ApprovalRegistry(SqlAlchemyApprovalPersistence(store.storage))
            store._mcp_binding_approval_registry = registry
    return application_identity_propose(request, store.storage, registry)


def recruitment_mail_binding_propose_operation(request, dependencies):
    from packages.approval import ApprovalRegistry, SqlAlchemyApprovalPersistence
    from packages.tools.recruitment_mail import recruitment_mail_binding_propose

    store = dependencies.mail_store
    with _SERVICE_LOCK:
        registry = getattr(store, "_mcp_binding_approval_registry", None)
        if registry is None:
            registry = ApprovalRegistry(SqlAlchemyApprovalPersistence(store.storage))
            store._mcp_binding_approval_registry = registry
    # Only a pending preview is created here. Approval is exclusively a user UI action.
    return recruitment_mail_binding_propose(request, store, registry)
