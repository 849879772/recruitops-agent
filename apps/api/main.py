from datetime import date, datetime, timedelta, timezone
from contextlib import asynccontextmanager
import asyncio
from functools import lru_cache
import importlib
import json
import logging
import os
from pathlib import Path
from secrets import compare_digest
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from apps.api.local_ui import is_local_ui, local_ui_request, router as local_ui_router
from apps.api.daily_progress import latest_daily_progress, task_progress
from apps.api.schedule_items import router as schedule_items_router
from apps.api.company_sources import router as company_sources_router, set_start_retry_callback
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from packages.codex_runtime import JsonRpcRemoteError

from packages.approval import (
    ApprovalDecision,
    ApprovalPreview,
    ApprovalStatus,
    ApprovalRegistry,
    SqlAlchemyApprovalPersistence,
    ApprovalToken,
    AgentApplicationWriteAdapter,
    ApprovedWriteExecutor,
    WriteAuditRecord,
    PolicyErrorCode,
)
from packages.config import get_settings
from packages.domain.models import (
    Application,
    ApplicationPage,
    ApplicationStage,
    Company,
    JobBrowsePage,
    JobDetail,
    JobPage,
    RecruitmentBatch,
    ScheduleEvent,
    ServiceStatus,
)
from packages.recruitment_mail import (
    RecruitmentMailProcessingStatus,
    RecruitmentMailStore,
    RecruitmentMessageCategory,
)
from packages.repositories.base import RecruitmentRepository
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.reporting import build_reporting_summary
from packages.storage import (
    AgentStateStore,
    Storage,
    create_storage_engine,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from packages.tools.browser import (
    BrowserObservationInput,
    BrowserObservationResponse,
    observe_browser_page,
)
from packages.tools.application_capture import (
    ApplicationCaptureInput,
    ApplicationCaptureResponse,
    prepare_application_capture,
)
from packages.tools.recruitment_mail import (
    RecruitmentMailDetailData,
    RecruitmentMailDetailInput,
    RecruitmentMailReviewData,
    RecruitmentMailReviewInput,
    RecruitmentMailSearchData,
    RecruitmentMailSearchInput,
    RecruitmentMailBindingCandidatesInput,
    RecruitmentMailBindingProposeInput,
    recruitment_mail_binding_candidates,
    recruitment_mail_binding_propose,
    get_recruitment_mail,
    review_recruitment_mail,
    search_recruitment_mail,
)
from apps.api.codex_bff import get_codex_bff_service
from packages.browser_bridge import BrowserBridgeServer, BrowserBridgeStore
from packages.codex_runtime.telemetry import JsonlTraceRecorder as CodexJsonlTraceRecorder
from packages.vision import VisionError, VisionResult, VisionService
from packages.vision.observation import analyze_observation
from packages.automation import (
    AutomationRunResult,
    AutomationStore,
    LocalAutomationWorker,
    automation_blocked_message,
    automation_blocked_reason,
)
from apps.api.automation import CodexAutomationExecutor


logger = logging.getLogger(__name__)


browser_bridge_store = BrowserBridgeStore(
    Storage.from_url(get_settings().database_url)
)
browser_bridge_server = BrowserBridgeServer(
    browser_bridge_store,
    lambda: get_settings().api_token,
)


class BrowserVisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    image_data_url: str = Field(min_length=32, max_length=9_000_000)
    page_url: str = Field(min_length=8, max_length=2_048)
    operation_id: str = Field(min_length=1, max_length=128)


@lru_cache
def browser_vision_service() -> VisionService:
    settings = get_settings()
    return VisionService(
        api_key=settings.llm_api_key, model=settings.vision_model,
        endpoint=settings.vision_endpoint, timeout=settings.vision_timeout_seconds,
        max_bytes=settings.vision_max_image_bytes,
    )


def _run_recruitment_mail_sync(*, limit: int = 100) -> dict[str, Any]:
    """Run the same local mailbox sync path used by the API and scheduler."""

    settings = get_settings()
    if not getattr(settings, "mail_enabled", False):
        return {"status": "disabled", "reason": "mail_disabled"}
    store = recruitment_mail_store()
    from packages.recruitment_mail.freshness import ensure_mail_fresh

    return ensure_mail_fresh(settings, store, limit=limit)


async def _run_startup_mail_sync(app_state: Any) -> None:
    """Perform one best-effort mailbox catch-up without blocking API startup."""

    app_state.startup_mail_sync_status = "running"
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run_recruitment_mail_sync),
            timeout=60.0,
        )
        app_state.startup_mail_sync_status = result.get("status", "failed")
        logger.info(
            "startup mailbox sync finished: status=%s fetched=%s inserted=%s reused=%s",
            result.get("status"),
            (result.get("sync") or {}).get("fetched", 0),
            (result.get("sync") or {}).get("inserted", 0),
            (result.get("sync") or {}).get("reused", 0),
        )
        print("startup mailbox sync finished", flush=True)
    except Exception as exc:
        # Mail is an auxiliary integration; a provider outage must not prevent startup.
        app_state.startup_mail_sync_status = "failed"
        logger.warning("startup mailbox sync failed: %s", type(exc).__name__)
        print(f"startup mailbox sync failed: {type(exc).__name__}", flush=True)


def _recover_desktop_browser_operations(settings: Any, storage: Storage) -> int:
    # The desktop supervisor owns an exclusive instance lock before API startup.
    # Other servers and read-only launches must not cancel another owner's work.
    instance_id = os.environ.get("RECRUITOPS_DESKTOP_INSTANCE_ID", "")
    if (getattr(settings, "env", None) != "desktop-isolated"
            or getattr(settings, "write_enabled", False) is not True
            or not instance_id
            or os.environ.get("RECRUITOPS_ENV") != "desktop-isolated"
            or os.environ.get("RECRUITOPS_WRITE_ENABLED") != "true"
            or os.environ.get("RECRUITOPS_DESKTOP_WRITE_OPTIN") != instance_id):
        return 0
    return BrowserBridgeStore(storage).recover_interrupted_operations()


