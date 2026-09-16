from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from packages.domain.models import (
    Approval as DomainApproval,
    Application,
    ApplicationStage,
    Company,
    Job,
    JobAnalysis,
    JobDetail,
    JobPage,
    RecruitmentBatch,
    ScheduleEvent,
    TaskRun as DomainTaskRun,
    TaskStatus,
    ToolCall as DomainToolCall,
)
from packages.storage import (
    AgentStateStore,
    Approval,
    ApplicationSnapshot,
    CompanySnapshot,
    JobAnalysisSnapshot,
    JobSnapshot,
    ScheduleEventSnapshot,
    SnapshotSyncService,
    Storage,
    TaskRun,
    ToolCall,
    WriteAudit,
    create_storage_engine,
    initialize_schema,
)


UTC = timezone.utc


def _storage(pre_write_hook=None) -> Storage:
    engine = create_storage_engine("sqlite:///:memory:")
    initialize_schema(engine)
    return Storage(engine, pre_write_hook=pre_write_hook)


def _company() -> Company:
    return Company(
        id="company-1",
        name="Example Co",
        aliases=["Example"],
        campus_url="https://example.com/campus",
        crawler_key="example",
        integration_status="connected",
        source="fixture",
        source_ref="companies[0]",
    )


def _job() -> Job:
    timestamp = datetime(2026, 8, 19, 1, 0, tzinfo=UTC)
    return Job(
        id="job-1",
        company_id="company-1",
        title="Python Engineer",
        city="Shanghai",
        detail_url="https://example.com/jobs/1",
        jd_raw="Build useful systems",
        cohort=2027,
        cohort_status="confirmed",
        batch=RecruitmentBatch.FORMAL,
        match_score=88,
        first_seen_at=timestamp,
        last_seen_at=timestamp,
        source="fixture",
        source_ref="job-1",
        created_at=timestamp,
        updated_at=timestamp,
    )


class _RepositoryFixture:
    def __init__(self) -> None:
        self.write_calls = 0
        self.company = _company()
        self.job = _job()
        self.application = Application(
            id="application-1",
            company_name="Example Co",
            job_title="Python Engineer",
            job_id="job-1",
            record_url="https://example.com/application/1",
            stage=ApplicationStage.APPLIED,
            idempotency_key="application:1",
            source="fixture",
            source_ref="application-1",
        )
        self.event = ScheduleEvent(
            id="event-1",
            title="Written test",
            event_date=date(2026, 8, 20),
            event_type="written",
            company_name="Example Co",
            job_title="Python Engineer",
            application_stage=ApplicationStage.APPLIED,
            application_id="application-1",
            location_or_link="online",
            source="fixture",
            source_ref="event-1",
        )

    def search_jobs(self, **kwargs):
        return JobPage(
            items=[self.job.model_dump(mode="python")],
            total=1,
            limit=kwargs["limit"],
            offset=kwargs["offset"],
        )

    def get_job(self, job_id: str):
        return JobDetail(
            job=self.job,
            analysis=JobAnalysis(
                match_score=88,
                summary="Strong fit",
                evidence=[{"source": "fixture", "text": "Python"}],
                evidence_level="verified",
            ),
        )

    def list_companies(self):
        return [self.company]

    def list_applications(self):
        return [self.application]

    def list_schedule(self, on_date=None):
        return [self.event]


def test_schema_contains_agent_state_and_snapshot_tables() -> None:
    storage = _storage()
    tables = set(inspect(storage.engine).get_table_names())

    assert {
        "task_runs",
        "approvals",
        "tool_calls",
        "company_snapshots",
        "job_snapshots",
        "job_analysis_snapshots",
        "application_snapshots",
        "schedule_event_snapshots",
        "write_audits",
    } <= tables
    approval_constraints = inspect(storage.engine).get_unique_constraints("approvals")
    assert {item["name"] for item in approval_constraints} >= {
        "uq_approvals_idempotency_key"
    }


