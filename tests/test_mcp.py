from datetime import date, datetime, timezone
from hashlib import sha256
import asyncio
from importlib.util import find_spec
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from packages.browser_bridge import BrowserBridgeStore
from packages.mcp import (
    MCP_AGENT_TOOL_NAMES,
    MCP_READ_ONLY_TOOL_NAMES,
    MCP_TOOL_NAMES,
    MCP_TOOL_PROTOCOL_VERSION,
    MCPUnavailableError,
    create_fastmcp_server,
    register_agent_tools,
    register_read_only_tools,
)
from packages.recruitment_mail import (
    CompanyCandidate,
    JobCandidate,
    MailIdentity,
    ParsedRecruitmentEmail,
    RecruitmentMailStore,
    RecruitmentMessageCategory,
    TimeCandidate,
)
from packages.recruitment_mail.analysis_store import save_model_analysis
from packages.recruitment_mail.model_analysis import MAIL_ANALYSIS_VERSION
from packages.storage import Storage
from packages.tools.typed import (
    ApplicationQueryInput,
    ApplicationQueryResponse,
    CompanyCoverageInput,
    CompanyCoverageResponse,
    JobDetailInput,
    JobDetailResponse,
    JobSearchInput,
    JobSearchResponse,
    TodayScheduleInput,
    TodayScheduleResponse,
)
from packages.tools.application_review import ApplicationStatusReviewResponse
from packages.tools.recruitment_mail import (
    RecruitmentMailDetailResponse,
    RecruitmentMailReviewResponse,
    RecruitmentMailSearchResponse,
)
from packages.tools.schedule_ops import ScheduleWindowResponse
from tests.test_typed_tools import InMemoryRepository


ROOT = Path(__file__).resolve().parents[1]


class FakeMCPServer:
    def __init__(self) -> None:
        self.tools: dict[str, tuple[object, str]] = {}

    def tool(self, *, name: str, description: str):
        def register(handler):
            self.tools[name] = (handler, description)
            return handler

        return register


def _mail_store() -> RecruitmentMailStore:
    return RecruitmentMailStore(Storage.from_url("sqlite+pysqlite:///:memory:"))


def _browser_bridge_store() -> BrowserBridgeStore:
    return BrowserBridgeStore(Storage.from_url("sqlite+pysqlite:///:memory:"))


def _parsed_mail() -> ParsedRecruitmentEmail:
    starts_at = datetime(2026, 8, 21, 10, 30, tzinfo=timezone.utc)
    return ParsedRecruitmentEmail(
        identity=MailIdentity(message_id="mcp-mail-1"),
        sender="招聘团队",
        subject="示例公司 C++开发工程师面试邀请",
        body_text="请参加面试。",
        received_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        category=RecruitmentMessageCategory.INTERVIEW,
        company_candidates=[
            CompanyCandidate(value="示例公司", evidence="主题", confidence=1.0)
        ],
        job_candidates=[
            JobCandidate(value="C++开发工程师", evidence="主题", confidence=1.0)
        ],
        time_candidates=[
            TimeCandidate(
                value="2026-08-21 10:30",
                evidence="邮件正文",
                confidence=1.0,
                normalized=starts_at,
            )
        ],
        confidence=0.95,
    )


def test_registers_exactly_the_read_only_tools() -> None:
    server = FakeMCPServer()

    registered = register_read_only_tools(
        server,
        InMemoryRepository(),
        _mail_store(),
        _browser_bridge_store(),
    )

    assert MCP_TOOL_PROTOCOL_VERSION == "25"
    assert len(MCP_TOOL_NAMES) == 47
    assert len(MCP_READ_ONLY_TOOL_NAMES) == 28
    assert MCP_READ_ONLY_TOOL_NAMES == (
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
        "background_task_status",
        "application_review_status",
        "recruitment_mail_run_status",
        "recruitment_mail_binding_candidates",
    )
    assert registered == MCP_READ_ONLY_TOOL_NAMES
    assert tuple(server.tools) == MCP_READ_ONLY_TOOL_NAMES
    assert all(description for _, description in server.tools.values())
    assert "lujie_task_preview" not in server.tools