def _build_automation_worker(settings: Any):
    storage = Storage.from_url(settings.database_url)
    try:
        store = AutomationStore(storage)
        executor = CodexAutomationExecutor(
            get_codex_bff_service(),
            store,
            timeout_seconds=getattr(settings, "automation_run_timeout_seconds", 600.0),
        )
        worker = LocalAutomationWorker(
            store,
            lambda task: _execute_automation_if_authorized(executor, task),
            poll_seconds=getattr(settings, "automation_poll_seconds", 10.0),
        )
        return storage, store, worker
    except BaseException:
        storage.engine.dispose()
        raise


async def _execute_automation_if_authorized(executor: Any, task: Any) -> AutomationRunResult:
    settings = get_settings()
    reason = automation_blocked_reason(task.task_id, settings)
    if reason is not None:
        return AutomationRunResult(status="blocked", error=reason)
    return await executor(task)


class _AutomationWorkerLifecycle:
    def __init__(self, app_state: Any, *, codex_runtime_started: bool):
        self.app_state = app_state
        self.codex_runtime_started = codex_runtime_started
        self.storage = None
        self.store = None
        self.worker = None
        self.task = None
        self.retry_after = 0.0

    def _status(self, value: str) -> None:
        self.app_state.automation_worker_status = value

    async def _release_worker(self) -> None:
        storage = self.storage
        self.storage = self.store = self.worker = self.task = None
        if storage is not None:
            storage.engine.dispose()

    async def reconcile(self) -> None:
        settings = get_settings()
        enabled = (
            self.codex_runtime_started
            and bool(getattr(settings, "codex_runtime_enabled", False))
            and bool(getattr(settings, "automation_enabled", False))
        )
        loop = asyncio.get_running_loop()

        if self.task is not None and self.task.done():
            error = None if self.task.cancelled() else self.task.exception()
            await self._release_worker()
            if error is not None:
                self.retry_after = loop.time() + 5.0
                logger.error("local automation worker exited: %s", type(error).__name__)

        if not enabled:
            if self.worker is not None:
                self.worker.stop()
                self._status("stopping")
                if self.task is not None and not self.task.done():
                    return
                await self._release_worker()
            self._status("disabled")
            return

        if self.worker is not None:
            self._status("running")
            return
        if loop.time() < self.retry_after:
            self._status("retry_wait")
            return

        storage, store, worker = _build_automation_worker(settings)
        try:
            await asyncio.to_thread(store.recover_interrupted)
            await asyncio.to_thread(store.skip_missed_occurrences)
        except BaseException:
            storage.engine.dispose()
            raise
        self.storage, self.store, self.worker = storage, store, worker
        self.task = asyncio.create_task(worker.run_forever(), name="local-automation-worker")
        self._status("running")

    async def close(self) -> None:
        if self.worker is not None:
            self.worker.stop()
        if self.task is not None and not self.task.done():
            self.task.cancel()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)
        await self._release_worker()


async def _watch_automation_configuration(lifecycle: _AutomationWorkerLifecycle) -> None:
    while True:
        await asyncio.sleep(1.0)
        try:
            await lifecycle.reconcile()
        except Exception as exc:
            logger.warning("automation configuration refresh failed: %s", type(exc).__name__)
            await asyncio.sleep(4.0)


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    settings = get_settings()
    database_url = getattr(settings, "database_url", None)
    recovered_runs = 0
    recovered_browser_operations = 0
    if database_url:
        recovery_storage = Storage.from_url(database_url)
        try:
            recovered_runs = AgentStateStore(recovery_storage).recover_interrupted_task_runs()
            recovered_browser_operations = _recover_desktop_browser_operations(settings, recovery_storage)
        finally:
            recovery_storage.engine.dispose()
    _app.state.recovered_task_runs = recovered_runs
    _app.state.recovered_browser_operations = recovered_browser_operations
    if recovered_runs:
        logger.warning("marked %s interrupted task runs as recoverable", recovered_runs)
    bridge_enabled = bool(str(getattr(settings, "api_token", "")).strip())
    if bridge_enabled:
        await browser_bridge_server.start()
    if getattr(settings, "codex_runtime_enabled", False):
        await get_codex_bff_service().start()
    automation_lifecycle = _AutomationWorkerLifecycle(
        _app.state,
        codex_runtime_started=bool(getattr(settings, "codex_runtime_enabled", False)),
    )
    automation_lifecycle_task = None
    startup_mail_task = None
    if getattr(settings, "mail_enabled", False) and getattr(settings, "mail_sync_on_startup", True):
        _app.state.startup_mail_sync_status = "scheduled"
        print("startup mailbox sync scheduled", flush=True)
        startup_mail_task = asyncio.create_task(
            _run_startup_mail_sync(_app.state),
            name="startup-mail-sync",
        )
        _app.state.startup_mail_sync_task = startup_mail_task
    await automation_lifecycle.reconcile()
    automation_lifecycle_task = asyncio.create_task(
        _watch_automation_configuration(automation_lifecycle),
        name="automation-configuration-watch",
    )
    try:
        yield
    finally:
        if automation_lifecycle_task is not None:
            automation_lifecycle_task.cancel()
            await asyncio.gather(automation_lifecycle_task, return_exceptions=True)
        await automation_lifecycle.close()
        if startup_mail_task is not None and not startup_mail_task.done():
            startup_mail_task.cancel()
            await asyncio.gather(startup_mail_task, return_exceptions=True)
        if getattr(settings, "codex_runtime_enabled", False):
            await get_codex_bff_service().stop()
        if bridge_enabled:
            await browser_bridge_server.stop()


app = FastAPI(
    title="RecruitOps Agent API",
    version="0.1.0",
    description="Approval-gated recruitment operations agent API.",
    lifespan=app_lifespan,
)
app.include_router(browser_bridge_server.router())
app.include_router(local_ui_router)
app.include_router(schedule_items_router)
from apps.api.configuration import router as configuration_router
app.include_router(configuration_router)
from apps.api.resume_filler import router as resume_filler_router
app.include_router(resume_filler_router)
app.include_router(company_sources_router)


