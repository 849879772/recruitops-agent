from __future__ import annotations

import asyncio
import re
from datetime import datetime, time, timezone
from hashlib import sha256
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Any, Callable, Mapping

import pytest
import yaml
from sqlalchemy import func, select

from apps.api.automation import CodexAutomationExecutor
from packages.automation import AutomationRunResult, AutomationStore, LocalAutomationWorker
from packages.codex_runtime.events import CodexEvent, CodexEventType
from packages.domain.models import Application, ApplicationStage
from packages.mcp import register_tools
from packages.pipeline import CrawlResult, DailyRecruitmentPipeline
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.scheduler import LocalTaskScheduler, TaskType, build_runtime_task_handlers
from packages.storage import JobAnalysisSnapshot, JobSnapshot, Storage
from packages.tools.operations import OperationalTaskRunner


UTC = timezone.utc
OLD_OCCURRENCE = datetime(2000, 1, 1, tzinfo=UTC)
LISTING_URL = "https://jobs.bytedance.com/campus"
DETAIL_URL = "https://jobs.bytedance.com/campus/position/123456/detail"
POST_ID = "123456"
DETAIL_TEXT = (
    "Responsibilities: Develop and maintain C++ services on Linux, write unit tests, "
    "and investigate production failures. Requirements: Experience with C++ and Linux."
)


@pytest.fixture
def isolated_storage(tmp_path: Path):
    """Use only a temporary Agent database and record every allowed write target."""

    database_path = tmp_path / "agent.sqlite"
    storage = Storage.from_url(
        f"sqlite:///{database_path.as_posix()}",
        initialize=True,
    )
    write_targets: list[str | None] = []

    def record_write_target(engine: Any) -> None:
        write_targets.append(engine.url.database)

    storage.pre_write_hook = record_write_target
    try:
        yield storage, database_path, write_targets
    finally:
        storage.engine.dispose()


