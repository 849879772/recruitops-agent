"""Protocol-level MCP registration for typed recruitment and browser tools.

The MCP SDK is optional here.  ``register_read_only_tools`` only needs a
FastMCP-compatible object exposing ``tool(name=..., description=...)`` and is
therefore easy to test without importing an SDK.  ``create_fastmcp_server``
is the convenience entry point when the official SDK is installed.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from packages.browser_bridge import BrowserBridgeStore
from packages.automation import AutomationStore
from packages.rag import EvidenceGrounder
from packages.config import get_settings
from packages.recruitment_mail import (
    RecruitmentMailStore,
)
from packages.repositories.base import RecruitmentRepository
from packages.scheduler.runner import LocalTaskScheduler
from packages.tools.application_capture import (
    ApplicationCaptureInput,
    ApplicationCaptureResponse,
    prepare_application_capture,
)
from packages.tools.application_review import (
    ApplicationStatusReviewInput,
    ApplicationStatusReviewResponse,
    application_status_review,
)
from packages.tools.browser import (
    BrowserObservationInput,
    BrowserObservationResponse,
    observe_browser_page,
)
from packages.tools.browser_bridge import (
    BrowserOperationStatusInput,
    BrowserOperationStatusResponse,
    CancelBrowserOperationInput,
    CancelBrowserOperationResponse,
    ConnectionStatusProvider,
    EdgeConnectionStatusInput,
    EdgeConnectionStatusResponse,
    ObserveApplicationStatusPageInput,
    ObserveApplicationStatusPageResponse,
    ReviewAndUpdateApplicationStatusInput,
    ReviewAndUpdateApplicationStatusResponse,
    browser_operation_status,
    cancel_browser_operation,
    edge_connection_status,
    observe_application_status_page_workflow,
    review_and_update_application_status,
    review_and_update_application_status_workflow,
)
from packages.tools.application_status_evidence import (
    VerifyApplicationStatusEvidenceInput,
    VerifyApplicationStatusEvidenceResponse,
    verify_application_status_evidence,
)
from packages.tools.batch_browser_operations import (
    BatchObserveApplicationStatusInput,
    BatchObserveApplicationStatusResponse,
    batch_observe_application_status,
)
from packages.tools.crawler_audit import (
    CrawlerAcceptanceInput,
    CrawlerAcceptanceResponse,
    accept_crawler_run,
)
from packages.tools.crawler_run import (
    ConfiguredCrawlerRunInput,
    ConfiguredCrawlerRunResponse,
    ConfiguredCrawlerRunner,
)
from packages.tools.daily_sync import (
    DailyRecruitmentSyncInput,
    DailyRecruitmentSyncResponse,
    DailyRecruitmentSyncStatusInput,
    DailyRecruitmentSyncStatusResponse,
    get_daily_recruitment_sync_status,
    run_daily_recruitment_sync,
)
from packages.discovery.offerbiu_refresh import OfferBiuRefreshService
from packages.tools.offerbiu_refresh import (
    OfferBiuSourceRefreshInput,
    OfferBiuSourceRefreshResponse,
    refresh_offerbiu_sources,
)
from packages.tools.knowledge import (
    KnowledgeSearchInput,
    KnowledgeSearchResponse,
    search_knowledge,
)
from packages.tools.oc_candidates import (
    OcCandidateRunner,
)
from packages.tools.public_entry_discovery import (
    PublicEntryDiscoveryInput,
    PublicEntryDiscoveryResponse,
    PublicEntryValidationInput,
    PublicEntryValidationResponse,
    discover_public_recruitment_entries,
    validate_public_recruitment_entry,
)
from packages.tools.application_status_update import (
    ApplicationStatusUpdateInput,
    ApplicationStatusUpdateResponse,
    update_application_status,
)
from packages.tools.operations import (
    AutomationScheduleDisableInput,
    AutomationScheduleDisableResponse,
    AutomationScheduleInput,
    AutomationScheduleListInput,
    AutomationScheduleListResponse,
    AutomationScheduleResponse,
    AutomationPlanInput,
    AutomationPlanResponse,
    OperationalTaskRunInput,
    OperationalTaskRunResponse,
    OperationalTaskRunner,
    activate_automation,
    disable_automation,
    list_automations,
    plan_automation,
)
from packages.tools.schedule_ops import (
    ScheduleWindowInput,
    ScheduleWindowResponse,
    inspect_schedule_window,
)
from packages.tools.schedule_manage import (
    ScheduleManageInput,
    ScheduleManageResponse,
    schedule_manage,
)
from packages.tools.application_edit import ApplicationEditInput, ApplicationEditResponse, edit_application_metadata
from packages.tools.typed import (
    ApplicationQueryInput,
    ApplicationQueryResponse,
    CompanyCoverageInput,
    CompanyCoverageResponse,
    CapabilitiesInput,
    CapabilitiesResponse,
    EvidenceSource,
    JobDetailInput,
    JobDetailResponse,
    JobSearchInput,
    JobSearchResponse,
    ToolErrorCode,
    ToolStatus,
    TodayScheduleInput,
    TodayScheduleResponse,
    application_query,
    company_coverage,
    describe_capabilities,
    job_detail,
    search_jobs,
    today_schedule,
)
from packages.tools.recruitment_mail import (
    RecruitmentMailDetailInput,
    RecruitmentMailDetailResponse,
    RecruitmentMailReviewInput,
    RecruitmentMailReviewResponse,
    RecruitmentMailSearchInput,
    RecruitmentMailSearchResponse,
    RecruitmentMailSyncInput,
    RecruitmentMailSyncResponse,
    get_recruitment_mail,
    review_recruitment_mail,
    search_recruitment_mail,
)
from packages.tools.mail_processing import (
    RecruitmentMailProcessInput,
    RecruitmentMailProcessResponse,
    RecruitmentMailProcessingStatusInput,
    RecruitmentMailProcessingStatusResponse,
    recruitment_mail_process,
    recruitment_mail_processing_status,
)


class MCPToolRegistrar(Protocol):
    """The small server surface required by this adapter."""

    def tool(
        self,
        *,
        name: str,
        description: str,
        annotations: Any | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]: ...


class MCPUnavailableError(RuntimeError):
    """Raised when the optional MCP SDK is requested but not installed."""


class MCPOperationRunResponse(OperationalTaskRunResponse):
    """MCP boundary response for the side-effectful local operation runner."""

    read_only: Literal[False] = False


@dataclass(frozen=True)
class MCPToolDependencies:
    """Explicit local dependencies available to an MCP tool invocation."""

    repository: RecruitmentRepository
    mail_store: RecruitmentMailStore
    browser_bridge: BrowserBridgeStore | None = None
    connection_status_provider: ConnectionStatusProvider | None = None
    evidence_grounder: EvidenceGrounder | None = None
    crawler_runner: ConfiguredCrawlerRunner | None = None
    oc_candidate_runner: OcCandidateRunner | None = None
    operational_task_runner: OperationalTaskRunner | None = None
    offerbiu_refresher: OfferBiuRefreshService | None = None
    automation_scheduler: LocalTaskScheduler | None = None
    automation_store: AutomationStore | None = None

    @property
    def browser_bridge_store(self) -> BrowserBridgeStore | None:
        """Compatibility name for callers that use the concrete store name."""

        return self.browser_bridge


@dataclass(frozen=True)
class MCPToolDefinition:
    """A typed tool plus the models used at the protocol boundary."""

    name: str
    description: str
    input_model: type[BaseModel]
    response_model: type[BaseModel]
    operation: Callable[[Any, MCPToolDependencies], BaseModel | Awaitable[BaseModel]]
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    open_world: bool = False


MCP_TOOL_PROTOCOL_VERSION = "24"


MCP_READ_ONLY_TOOL_NAMES: tuple[str, ...] = (
    "capabilities",
    "today_schedule",
    "search_jobs",
    "job_detail",
    "company_coverage",
    "application_query",
    "application_status_review",
    "browser_observation",
    "crawler_acceptance",
    "recruitment_mail_search",
    "recruitment_mail_detail",
    "recruitment_mail_review",
    "recruitment_mail_processing_status",
    "schedule_window",
    "edge_connection_status",
    "browser_operation_status",
    "knowledge_search",
    "configured_crawler_run",
    "public_recruitment_entry_discovery",
    "public_recruitment_entry_validate",
    "automation_plan",
    "automation_schedule_list",
    "application_capture",
    "daily_recruitment_sync_status",
)

MCP_ACTION_TOOL_NAMES: tuple[str, ...] = (
    "application_edit",
    "recruitment_mail_process",
    "recruitment_mail_sync",
    "application_status_update",
    "schedule_manage",
    "observe_application_status_page",
    "batch_observe_application_status",
    "verify_application_status_evidence",
    "cancel_browser_operation",
    "operation_run",
    "daily_recruitment_sync",
    "offerbiu_source_refresh",
    "automation_schedule",
    "automation_schedule_disable",
)

MCP_TOOL_NAMES: tuple[str, ...] = (
    "capabilities",
    "today_schedule",
    "search_jobs",
    "job_detail",
    "company_coverage",
    "application_query",
    "application_status_review",
    "browser_observation",
    "crawler_acceptance",
    "recruitment_mail_search",
    "recruitment_mail_detail",
    "recruitment_mail_review",
    "recruitment_mail_processing_status",
    "recruitment_mail_process",
    "recruitment_mail_sync",
    "application_status_update",
    "schedule_window",
    "application_edit",
    "schedule_manage",
    "edge_connection_status",
    "observe_application_status_page",
    "batch_observe_application_status",
    "verify_application_status_evidence",
    "browser_operation_status",
    "cancel_browser_operation",
    "knowledge_search",
    "configured_crawler_run",
    "public_recruitment_entry_discovery",
    "public_recruitment_entry_validate",
    "automation_plan",
    "automation_schedule",
    "automation_schedule_list",
    "automation_schedule_disable",
    "application_capture",
    "operation_run",
    "daily_recruitment_sync",
    "offerbiu_source_refresh",
    "daily_recruitment_sync_status",
)

# The Codex App Server uses this deliberately smaller surface. Low-level audit,
# diagnostic and administrative tools remain available through the full profile.
# The read-only operation status tool stays visible because durable browser tasks
# require the Agent to follow its own work to a terminal state.
MCP_AGENT_TOOL_NAMES: tuple[str, ...] = (
    "capabilities",
    "today_schedule",
    "search_jobs",
    "job_detail",
    "company_coverage",
    "application_query",
    "recruitment_mail_search",
    "recruitment_mail_detail",
    "recruitment_mail_review",
    "recruitment_mail_processing_status",
    "recruitment_mail_process",
    "recruitment_mail_sync",
    "application_status_update",
    "schedule_window",
    "application_edit",
    "schedule_manage",
    "observe_application_status_page",
    "batch_observe_application_status",
    "browser_operation_status",
    "verify_application_status_evidence",
    "knowledge_search",
    "configured_crawler_run",
    "public_recruitment_entry_discovery",
    "public_recruitment_entry_validate",
    "automation_schedule",
    "automation_schedule_list",
    "automation_schedule_disable",
    "daily_recruitment_sync",
    "offerbiu_source_refresh",
    "daily_recruitment_sync_status",
)


def _repository_operation(
    operation: Callable[[Any, RecruitmentRepository], BaseModel],
) -> Callable[[Any, MCPToolDependencies], BaseModel]:
    def bound(request: Any, dependencies: MCPToolDependencies) -> BaseModel:
        return operation(request, dependencies.repository)

    return bound


def _mail_store_operation(
    operation: Callable[[Any, RecruitmentMailStore], BaseModel],
) -> Callable[[Any, MCPToolDependencies], BaseModel]:
    def bound(request: Any, dependencies: MCPToolDependencies) -> BaseModel:
        freshness = _sync_mail_before_read(dependencies)
        return operation(request, dependencies.mail_store).model_copy(update={"freshness": freshness})

    return bound


def _mail_review_operation(
    operation: Callable[[Any, RecruitmentMailStore, RecruitmentRepository], BaseModel],
) -> Callable[[Any, MCPToolDependencies], BaseModel]:
    def bound(request: Any, dependencies: MCPToolDependencies) -> BaseModel:
        freshness = _sync_mail_before_read(dependencies)
        return operation(request, dependencies.mail_store, dependencies.repository).model_copy(
            update={"freshness": freshness}
        )

    return bound


def _sync_mail_before_read(
    dependencies: MCPToolDependencies,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    """Refresh local mail state before every mail read/review invocation."""

    from packages.recruitment_mail.freshness import ensure_mail_fresh

    settings = get_settings()
    if not bool(getattr(settings, "write_enabled", False)):
        return {"status": "disabled", "reason": "write_disabled", "sync": {}}
    return ensure_mail_fresh(settings, dependencies.mail_store, limit=limit)


def _recruitment_mail_process_operation(
    request: RecruitmentMailProcessInput,
    dependencies: MCPToolDependencies,
) -> RecruitmentMailProcessResponse:
    settings = get_settings()
    # Keep the write guard ahead of the explicit refresh. A permitted process
    # pass refreshes the mailbox exactly once, then delegates all triage,
    # analysis, matching, and guarded writes to the parent service.
    if not bool(getattr(settings, "write_enabled", False)):
        return recruitment_mail_process(
            request,
            dependencies.mail_store,
            dependencies.repository,
            settings=settings,
        )
    freshness = _sync_mail_before_read(dependencies, limit=request.limit)
    response = recruitment_mail_process(
        request,
        dependencies.mail_store,
        dependencies.repository,
        settings=settings,
    )
    return response.model_copy(update={"freshness": freshness})


def _recruitment_mail_processing_status_operation(
    request: RecruitmentMailProcessingStatusInput,
    dependencies: MCPToolDependencies,
) -> RecruitmentMailProcessingStatusResponse:
    # This path intentionally does not call _sync_mail_before_read.
    return recruitment_mail_processing_status(request, dependencies.mail_store)


def _mail_sync_operation(
    request: RecruitmentMailSyncInput,
    dependencies: MCPToolDependencies,
) -> RecruitmentMailSyncResponse:
    from time import perf_counter

    started = perf_counter()
    result = _sync_mail_before_read(dependencies, limit=request.limit)
    if result.get("status") not in {"synced", "cached"}:
        return RecruitmentMailSyncResponse(
            tool_name="recruitment_mail_sync",
            status=ToolStatus.FAILURE,
            success=False,
            data={"sync": result.get("sync") or {}, "freshness": result},
            evidence=[EvidenceSource(source="imap_readonly")],
            error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
            error_message="Mailbox synchronization was unavailable.",
            timeout_ms=request.timeout_ms,
            elapsed_ms=max(0, int((perf_counter() - started) * 1000)),
            read_only=False,
        )
    return RecruitmentMailSyncResponse(
        tool_name="recruitment_mail_sync",
        status=ToolStatus.SUCCESS,
        success=True,
        data={"sync": result.get("sync") or {}, "freshness": result},
        evidence=[EvidenceSource(source="imap_readonly")],
        timeout_ms=request.timeout_ms,
        elapsed_ms=max(0, int((perf_counter() - started) * 1000)),
        read_only=False,
    )


def _application_status_update_operation(
    request: ApplicationStatusUpdateInput,
    dependencies: MCPToolDependencies,
) -> ApplicationStatusUpdateResponse:
    return update_application_status(
        request, dependencies.repository, dependencies.mail_store,
        dependencies.browser_bridge, settings=get_settings(),
    )


def _schedule_manage_operation(
    request: ScheduleManageInput,
    dependencies: MCPToolDependencies,
) -> ScheduleManageResponse:
    storage = getattr(dependencies.repository, "storage", None)
    if storage is None:
        return _missing_dependency_response(
            request,
            ScheduleManageResponse,
            tool_name="schedule_manage",
            dependency_name="repository.storage",
            read_only=False,
        )
    return schedule_manage(
        request,
        storage,
        write_enabled=bool(getattr(get_settings(), "write_enabled", False)),
    )


def _application_edit_operation(request, dependencies):
    return edit_application_metadata(request, getattr(dependencies.repository, "storage", None),
                                     write_enabled=bool(get_settings().write_enabled))


def _browser_store_operation(
    operation: Callable[[Any, BrowserBridgeStore | None], BaseModel],
) -> Callable[[Any, MCPToolDependencies], BaseModel]:
    def bound(request: Any, dependencies: MCPToolDependencies) -> BaseModel:
        return operation(request, dependencies.browser_bridge)

    return bound


def _edge_connection_operation(
    request: Any,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    return edge_connection_status(
        request,
        dependencies.browser_bridge,
        dependencies.connection_status_provider,
    )


def _missing_dependency_response(
    request: Any,
    response_model: type[BaseModel],
    *,
    tool_name: str,
    dependency_name: str,
    read_only: bool,
) -> BaseModel:
    return response_model.model_validate(
        {
            "tool_name": tool_name,
            "status": ToolStatus.FAILURE,
            "success": False,
            "data": None,
            "evidence": [
                EvidenceSource(
                    source="mcp_dependencies",
                    source_ref=dependency_name,
                )
            ],
            "error_code": ToolErrorCode.SOURCE_UNAVAILABLE,
            "error_message": (
                f"{dependency_name} is not configured for MCP tool {tool_name}."
            ),
            "timeout_ms": request.timeout_ms,
            "timed_out": False,
            "elapsed_ms": 0,
            "read_only": read_only,
        }
    )


def _knowledge_search_operation(
    request: Any,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    if dependencies.evidence_grounder is None:
        return _missing_dependency_response(
            request,
            KnowledgeSearchResponse,
            tool_name="knowledge_search",
            dependency_name="evidence_grounder",
            read_only=True,
        )
    return search_knowledge(request, dependencies.evidence_grounder)


def _configured_crawler_run_operation(
    request: Any,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    if dependencies.crawler_runner is None:
        return _missing_dependency_response(
            request,
            ConfiguredCrawlerRunResponse,
            tool_name="configured_crawler_run",
            dependency_name="crawler_runner",
            read_only=True,
        )
    return dependencies.crawler_runner.run(request)


def _operation_run_response(result: Any) -> MCPOperationRunResponse:
    typed = OperationalTaskRunResponse.model_validate(result)
    payload = typed.model_dump()
    payload["read_only"] = False
    return MCPOperationRunResponse.model_validate(payload)


def _operation_run_operation(
    request: Any,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    if dependencies.operational_task_runner is None:
        return _missing_dependency_response(
            request,
            MCPOperationRunResponse,
            tool_name="operation_run",
            dependency_name="operational_task_runner",
            read_only=False,
        )
    return _operation_run_response(dependencies.operational_task_runner.run(request))


def _daily_recruitment_sync_operation(
    request: Any,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    if dependencies.operational_task_runner is None:
        return _missing_dependency_response(
            request,
            DailyRecruitmentSyncResponse,
            tool_name="daily_recruitment_sync",
            dependency_name="operational_task_runner",
            read_only=False,
        )
    return run_daily_recruitment_sync(request, dependencies.operational_task_runner)


def _offerbiu_source_refresh_operation(
    request: Any,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    if dependencies.offerbiu_refresher is None:
        return _missing_dependency_response(
            request,
            OfferBiuSourceRefreshResponse,
            tool_name="offerbiu_source_refresh",
            dependency_name="offerbiu_refresher",
            read_only=False,
        )
    return refresh_offerbiu_sources(request, dependencies.offerbiu_refresher)


def _daily_recruitment_sync_status_operation(
    request: Any,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    if dependencies.operational_task_runner is None:
        return _missing_dependency_response(
            request,
            DailyRecruitmentSyncStatusResponse,
            tool_name="daily_recruitment_sync_status",
            dependency_name="operational_task_runner",
        )
    return get_daily_recruitment_sync_status(request, dependencies.operational_task_runner)


def _automation_plan_operation(
    request: Any,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    if dependencies.automation_scheduler is None:
        return _missing_dependency_response(
            request,
            AutomationPlanResponse,
            tool_name="automation_plan",
            dependency_name="automation_scheduler",
            read_only=True,
        )
    return plan_automation(request, dependencies.automation_scheduler)


def _automation_schedule_operation(
    request: AutomationScheduleInput,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    if dependencies.automation_scheduler is None or dependencies.automation_store is None:
        return _missing_dependency_response(
            request,
            AutomationScheduleResponse,
            tool_name="automation_schedule",
            dependency_name="automation_store",
            read_only=False,
        )
    target_label = None
    if request.application_id is not None:
        application = next(
            (
                item
                for item in dependencies.repository.list_applications()
                if item.id == request.application_id
            ),
            None,
        )
        if application is None:
            return AutomationScheduleResponse(
                tool_name="automation_schedule",
                status=ToolStatus.NO_RESULTS,
                success=False,
                evidence=[
                    EvidenceSource(
                        source="agent_postgres",
                        source_ref=f"application:{request.application_id}",
                    )
                ],
                error_code=ToolErrorCode.NOT_FOUND,
                error_message="Application record was not found; the schedule was not created.",
                timeout_ms=request.timeout_ms,
                elapsed_ms=0,
            )
        if request.task_id != "application_progress":
            return AutomationScheduleResponse(
                tool_name="automation_schedule",
                status=ToolStatus.FAILURE,
                success=False,
                evidence=[EvidenceSource(source="agent_scheduler_catalog")],
                error_code=ToolErrorCode.INVALID_INPUT,
                error_message="application_id is only valid for application_progress.",
                timeout_ms=request.timeout_ms,
                elapsed_ms=0,
            )
        target_label = f"{application.company_name} / {application.job_title}"
    return activate_automation(
        request,
        dependencies.automation_scheduler,
        dependencies.automation_store,
        target_label=target_label,
    )


def _automation_schedule_list_operation(
    request: AutomationScheduleListInput,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    if dependencies.automation_store is None:
        return _missing_dependency_response(
            request,
            AutomationScheduleListResponse,
            tool_name="automation_schedule_list",
            dependency_name="automation_store",
            read_only=True,
        )
    return list_automations(request, dependencies.automation_store)


def _automation_schedule_disable_operation(
    request: AutomationScheduleDisableInput,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    if dependencies.automation_store is None:
        return _missing_dependency_response(
            request,
            AutomationScheduleDisableResponse,
            tool_name="automation_schedule_disable",
            dependency_name="automation_store",
            read_only=False,
        )
    return disable_automation(request, dependencies.automation_store)


def _application_capture_operation(
    request: Any,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    return prepare_application_capture(request, dependencies.repository)


def _capabilities_operation(
    request: Any,
    _dependencies: MCPToolDependencies,
) -> BaseModel:
    return describe_capabilities(request)


async def _review_and_update_workflow_operation(
    request: Any,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    return await review_and_update_application_status_workflow(
        request,
        dependencies.browser_bridge,
        dependencies.repository,
    )


async def _observe_application_status_page_operation(
    request: Any,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    return await observe_application_status_page_workflow(
        request,
        dependencies.browser_bridge,
        dependencies.repository,
    )


async def _batch_observe_application_status_operation(
    request: Any,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    return await batch_observe_application_status(
        request,
        dependencies.browser_bridge,
        dependencies.repository,
    )


def _verify_application_status_evidence_operation(
    request: Any,
    dependencies: MCPToolDependencies,
) -> BaseModel:
    return verify_application_status_evidence(request, dependencies.browser_bridge)


def _public_entry_discovery_operation(request: Any, dependencies: MCPToolDependencies) -> BaseModel:
    return discover_public_recruitment_entries(request)


def _public_entry_validation_operation(request: Any, dependencies: MCPToolDependencies) -> BaseModel:
    if dependencies.oc_candidate_runner is None:
        return _missing_dependency_response(
            request, PublicEntryValidationResponse,
            tool_name="public_recruitment_entry_validate",
            dependency_name="oc_candidate_runner", read_only=True,
        )
    return validate_public_recruitment_entry(request, dependencies.oc_candidate_runner)


TOOL_DEFINITIONS: tuple[MCPToolDefinition, ...] = (
    MCPToolDefinition(
        name="capabilities",
        description="Describe the available recruitment capabilities and safety boundary.",
        input_model=CapabilitiesInput,
        response_model=CapabilitiesResponse,
        operation=_capabilities_operation,
    ),
    MCPToolDefinition(
        name="today_schedule",
        description="Read schedule events for a requested date.",
        input_model=TodayScheduleInput,
        response_model=TodayScheduleResponse,
        operation=_repository_operation(today_schedule),
    ),
    MCPToolDefinition(
        name="search_jobs",
        description="Read and filter persisted recruitment jobs.",
        input_model=JobSearchInput,
        response_model=JobSearchResponse,
        operation=_repository_operation(search_jobs),
    ),
    MCPToolDefinition(
        name="job_detail",
        description="Read one persisted job and its available analysis.",
        input_model=JobDetailInput,
        response_model=JobDetailResponse,
        operation=_repository_operation(job_detail),
    ),
    MCPToolDefinition(
        name="company_coverage",
        description="Read recruitment company integration coverage.",
        input_model=CompanyCoverageInput,
        response_model=CompanyCoverageResponse,
        operation=_repository_operation(company_coverage),
    ),
    MCPToolDefinition(
        name="application_query",
        description=(
            "Read and uniquely match persisted application records. Call this before interpreting "
            "an update/check request for a named company or job; an existing application makes "
            "application progress review the default intent."
        ),
        input_model=ApplicationQueryInput,
        response_model=ApplicationQueryResponse,
        operation=_repository_operation(application_query),
    ),
    MCPToolDefinition(
        name="application_status_review",
        description="Plan or reconcile approval-gated application status browser observations.",
        input_model=ApplicationStatusReviewInput,
        response_model=ApplicationStatusReviewResponse,
        operation=_repository_operation(application_status_review),
    ),
    MCPToolDefinition(
        name="browser_observation",
        description="Validate a passive sanitized browser observation without navigating or clicking.",
        input_model=BrowserObservationInput,
        response_model=BrowserObservationResponse,
        operation=_repository_operation(observe_browser_page),
    ),
    MCPToolDefinition(
        name="crawler_acceptance",
        description="Audit observed crawler rows, JD completeness, cohort, links, and pagination.",
        input_model=CrawlerAcceptanceInput,
        response_model=CrawlerAcceptanceResponse,
        operation=_repository_operation(accept_crawler_run),
    ),
    MCPToolDefinition(
        name="recruitment_mail_search",
        description="Read redacted recruitment email summaries from the local mail store.",
        input_model=RecruitmentMailSearchInput,
        response_model=RecruitmentMailSearchResponse,
        operation=_mail_store_operation(search_recruitment_mail),
    ),
    MCPToolDefinition(
        name="recruitment_mail_detail",
        description="Read one redacted, persisted recruitment email.",
        input_model=RecruitmentMailDetailInput,
        response_model=RecruitmentMailDetailResponse,
        operation=_mail_store_operation(get_recruitment_mail),
    ),
    MCPToolDefinition(
        name="recruitment_mail_review",
        description=(
            "Preview associations from existing validated model analysis without executing writes. "
            "Without current analysis, use recruitment_mail_process; legacy extracted fields are not accepted."
        ),
        input_model=RecruitmentMailReviewInput,
        response_model=RecruitmentMailReviewResponse,
        operation=_mail_review_operation(review_recruitment_mail),
    ),
    MCPToolDefinition(
        name="recruitment_mail_processing_status",
        description=(
            "Read persisted recruitment-mail processing status without mailbox synchronization, "
            "model calls, or writes. Report proposed analysis separately from verified identity "
            "and actual written or unchanged outcomes."
        ),
        input_model=RecruitmentMailProcessingStatusInput,
        response_model=RecruitmentMailProcessingStatusResponse,
        operation=_recruitment_mail_processing_status_operation,
    ),
    MCPToolDefinition(
        name="recruitment_mail_process",
        description=(
            "For an explicit processing request, refresh the mailbox once and process a bounded "
            "set of eligible pending or retryable messages. The service skips already processed "
            "and terminal messages, limits its internal model batch to ten, and returns the actual "
            "per-record proposed, verified, written, unchanged, unresolved, and failed outcomes. "
            "Inspect has_more, remaining_count and scope_complete. For all-pending requests, "
            "continue the same scope after progress while has_more is true, at most five calls. "
            "Aggregate counts; do not claim all mail is complete after a single batch. "
            "Do not retry this tool speculatively or replay pending body classification in the tool."
        ),
        input_model=RecruitmentMailProcessInput,
        response_model=RecruitmentMailProcessResponse,
        operation=_recruitment_mail_process_operation,
        read_only=False,
        idempotent=False,
        open_world=True,
    ),
    MCPToolDefinition(
        name="recruitment_mail_sync",
        description=(
            "Synchronize new local mailbox messages without changing application progress. "
            "Mail search, detail, and review invoke this automatically first."
        ),
        input_model=RecruitmentMailSyncInput,
        response_model=RecruitmentMailSyncResponse,
        operation=_mail_sync_operation,
        read_only=False,
        idempotent=True,
        open_world=True,
    ),
    MCPToolDefinition(
        name="application_status_update",
        description=(
            "Update one application from persisted mail or page evidence after identity, "
            "source, event-time and conflict checks. Use only when processing progress "
            "is requested; reading/synchronizing mail does not require this tool. "
            "Repeated successful evidence is idempotent. A retryable=false failure is terminal "
            "until persisted evidence changes; never vary or invent evidence IDs, use RAG/web "
            "search, or use a schedule as a workaround. Call this once for older status mail too: "
            "a safe stale event returns success with state=unchanged and settles the mail."
        ),
        input_model=ApplicationStatusUpdateInput,
        response_model=ApplicationStatusUpdateResponse,
        operation=_application_status_update_operation,
        read_only=False,
        idempotent=True,
    ),
    MCPToolDefinition(
        name="schedule_window",
        description="Inspect a local schedule window for events and conflicts.",
        input_model=ScheduleWindowInput,
        response_model=ScheduleWindowResponse,
        operation=_repository_operation(inspect_schedule_window),
    ),
    MCPToolDefinition(
        name="application_edit",
        description="按用户明确要求纠正一条本地投递记录的公司名、岗位名或投递进度页面链接。先 application_query 获取准确 ID 与 updated_at。无需额外审批或 shell 权限。不得自主改名；不改变阶段、阶段历史或关联岗位库。返回实际修改前后值。",
        input_model=ApplicationEditInput,
        response_model=ApplicationEditResponse,
        operation=_application_edit_operation,
        read_only=False,
        idempotent=True,
    ),
    MCPToolDefinition(
        name="schedule_manage",
        description=(
            "Create or update one local todo or calendar item in schedule_event_snapshots. "
            "Create requires request_key, title, event_type, and company_name, and reuses the "
            "request key only when the normalized payload is "
            "identical; a different payload returns a conflict. Update requires event_id and "
            "may include expected_updated_at. company_name is required but job_title may be "
            "empty for a company-level todo. Explicit application_id is checked exactly; a "
            "missing binding remains null and an empty bound job is filled from the application. "
            "An item without event_date is canonicalized to time_kind=unspecified; adding a "
            "date canonicalizes unspecified to appointment, while a date without a clock stays "
            "date-only. This local schedule write does not submit an "
            "application or change application progress, and does not require the application "
            "approval flow. It never invents duration; deadline items have no appointment bounds."
        ),
        input_model=ScheduleManageInput,
        response_model=ScheduleManageResponse,
        operation=_schedule_manage_operation,
        read_only=False,
        idempotent=True,
    ),
    MCPToolDefinition(
        name="edge_connection_status",
        description="Read the persisted or explicitly injected Edge connection status.",
        input_model=EdgeConnectionStatusInput,
        response_model=EdgeConnectionStatusResponse,
        operation=_edge_connection_operation,
        read_only=True,
    ),
    MCPToolDefinition(
        name="observe_application_status_page",
        description=(
            "Open one non-terminal stored application page in Edge and return bounded semantic "
            "evidence. Rejected and withdrawn applications are closed and are never opened. "
            "Start with include_vision=false. Only use include_vision=true for an individual "
            "follow-up after a prior DOM-only observation has no bindable evidence and the single "
            "page has a clear target; supply "
            "vision_fallback_reason=no_structured_evidence_visible_status_likely. Never request "
            "vision for CAPTCHA, login walls, blank pages or ambiguous targets, and never solve CAPTCHA."
        ),
        input_model=ObserveApplicationStatusPageInput,
        response_model=ObserveApplicationStatusPageResponse,
        operation=_observe_application_status_page_operation,
        read_only=False,
        open_world=True,
    ),
    MCPToolDefinition(
        name="batch_observe_application_status",
        description=(
            "Batch observe stored application pages concurrently; this tool is DOM-only and never "
            "starts vision analysis. Rejected and withdrawn applications are excluded before any "
            "browser operation. For a complete current/non-terminal review, set "
            "all_non_terminal=true and omit application_ids; do not call application_query "
            "with list_all=true just to collect IDs. "
            "The all-mode processes a bounded wave and persists its frozen scope. "
            "Omit timeout_ms or use 120000 at most; never submit a larger timeout. "
            "While remaining_count > 0, call again with ONLY run_id from the result until "
            "scope_complete=true. Counts are cumulative, not per-call. Never subtract "
            "excluded_terminal from scope_total again. Do not restart all-mode on timeout. "
            "No separate bridge or capabilities call is required. "
            "Return updated, unchanged, excluded, blocked, unresolved and failed "
            "counts accurately. In normal user-facing summaries translate them to 已更新, 状态未变化, "
            "已跳过（已挂）, 需要登录或验证, 无法确认, and 执行失败; "
            "login, CAPTCHA and unclear evidence do not count as successful verification. "
            "Use the separate individual observation flow for a justified visual fallback."
        ),
        input_model=BatchObserveApplicationStatusInput,
        response_model=BatchObserveApplicationStatusResponse,
        operation=_batch_observe_application_status_operation,
        read_only=False,
        open_world=True,
    ),
    MCPToolDefinition(
        name="verify_application_status_evidence",
        description=(
            "Commit a model-interpreted canonical status only when it is bound to persisted "
            "Edge evidence and passes confidence, direction, URL and audit checks."
        ),
        input_model=VerifyApplicationStatusEvidenceInput,
        response_model=VerifyApplicationStatusEvidenceResponse,
        operation=_verify_application_status_evidence_operation,
        read_only=False,
        open_world=True,
    ),
    MCPToolDefinition(
        name="browser_operation_status",
        description=(
            "Read bounded incremental browser-operation events. On follow-up calls pass the "
            "prior next_sequence as after_sequence; the tool waits up to timeout_ms for new "
            "progress or a terminal result instead of requiring rapid polling."
        ),
        input_model=BrowserOperationStatusInput,
        response_model=BrowserOperationStatusResponse,
        operation=_browser_store_operation(browser_operation_status),
        read_only=True,
    ),
    MCPToolDefinition(
        name="cancel_browser_operation",
        description="Cancel one active persistent Edge browser operation.",
        input_model=CancelBrowserOperationInput,
        response_model=CancelBrowserOperationResponse,
        operation=_browser_store_operation(cancel_browser_operation),
        read_only=False,
        destructive=True,
    ),
    MCPToolDefinition(
        name="knowledge_search",
        description="Search approved crawler or candidate evidence. Cite returned source_ref URLs. No personal document access, document writes or application changes.",
        input_model=KnowledgeSearchInput,
        response_model=KnowledgeSearchResponse,
        operation=_knowledge_search_operation,
    ),
    MCPToolDefinition(
        name="configured_crawler_run",
        description=(
            "Run a configured company crawler only for explicit requests to fetch recruitment "
            "jobs or refresh the job catalog; do not use it to update an existing application."
        ),
        input_model=ConfiguredCrawlerRunInput,
        response_model=ConfiguredCrawlerRunResponse,
        operation=_configured_crawler_run_operation,
        open_world=True,
    ),
    MCPToolDefinition(
        name="public_recruitment_entry_discovery",
        description="Search bounded public recruitment-entry candidates; returned URLs require validation before use.",
        input_model=PublicEntryDiscoveryInput,
        response_model=PublicEntryDiscoveryResponse,
        operation=_public_entry_discovery_operation,
        open_world=True,
    ),
    MCPToolDefinition(
        name="public_recruitment_entry_validate",
        description="Validate company identity and isolated crawler completeness for one discovered recruitment entry without business writes.",
        input_model=PublicEntryValidationInput,
        response_model=PublicEntryValidationResponse,
        operation=_public_entry_validation_operation,
        open_world=True,
    ),
    MCPToolDefinition(
        name="automation_plan",
        description=(
            "Create an inactive local daily-task preview. A recurring update/check for a company "
            "or job already in applications must use task_id application_progress, never a "
            "crawler or cloud task."
        ),
        input_model=AutomationPlanInput,
        response_model=AutomationPlanResponse,
        operation=_automation_plan_operation,
    ),
    MCPToolDefinition(
        name="automation_schedule",
        description=(
            "Create or update and immediately activate a persistent local daily automation. "
            "For a named applied job, first resolve the application and pass its application_id "
            "with task_id application_progress. This operation is reversible and does not use "
            "Windows Task Scheduler or any cloud service."
        ),
        input_model=AutomationScheduleInput,
        response_model=AutomationScheduleResponse,
        operation=_automation_schedule_operation,
        read_only=False,
        open_world=True,
    ),
    MCPToolDefinition(
        name="automation_schedule_list",
        description="List persisted local automations, their next run and latest result.",
        input_model=AutomationScheduleListInput,
        response_model=AutomationScheduleListResponse,
        operation=_automation_schedule_list_operation,
    ),
    MCPToolDefinition(
        name="automation_schedule_disable",
        description="Disable one persistent local automation without deleting its audit history.",
        input_model=AutomationScheduleDisableInput,
        response_model=AutomationScheduleDisableResponse,
        operation=_automation_schedule_disable_operation,
        read_only=False,
    ),
    MCPToolDefinition(
        name="application_capture",
        description="Match a browser page to a job and return an application approval preview.",
        input_model=ApplicationCaptureInput,
        response_model=ApplicationCaptureResponse,
        operation=_application_capture_operation,
    ),
    MCPToolDefinition(
        name="operation_run",
        description="Execute one explicitly allowlisted local recruitment operation.",
        input_model=OperationalTaskRunInput,
        response_model=MCPOperationRunResponse,
        operation=_operation_run_operation,
        read_only=False,
        idempotent=False,
        open_world=True,
    ),
    MCPToolDefinition(
        name="daily_recruitment_sync",
        description=(
            "Run the complete local recruitment flow as one observable Harness operation: "
            "OfferBiu discovery, company reconciliation, deterministic title-first crawl, "
            "JD capture, deduplicated persistence, incremental matching, offline reconciliation, "
            "and reporting. An unscoped full or crawl_only run automatically queues every crawlable company "
            "from the current complete OfferBiu snapshot and refreshes existing companies. Pass "
            "up to ten source_record_ids only for an explicitly bounded diagnostic run. Use "
            "crawl_only to skip scoring, score_only for saved JDs, and resume with the original "
            "resume_run_id to restore its frozen scope; missing recovery state is an error, not "
            "permission to expand the queue. Use dry_run for validation without business writes."
            " An explicit user request to run the flow authorizes this controlled operation without "
            "another confirmation or a recurring schedule, subject to instance write/configuration gates. "
            "Returns a background run_id promptly; accepted/running is not completion. The chat may "
            "finish while the local runtime stays open. Query daily_recruitment_sync_status with that ID."
        ),
        input_model=DailyRecruitmentSyncInput,
        response_model=DailyRecruitmentSyncResponse,
        operation=_daily_recruitment_sync_operation,
        read_only=False,
        idempotent=False,
        open_world=True,
    ),
    MCPToolDefinition(
        name="offerbiu_source_refresh",
        description=(
            "Refresh the public OfferBiu 2027 autumn-recruitment source, reject unusable "
            "entries, and register valid company entry URLs. Use the defaults for a complete "
            "refresh; partial snapshots are never applied. The result includes a bounded, readable "
            "pending_entries list whose record_id values can be passed to daily_recruitment_sync."
        ),
        input_model=OfferBiuSourceRefreshInput,
        response_model=OfferBiuSourceRefreshResponse,
        operation=_offerbiu_source_refresh_operation,
        read_only=False,
        idempotent=True,
        open_world=True,
    ),
    MCPToolDefinition(
        name="daily_recruitment_sync_status",
        description=(
            "Long-poll the current state or terminal result of a started daily sync run. "
            "Use timeout_ms=120000 (the maximum) and call again only if the returned run is still active."
        ),
        input_model=DailyRecruitmentSyncStatusInput,
        response_model=DailyRecruitmentSyncStatusResponse,
        operation=_daily_recruitment_sync_status_operation,
    ),
)

READ_ONLY_TOOL_DEFINITIONS: tuple[MCPToolDefinition, ...] = tuple(
    definition for definition in TOOL_DEFINITIONS if definition.read_only
)
AGENT_TOOL_DEFINITIONS: tuple[MCPToolDefinition, ...] = tuple(
    definition for definition in TOOL_DEFINITIONS
    if definition.name in MCP_AGENT_TOOL_NAMES
)


def _build_handler(
    definition: MCPToolDefinition,
    dependencies: MCPToolDependencies,
) -> Callable[[Any], Any]:
    def require_write_opt_in() -> None:
        if not definition.read_only and not bool(getattr(get_settings(), "write_enabled", False)):
            raise PermissionError("Business writes are disabled; owner opt-in is required.")

    def validate_response(result: Any) -> BaseModel:
        response = definition.response_model.model_validate(result)
        if definition.read_only and getattr(response, "read_only", True) is not True:
            raise RuntimeError(
                f"MCP tool {definition.name} returned an invalid read_only classification"
            )
        return response

    if inspect.iscoroutinefunction(definition.operation):
        async def handler(request: Any) -> BaseModel:
            require_write_opt_in()
            typed_request = definition.input_model.model_validate(request)
            result = await definition.operation(typed_request, dependencies)
            return validate_response(result)
    else:
        def handler(request: Any) -> BaseModel:
            require_write_opt_in()
            typed_request = definition.input_model.model_validate(request)
            result = definition.operation(typed_request, dependencies)
            return validate_response(result)

    handler.__name__ = definition.name
    handler.__doc__ = definition.description
    handler.__annotations__ = {
        "request": definition.input_model,
        "return": definition.response_model,
    }
    return handler


def _register_tool(
    server: MCPToolRegistrar,
    definition: MCPToolDefinition,
    handler: Callable[[Any], Any],
) -> None:
    """Register MCP safety hints when the concrete SDK supports them."""

    kwargs: dict[str, Any] = {
        "name": definition.name,
        "description": definition.description,
    }
    try:
        signature = inspect.signature(server.tool)
    except (TypeError, ValueError):
        signature = None
    if signature is not None and "annotations" in signature.parameters:
        try:
            from mcp.types import ToolAnnotations

            kwargs["annotations"] = ToolAnnotations(
                readOnlyHint=definition.read_only,
                destructiveHint=definition.destructive,
                idempotentHint=definition.idempotent,
                openWorldHint=definition.open_world,
            )
        except ImportError:
            pass
    server.tool(**kwargs)(handler)


class ReadOnlyMCPAdapter:
    """Bind the existing repository-backed tools to an MCP-compatible server."""

    def __init__(
        self,
        repository: RecruitmentRepository,
        mail_store: RecruitmentMailStore,
        browser_bridge: BrowserBridgeStore | None = None,
        *,
        connection_status_provider: ConnectionStatusProvider | None = None,
        evidence_grounder: EvidenceGrounder | None = None,
        crawler_runner: ConfiguredCrawlerRunner | None = None,
        oc_candidate_runner: OcCandidateRunner | None = None,
        operational_task_runner: OperationalTaskRunner | None = None,
        offerbiu_refresher: OfferBiuRefreshService | None = None,
        automation_scheduler: LocalTaskScheduler | None = None,
        automation_store: AutomationStore | None = None,
    ):
        self._dependencies = MCPToolDependencies(
            repository=repository,
            mail_store=mail_store,
            browser_bridge=browser_bridge,
            connection_status_provider=connection_status_provider,
            evidence_grounder=evidence_grounder,
            crawler_runner=crawler_runner,
            oc_candidate_runner=oc_candidate_runner,
            operational_task_runner=operational_task_runner,
            offerbiu_refresher=offerbiu_refresher,
            automation_scheduler=automation_scheduler,
            automation_store=automation_store,
        )

    @property
    def tool_names(self) -> tuple[str, ...]:
        return MCP_READ_ONLY_TOOL_NAMES

    def register(self, server: MCPToolRegistrar) -> tuple[str, ...]:
        for definition in READ_ONLY_TOOL_DEFINITIONS:
            handler = _build_handler(definition, self._dependencies)
            _register_tool(server, definition, handler)
        return self.tool_names


class MCPAdapter(ReadOnlyMCPAdapter):
    """Bind both read-only queries and explicitly classified browser actions."""

    @property
    def tool_names(self) -> tuple[str, ...]:
        return MCP_TOOL_NAMES

    def register(self, server: MCPToolRegistrar) -> tuple[str, ...]:
        for definition in TOOL_DEFINITIONS:
            handler = _build_handler(definition, self._dependencies)
            _register_tool(server, definition, handler)
        return self.tool_names


class AgentMCPAdapter(ReadOnlyMCPAdapter):
    """Bind the compact business-level surface used by the Agent runtime."""

    @property
    def tool_names(self) -> tuple[str, ...]:
        return MCP_AGENT_TOOL_NAMES

    def register(self, server: MCPToolRegistrar) -> tuple[str, ...]:
        definitions = {definition.name: definition for definition in AGENT_TOOL_DEFINITIONS}
        for name in self.tool_names:
            definition = definitions[name]
            handler = _build_handler(definition, self._dependencies)
            _register_tool(server, definition, handler)
        return self.tool_names


def register_read_only_tools(
    server: MCPToolRegistrar,
    repository: RecruitmentRepository,
    mail_store: RecruitmentMailStore,
    browser_bridge: BrowserBridgeStore | None = None,
    *,
    connection_status_provider: ConnectionStatusProvider | None = None,
    evidence_grounder: EvidenceGrounder | None = None,
    crawler_runner: ConfiguredCrawlerRunner | None = None,
    oc_candidate_runner: OcCandidateRunner | None = None,
    operational_task_runner: OperationalTaskRunner | None = None,
    offerbiu_refresher: OfferBiuRefreshService | None = None,
    automation_scheduler: LocalTaskScheduler | None = None,
    automation_store: AutomationStore | None = None,
) -> tuple[str, ...]:
    """Register the fixed read-only tool set with explicit local dependencies."""

    return ReadOnlyMCPAdapter(
        repository,
        mail_store,
        browser_bridge,
        connection_status_provider=connection_status_provider,
        evidence_grounder=evidence_grounder,
        crawler_runner=crawler_runner,
        oc_candidate_runner=oc_candidate_runner,
        operational_task_runner=operational_task_runner,
        offerbiu_refresher=offerbiu_refresher,
        automation_scheduler=automation_scheduler,
        automation_store=automation_store,
    ).register(server)


def register_tools(
    server: MCPToolRegistrar,
    repository: RecruitmentRepository,
    mail_store: RecruitmentMailStore,
    browser_bridge: BrowserBridgeStore | None = None,
    *,
    connection_status_provider: ConnectionStatusProvider | None = None,
    evidence_grounder: EvidenceGrounder | None = None,
    crawler_runner: ConfiguredCrawlerRunner | None = None,
    oc_candidate_runner: OcCandidateRunner | None = None,
    operational_task_runner: OperationalTaskRunner | None = None,
    offerbiu_refresher: OfferBiuRefreshService | None = None,
    automation_scheduler: LocalTaskScheduler | None = None,
    automation_store: AutomationStore | None = None,
) -> tuple[str, ...]:
    """Register the complete MCP surface with explicitly supplied dependencies."""

    return MCPAdapter(
        repository,
        mail_store,
        browser_bridge,
        connection_status_provider=connection_status_provider,
        evidence_grounder=evidence_grounder,
        crawler_runner=crawler_runner,
        oc_candidate_runner=oc_candidate_runner,
        operational_task_runner=operational_task_runner,
        offerbiu_refresher=offerbiu_refresher,
        automation_scheduler=automation_scheduler,
        automation_store=automation_store,
    ).register(server)


def register_agent_tools(
    server: MCPToolRegistrar,
    repository: RecruitmentRepository,
    mail_store: RecruitmentMailStore,
    browser_bridge: BrowserBridgeStore | None = None,
    *,
    connection_status_provider: ConnectionStatusProvider | None = None,
    evidence_grounder: EvidenceGrounder | None = None,
    crawler_runner: ConfiguredCrawlerRunner | None = None,
    oc_candidate_runner: OcCandidateRunner | None = None,
    operational_task_runner: OperationalTaskRunner | None = None,
    offerbiu_refresher: OfferBiuRefreshService | None = None,
    automation_scheduler: LocalTaskScheduler | None = None,
    automation_store: AutomationStore | None = None,
) -> tuple[str, ...]:
    """Register the compact model-visible surface for normal Agent sessions."""

    return AgentMCPAdapter(
        repository,
        mail_store,
        browser_bridge,
        connection_status_provider=connection_status_provider,
        evidence_grounder=evidence_grounder,
        crawler_runner=crawler_runner,
        oc_candidate_runner=oc_candidate_runner,
        operational_task_runner=operational_task_runner,
        offerbiu_refresher=offerbiu_refresher,
        automation_scheduler=automation_scheduler,
        automation_store=automation_store,
    ).register(server)


register_all_tools = register_tools


def create_fastmcp_server(
    repository: RecruitmentRepository,
    mail_store: RecruitmentMailStore,
    browser_bridge: BrowserBridgeStore | None = None,
    *,
    connection_status_provider: ConnectionStatusProvider | None = None,
    evidence_grounder: EvidenceGrounder | None = None,
    crawler_runner: ConfiguredCrawlerRunner | None = None,
    oc_candidate_runner: OcCandidateRunner | None = None,
    operational_task_runner: OperationalTaskRunner | None = None,
    offerbiu_refresher: OfferBiuRefreshService | None = None,
    automation_scheduler: LocalTaskScheduler | None = None,
    automation_store: AutomationStore | None = None,
    name: str = "RecruitOps Agent",
    profile: Literal["agent", "full", "read_only"] = "full",
) -> Any:
    """Create and populate an official ``FastMCP`` server when available."""

    try:
        from mcp.server.mcpserver import MCPServer

        server = MCPServer(name=name, title=name, version=MCP_TOOL_PROTOCOL_VERSION)
    except ImportError:
        try:
            from mcp.server.fastmcp import FastMCP

            server = FastMCP(name)
        except ImportError as exc:
            raise MCPUnavailableError(
                "The MCP SDK is not installed; install the 'mcp' package."
            ) from exc
    registrar = {
        "agent": register_agent_tools,
        "full": register_tools,
        "read_only": register_read_only_tools,
    }[profile]
    registrar(
        server,
        repository,
        mail_store,
        browser_bridge,
        connection_status_provider=connection_status_provider,
        evidence_grounder=evidence_grounder,
        crawler_runner=crawler_runner,
        oc_candidate_runner=oc_candidate_runner,
        operational_task_runner=operational_task_runner,
        offerbiu_refresher=offerbiu_refresher,
        automation_scheduler=automation_scheduler,
        automation_store=automation_store,
    )
    return server