@lru_cache(maxsize=1)
def get_company_source_retry_service():
    from packages.discovery.company_registry import CompanySourceRegistry
    from packages.discovery.company_source_retry import CompanySourceRetryService

    return CompanySourceRetryService(
        CompanySourceRegistry(Storage.from_url(get_settings().database_url)),
        max_concurrency=2, timeout_seconds=75.0,
    )


def _start_company_source_retry(record_id: str):
    return get_company_source_retry_service().start_retry(record_id)


set_start_retry_callback(_start_company_source_retry)


def _is_same_origin_progress_get(request: Request) -> bool:
    origin = request.headers.get("origin")
    return (request.method == "GET"
            and request.url.path in {"/api/local-ui/daily-recruitment/progress", "/api/local-ui/tasks/progress"}
            and request.headers.get("x-recruitops-local-ui") == "1"
            and request.url.hostname in {"localhost", "127.0.0.1", "::1"}
            and request.headers.get("sec-fetch-site") == "same-origin"
            and (origin is None or (
                urlsplit(origin).scheme == request.url.scheme
                and urlsplit(origin).netloc == request.url.netloc
            )))


@app.middleware("http")
async def local_ui_boundary(request, call_next):
    trusted = is_local_ui(request) or _is_same_origin_progress_get(request)
    if request.headers.get("x-recruitops-local-ui") and not trusted:
        return JSONResponse({"detail": "Local same-origin UI request required"}, status_code=403)
    token = local_ui_request.set(trusted)
    try:
        response = await call_next(request)
        if request.url.path in {
            "/", "/index.html", "/app.js", "/company-sources.js", "/configuration.js", "/styles.css"
        }:
            # Revalidate HTML and its mutable assets together after deployment.
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response
    finally:
        local_ui_request.reset(token)
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
approval_registry = ApprovalRegistry(
    SqlAlchemyApprovalPersistence(Storage.from_url(get_settings().database_url))
)


@app.get("/", include_in_schema=False)
def web_index() -> FileResponse:
    """Serve the local dashboard without putting it ahead of API routes."""

    return FileResponse(WEB_ROOT / "index.html")




class CodexThreadStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cwd: str | None = None


class CodexTurnStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=40_000)
    job_id: str | None = Field(default=None, max_length=255)

    def prompt(self):
        if not self.job_id:
            return self.text
        context = {"job_id": self.job_id}
        return self.text + "\n\n[本轮页面上下文，仅当前选择有效；岗位内容请用 job_detail 核对]\n" + json.dumps(context, ensure_ascii=False)


class CodexTurnInterruptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1, max_length=256)


@app.get("/api/codex/health", tags=["codex"])
async def codex_health() -> dict[str, Any]:
    settings = get_settings()
    if not settings.codex_runtime_enabled:
        return {"enabled": False, "ready": False, "state": "disabled"}
    health = await get_codex_bff_service().health()
    return {
        "enabled": True,
        **health.model_dump(mode="json"),
        "context_management": {
            "context_window_tokens": getattr(settings, "codex_model_context_window", 1_000_000),
            "auto_compact_token_limit": getattr(
                settings,
                "codex_model_auto_compact_token_limit",
                96_000,
            ),
        },
    }


@app.get("/api/codex/mcp-status", tags=["codex"])
async def codex_mcp_status() -> dict[str, Any]:
    if not get_settings().codex_runtime_enabled:
        raise HTTPException(status_code=503, detail="Codex runtime is disabled")
    result = await get_codex_bff_service().mcp_status()
    return dict(result) if isinstance(result, Mapping) else {"data": result}


@app.get("/api/codex/traces", tags=["codex"])
def codex_traces(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
    settings = get_settings()
    path = settings.codex_trace_path
    if not path.is_absolute():
        path = settings.agent_root / path
    return [
        item.model_dump(mode="json")
        for item in CodexJsonlTraceRecorder(path).read(limit=limit)
    ]


def _is_unmaterialized_thread_error(error: JsonRpcRemoteError) -> bool:
    detail = f"{error.message} {error.data or ''}".casefold()
    return "not materialized" in detail or "includeturns is unavailable" in detail


def _is_unloaded_thread_error(error: JsonRpcRemoteError) -> bool:
    detail = f"{error.message} {error.data or ''}".casefold()
    return "thread not loaded" in detail


@app.post("/api/codex/threads", tags=["codex"])
async def codex_thread_start(request: CodexThreadStartRequest) -> dict[str, Any]:
    if not get_settings().codex_runtime_enabled:
        raise HTTPException(status_code=503, detail="Codex runtime is disabled")
    params = {"cwd": request.cwd} if request.cwd else {}
    thread = await get_codex_bff_service().thread_start(**params)
    return thread.model_dump(mode="json") if isinstance(thread, BaseModel) else dict(thread)


@app.get("/api/codex/threads", tags=["codex"])
async def codex_thread_list(
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=20, ge=1, le=100),
    archived: bool = False,
) -> dict[str, Any]:
    if not get_settings().codex_runtime_enabled:
        raise HTTPException(status_code=503, detail="Codex runtime is disabled")
    page = await get_codex_bff_service().thread_list(
        cursor=cursor,
        limit=limit,
        archived=archived,
    )
    return page.model_dump(mode="json") if isinstance(page, BaseModel) else dict(page)