class _FakeMcpServer:
    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *, name: str, description: str):
        del description

        def register(handler: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = handler
            return handler

        return register


class _FixtureCrawler:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = Lock()

    def __call__(self, company: Any) -> CrawlResult:
        with self._lock:
            self.calls.append(company.id)
        return CrawlResult(
            jobs=[
                {
                    "id": f"local-{company.id}",
                    "native_job_id": POST_ID,
                    "title": "C++ Software Engineer",
                    "city": "Shanghai",
                    "detail_url": DETAIL_URL,
                    "jd_raw": "",
                    "capture_evidence": {},
                    "cohort": 2027,
                    "cohort_status": "confirmed",
                    "batch": "formal",
                    "job_type": "campus",
                }
            ],
            source_url=LISTING_URL,
            allowed_origins=("https://jobs.bytedance.com",),
            pages_seen=1,
            total_pages=1,
            has_more=False,
            pagination_complete=True,
            completeness_known=True,
            advertised_total=1,
            crawler_key="fixture",
            run_reason="isolated_fixture",
        )


class _FixtureDetailFetch:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = Lock()

    def __call__(self, job: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.calls.append(str(job["company_id"]))
        return {
            "status": "complete",
            "detail": DETAIL_TEXT,
            "detail_url": DETAIL_URL,
            "source": "isolated_fixture_api",
            "identity_status": "request_bound",
            "identity_evidence": [
                f"native_id:{POST_ID}",
                f"title:{job['title']}",
            ],
            "capture_evidence": {
                "status": "complete",
                "method": "official_api",
                "source_url": DETAIL_URL,
                "identity_verified": True,
                "terminal_observed": True,
                "remaining_controls": [],
                "content_sha256": sha256(DETAIL_TEXT.encode("utf-8")).hexdigest(),
                "captured_at": datetime.now(timezone.utc).isoformat(),
            },
        }


class _DeterministicMatcher:
    """A local matcher stub; any model-shaped call would be a test failure."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._lock = Lock()

    def match(self, job: Mapping[str, Any], *, existing_analysis: Any = None) -> dict[str, Any]:
        del existing_analysis
        with self._lock:
            self.calls.append(str(job["id"]))
        return {
            "analysis_status": "complete",
            "match_score": 88,
            "summary": "deterministic fixture match",
            "recommendation": "recommend",
            "matched_directions": ["cpp_software"],
        }


class _RejectingModelCall:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("real model invocation is forbidden in this acceptance test")


class _Subscription:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[CodexEvent] = asyncio.Queue()
        self.closed = False

    async def get(self) -> CodexEvent:
        return await self.queue.get()

    def close(self) -> None:
        self.closed = True


def _event(event_type: CodexEventType, *, text: str | None = None) -> CodexEvent:
    return CodexEvent(
        event_type=event_type,
        method=event_type.value,
        thread_id="scheduled-thread",
        turn_id="scheduled-turn",
        text=text,
    )


class _NoModelCodexService:
    """Deterministically replace only Codex's model decision/transport boundary."""

    def __init__(
        self,
        *,
        operation_handler: Callable[[Mapping[str, Any]], Any] | None = None,
        rejecting_model: _RejectingModelCall | None = None,
    ) -> None:
        self.operation_handler = operation_handler
        self.rejecting_model = rejecting_model
        self.prompts: list[str] = []
        self.subscription: _Subscription | None = None
        self.operation_response: Any | None = None
        self.interrupted: list[tuple[str, str]] = []

    async def thread_start(self) -> SimpleNamespace:
        return SimpleNamespace(id="scheduled-thread")

    def subscribe(self, thread_id: str) -> _Subscription:
        assert thread_id == "scheduled-thread"
        self.subscription = _Subscription()
        return self.subscription

    async def turn_start(self, thread_id: str, prompt: str) -> SimpleNamespace:
        assert thread_id == "scheduled-thread"
        self.prompts.append(prompt)
        if self.operation_handler is None:
            summary = "model-free prompt boundary completed"
        else:
            match = re.search(r"task_id=([a-z_]+)", prompt)
            assert match is not None, prompt
            self.operation_response = self.operation_handler({"task_id": match.group(1)})
            assert self.operation_response.success is True
            summary = f"operation_run completed: {self.operation_response.data.run_status}"
        assert self.subscription is not None
        self.subscription.queue.put_nowait(_event(CodexEventType.TEXT_DELTA, text=summary))
        self.subscription.queue.put_nowait(_event(CodexEventType.TURN_COMPLETED))
        return SimpleNamespace(id="scheduled-turn")

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> None:
        self.interrupted.append((thread_id, turn_id))


class _GuardedRepository:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_applications(self) -> list[Application]:
        self.calls.append("list_applications")
        return [
            Application(
                id="application-1",
                company_name="Fixture Co",
                job_title="C++ Software Engineer",
                record_url="https://example.test/application/1",
                stage=ApplicationStage.APPLIED,
                idempotency_key="application-1",
                source="fixture",
                source_ref="application-1",
            )
        ]

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"non-target repository method was called: {name}")


class _RejectingDailyPipeline:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, **_kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("non-daily scheduled task was routed to daily crawl")


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        mail_enabled=False,
        discovery_enabled=False,
        offline_reconciliation_enabled=False,
        llm_enabled=False,
        job_analysis_enabled=False,
    )


