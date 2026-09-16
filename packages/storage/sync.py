from __future__ import annotations

from dataclasses import dataclass
import json
from collections.abc import Mapping
import hashlib
from typing import Any

from pydantic import BaseModel
from sqlalchemy import and_, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.schema import Table

from packages.domain.models import (
    Approval as DomainApproval,
    Application,
    Company,
    Job,
    JobAnalysis,
    JobDetail,
    ScheduleEvent,
    TaskRun as DomainTaskRun,
    ToolCall as DomainToolCall,
)
from packages.repositories.base import RecruitmentRepository

from .database import Storage
from .models import (
    AVAILABILITY_STATUSES,
    Approval,
    ApplicationSnapshot,
    CAPTURE_STATUSES,
    CompanySnapshot,
    JobAnalysisSnapshot,
    JobSnapshot,
    ScheduleEventSnapshot,
    TaskRun,
    ToolCall,
    WriteAudit,
    utc_now,
)


def _upsert(
    session: Session,
    table: Table,
    values: dict[str, Any],
    *,
    conflict_columns: tuple[str, ...],
) -> None:
    """Use a native PostgreSQL/SQLite upsert, with a portable fallback."""

    dialect = session.get_bind().dialect.name
    update_values = {
        key: value
        for key, value in values.items()
        if key not in {"id", "created_at", *conflict_columns}
    }
    if dialect == "sqlite":
        statement = sqlite_insert(table).values(**values)
        if update_values:
            statement = statement.on_conflict_do_update(
                index_elements=list(conflict_columns),
                set_={key: getattr(statement.excluded, key) for key in update_values},
            )
        else:
            statement = statement.on_conflict_do_nothing(
                index_elements=list(conflict_columns)
            )
        session.execute(statement)
        return
    if dialect == "postgresql":
        statement = postgresql_insert(table).values(**values)
        if update_values:
            statement = statement.on_conflict_do_update(
                index_elements=list(conflict_columns),
                set_={key: getattr(statement.excluded, key) for key in update_values},
            )
        else:
            statement = statement.on_conflict_do_nothing(
                index_elements=list(conflict_columns)
            )
        session.execute(statement)
        return

    predicates = [table.c[key] == values[key] for key in conflict_columns]
    identity_column = table.c[conflict_columns[0]]
    exists = session.execute(select(identity_column).where(and_(*predicates))).first()
    if exists:
        if update_values:
            session.execute(update(table).where(and_(*predicates)).values(**update_values))
    else:
        session.execute(insert(table).values(**values))


def _audit_values(model: Any, *, source_ref: str | None = None) -> dict[str, Any]:
    return {
        "created_at": model.created_at,
        "updated_at": model.updated_at,
        "source": model.source,
        "source_ref": source_ref if source_ref is not None else model.source_ref,
    }


def _write_audit_values(record: Any) -> dict[str, Any]:
    """Normalize the approval executor's audit record into storage fields."""

    execution_id = getattr(record, "execution_id", None) or getattr(record, "id", None)
    if not execution_id:
        raise ValueError("write audit requires an execution_id")

    operation = getattr(record, "operation", "")
    operation = getattr(operation, "value", operation)
    before_diff = getattr(record, "before_diff", None)
    if before_diff is None:
        before_diff = getattr(record, "before", None)
    after_diff = getattr(record, "after_diff", None)
    if after_diff is None:
        after_diff = getattr(record, "after", None)
    rollback_payload = getattr(record, "rollback_payload", None)
    if rollback_payload is None:
        rollback_payload = getattr(record, "rollback", None)
    backup_ref = getattr(record, "backup_ref", None)
    if backup_ref is None:
        backup_ref = getattr(record, "backup", None)
    if backup_ref is not None:
        backup_ref = str(backup_ref)

    started_at = getattr(record, "started_at", None) or utc_now()
    completed_at = getattr(record, "completed_at", None) or utc_now()
    evidence = getattr(record, "evidence", None)
    if evidence is None:
        evidence = []

    return {
        "execution_id": str(execution_id),
        "token_id": str(getattr(record, "token_id", "")),
        "task_id": str(getattr(record, "task_id", "")),
        "operation": str(operation),
        "idempotency_key": str(
            getattr(record, "idempotency_key", None) or f"write_audit:{execution_id}"
        ),
        "operator": str(getattr(record, "operator", "")),
        "evidence": evidence,
        "evidence_digest": getattr(record, "evidence_digest", None),
        "before_diff": before_diff,
        "after_diff": after_diff,
        "backup_ref": backup_ref,
        "rollback_payload": rollback_payload,
        "started_at": started_at,
        "completed_at": completed_at,
        "success": bool(getattr(record, "success", False)),
        "error_code": getattr(record, "error_code", None)
        or getattr(record, "error", None),
    }