@app.get("/api/codex/threads/{thread_id}", tags=["codex"])
async def codex_thread_read(
    thread_id: str,
    include_turns: bool = Query(default=True),
) -> dict[str, Any]:
    if not get_settings().codex_runtime_enabled:
        raise HTTPException(status_code=503, detail="Codex runtime is disabled")
    service = get_codex_bff_service()
    try:
        thread = await service.thread_read(thread_id, include_turns=include_turns)
    except JsonRpcRemoteError as error:
        if _is_unloaded_thread_error(error):
            await service.thread_resume(thread_id)
            try:
                thread = await service.thread_read(thread_id, include_turns=include_turns)
            except JsonRpcRemoteError as resumed_error:
                if not include_turns or not _is_unmaterialized_thread_error(resumed_error):
                    raise
                thread = await service.thread_read(thread_id, include_turns=False)
            return thread.model_dump(mode="json") if isinstance(thread, BaseModel) else dict(thread)
        # A newly created App Server thread has no materialized turn history yet.
        # Reading the thread itself is still valid, so retry without turns instead
        # of surfacing a transient 500 to the workbench.
        if not include_turns or not _is_unmaterialized_thread_error(error):
            raise
        thread = await service.thread_read(thread_id, include_turns=False)
    return thread.model_dump(mode="json") if isinstance(thread, BaseModel) else dict(thread)


@app.post("/api/codex/threads/{thread_id}/resume", tags=["codex"])
async def codex_thread_resume(thread_id: str) -> dict[str, Any]:
    if not get_settings().codex_runtime_enabled:
        raise HTTPException(status_code=503, detail="Codex runtime is disabled")
    thread = await get_codex_bff_service().thread_resume(thread_id)
    return thread.model_dump(mode="json") if isinstance(thread, BaseModel) else dict(thread)


@app.delete("/api/codex/threads/{thread_id}", tags=["codex"])
async def codex_thread_delete(thread_id: str) -> dict[str, str]:
    if not get_settings().codex_runtime_enabled:
        raise HTTPException(status_code=503, detail="Codex runtime is disabled")
    await get_codex_bff_service().thread_delete(thread_id)
    return {"status": "deleted", "thread_id": thread_id}


@app.post("/api/codex/threads/{thread_id}/turns", tags=["codex"])
async def codex_turn_start(thread_id: str, request: CodexTurnStartRequest) -> dict[str, Any]:
    if not get_settings().codex_runtime_enabled:
        raise HTTPException(status_code=503, detail="Codex runtime is disabled")
    turn = await get_codex_bff_service().turn_start(thread_id, request.prompt())
    return turn.model_dump(mode="json") if isinstance(turn, BaseModel) else dict(turn)


@app.post("/api/codex/threads/{thread_id}/turns/stream", tags=["codex"])
async def codex_turn_stream(
    thread_id: str,
    request: CodexTurnStartRequest,
) -> StreamingResponse:
    if not get_settings().codex_runtime_enabled:
        raise HTTPException(status_code=503, detail="Codex runtime is disabled")
    service = get_codex_bff_service()

    async def stream():
        subscription = service.subscribe(thread_id)
        try:
            turn = await service.turn_start(thread_id, request.prompt())
            turn_payload = (
                turn.model_dump(mode="json") if isinstance(turn, BaseModel) else dict(turn)
            )
            turn_id = str(turn_payload.get("id") or "")
            yield f"event: turn\ndata: {json.dumps(turn_payload, ensure_ascii=False)}\n\n"
            async for event in subscription:
                payload = event.model_dump(mode="json")
                yield (
                    f"event: {event.event_type.value}\n"
                    f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                )
                if event.event_type.value == "turn_completed" and (
                    not turn_id or event.turn_id == turn_id
                ):
                    break
        finally:
            subscription.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/codex/threads/{thread_id}/interrupt", tags=["codex"])
async def codex_turn_interrupt(
    thread_id: str,
    request: CodexTurnInterruptRequest,
) -> dict[str, str]:
    if not get_settings().codex_runtime_enabled:
        raise HTTPException(status_code=503, detail="Codex runtime is disabled")
    await get_codex_bff_service().turn_interrupt(thread_id, request.turn_id)
    return {"status": "interrupt_requested", "thread_id": thread_id, "turn_id": request.turn_id}


@app.get("/api/codex/threads/{thread_id}/events", tags=["codex"])
async def codex_thread_events(thread_id: str) -> StreamingResponse:
    if not get_settings().codex_runtime_enabled:
        raise HTTPException(status_code=503, detail="Codex runtime is disabled")

    async def stream():
        async for event in get_codex_bff_service().event_stream(thread_id):
            payload = event.model_dump(mode="json")
            yield f"event: {event.event_type.value}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class WriteExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    operator: str = Field(min_length=1, max_length=200)


class ApprovalQueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: ApprovalToken
    preview: ApprovalPreview


class ApplicationCaptureApiResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    message: str
    result: ApplicationCaptureResponse
    approval: ApprovalDecision | None = None


class RecruitmentMailReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_confidence: float = Field(default=0.65, ge=0.0, le=1.0)
    minimum_margin: float = Field(default=0.15, ge=0.0, le=1.0)


@app.get("/health", response_model=ServiceStatus, tags=["system"])
def health() -> ServiceStatus:
    return ServiceStatus(
        status="ok",
        mode="approval_gated" if get_settings().write_enabled else "read_only",
    )


class ReadinessStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    checks: dict[str, bool]
    mode: str


@lru_cache
def get_storage_engine():
    return create_storage_engine(get_settings().database_url)


@app.get("/api/local-ui/daily-recruitment/progress", tags=["local-ui"])
def daily_recruitment_progress(request: Request) -> dict[str, object]:
    # Same-origin GET normally has no Origin header, unlike local UI writes.
    if not _is_same_origin_progress_get(request):
        raise HTTPException(403, "Local same-origin UI request required")
    return latest_daily_progress(Storage(get_storage_engine()))


@app.get("/api/local-ui/tasks/progress", tags=["local-ui"])
def current_task_progress(request: Request, run_id: str | None = Query(default=None, min_length=8, max_length=128)) -> dict[str, object]:
    if not _is_same_origin_progress_get(request):
        raise HTTPException(403, "Local same-origin UI request required")
    # No history fallback: a new conversation never resurrects a finished card.
    return task_progress(Storage(get_storage_engine()), run_id=run_id)


class TaskControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_kind: Literal["daily", "application_review", "recruitment_mail"]
    action: Literal["pause", "cancel"]


@app.post("/api/local-ui/tasks/{run_id}/control", tags=["local-ui"])
async def control_current_task(run_id: str, payload: TaskControlRequest,
                               authorization: str | None = Header(default=None)):
    _require_local_api_token(authorization, require_configured=True)
    if not get_settings().write_enabled:
        raise HTTPException(503, "RECRUITOPS_WRITE_ENABLED must be true")
    try:
        if payload.task_kind == "application_review":
            from packages.tools.application_review_tasks import ApplicationReviewControlInput, control_application_review
            return await control_application_review(
                ApplicationReviewControlInput(run_id=run_id, action=payload.action), browser_bridge_store, repository())
        if payload.task_kind == "recruitment_mail":
            return mail_processing_run_service().control(run_id, payload.action)
        from packages.tools.task_runtime_control import request_daily_control
        return request_daily_control(Storage(get_storage_engine()), run_id, payload.action)
    except (KeyError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@lru_cache
def mail_processing_run_service():
    from packages.recruitment_mail.run_service import MailProcessingRunService
    return MailProcessingRunService(recruitment_mail_store(), repository(), get_settings(),
                                    sync_mail=_run_recruitment_mail_sync)


@lru_cache
def get_recruitment_mail_store() -> RecruitmentMailStore:
    return RecruitmentMailStore(Storage.from_url(get_settings().database_url))


def recruitment_mail_store() -> RecruitmentMailStore:
    return get_recruitment_mail_store()


def _independent_crawler_available() -> bool:
    try:
        module = importlib.import_module("packages.recruitment_core")
    except (ImportError, AttributeError):
        return False
    return bool(getattr(module, "CRAWLER_MAP", None))


@app.get("/ready", response_model=ReadinessStatus, tags=["system"])
def readiness(response: Response) -> ReadinessStatus:
    settings = get_settings()
    checks = {
        "companies_config": settings.companies_config.is_file(),
        "candidate_profile": settings.candidate_profile_config.is_file(),
        "crawler_import": _independent_crawler_available(),
        "postgres": False,
    }
    try:
        with get_storage_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["postgres"] = True
    except Exception:
        pass
    ready = all(checks.values())
    if not ready:
        response.status_code = 503
    return ReadinessStatus(
        status="ready" if ready else "not_ready",
        checks=checks,
        mode="approval_gated" if settings.write_enabled else "read_only",
    )


@lru_cache
def get_repository() -> PostgresRecruitmentRepository:
    return PostgresRecruitmentRepository(
        Storage.from_url(get_settings().database_url)
    )


def repository() -> PostgresRecruitmentRepository:
    return get_repository()


@app.get("/api/jobs", response_model=JobPage, tags=["jobs"])
def search_jobs(
    query: str | None = None,
    company: str | None = None,
    cohort: int | None = None,
    cohort_status: str | None = None,
    recruitment_track: str | None = None,
    first_seen_on: date | None = None,
    min_score: int | None = Query(default=None, ge=0, le=100),
    batches: list[RecruitmentBatch] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    repo: RecruitmentRepository = Depends(repository),
) -> JobPage:
    try:
        return repo.search_jobs(
            query=query,
            company=company,
            cohort=cohort,
            cohort_status=cohort_status,
            recruitment_track=recruitment_track,
            first_seen_on=first_seen_on,
            min_score=min_score,
            batches=tuple(batches) if batches else None,
            limit=limit,
            offset=offset,
        )
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/jobs/browse", response_model=JobBrowsePage, tags=["jobs"])
def browse_jobs(
    query: str | None = None,
    company: str | None = None,
    category: str | None = None,
    platform: str | None = None,
    evaluation: Literal["scored", "unscored", "pending", "jd_incomplete", "excluded"] | None = None,
    score_band: Literal["high", "medium", "low"] | None = None,
    first_seen_on: date | None = None,
    sort: Literal["score", "newest", "company"] = "score",
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    include_summary: bool = True,
    repo: RecruitmentRepository = Depends(repository),
) -> JobBrowsePage:
    """Browse confirmed 2027 campus jobs without transferring full JD text."""

    browse = getattr(repo, "browse_jobs", None)
    if not callable(browse):
        raise HTTPException(status_code=501, detail="Job browsing is unavailable")
    try:
        return browse(
            query=query,
            company=company,
            category=category,
            platform=platform,
            evaluation=evaluation,
            score_band=score_band,
            first_seen_on=first_seen_on,
            sort=sort,
            limit=limit,
            offset=offset,
            **({"include_summary": False} if not include_summary else {}),
        )
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}", response_model=JobDetail, tags=["jobs"])
def get_job(
    job_id: str,
    repo: RecruitmentRepository = Depends(repository),
) -> JobDetail:
    try:
        result = repo.get_job(job_id)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return result


@app.get("/api/companies", response_model=list[Company], tags=["companies"])
def list_companies(repo: RecruitmentRepository = Depends(repository)) -> list[Company]:
    try:
        return repo.list_companies()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/applications", response_model=list[Application], tags=["applications"])
def list_applications(repo: RecruitmentRepository = Depends(repository)) -> list[Application]:
    try:
        return repo.list_applications()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/applications/page", response_model=ApplicationPage, tags=["applications"])
