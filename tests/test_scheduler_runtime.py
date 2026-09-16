from datetime import date, datetime, timezone
from types import SimpleNamespace

import packages.scheduler.runtime as scheduler_runtime
from packages.domain.models import (
    Application,
    ApplicationStage,
    Company,
    Job,
    JobPage,
    RecruitmentBatch,
)
from packages.recruitment_mail import (
    MailIdentity,
    ParsedRecruitmentEmail,
    RecruitmentMailProcessingStatus,
    RecruitmentMailStore,
    RecruitmentMessageCategory,
)
from packages.scheduler import TaskContext, TaskType, build_runtime_task_handlers
from packages.storage import Storage


class Repository:
    def search_jobs(self, **_kwargs):
        return JobPage(
            items=[
                Job(
                    id="job-1", company_id="company-1", title="C++",
                    detail_url="https://example.com/job/1", cohort=2027,
                    cohort_status="confirmed", batch=RecruitmentBatch.FORMAL,
                    source="test", source_ref="job-1",
                )
            ],
            total=1, limit=20, offset=0,
        )

    def list_companies(self):
        return [
            Company(id="company-1", name="A", integration_status="connected", source="test", source_ref="1"),
            Company(id="company-2", name="B", integration_status="pending", source="test", source_ref="2"),
        ]

    def list_applications(self):
        return [
            Application(
                id="app-1", company_name="A", job_title="C++", stage=ApplicationStage.APPLIED,
                idempotency_key="app-1", record_url="https://example.com/app/1",
                source="test", source_ref="app-1",
            )
        ]


def _context(task_id: str, *, metadata: dict[str, object] | None = None) -> TaskContext:
    return TaskContext(
        task_id=task_id,
        task_label=task_id,
        scheduled_for=datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc),
        run_id="run-1",
        attempt=1,
        metadata=metadata or {},
    )


def test_runtime_handlers_observe_agent_repository_without_source_writes() -> None:
    settings = SimpleNamespace(mail_enabled=False)
    handlers = build_runtime_task_handlers(settings=settings, repository=Repository())

    jobs = handlers[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value](
        _context(TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value)
    )
    health = handlers[TaskType.CRAWLER_HEALTH.value](
        _context(TaskType.CRAWLER_HEALTH.value)
    )
    applications = handlers[TaskType.APPLICATION_PROGRESS.value](
        _context(TaskType.APPLICATION_PROGRESS.value)
    )
    mail = handlers[TaskType.RECRUITMENT_MAILBOX.value](
        _context(TaskType.RECRUITMENT_MAILBOX.value)
    )

    assert jobs["confirmed_2027_new_jobs"] == 1
    assert health["integration_status_counts"] == {"connected": 1, "pending": 1}
    assert applications["reviewable_page_count"] == 1
    assert mail["status"] == "disabled"
    assert all(
        result["source_write_attempted"] is False
        for result in (jobs, health, applications, mail)
    )


def test_runtime_defaults_to_agent_postgres_repository(monkeypatch) -> None:
    calls: list[object] = []

    class StorageFactory:
        @classmethod
        def from_url(cls, database_url):
            calls.append(("storage", database_url))
            return "agent-storage"

    class RepositoryFactory:
        def __init__(self, storage):
            calls.append(("repository", storage))

    monkeypatch.setattr(scheduler_runtime, "Storage", StorageFactory)
    monkeypatch.setattr(
        scheduler_runtime,
        "PostgresRecruitmentRepository",
        RepositoryFactory,
    )

    scheduler_runtime.build_runtime_task_handlers(
        settings=SimpleNamespace(
            database_url="postgresql+psycopg://agent",
            mail_enabled=False,
        )
    )

    assert calls == [
        ("storage", "postgresql+psycopg://agent"),
        ("repository", "agent-storage"),
    ]