def _url(value: Any) -> str | None:
    return str(value) if value is not None else None


def upsert_company_snapshot(session: Session, company: Company) -> None:
    values = {
        **_audit_values(company),
        "id": company.id,
        "name": company.name,
        "aliases": list(company.aliases),
        "campus_url": _url(company.campus_url),
        "crawler_key": company.crawler_key,
        "integration_status": company.integration_status,
        "organization_id": company.organization_id,
        "recruitment_unit_name": company.recruitment_unit_name,
        "source_identity": company.source_identity,
    }
    _upsert(session, CompanySnapshot.__table__, values, conflict_columns=("id",))


def _validated_snapshot_status(value: Any, field: str, allowed: frozenset[str]) -> str:
    normalized = str(value).strip()
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{field} must be one of: {choices}")
    return normalized


def _optional_job_field(job: Any, name: str, override: Any) -> Any:
    if override is not None:
        return override
    # Pydantic defaults are present on every new Job, but should not be treated
    # as an explicit write when syncing an older caller's object. This keeps a
    # persisted failed/inactive snapshot intact unless the caller opts in.
    fields_set = getattr(job, "model_fields_set", None)
    if fields_set is not None and name not in fields_set:
        return None
    return getattr(job, name, None)


def upsert_job_snapshot(
    session: Session,
    job: Job,
    *,
    capture_status: str | None = None,
    capture_failure_reason: str | None = None,
    availability_status: str | None = None,
    title_key: str | None = None,
) -> None:
    values = {
        **_audit_values(job),
        "id": job.id,
        "company_id": job.company_id,
        "title": job.title,
        "city": job.city,
        "detail_url": str(job.detail_url),
        "jd_raw": job.jd_raw,
        "cohort": job.cohort,
        "cohort_status": job.cohort_status,
        "batch": job.batch.value,
        "match_score": job.match_score,
        "first_seen_at": job.first_seen_at,
        "last_seen_at": job.last_seen_at,
        "organization_id": job.organization_id,
        "recruitment_unit_id": job.recruitment_unit_id,
        "recruitment_campaign_id": job.recruitment_campaign_id,
        "source_platform": job.source_platform,
        "source_tenant": job.source_tenant,
        "native_job_id": job.native_job_id,
        "normalized_detail_url": job.normalized_detail_url,
        "business_key": job.business_key,
        "capture_evidence": dict(job.capture_evidence),
    }
    stored_capture_status = _optional_job_field(job, "capture_status", capture_status)
    if stored_capture_status is not None:
        values["capture_status"] = _validated_snapshot_status(
            stored_capture_status, "capture_status", CAPTURE_STATUSES
        )
    stored_failure_reason = _optional_job_field(
        job, "capture_failure_reason", capture_failure_reason
    )
    if stored_failure_reason is not None:
        values["capture_failure_reason"] = str(stored_failure_reason)
    stored_availability = _optional_job_field(job, "availability_status", availability_status)
    if stored_availability is not None:
        values["availability_status"] = _validated_snapshot_status(
            stored_availability, "availability_status", AVAILABILITY_STATUSES
        )
    stored_title_key = _optional_job_field(job, "title_key", title_key)
    if stored_title_key is not None:
        normalized_title_key = str(stored_title_key).strip()
        values["title_key"] = normalized_title_key or None
    _upsert(session, JobSnapshot.__table__, values, conflict_columns=("id",))