def test_official_mcp_server_advertises_standard_safety_annotations() -> None:
    server = create_fastmcp_server(
        InMemoryRepository(),
        _mail_store(),
        _browser_bridge_store(),
    )

    capabilities = server._tool_manager._tools["capabilities"].annotations
    browser_observe = server._tool_manager._tools[
        "observe_application_status_page"
    ].annotations
    browser_update = server._tool_manager._tools[
        "verify_application_status_evidence"
    ].annotations
    cancel = server._tool_manager._tools["cancel_browser_operation"].annotations

    assert capabilities.read_only_hint is True
    assert capabilities.destructive_hint is False
    assert capabilities.idempotent_hint is True
    assert browser_observe.read_only_hint is False
    assert browser_observe.open_world_hint is True
    assert browser_update.read_only_hint is False
    assert browser_update.open_world_hint is True
    assert cancel.destructive_hint is True


def test_agent_profile_exposes_business_tools_not_diagnostic_primitives() -> None:
    server = FakeMCPServer()

    registered = register_agent_tools(
        server,
        InMemoryRepository(),
        _mail_store(),
        _browser_bridge_store(),
    )

    assert registered == MCP_AGENT_TOOL_NAMES
    assert tuple(server.tools) == MCP_AGENT_TOOL_NAMES
    assert len(MCP_AGENT_TOOL_NAMES) == 39
    assert {
        "search_jobs",
        "application_query",
        "recruitment_mail_processing_status",
        "recruitment_mail_process",
        "schedule_manage",
            "observe_application_status_page",
            "browser_operation_status",
            "verify_application_status_evidence",
        "daily_recruitment_sync",
        "offerbiu_source_refresh",
        "public_recruitment_entry_discovery",
        "public_recruitment_entry_validate",
        "offerbiu_source_refresh",
    } <= set(registered)
    assert {
        "application_status_review",
        "browser_observation",
        "crawler_acceptance",
        "edge_connection_status",
        "cancel_browser_operation",
        "automation_plan",
        "application_capture",
        "operation_run",
    }.isdisjoint(registered)


def test_handlers_validate_models_call_typed_tools_and_return_models() -> None:
    repository = InMemoryRepository()
    server = FakeMCPServer()
    mail_store = _mail_store()
    mail_record = mail_store.upsert(_parsed_mail())
    save_model_analysis(
        mail_store,
        mail_record.id,
        mail_record.content_digest,
        MAIL_ANALYSIS_VERSION,
        {
            "record_id": mail_record.id,
            "content_digest": mail_record.content_digest,
            "company_name": "示例公司",
            "job_title": "C++开发工程师",
            "job_code": None,
            "event_type": "interview",
            "event_time": None,
            "deadline": None,
            "evidence_quotes": [mail_record.subject],
            "candidate_application_id": "1",
            "match_reason": "explicit source-bound fixture",
            "action_summary": None,
        },
        "proposed",
        model="fixture-model",
    )
    register_read_only_tools(server, repository, mail_store, _browser_bridge_store())
    repository.applications[0] = repository.applications[0].model_copy(
        update={"id": "1", "record_url": "https://example.com/applications"}
    )

    captured_jd = (
        "Responsibilities: design and maintain C++ services, "
        "write unit and integration tests, investigate production "
        "issues, and collaborate with robotics engineers. "
        "Requirements: master's degree, strong C++ and Linux "
        "experience, familiarity with ROS, networking, algorithms, "
        "continuous integration, and clear technical communication."
    )
    responses = [
        server.tools["today_schedule"][0]({"on_date": date(2026, 8, 19)}),
        server.tools["search_jobs"][0]({"query": "C++"}),
        server.tools["job_detail"][0](JobDetailInput(job_id="job-1")),
        server.tools["company_coverage"][0](CompanyCoverageInput(company="Example")),
        server.tools["application_query"][0]({"application_id": "1"}),
        server.tools["application_status_review"][0](
            {"review_id": "review-mcp", "application_ids": ["1"]}
        ),
        server.tools["browser_observation"][0](
            {
                "url": "https://example.com/campus",
                "allowed_origins": ["https://example.com"],
                "page_text": "2027 campus role",
            }
        ),
        server.tools["crawler_acceptance"][0](
            {
                "company": "Example",
                "source_url": "https://example.com/campus",
                "jobs": [
                    {
                        "id": "job-1",
                        "title": "C++",
                        "detail_url": "https://example.com/jobs/1",
                        "jd_raw": captured_jd,
                        "capture_evidence": {
                            "status": "complete",
                            "method": "rendered_detail",
                            "source_url": "https://example.com/jobs/1",
                            "identity_verified": True,
                            "terminal_observed": True,
                            "remaining_controls": [],
                            "content_sha256": sha256(captured_jd.encode()).hexdigest(),
                        },
                        "cohort": 2027,
                        "cohort_status": "confirmed",
                        "batch": "formal",
                    }
                ],
                "pages_seen": 1,
                "pagination_complete": True,
            }
        ),
        server.tools["recruitment_mail_search"][0]({}),
        server.tools["recruitment_mail_detail"][0]({"record_id": mail_record.id}),
        server.tools["recruitment_mail_review"][0]({"record_id": mail_record.id}),
        server.tools["schedule_window"][0](
            {"start_date": date(2026, 8, 19), "end_date": date(2026, 8, 20)}
        ),
    ]

    assert isinstance(responses[0], TodayScheduleResponse)
    assert isinstance(responses[1], JobSearchResponse)
    assert isinstance(responses[2], JobDetailResponse)
    assert isinstance(responses[3], CompanyCoverageResponse)
    assert isinstance(responses[4], ApplicationQueryResponse)
    assert isinstance(responses[5], ApplicationStatusReviewResponse)
    assert responses[6].tool_name == "browser_observation"
    assert responses[7].tool_name == "crawler_acceptance"
    assert isinstance(responses[8], RecruitmentMailSearchResponse)
    assert isinstance(responses[9], RecruitmentMailDetailResponse)
    assert isinstance(responses[10], RecruitmentMailReviewResponse)
    assert isinstance(responses[11], ScheduleWindowResponse)
    assert all(response.success for response in responses)
    assert repository.calls == [
        "list_schedule",
        "search_jobs",
        "get_job",
        "list_companies",
        "list_applications",
        "list_applications",
        "list_applications",
        "list_schedule",
    ]
    assert responses[10].data is not None
    assert responses[10].data.approval_previews
    assert repository.applications[0].stage.value == "written"