def test_mailbox_runtime_links_unique_application_without_advancing_stage(
    monkeypatch,
) -> None:
    store = RecruitmentMailStore(Storage.from_url("sqlite+pysqlite:///:memory:"))
    record = store.upsert(
        ParsedRecruitmentEmail(
            identity=MailIdentity(message_id="mail-unique"),
            subject="A C++ 测评通知",
            body_text="公司：A\n职位：C++\n请完成在线测评。",
            category=RecruitmentMessageCategory.ASSESSMENT,
            confidence=0.95,
            company_candidates=[{"value": "A", "confidence": 1.0}],
            job_candidates=[{"value": "C++", "confidence": 1.0}],
        )
    )
    repository = Repository()
    original_stage = repository.list_applications()[0].stage

    events: list[tuple[object, ...]] = []
    settings = SimpleNamespace(
        mail_enabled=True,
        write_enabled=True,
        llm_enabled=True,
    )
    processing_summary = {
        "status": "completed",
        "processed": 1,
        "updated": 1,
        "unchanged": 0,
        "irrelevant": 0,
        "unresolved": 0,
        "failed": 0,
        "results": [{"record_id": record.id, "state": "processed_updated"}],
    }

    def sync(received_settings, received_store, **kwargs):
        events.append(("sync", received_settings, received_store, kwargs))
        return SimpleNamespace(
            model_dump=lambda **_kwargs: {
                "fetched": 1,
                "inserted": 1,
                "reused": 0,
                "next_cursor": "1",
            }
        )

    def process(received_store, received_repository, received_settings, *, limit):
        events.append(
            (
                "process",
                received_store,
                received_repository,
                received_settings,
                limit,
            )
        )
        return processing_summary

    monkeypatch.setattr(
        scheduler_runtime,
        "sync_configured_mail",
        sync,
    )
    monkeypatch.setattr(scheduler_runtime, "process_pending_mail", process)
    monkeypatch.setattr(
        scheduler_runtime,
        "_link_unambiguous_recruitment_mail",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("scheduled mailbox must not call the legacy updater")
        ),
    )
    handlers = build_runtime_task_handlers(
        settings=settings,
        repository=repository,
        mail_store=store,
    )

    result = handlers[TaskType.RECRUITMENT_MAILBOX.value](
        _context(TaskType.RECRUITMENT_MAILBOX.value)
    )

    updated = store.get(record_id=record.id)
    assert [event[0] for event in events] == ["sync", "process"]
    assert events[1][1:] == (store, repository, settings, 20)
    assert result["status"] == "synced"
    assert result["fetched"] == 1
    assert result["processing"] == processing_summary
    assert result["processing_status"] == "completed"
    assert result["results"] == processing_summary["results"]
    assert result["processed"] == 1
    assert result["updated"] == 1
    assert result["unchanged"] == 0
    assert result["association_reviewed"] == 1
    assert result["association_linked"] == 1
    assert result["approval_previews"] == 0
    assert updated is not None
    assert updated.application_id is None
    assert updated.processing_status == RecruitmentMailProcessingStatus.PENDING.value
    assert repository.list_applications()[0].stage is original_stage


def test_daily_runtime_executes_injected_pipeline_with_agent_write_permission() -> None:
    class Pipeline:
        def run(self, *, dry_run=False):
            assert dry_run is False
            return SimpleNamespace(
                written=True,
                to_dict=lambda: {"new_count": 2, "reused_count": 7},
            )

    handlers = build_runtime_task_handlers(
        settings=SimpleNamespace(mail_enabled=False),
        repository=Repository(),
        daily_pipeline=Pipeline(),
    )
    context = _context(TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value)
    context = TaskContext(**(context.__dict__ | {"write_enabled": True}))

    result = handlers[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value](context)

    assert result["status"] == "completed"
    assert result["new_count"] == 2
    assert result["reused_count"] == 7
    assert result["agent_write_performed"] is True
    assert result["source_write_attempted"] is False


def test_score_only_mode_does_not_invoke_crawler_pipeline() -> None:
    calls = []

    class Pipeline:
        def run(self, *, dry_run=False):
            calls.append(dry_run)
            raise AssertionError("score_only must not crawl")

    handlers = build_runtime_task_handlers(
        settings=SimpleNamespace(mail_enabled=False),
        repository=Repository(),
        daily_pipeline=Pipeline(),
    )
    context = _context(
        TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
        metadata={"details": {"mode": "score_only"}},
    )
    context = TaskContext(**(context.__dict__ | {"write_enabled": True}))

    result = handlers[TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value](context)

    assert result["status"] == "completed"
    assert result["effective_mode"] == "score_only"
    assert result["analysis_enabled"] is False
    assert calls == []


def test_crawler_health_wires_structured_reporting_without_live_crawl() -> None:
    handlers = build_runtime_task_handlers(
        settings=SimpleNamespace(mail_enabled=False),
        repository=Repository(),
    )
    context = _context(
        TaskType.CRAWLER_HEALTH.value,
        metadata={
            "daily_pipeline_result": {
                "companies": [
                    {
                        "company_id": "company-1",
                        "company_name": "A",
                        "status": "failed",
                        "raw_job_count": 0,
                        "failure_reason": "crawler_failed",
                    }
                ]
            },
            "previous_daily_pipeline_result": {
                "companies": [
                    {
                        "company_id": "company-1",
                        "company_name": "A",
                        "raw_job_count": 4,
                    }
                ]
            },
        },
    )

    result = handlers[TaskType.CRAWLER_HEALTH.value](context)

    assert result["integration_status_counts"] == {"connected": 1, "pending": 1}
    assert result["live_crawler_run_attempted"] is False
    assert result["source_write_attempted"] is False
    assert result["daily_summary"]["source"] == "daily_pipeline_result"
    assert result["crawler_health"]["issue_counts"]["crawler_failed"] == 1
    assert result["report"]["safety"]["model_call_attempted"] is False