def upsert_job_analysis_snapshot(
    session: Session,
    job: Job,
    analysis: JobAnalysis,
) -> None:
    values = {
        **_audit_values(job, source_ref=f"{job.source_ref or job.id}:analysis"),
        "job_id": job.id,
        "match_score": analysis.match_score,
        "advantages": json.dumps(analysis.advantages, ensure_ascii=False),
        "gaps": json.dumps(analysis.gaps, ensure_ascii=False),
        "summary": analysis.summary,
        "recommendation": analysis.recommendation,
        "score_breakdown": dict(analysis.score_breakdown),
        "evidence": list(analysis.evidence),
        "evidence_level": analysis.evidence_level,
        "matched_directions": list(analysis.matched_directions),
        "primary_match_direction": analysis.primary_match_direction,
        "analysis_status": analysis.analysis_status,
        "model": analysis.model,
        "analysis_version": analysis.analysis_version,
        "prompt_version": analysis.prompt_version,
        "content_fingerprint": analysis.content_fingerprint,
        "profile_fingerprint": analysis.profile_fingerprint,
        "input_tokens": analysis.input_tokens,
        "output_tokens": analysis.output_tokens,
        "filter_reasons": list(analysis.filter_reasons),
        "refusal_reason": analysis.refusal_reason,
        "error_code": analysis.error_code,
        "analyzed_at": analysis.analyzed_at,
    }
    _upsert(session, JobAnalysisSnapshot.__table__, values, conflict_columns=("job_id",))


def upsert_application_snapshot(session: Session, application: Application) -> None:
    values = {
        **_audit_values(application),
        "id": application.id,
        "company_name": application.company_name,
        "job_title": application.job_title,
        "job_id": application.job_id,
        "record_url": _url(application.record_url),
        "stage": application.stage.value,
        "idempotency_key": application.idempotency_key,
        "note": application.note,
        "stage_history": list(application.stage_history),
        "source_stage": application.source_stage,
        "source_status": application.source_status,
        "source_status_synced_at": application.source_status_synced_at,
    }
    _upsert(
        session,
        ApplicationSnapshot.__table__,
        values,
        conflict_columns=("idempotency_key",),
    )


def upsert_schedule_event_snapshot(session: Session, event: ScheduleEvent) -> None:
    values = {
        **_audit_values(event),
        "id": event.id,
        "title": event.title,
        "event_date": event.event_date,
        "event_time": event.event_time,
        "event_type": event.event_type,
        "company_name": event.company_name,
        "job_title": event.job_title,
        "application_stage": event.application_stage.value,
        "starts_at": event.starts_at,
        "ends_at": event.ends_at,
        "application_id": event.application_id,
        "location_or_link": event.location_or_link,
        "note": event.note,
    }
    _upsert(session, ScheduleEventSnapshot.__table__, values, conflict_columns=("id",))


def _as_model(value: Any, model_type: type[BaseModel]) -> BaseModel:
    if isinstance(value, model_type):
        return value
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    return model_type.model_validate(value)


def _as_job_detail(value: Any) -> JobDetail:
    if isinstance(value, JobDetail):
        return value
    if isinstance(value, Job):
        return JobDetail(job=value)
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, dict) and "job" in value:
        return JobDetail.model_validate(value)
    return JobDetail(job=Job.model_validate(value))


def upsert_job_detail_snapshot(
    session: Session,
    detail: JobDetail,
    *,
    capture_status: str | None = None,
    capture_failure_reason: str | None = None,
    availability_status: str | None = None,
    title_key: str | None = None,
) -> None:
    upsert_job_snapshot(
        session,
        detail.job,
        capture_status=capture_status,
        capture_failure_reason=capture_failure_reason,
        availability_status=availability_status,
        title_key=title_key,
    )
    if detail.analysis is not None:
        upsert_job_analysis_snapshot(session, detail.job, detail.analysis)


@dataclass(frozen=True)
class SyncResult:
    companies: int
    jobs: int
    applications: int
    schedule_events: int
    analyses: int = 0


