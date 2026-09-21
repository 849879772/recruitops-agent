from __future__ import annotations

import asyncio
import inspect
from hashlib import sha256

from packages.browser_bridge import BrowserBridgeStore
from packages.mcp import (
    MCP_ACTION_TOOL_NAMES,
    MCP_READ_ONLY_TOOL_NAMES,
    MCP_TOOL_NAMES,
    register_tools,
)
from packages.mcp.server import MCPToolDependencies, TOOL_DEFINITIONS
from packages.rag import DocumentChunk, EvidenceGrounder, LexicalCosineRetriever
from packages.recruitment_mail import RecruitmentMailStore
from packages.scheduler import LocalTaskScheduler, TaskType
from packages.storage import Storage
from packages.tools.crawler_run import ConfiguredCrawlerRunResponse, ConfiguredCrawlerRunner
from packages.tools.knowledge import KnowledgeSearchResponse
from packages.tools.operations import (
    OperationalTaskRunner,
    OperationalTaskRunResponse,
)
from packages.tools.typed import (
    CapabilitiesResponse,
    ToolErrorCode,
    ToolStatus,
)
from tests.test_typed_tools import InMemoryRepository


class FakeMCPServer:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *, name: str, description: str):
        def register(handler):
            self.tools[name] = handler
            return handler

        return register


def _mail_store() -> RecruitmentMailStore:
    return RecruitmentMailStore(Storage.from_url("sqlite+pysqlite:///:memory:"))


def _browser_bridge_store() -> BrowserBridgeStore:
    return BrowserBridgeStore(Storage.from_url("sqlite+pysqlite:///:memory:"))


def _grounder() -> EvidenceGrounder:
    retriever = LexicalCosineRetriever()
    retriever.add(
        [
            DocumentChunk(
                id="crawler-knowledge-1",
                content="Moka 校招岗位需要遍历全部分页并读取详情接口。",
                source="crawler_knowledge",
                source_ref="docs://moka",
                metadata={"domain": "crawler"},
            )
        ]
    )
    return EvidenceGrounder(retriever, minimum_score=0.15)


def _crawler_payload() -> dict[str, object]:
    payload = {
        "company": "示例公司",
        "crawler_key": "fixture",
        "configured_urls": ["https://jobs.example.com/campus"],
        "source_url": "https://jobs.example.com/campus",
        "allowed_origins": ["https://jobs.example.com"],
        "jobs": [
            {
                "id": "job-1",
                "title": "C++ 软件开发工程师",
                "city": "上海",
                "detail_url": "https://jobs.example.com/campus/job-1",
                "jd_raw": (
                    "职位描述：负责 Linux 平台 C++ 软件模块设计、开发和自动化测试，"
                    "参与生产问题定位、性能优化、代码评审和跨团队协作。"
                    "任职要求：熟悉 C++、多线程、数据结构、网络编程和软件工程实践，"
                    "有完整项目经验并能清晰完成技术沟通。"
                ),
                "cohort": 2027,
                "cohort_status": "confirmed",
                "batch": "formal",
            }
        ],
        "raw_job_count": 1,
        "pages_seen": 1,
        "total_pages": 1,
        "has_more": False,
        "run_reason": "fixture",
    }
    job = payload["jobs"][0]
    job["capture_evidence"] = {
        "status": "complete",
        "method": "rendered_detail",
        "source_url": job["detail_url"],
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": sha256(job["jd_raw"].encode()).hexdigest(),
    }
    return payload


def _crawler_runner(tmp_path) -> ConfiguredCrawlerRunner:
    return ConfiguredCrawlerRunner(
        tmp_path,
        lambda _config, _request: _crawler_payload(),
    )


def test_extended_mcp_surface_and_classification_are_explicit(tmp_path) -> None:
    definitions = {definition.name: definition for definition in TOOL_DEFINITIONS}

    assert {
        "capabilities",
        "knowledge_search",
        "configured_crawler_run",
        "automation_plan",
        "application_capture",
        "operation_run",
        "daily_recruitment_sync",
        "daily_recruitment_sync_status",
        "recruitment_mail_process",
        "recruitment_mail_processing_status",
    } <= definitions.keys()
    assert all(
        definitions[name].read_only is True
        for name in (
            "capabilities",
            "knowledge_search",
            "configured_crawler_run",
            "automation_plan",
            "application_capture",
        )
    )
    assert definitions["operation_run"].read_only is False
    assert definitions["daily_recruitment_sync"].read_only is False
    assert definitions["daily_recruitment_sync_status"].read_only is True
    assert definitions["recruitment_mail_process"].read_only is False
    assert definitions["recruitment_mail_processing_status"].read_only is True
    assert "operation_run" in MCP_ACTION_TOOL_NAMES
    assert "daily_recruitment_sync" in MCP_ACTION_TOOL_NAMES
    assert "daily_recruitment_sync_status" in MCP_READ_ONLY_TOOL_NAMES
    assert "recruitment_mail_process" in MCP_ACTION_TOOL_NAMES
    assert "recruitment_mail_processing_status" in MCP_READ_ONLY_TOOL_NAMES
    assert {
        "capabilities",
        "knowledge_search",
        "configured_crawler_run",
        "automation_plan",
        "application_capture",
    } <= set(MCP_READ_ONLY_TOOL_NAMES)

    server = FakeMCPServer()
    registered = register_tools(
        server,
        InMemoryRepository(),
        _mail_store(),
        _browser_bridge_store(),
        evidence_grounder=_grounder(),
        crawler_runner=_crawler_runner(tmp_path),
        operational_task_runner=OperationalTaskRunner(
            LocalTaskScheduler(lock_path=tmp_path / "operation.lock"),
            {TaskType.CRAWLER_HEALTH.value: lambda _context: {"status": "ok"}},
        ),
        automation_scheduler=LocalTaskScheduler(lock_path=tmp_path / "automation.lock"),
    )

    assert tuple(server.tools) == registered
    assert registered == MCP_TOOL_NAMES
    assert {
        "capabilities",
        "knowledge_search",
        "configured_crawler_run",
        "automation_plan",
        "application_capture",
        "operation_run",
        "daily_recruitment_sync",
        "daily_recruitment_sync_status",
    } <= server.tools.keys()