def search_applications(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    query: str | None = Query(default=None, max_length=200),
    stage: ApplicationStage | None = None,
    stages: list[ApplicationStage] | None = Query(default=None),
    repo: RecruitmentRepository = Depends(repository),
) -> ApplicationPage:
    """Return a bounded application page without changing the legacy list endpoint."""

    try:
        if stage is not None and stages:
            raise HTTPException(status_code=422, detail="stage 与 stages 不能同时使用")
        filters = {}
        if query and query.strip():
            filters["query"] = query.strip()
        if stage is not None:
            filters["stage"] = stage.value
        if stages:
            filters["stages"] = tuple(value.value for value in stages)
        paged = getattr(repo, "search_applications", None)
        if callable(paged):
            return paged(limit=limit, offset=offset, **filters)
        items = repo.list_applications()
        unfiltered_total = len(items)
        if filters.get("query"):
            keyword = " ".join(filters["query"].split()).casefold()
            items = [item for item in items if keyword in " ".join(
                f"{item.company_name} {item.job_title}".split()).casefold()]
        stage_counts = {}
        for item in items:
            stage_counts[item.stage.value] = stage_counts.get(item.stage.value, 0) + 1
        allowed = filters.get("stages") or ((filters["stage"],) if "stage" in filters else ())
        if allowed:
            items = [item for item in items if item.stage.value in allowed]
        return ApplicationPage(
            items=items[offset : offset + limit],
            total=len(items),
            limit=limit,
            offset=offset,
            unfiltered_total=unfiltered_total,
            stage_counts=stage_counts,
        )
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/schedule", response_model=list[ScheduleEvent], tags=["schedule"])
def list_schedule(
    on: date | None = None,
    repo: RecruitmentRepository = Depends(repository),
) -> list[ScheduleEvent]:
    try:
        return repo.list_schedule(on)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/reports/operational", tags=["reporting"])
def operational_report(
    on: date | None = None,
    repo: RecruitmentRepository = Depends(repository),
) -> dict[str, Any]:
    """Return a read-only dashboard summary and actionable crawler observations."""

    local_day = on or date.today()
    try:
        companies = repo.list_companies()
        jobs = repo.search_jobs(
            cohort=2027,
            cohort_status="confirmed",
            first_seen_on=local_day,
            batches=(RecruitmentBatch.FORMAL, RecruitmentBatch.EARLY),
            limit=20,
            offset=0,
        )
        applications = repo.list_applications()
        schedule = repo.list_schedule(local_day)
        report = build_reporting_summary(repository=repo, companies=companies)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    health = report["crawler_health"]
    repair_guidance = {
        "crawler_failed": "检查入口、鉴权与站点结构后重新运行单公司验收",
        "zero_results": "核对校招活动是否开放，并验证筛选条件和分页",
        "pagination_incomplete": "检查翻页游标、总页数与停止条件",
        "quantity_change": "对比上次快照，确认岗位下线或解析回归",
    }
    candidates = [
        {
            **issue,
            "recommendation": repair_guidance.get(issue["type"], "人工复核该异常"),
            "approval_required_before_change": True,
        }
        for issue in health.get("issues", [])
    ]
    return {
        "date": local_day.isoformat(),
        "counts": {
            "confirmed_2027_new_jobs": jobs.total,
            "applications": len(applications),
            "schedule_events": len(schedule),
            "companies": len(companies),
            "crawler_issues": health.get("issue_count", 0),
        },
        "new_job_sample": [item.model_dump(mode="json") for item in jobs.items],
        "crawler_health": health,
        "repair_candidates": candidates,
        "safety": {
            "write_attempted": False,
            "model_call_attempted": False,
            "external_web_access_attempted": False,
        },
    }


def _require_local_api_token(
    authorization: str | None,
    *,
    require_configured: bool = False,
) -> None:
    if local_ui_request.get():
        return
    expected = get_settings().api_token.strip()
    if not expected:
        if require_configured:
            raise HTTPException(
                status_code=503,
                detail="RECRUITOPS_API_TOKEN must be configured before writes are enabled",
            )
        return
    supplied = authorization or ""
    scheme, _, value = supplied.partition(" ")
    if scheme.casefold() != "bearer" or not value or not compare_digest(value, expected):
        raise HTTPException(status_code=401, detail="Local API token is required")