def _write_companies(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "companies": [
                    {
                        "id": "company-a",
                        "name": "Fixture A",
                        "careers_url": LISTING_URL,
                        "crawler": "fixture",
                        "integration_status": "connected",
                    },
                    {
                        "id": "company-b",
                        "name": "Fixture B",
                        "careers_url": LISTING_URL,
                        "crawler": "fixture",
                        "integration_status": "connected",
                    },
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


def _operation_handler(
    *,
    storage: Storage,
    lock_path: Path,
    repository: Any,
    daily_pipeline: Any,
) -> Callable[[Mapping[str, Any]], Any]:
    handlers = build_runtime_task_handlers(
        settings=_settings(),
        repository=repository,
        daily_pipeline=daily_pipeline,
    )
    operation_runner = OperationalTaskRunner(
        LocalTaskScheduler(lock_path=lock_path),
        handlers,
    )
    server = _FakeMcpServer()
    from packages.recruitment_mail import RecruitmentMailStore

    register_tools(
        server,
        repository,
        RecruitmentMailStore(storage),
        operational_task_runner=operation_runner,
    )
    return server.tools["operation_run"]


def _assert_temp_only_writes(
    database_path: Path,
    write_targets: list[str | None],
) -> None:
    assert write_targets
    assert {
        Path(str(target)).resolve()
        for target in write_targets
        if target is not None
    } == {database_path.resolve()}


def test_due_daily_schedule_reaches_pipeline_and_captures_verified_detail(
    tmp_path: Path,
    isolated_storage,
) -> None:
    storage, database_path, write_targets = isolated_storage
    companies_path = _write_companies(tmp_path / "companies.yaml")
    crawler = _FixtureCrawler()
    fetch = _FixtureDetailFetch()
    matcher = _DeterministicMatcher()
    pipeline = DailyRecruitmentPipeline(
        companies_path=companies_path,
        storage=storage,
        crawler=crawler,
        matcher=matcher,
        jd_hydrator=fetch,
        max_concurrency=2,
        match_max_concurrency=2,
    )
    repository = PostgresRecruitmentRepository(storage)
    operation = _operation_handler(
        storage=storage,
        lock_path=tmp_path / "operation.lock",
        repository=repository,
        daily_pipeline=pipeline,
    )
    rejecting_model = _RejectingModelCall()
    codex_service = _NoModelCodexService(
        operation_handler=operation,
        rejecting_model=rejecting_model,
    )
    automation_store = AutomationStore(storage)
    schedule = automation_store.upsert_daily(
        task_id=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
        task_label="fixture daily",
        start_time=time(3, 0),
        now=OLD_OCCURRENCE,
    )
    worker = LocalAutomationWorker(
        automation_store,
        CodexAutomationExecutor(codex_service, automation_store, timeout_seconds=10),
    )

    assert asyncio.run(worker.run_once()) is True

    executions = automation_store.executions(schedule.id)
    assert len(executions) == 1
    execution = executions[0]
    assert execution.status == "succeeded"
    assert execution.thread_id == "scheduled-thread"
    assert execution.turn_id == "scheduled-turn"
    assert execution.result_summary == "operation_run completed: success"
    assert len(codex_service.prompts) == 1
    assert "operation_run" in codex_service.prompts[0]
    assert f"task_id={TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value}" in codex_service.prompts[0]
    assert rejecting_model.calls == 0
    assert codex_service.interrupted == []

    operation_response = codex_service.operation_response
    assert operation_response is not None
    assert operation_response.data is not None
    assert operation_response.data.run_status == "success"
    result = operation_response.data.result
    assert result["status"] == "completed"
    assert result["sync_status"] == "succeeded"
    assert result["agent_write_performed"] is True
    assert result["source_write_attempted"] is False

    daily_sync = result["daily_sync"]
    assert daily_sync["status"] == "succeeded"
    assert any(
        stage["stage"] == "crawl" and stage["status"] == "succeeded"
        for stage in daily_sync["stages"]
    )
    pipeline_result = daily_sync["pipeline"]
    assert pipeline_result["written"] is True
    assert pipeline_result["new"] == 2
    detail_rows = [
        row
        for company in pipeline_result["companies"]
        for row in company["jd_results"]
    ]
    assert len(detail_rows) == 2
    assert all(row["status"] == "complete" for row in detail_rows)
    # Title-first diagnostics describe the capture itself.  The old
    # detail_reuse payload was a 24-hour cache contract and is not emitted
    # for new rows; cross-company rows remain distinct despite the same URL.
    assert all("detail_reuse" not in row for row in detail_rows)
    assert all(row["capture_evidence"]["status"] == "complete" for row in detail_rows)
    assert daily_sync["report"]["safety"]["model_call_attempted"] is False

    assert sorted(crawler.calls) == ["company-a", "company-b"]
    assert len(fetch.calls) == 2
    assert len(matcher.calls) == 2
    with storage.session() as session:
        assert session.scalar(select(func.count()).select_from(JobSnapshot)) == 2
        assert session.scalar(select(func.count()).select_from(JobAnalysisSnapshot)) == 2
    _assert_temp_only_writes(database_path, write_targets)


@pytest.mark.parametrize(
    "task_id",
    [TaskType.APPLICATION_PROGRESS.value, TaskType.RECRUITMENT_MAILBOX.value],
)
def test_non_daily_schedules_do_not_route_to_daily_crawl(
    task_id: str,
    tmp_path: Path,
    isolated_storage,
) -> None:
    storage, database_path, write_targets = isolated_storage
    repository = _GuardedRepository()
    daily_pipeline = _RejectingDailyPipeline()
    operation = _operation_handler(
        storage=storage,
        lock_path=tmp_path / f"{task_id}.operation.lock",
        repository=repository,
        daily_pipeline=daily_pipeline,
    )
    automation_store = AutomationStore(storage)
    schedule = automation_store.upsert_daily(
        task_id=task_id,
        task_label=f"fixture {task_id}",
        start_time=time(3, 0),
        now=OLD_OCCURRENCE,
    )
    seen: list[dict[str, Any]] = []

    async def execute(claimed) -> AutomationRunResult:
        assert claimed.task_id == task_id
        response = operation({"task_id": claimed.task_id})
        assert response.success is True
        assert response.data is not None
        assert response.data.agent_write_enabled is False
        seen.append(response.data.result)
        return AutomationRunResult(
            status="succeeded",
            summary=f"deterministic {task_id} route",
        )

    assert asyncio.run(LocalAutomationWorker(automation_store, execute).run_once()) is True

    assert len(seen) == 1
    result = seen[0]
    assert result["source_write_attempted"] is False
    assert daily_pipeline.calls == 0
    if task_id == TaskType.APPLICATION_PROGRESS.value:
        assert result["status"] == "waiting_browser"
        assert result["browser_navigation_attempted"] is False
        assert result["reviewable_page_count"] == 1
        assert repository.calls == ["list_applications"]
    else:
        assert result["status"] == "disabled"
        assert repository.calls == []

    executions = automation_store.executions(schedule.id)
    assert len(executions) == 1
    assert executions[0].status == "succeeded"
    _assert_temp_only_writes(database_path, write_targets)


def test_default_executor_is_codex_model_dispatch_boundary(
    isolated_storage,
) -> None:
    storage, _database_path, _write_targets = isolated_storage
    automation_store = AutomationStore(storage)
    schedule = automation_store.upsert_daily(
        task_id=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
        task_label="fixture daily",
        start_time=time(3, 0),
        now=OLD_OCCURRENCE,
    )
    rejecting_model = _RejectingModelCall()
    codex_service = _NoModelCodexService(rejecting_model=rejecting_model)
    worker = LocalAutomationWorker(
        automation_store,
        CodexAutomationExecutor(codex_service, automation_store, timeout_seconds=10),
    )

    assert asyncio.run(worker.run_once()) is True

    assert len(codex_service.prompts) == 1
    assert "operation_run" in codex_service.prompts[0]
    assert f"task_id={TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value}" in codex_service.prompts[0]
    assert rejecting_model.calls == 0
    assert codex_service.operation_response is None
    executions = automation_store.executions(schedule.id)
    assert len(executions) == 1
    assert executions[0].status == "succeeded"