def test_extended_tools_bind_dependencies_and_preserve_typed_results(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "packages.mcp.server.get_settings",
        lambda: type("Settings", (), {"write_enabled": True})(),
    )
    repository = InMemoryRepository()
    repository.applications = []
    server = FakeMCPServer()
    register_tools(
        server,
        repository,
        _mail_store(),
        _browser_bridge_store(),
        evidence_grounder=_grounder(),
        crawler_runner=_crawler_runner(tmp_path),
        operational_task_runner=OperationalTaskRunner(
            LocalTaskScheduler(lock_path=tmp_path / "operation.lock"),
            {TaskType.CRAWLER_HEALTH.value: lambda _context: {"status": "ok"}},
        ),
        automation_scheduler=LocalTaskScheduler(lock_path=tmp_path / "automation.lock"),
    )

    capabilities = server.tools["capabilities"]({})
    knowledge = server.tools["knowledge_search"]({"query": "Moka 分页"})
    crawler = server.tools["configured_crawler_run"]({"company": "示例公司"})
    plan = server.tools["automation_plan"](
        {"task_id": TaskType.CRAWLER_HEALTH.value, "start_time": "09:30"}
    )
    capture = server.tools["application_capture"](
        {
            "request_id": "capture-mcp-1",
            "url": "https://example.com/jobs/1",
            "title": "C++开发工程师",
            "page_text": "示例公司 C++开发工程师 投递成功",
        }
    )
    operation = server.tools["operation_run"](
        {"task_id": TaskType.CRAWLER_HEALTH.value}
    )

    assert isinstance(capabilities, CapabilitiesResponse)
    assert isinstance(knowledge, KnowledgeSearchResponse)
    assert knowledge.status is ToolStatus.SUCCESS
    refused = server.tools["knowledge_search"]({"query": "Kubernetes operator"})
    assert refused.status is ToolStatus.NO_RESULTS
    assert refused.error_code is ToolErrorCode.NO_RESULTS
    assert isinstance(crawler, ConfiguredCrawlerRunResponse)
    assert crawler.status is ToolStatus.SUCCESS
    assert plan.status is ToolStatus.SUCCESS
    assert plan.data is not None and plan.data.active is False
    assert capture.status is ToolStatus.SUCCESS
    assert capture.data is not None
    assert capture.data.capture_status == "approval_required"
    assert isinstance(operation, OperationalTaskRunResponse)
    assert operation.status is ToolStatus.SUCCESS
    assert operation.read_only is False
    assert operation.data is not None and operation.data.run_status == "success"


def test_missing_mcp_dependencies_fail_closed_with_structured_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        "packages.mcp.server.get_settings",
        lambda: type("Settings", (), {"write_enabled": True})(),
    )
    server = FakeMCPServer()
    register_tools(
        server,
        InMemoryRepository(),
        _mail_store(),
        _browser_bridge_store(),
    )

    failures = {
        "knowledge_search": server.tools["knowledge_search"]({"query": "Moka"}),
        "configured_crawler_run": server.tools["configured_crawler_run"](
            {"company": "示例公司"}
        ),
        "automation_plan": server.tools["automation_plan"](
            {"task_id": TaskType.CRAWLER_HEALTH.value, "start_time": "09:30"}
        ),
        "operation_run": server.tools["operation_run"](
            {"task_id": TaskType.CRAWLER_HEALTH.value}
        ),
    }

    for name, response in failures.items():
        assert response.status is ToolStatus.FAILURE, name
        assert response.success is False, name
        assert response.error_code is ToolErrorCode.SOURCE_UNAVAILABLE, name
        assert response.error_message and "not configured" in response.error_message
        assert response.evidence[0].source == "mcp_dependencies"
    assert failures["operation_run"].read_only is False


def test_mcp_keeps_async_handlers_and_structured_typed_response(monkeypatch) -> None:
    monkeypatch.setattr(
        "packages.mcp.server.get_settings",
        lambda: type("Settings", (), {"write_enabled": True})(),
    )
    server = FakeMCPServer()
    register_tools(
        server,
        InMemoryRepository(),
        _mail_store(),
        _browser_bridge_store(),
    )
    handler = server.tools["observe_application_status_page"]

    assert inspect.iscoroutinefunction(handler)

    async def invoke():
        return await handler(
            {
                "application_id": "application-1",
                "device_id": "edge-1",
                "idempotency_key": "mcp-async-1",
            }
        )

    response = asyncio.run(invoke())
    assert response.read_only is False
    assert response.success is False
    assert response.error_code is not None
    assert response.evidence


def test_dependencies_are_explicit_and_frozen() -> None:
    dependencies = MCPToolDependencies(
        repository=InMemoryRepository(),
        mail_store=_mail_store(),
    )

    assert dependencies.evidence_grounder is None
    assert dependencies.crawler_runner is None
    assert dependencies.operational_task_runner is None
    assert dependencies.automation_scheduler is None