def _automation_payload(
    store: AutomationStore,
    row: Any,
    *,
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    executions = store.executions(row.id, limit=1)
    latest = executions[0] if executions else None
    return {
        "id": row.id,
        "task_id": row.task_id,
        "task_label": row.task_label,
        "target_kind": row.target_kind,
        "target_id": row.target_id,
        "target_label": row.target_label,
        "frequency": row.frequency,
        "start_time": row.start_time.strftime("%H:%M"),
        "timezone": row.timezone_name,
        "active": bool(row.active),
        "runnable": bool(row.active and blocked_reason is None),
        "blocked_reason": blocked_reason,
        "next_run_at": row.next_run_at,
        "last_run_at": row.last_run_at,
        "last_status": row.last_status,
        "last_error": row.last_error,
        "latest_execution": (
            {
                "id": latest.id,
                "status": latest.status,
                "scheduled_for": latest.scheduled_for,
                "started_at": latest.started_at,
                "completed_at": latest.completed_at,
                "result_summary": latest.result_summary,
                "error": latest.error,
                "thread_id": latest.thread_id,
                    "turn_id": getattr(latest, "turn_id", None),
            }
            if latest is not None
            else None
        ),
    }


@app.get("/api/automations", tags=["automations"])
def list_local_automations(active_only: bool = False) -> dict[str, Any]:
    settings = get_settings()
    store = AutomationStore(Storage.from_url(settings.database_url))
    rows = store.list(active_only=active_only)
    worker_status = getattr(app.state, "automation_worker_status", "not_started")
    engine_blocked_code = automation_blocked_reason(None, settings)
    engine_blocked_reason = (
        automation_blocked_message(engine_blocked_code)
        if engine_blocked_code is not None
        else (None if worker_status == "running" else f"调度 worker 状态：{worker_status}")
    )
    items = []
    for row in rows:
        task_blocked_code = automation_blocked_reason(row.task_id, settings)
        blocked_reason = (
            automation_blocked_message(task_blocked_code)
            if task_blocked_code is not None
            else (
                f"调度 worker 状态：{worker_status}"
                if row.active and worker_status != "running"
                else None
            )
        )
        items.append(_automation_payload(store, row, blocked_reason=blocked_reason))
    return {
        "items": items,
        "total": len(rows),
        "engine": {
            "enabled": engine_blocked_reason is None,
            "status": worker_status,
            "blocked_reason": engine_blocked_reason,
        },
    }


@app.post("/api/automations/{schedule_id}/disable", tags=["automations"])
def disable_local_automation(
    schedule_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    settings = get_settings()
    if not settings.write_enabled:
        raise HTTPException(status_code=503, detail="RECRUITOPS_WRITE_ENABLED must be true")
    _require_local_api_token(authorization, require_configured=True)
    store = AutomationStore(Storage.from_url(settings.database_url))
    row = store.disable(schedule_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Automation schedule was not found")
    return _automation_payload(store, row)


def _mail_data_or_error(result: Any, *, not_found_status: int = 503) -> Any:
    from packages.tools.typed import ToolErrorCode

    if result.data is not None:
        return result.data
    status_code = (
        422 if result.error_code in {ToolErrorCode.INVALID_SOURCE, ToolErrorCode.INVALID_INPUT}
        else not_found_status
    )
    raise HTTPException(
        status_code=status_code,
        detail=result.error_message or "Recruitment mail operation failed",
    )


@app.get(
    "/api/recruitment-mails",
    response_model=RecruitmentMailSearchData,
    tags=["recruitment-mail"],
)
def list_recruitment_mails(
    refresh: bool = True,
    on_date: date | None = None,
    category: RecruitmentMessageCategory | None = None,
    processing_status: RecruitmentMailProcessingStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    store: RecruitmentMailStore = Depends(recruitment_mail_store),
) -> RecruitmentMailSearchData:
    from packages.recruitment_mail.freshness import ensure_mail_fresh

    freshness = ensure_mail_fresh(get_settings(), store) if refresh else {"status": "cached", "synced_at": None}
    result = search_recruitment_mail(
        RecruitmentMailSearchInput(
            on_date=on_date,
            category=category,
            processing_status=processing_status,
            limit=limit,
            offset=offset,
        ),
        store,
    )
    return _mail_data_or_error(result).model_copy(update={"freshness": freshness})


@app.post("/api/recruitment-mails/sync", tags=["recruitment-mail"])
def sync_recruitment_mails(
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """Refresh the local mail cache without changing application progress."""

    settings = get_settings()
    if not getattr(settings, "mail_enabled", False):
        raise HTTPException(status_code=503, detail="RECRUITOPS_MAIL_ENABLED is false")
    try:
        result = _run_recruitment_mail_sync(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"mail_sync_failed:{type(exc).__name__}") from exc
    return result


@app.get(
    "/api/recruitment-mails/{record_id}",
    response_model=RecruitmentMailDetailData,
    tags=["recruitment-mail"],
)
def get_recruitment_mail_detail(
    record_id: str,
    refresh: bool = True,
    store: RecruitmentMailStore = Depends(recruitment_mail_store),
) -> RecruitmentMailDetailData:
    from packages.recruitment_mail.freshness import ensure_mail_fresh

    freshness = ensure_mail_fresh(get_settings(), store) if refresh else {"status": "cached", "synced_at": None}
    result = get_recruitment_mail(
        RecruitmentMailDetailInput(record_id=record_id),
        store,
    )
    return _mail_data_or_error(result, not_found_status=404).model_copy(update={"freshness": freshness})


@app.get("/api/recruitment-mails/{record_id}/binding-candidates", tags=["recruitment-mail"])
def mail_binding_candidates(record_id: str, query: str = Query(default="", max_length=500),
                            store: RecruitmentMailStore = Depends(recruitment_mail_store)):
    try:
        return recruitment_mail_binding_candidates(
            RecruitmentMailBindingCandidatesInput(record_id=record_id, query=query), store).data
    except KeyError as exc:
        raise HTTPException(404, "邮件不存在") from exc


@app.post("/api/recruitment-mails/{record_id}/binding-proposals", tags=["recruitment-mail"])
def propose_mail_binding(record_id: str, payload: RecruitmentMailBindingProposeInput,
                          authorization: str | None = Header(default=None),
                          store: RecruitmentMailStore = Depends(recruitment_mail_store)):
    _require_local_api_token(authorization, require_configured=True)
    if not get_settings().write_enabled:
        raise HTTPException(503, "RECRUITOPS_WRITE_ENABLED must be true")
    if record_id != payload.record_id:
        raise HTTPException(422, "邮件范围不一致")
    try:
        return recruitment_mail_binding_propose(payload, store, approval_registry)
    except (KeyError, ValueError) as exc:
        raise HTTPException(409, "邮件或投递记录已变化，请刷新后重新确认") from exc


@app.post(
    "/api/recruitment-mails/{record_id}/review",
    response_model=RecruitmentMailReviewData,
    tags=["recruitment-mail"],
)
def preview_recruitment_mail_review(
    record_id: str,
    request: RecruitmentMailReviewRequest,
    authorization: str | None = Header(default=None),
    store: RecruitmentMailStore = Depends(recruitment_mail_store),
    repo: RecruitmentRepository = Depends(repository),
) -> RecruitmentMailReviewData:
    """Return approval previews only; this endpoint never registers or executes them."""

    _require_local_api_token(authorization)
    from packages.recruitment_mail.freshness import ensure_mail_fresh

    freshness = ensure_mail_fresh(get_settings(), store)
    try:
        result = review_recruitment_mail(
            RecruitmentMailReviewInput(
                record_id=record_id,
                minimum_confidence=request.minimum_confidence,
                minimum_margin=request.minimum_margin,
            ),
            store,
            repo,
        )
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _mail_data_or_error(result, not_found_status=404).model_copy(update={"freshness": freshness})


@app.post(
    "/api/browser/observations",
    response_model=BrowserObservationResponse,
    tags=["browser"],
)
def submit_browser_observation(
    request: BrowserObservationInput,
    authorization: str | None = Header(default=None),
) -> BrowserObservationResponse:
    """Accept one user-authorized, sanitized observation from the local extension."""

    _require_local_api_token(authorization)
    return observe_browser_page(request)


@app.post(
    "/api/browser/vision",
    response_model=VisionResult,
    tags=["browser"],
)
def recognize_browser_screenshot(
    request: BrowserVisionRequest,
    authorization: str | None = Header(default=None),
) -> VisionResult:
    """Send one authorized screenshot to the vision model, without changing application status."""

    _require_local_api_token(authorization, require_configured=True)
    if not get_settings().vision_enabled:
        raise HTTPException(status_code=503, detail="Screenshot understanding is disabled.")
    try:
        return analyze_observation(
            browser_bridge_store, browser_vision_service(),
            operation_id=request.operation_id, page_url=request.page_url,
            image_data_url=request.image_data_url,
        )
    except VisionError as exc:
        unavailable = exc.code in {"vision_not_configured", "vision_model_unsupported", "transport_failed"}
        raise HTTPException(status_code=503 if unavailable else 422, detail={"code": exc.code}) from exc


@app.post(
    "/api/browser/application-captures",
    response_model=ApplicationCaptureApiResponse,
    tags=["browser"],
)
def prepare_browser_application_capture(
    request: ApplicationCaptureInput,
    authorization: str | None = Header(default=None),
    repo: RecruitmentRepository = Depends(repository),
) -> ApplicationCaptureApiResponse:
    """Match the current job page and enqueue one application-create preview."""

    _require_local_api_token(authorization, require_configured=True)
    try:
        result = prepare_application_capture(request, repo)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    data = result.data
    if data is None:
        raise HTTPException(status_code=500, detail="Application capture returned no data")
    approval = None
    if data.approval_preview is not None:
        try:
            approval = _issue_or_reuse_approval(data.approval_preview)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApplicationCaptureApiResponse(
        status=data.capture_status,
        message=data.message,
        result=result,
        approval=approval,
    )


def _same_approval_binding(left: ApprovalPreview, right: ApprovalPreview) -> bool:
    fields = (
        "task_id",
        "operation",
        "idempotency_key",
        "evidence_summary",
        "target_id",
        "payload",
        "before",
        "after",
        "current_stage",
        "target_stage",
    )
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def _decision_for_existing_token(token: ApprovalToken) -> ApprovalDecision:
    if token.status is ApprovalStatus.REJECTED:
        return ApprovalDecision(
            allowed=False,
            status=token.status,
            reason="The existing approval was rejected.",
            error_code=PolicyErrorCode.TOKEN_REJECTED,
            token=token,
        )
    if token.status is ApprovalStatus.EXPIRED:
        return ApprovalDecision(
            allowed=False,
            status=token.status,
            reason="The existing approval expired.",
            error_code=PolicyErrorCode.TOKEN_EXPIRED,
            token=token,
        )
    return ApprovalDecision(
        allowed=True,
        status=token.status,
        reason="The existing approval capability was reused idempotently.",
        token=token,
    )


def _issue_or_reuse_approval(preview: ApprovalPreview) -> ApprovalDecision:
    for token, existing_preview in approval_registry.queue():
        if token.idempotency_key != preview.idempotency_key:
            continue
        if not _same_approval_binding(existing_preview, preview):
            raise ValueError("approval idempotency key conflicts with another preview")
        return _decision_for_existing_token(token)
    return approval_registry.issue(preview)


@app.post("/api/approvals", response_model=ApprovalDecision, tags=["approvals"])
def create_approval(
    preview: ApprovalPreview,
    authorization: str | None = Header(default=None),
) -> ApprovalDecision:
    """Validate and register a write preview without executing it."""

    _require_local_api_token(authorization, require_configured=True)
    return approval_registry.issue(preview)


@app.get("/api/approvals", response_model=list[ApprovalQueueItem], tags=["approvals"])
def list_approvals() -> list[ApprovalQueueItem]:
    return [
        ApprovalQueueItem(token=token, preview=preview)
        for token, preview in approval_registry.queue()
    ]


@app.post(
    "/api/approvals/{token_id}/approve",
    response_model=ApprovalDecision,
    tags=["approvals"],
)
def approve_approval(
    token_id: str,
    authorization: str | None = Header(default=None),
) -> ApprovalDecision:
    _require_local_api_token(authorization, require_configured=True)
    try:
        return approval_registry.approve(token_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/approvals/{token_id}/reject",
    response_model=ApprovalDecision,
    tags=["approvals"],
)
def reject_approval(
    token_id: str,
    authorization: str | None = Header(default=None),
) -> ApprovalDecision:
    _require_local_api_token(authorization, require_configured=True)
    try:
        return approval_registry.reject(token_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@lru_cache
def get_write_executor() -> ApprovedWriteExecutor:
    settings = get_settings()
    audit_store = AgentStateStore(
        Storage.from_url(settings.database_url, initialize=True)
    )
    return ApprovedWriteExecutor(
        approval_registry,
        AgentApplicationWriteAdapter(
            Storage.from_url(settings.database_url, initialize=True),
            settings.agent_root / "packages" / "recruitment_core" / "data" / "crawler_recipes.json",
        ),
        audit_sink=audit_store.save_write_audit,
    )


@app.post(
    "/api/approvals/{token_id}/execute",
    response_model=WriteAuditRecord,
    tags=["approvals"],
)
def execute_approval(
    token_id: str,
    request: WriteExecutionRequest,
    authorization: str | None = Header(default=None),
) -> WriteAuditRecord:
    """Execute one exact approved write after local bearer authentication."""

    if not get_settings().write_enabled:
        raise HTTPException(
            status_code=503,
            detail="RECRUITOPS_WRITE_ENABLED must be true before writes are enabled",
        )
    _require_local_api_token(authorization, require_configured=True)
    try:
        return get_write_executor().execute(token_id, operator=request.operator)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


if WEB_ROOT.is_dir():
    # Mount after every API route so explicit /api/* routes keep precedence.
    app.mount("/", StaticFiles(directory=WEB_ROOT, html=True), name="web")