def test_write_audit_persists_evidence_diffs_backup_rollback_and_failure() -> None:
    storage = _storage()
    started = datetime(2026, 8, 19, 2, 0, tzinfo=UTC)
    audit = WriteAudit(
        execution_id="execution-1",
        token_id="token-1",
        task_id="task-1",
        operation="application_stage_update",
        idempotency_key="application-stage:1",
        operator="local-user",
        evidence=[{"source": "approval", "source_ref": "evidence-1"}],
        evidence_digest="digest-1",
        before_diff={"stage": "applied"},
        after_diff={"stage": "written"},
        backup_ref="backups/20260819-020000",
        rollback_payload={"operation": "restore_stage", "stage": "applied"},
        started_at=started,
        completed_at=started,
        success=False,
        error_code="RuntimeError",
    )

    AgentStateStore(storage).save_write_audit(audit)

    with storage.session() as session:
        saved = session.scalar(select(WriteAudit).where(WriteAudit.execution_id == "execution-1"))

    assert saved is not None
    assert saved.operator == "local-user"
    assert saved.evidence == [{"source": "approval", "source_ref": "evidence-1"}]
    assert saved.before_diff == {"stage": "applied"}
    assert saved.after_diff == {"stage": "written"}
    assert saved.backup_ref == "backups/20260819-020000"
    assert saved.rollback_payload == {"operation": "restore_stage", "stage": "applied"}
    assert saved.success is False
    assert saved.error_code == "RuntimeError"


def test_write_transaction_rolls_back_and_calls_backup_hook_first() -> None:
    calls = []
    storage = _storage(lambda engine: calls.append(engine))
    task = TaskRun(
        id="task-rollback",
        task_type="test",
        user_request="rollback",
        source="agent",
    )

    with pytest.raises(RuntimeError, match="abort"):
        with storage.write_transaction() as session:
            session.add(task)
            raise RuntimeError("abort")

    with storage.session() as session:
        assert session.scalar(select(TaskRun).where(TaskRun.id == task.id)) is None
    assert calls == [storage.engine]


def test_approval_idempotency_is_unique() -> None:
    storage = _storage()
    with storage.write_transaction() as session:
        session.add(
            TaskRun(
                id="task-1",
                task_type="approval",
                user_request="approve",
                source="agent",
            )
        )
        session.flush()
        session.add(
            Approval(
                id="approval-1",
                task_id="task-1",
                operation="submit",
                preview={},
                idempotency_key="approval:1",
                source="agent",
            )
        )

    with pytest.raises(IntegrityError):
        with storage.write_transaction() as session:
            session.add(
                Approval(
                    id="approval-2",
                    task_id="task-1",
                    operation="submit-again",
                    preview={},
                    idempotency_key="approval:1",
                    source="agent",
                )
            )


def test_snapshot_sync_upserts_without_source_write() -> None:
    repository = _RepositoryFixture()
    backup_calls = []
    storage = _storage(lambda engine: backup_calls.append(engine))
    service = SnapshotSyncService(storage, repository, page_size=10)

    first = service.sync_all(hydrate_job_details=True)
    second = service.sync_all(hydrate_job_details=True)

    expected = {
        "companies": 1,
        "jobs": 1,
        "applications": 1,
        "schedule_events": 1,
        "analyses": 1,
    }
    assert first.__dict__ == second.__dict__ == expected
    assert repository.write_calls == 0
    assert len(backup_calls) == 2
    with storage.session() as session:
        assert session.scalar(select(CompanySnapshot).where(CompanySnapshot.id == "company-1"))
        assert session.scalar(select(JobSnapshot).where(JobSnapshot.id == "job-1"))
        assert session.scalar(
            select(JobAnalysisSnapshot).where(JobAnalysisSnapshot.job_id == "job-1")
        )
        assert session.scalar(
            select(ApplicationSnapshot).where(ApplicationSnapshot.id == "application-1")
        )
        assert session.scalar(
            select(ScheduleEventSnapshot).where(ScheduleEventSnapshot.id == "event-1")
        )


def test_agent_state_store_uses_stable_idempotency_keys() -> None:
    storage = _storage()
    store = AgentStateStore(storage)
    task = DomainTaskRun(
        id="task-1",
        task_type="search",
        user_request="find jobs",
        source="agent",
    )
    store.save_task_run(task)
    store.save_task_run(task.model_copy(update={"status": TaskStatus.RUNNING}))
    persisted = store.get_task_run("task-1")
    assert persisted is not None
    assert persisted["run_status"] == "running"
    assert persisted["task_id"] == "search"
    assert store.update_task_progress("task-1", "matching:25/100") is True
    assert store.update_task_progress("missing-task", "matching:1/1") is False
    assert store.get_task_run("task-1")["current_step"] == "matching:25/100"
    store.save_tool_call(
        DomainToolCall(
            id="call-1",
            task_id="task-1",
            tool_name="search_jobs",
            arguments={"query": "Python"},
            source="agent",
        )
    )
    with storage.session() as session:
        assert session.scalar(select(TaskRun).where(TaskRun.id == "task-1")).status == "running"
        assert session.scalar(select(ToolCall).where(ToolCall.id == "call-1")) is not None