def test_mcp_boundary_preserves_typed_validation_and_read_only_preview() -> None:
    server = FakeMCPServer()
    repository = InMemoryRepository()
    register_read_only_tools(server, repository, _mail_store(), _browser_bridge_store())

    with pytest.raises(ValidationError):
        server.tools["search_jobs"][0]({"unknown": True})

    assert "lujie_task_preview" not in server.tools
    assert repository.calls == []


def test_mcp_requires_an_explicit_mail_store_dependency() -> None:
    with pytest.raises(TypeError):
        register_read_only_tools(FakeMCPServer(), InMemoryRepository())  # type: ignore[call-arg]


def test_mcp_check_cli_is_offline_and_returns_the_frozen_surface() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/run_mcp_server.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload == {
        "protocol_version": MCP_TOOL_PROTOCOL_VERSION,
        "profile": "agent",
        "tools": list(MCP_AGENT_TOOL_NAMES),
        "full_tool_count": len(MCP_TOOL_NAMES),
    }


def test_mcp_check_cli_can_report_the_full_diagnostic_surface() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/run_mcp_server.py", "--check", "--profile", "full"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["profile"] == "full"
    assert payload["tools"] == list(MCP_TOOL_NAMES)
    assert payload["full_tool_count"] == len(MCP_TOOL_NAMES)


@pytest.mark.skipif(find_spec("mcp") is None, reason="mcp SDK is not installed")
def test_real_mcp_server_registers_tools_with_current_sdk() -> None:
    server = create_fastmcp_server(
        InMemoryRepository(),
        _mail_store(),
        _browser_bridge_store(),
    )

    tools = asyncio.run(server.list_tools())

    assert tuple(tool.name for tool in tools) == MCP_TOOL_NAMES


@pytest.mark.skipif(find_spec("mcp") is not None, reason="mcp SDK is installed")
def test_fastmcp_factory_reports_optional_dependency_boundary() -> None:
    with pytest.raises(MCPUnavailableError, match="MCP SDK is not installed"):
        create_fastmcp_server(InMemoryRepository(), _mail_store())