class SnapshotSyncService:
    """Read a ``RecruitmentRepository`` and write only Agent-owned snapshots."""

    def __init__(
        self,
        storage: Storage,
        repository: RecruitmentRepository,
        *,
        page_size: int = 100,
    ):
        if page_size < 1:
            raise ValueError("page_size must be positive")
        self.storage = storage
        self.repository = repository
        self.page_size = page_size

    def sync_all(self, *, hydrate_job_details: bool = False) -> SyncResult:
        """Fetch all source data before one atomic Agent-database write."""

        companies = [
            _as_model(item, Company) for item in self.repository.list_companies()
        ]
        details = self._read_jobs()
        if hydrate_job_details:
            details = self._hydrate(details)
        applications = [
            _as_model(item, Application) for item in self.repository.list_applications()
        ]
        events = [
            _as_model(item, ScheduleEvent) for item in self.repository.list_schedule()
        ]

        with self.storage.write_transaction() as session:
            for company in companies:
                upsert_company_snapshot(session, company)
            for detail in details:
                upsert_job_detail_snapshot(session, detail)
            for application in applications:
                upsert_application_snapshot(session, application)
            for event in events:
                upsert_schedule_event_snapshot(session, event)

        return SyncResult(
            companies=len(companies),
            jobs=len(details),
            applications=len(applications),
            schedule_events=len(events),
            analyses=sum(detail.analysis is not None for detail in details),
        )

    sync = sync_all

    def sync_job_detail(self, detail: JobDetail) -> None:
        with self.storage.write_transaction() as session:
            upsert_job_detail_snapshot(session, detail)

    def _read_jobs(self) -> list[JobDetail]:
        details: list[JobDetail] = []
        offset = 0
        while True:
            page = self.repository.search_jobs(limit=self.page_size, offset=offset)
            page_items = list(page.items)
            details.extend(_as_job_detail(item) for item in page_items)
            offset += len(page_items)
            if not page_items or offset >= page.total or len(page_items) < self.page_size:
                break
        return details

    def _hydrate(self, details: list[JobDetail]) -> list[JobDetail]:
        hydrated: list[JobDetail] = []
        for detail in details:
            full_detail = self.repository.get_job(detail.job.id)
            hydrated.append(full_detail or detail)
        return hydrated


class AgentStateStore:
    """Persist task, approval, tool-call, and write-audit records transactionally."""

    _TASK_STATE_TOOL_NAME = "daily_sync_state"

    def __init__(self, storage: Storage):
        self.storage = storage

    def save_task_run(self, task_run: DomainTaskRun) -> None:
        values = {
            **_audit_values(task_run),
            "id": task_run.id,
            "idempotency_key": f"task_run:{task_run.id}",
            "task_type": task_run.task_type,
            "status": task_run.status.value,
            "user_request": task_run.user_request,
            "current_step": task_run.current_step,
            "step_count": task_run.step_count,
            "max_steps": task_run.max_steps,
            "error_code": task_run.error_code,
        }
        with self.storage.write_transaction() as session:
            _upsert(session, TaskRun.__table__, values, conflict_columns=("idempotency_key",))

    def get_task_run(self, run_id: str) -> dict[str, Any] | None:
        with self.storage.session() as session:
            row = session.get(TaskRun, run_id)
            if row is None:
                return None
            state_row = session.scalar(
                select(ToolCall)
                .where(
                    ToolCall.task_id == run_id,
                    ToolCall.tool_name == self._TASK_STATE_TOOL_NAME,
                )
                .order_by(ToolCall.updated_at.desc())
            )
            state = (
                dict(state_row.arguments)
                if state_row is not None and isinstance(state_row.arguments, Mapping)
                else {}
            )
            metadata = state.get("metadata")
            details = state.get("details")
            steps = state.get("steps")
            return {
                "run_id": row.id,
                "task_id": row.task_type,
                "run_status": row.status,
                "current_step": row.current_step,
                "step_count": row.step_count,
                "error": row.error_code,
                "source_ref": row.source_ref,
                "state": state,
                "metadata": dict(metadata) if isinstance(metadata, Mapping) else {},
                "details": dict(details) if isinstance(details, Mapping) else {},
                "steps": list(steps) if isinstance(steps, list) else [],
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }

    def get_task_state(self, run_id: str) -> dict[str, Any] | None:
        """Read the compact JSON state attached to a task without a schema migration."""

        with self.storage.session() as session:
            row = session.scalar(
                select(ToolCall)
                .where(
                    ToolCall.task_id == run_id,
                    ToolCall.tool_name == self._TASK_STATE_TOOL_NAME,
                )
                .order_by(ToolCall.updated_at.desc())
            )
            if row is None or not isinstance(row.arguments, Mapping):
                return None
            return dict(row.arguments)

    def save_task_state(
        self,
        run_id: str,
        state: Mapping[str, Any],
        *,
        ensure_task_run: bool = False,
        task_type: str = "daily_recruitment_intelligence",
        user_request: str = "本地每日招聘情报统一同步",
    ) -> None:
        """Persist restart scope/checkpoints in the existing ToolCall JSON column.

        ``task_runs`` deliberately remains unchanged: ToolCall.arguments already
        provides a durable JSON envelope for metadata, details, and step history.
        """

        payload = dict(state)
        state_id = "daily-state:" + hashlib.sha256(
            str(run_id).encode("utf-8")
        ).hexdigest()[:32]
        with self.storage.write_transaction() as session:
            task = session.get(TaskRun, run_id)
            if task is None and ensure_task_run:
                now = utc_now()
                details = payload.get("details")
                detail_stage = (
                    details.get("stage")
                    if isinstance(details, Mapping)
                    else None
                )
                task = TaskRun(
                    id=run_id,
                    idempotency_key=f"task_run:{run_id}",
                    task_type=task_type,
                    status="running",
                    user_request=user_request,
                    current_step=str(
                        payload.get("current_step")
                        or detail_stage
                        or "starting"
                    )[:255],
                    step_count=len(payload.get("steps") or []),
                    max_steps=10,
                    source="recruitops-agent.codex-harness",
                    source_ref=f"daily-sync:{run_id}",
                    created_at=now,
                    updated_at=now,
                )
                session.add(task)
                session.flush()
            if task is None:
                return
            values = {
                "id": state_id,
                "task_id": run_id,
                "idempotency_key": f"daily_sync_state:{run_id}",
                "tool_name": self._TASK_STATE_TOOL_NAME,
                "arguments": payload,
                "result_summary": str(payload.get("summary") or "daily sync state"),
                "success": True,
                "latency_ms": None,
                "error_code": None,
                "source": "recruitops-agent.codex-harness",
                "source_ref": f"daily-state:{run_id}",
                "created_at": task.created_at or utc_now(),
                "updated_at": utc_now(),
            }
            _upsert(
                session,
                ToolCall.__table__,
                values,
                conflict_columns=("idempotency_key",),
            )

    def update_task_progress(self, run_id: str, current_step: str) -> bool:
        """Update visible progress without replacing task lifecycle fields."""

        with self.storage.write_transaction() as session:
            result = session.execute(
                update(TaskRun)
                .where(TaskRun.id == run_id)
                .values(current_step=current_step, updated_at=utc_now())
            )
            return bool(result.rowcount)

    def heartbeat_task_run(self, run_id: str) -> bool:
        """Refresh a running task lease without changing its visible stage."""

        with self.storage.write_transaction() as session:
            result = session.execute(
                update(TaskRun)
                .where(TaskRun.id == run_id, TaskRun.status == "running")
                .values(updated_at=utc_now())
            )
            return bool(result.rowcount)

    def recover_interrupted_task_runs(self) -> int:
        """Close leases whose in-process workers could not survive API restart."""

        with self.storage.write_transaction() as session:
            rows = list(session.scalars(select(TaskRun).where(TaskRun.status == "running")))
            for row in rows:
                previous_step = row.current_step or "unknown"
                row.status = "stopped"
                row.current_step = f"recoverable:{previous_step}"[:255]
                row.error_code = "process_interrupted"
                row.updated_at = utc_now()
            return len(rows)

    def save_approval(self, approval: DomainApproval) -> None:
        values = {
            **_audit_values(approval),
            "id": approval.id,
            "task_id": approval.task_id,
            "operation": approval.operation,
            "preview": dict(approval.preview),
            "status": approval.status.value,
            "idempotency_key": approval.idempotency_key,
            "decided_at": approval.decided_at,
        }
        with self.storage.write_transaction() as session:
            _upsert(session, Approval.__table__, values, conflict_columns=("idempotency_key",))

    def save_tool_call(self, tool_call: DomainToolCall) -> None:
        values = {
            **_audit_values(tool_call),
            "id": tool_call.id,
            "task_id": tool_call.task_id,
            "idempotency_key": f"tool_call:{tool_call.id}",
            "tool_name": tool_call.tool_name,
            "arguments": dict(tool_call.arguments),
            "result_summary": tool_call.result_summary,
            "success": tool_call.success,
            "latency_ms": tool_call.latency_ms,
            "error_code": tool_call.error_code,
        }
        with self.storage.write_transaction() as session:
            _upsert(session, ToolCall.__table__, values, conflict_columns=("idempotency_key",))

    def save_write_audit(self, write_audit: Any) -> None:
        """Persist an approval write audit without importing the approval package."""

        values = _write_audit_values(write_audit)
        with self.storage.write_transaction() as session:
            _upsert(session, WriteAudit.__table__, values, conflict_columns=("execution_id",))

    save_write_audit_record = save_write_audit


__all__ = [
    "AgentStateStore",
    "SnapshotSyncService",
    "SyncResult",
    "upsert_application_snapshot",
    "upsert_company_snapshot",
    "upsert_job_analysis_snapshot",
    "upsert_job_detail_snapshot",
    "upsert_job_snapshot",
    "upsert_schedule_event_snapshot",
]
