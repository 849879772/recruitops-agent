from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, synonym


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


CAPTURE_STATUSES = frozenset({"unknown", "pending", "complete", "failed"})
AVAILABILITY_STATUSES = frozenset({"active", "inactive"})


class Base(DeclarativeBase):
    pass


class AuditMixin:
    """Fields shared by Agent records and imported source snapshots."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)


class TaskRun(AuditMixin, Base):
    __tablename__ = "task_runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_task_runs_idempotency_key"),
        Index("ix_task_runs_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    task_type: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", nullable=False
    )
    user_request: Mapped[str] = mapped_column(Text, nullable=False)
    current_step: Mapped[str | None] = mapped_column(String(255), nullable=True)
    step_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    max_steps: Mapped[int] = mapped_column(
        Integer, default=12, server_default="12", nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Approval(AuditMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_approvals_idempotency_key"),
        Index("ix_approvals_task_id", "task_id"),
        Index("ix_approvals_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False
    )
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    preview: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="pending", server_default="pending", nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolCall(AuditMixin, Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_tool_calls_idempotency_key"),
        Index("ix_tool_calls_task_id", "task_id"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("task_runs.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ConversationThread(Base):
    """Durable local conversation metadata and compact working context."""

    __tablename__ = "conversation_threads"
    __table_args__ = (Index("ix_conversation_threads_updated_at", "updated_at"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
        nullable=False,
    )


class ConversationMessage(Base):
    """One persisted user or assistant message with an optional task result."""

    __tablename__ = "conversation_messages"
    __table_args__ = (
        Index("ix_conversation_messages_thread_created", "thread_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    thread_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("conversation_threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )


class CompanySnapshot(AuditMixin, Base):
    __tablename__ = "company_snapshots"
    __table_args__ = (
        UniqueConstraint("source", "source_ref", name="uq_company_snapshots_source_ref"),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    campus_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    crawler_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    integration_status: Mapped[str] = mapped_column(String(64), nullable=False)
    organization_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recruitment_unit_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_identity: Mapped[str | None] = mapped_column(String(512), nullable=True)


class JobSnapshot(AuditMixin, Base):
    __tablename__ = "job_snapshots"
    __table_args__ = (
        UniqueConstraint("source", "source_ref", name="uq_job_snapshots_source_ref"),
        Index("ix_job_snapshots_company_id", "company_id"),
        Index("ix_job_snapshots_cohort", "cohort", "cohort_status"),
        Index("ix_job_snapshots_company_title_key", "company_id", "title_key"),
        CheckConstraint(
            "capture_status IN ('unknown', 'pending', 'complete', 'failed')",
            name="ck_job_snapshots_capture_status",
        ),
        CheckConstraint(
            "availability_status IN ('active', 'inactive')",
            name="ck_job_snapshots_availability_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    company_id: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    city: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    jd_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    cohort: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cohort_status: Mapped[str] = mapped_column(String(64), nullable=False)
    batch: Mapped[str] = mapped_column(String(64), nullable=False)
    match_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    organization_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recruitment_unit_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    recruitment_campaign_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_platform: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_tenant: Mapped[str | None] = mapped_column(String(255), nullable=True)
    native_job_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    normalized_detail_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    business_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    capture_status: Mapped[str] = mapped_column(
        String(16), default="unknown", server_default="unknown", nullable=False
    )
    capture_failure_reason: Mapped[str] = mapped_column(
        Text, default="", server_default="", nullable=False
    )
    availability_status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active", nullable=False
    )
    title_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    capture_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class JobAnalysisSnapshot(AuditMixin, Base):
    __tablename__ = "job_analysis_snapshots"

    job_id: Mapped[str] = mapped_column(
        String(255), ForeignKey("job_snapshots.id", ondelete="CASCADE"), primary_key=True
    )
    match_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    advantages: Mapped[str | None] = mapped_column(Text, nullable=True)
    gaps: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendation: Mapped[str | None] = mapped_column(Text, nullable=True)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    evidence_level: Mapped[str | None] = mapped_column(String(64), nullable=True)
    matched_directions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    primary_match_direction: Mapped[str | None] = mapped_column(String(128), nullable=True)
    analysis_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    analysis_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    content_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    filter_reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    refusal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApplicationSnapshot(AuditMixin, Base):
    __tablename__ = "application_snapshots"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_application_snapshots_idempotency_key"),
        UniqueConstraint("source", "source_ref", name="uq_application_snapshots_source_ref"),
        Index("ix_application_snapshots_stage", "stage"),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_title: Mapped[str] = mapped_column(String(512), nullable=False)
    job_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    record_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage_history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    source_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_status_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScheduleEventSnapshot(AuditMixin, Base):
    __tablename__ = "schedule_event_snapshots"
    __table_args__ = (
        UniqueConstraint("source", "source_ref", name="uq_schedule_event_snapshots_source_ref"),
        Index("ix_schedule_event_snapshots_event_date", "event_date"),
        CheckConstraint("status IN ('pending', 'completed', 'ignored')", name="ck_schedule_item_status"),
        CheckConstraint("time_kind IN ('appointment', 'deadline', 'unspecified')", name="ck_schedule_time_kind"),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending", nullable=False)
    time_kind: Mapped[str] = mapped_column(String(16), default="appointment", server_default="appointment", nullable=False)
    event_time: Mapped[Any | None] = mapped_column(Time, nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    company_name: Mapped[str] = mapped_column(String(255), nullable=False)
    job_title: Mapped[str] = mapped_column(String(512), nullable=False)
    application_stage: Mapped[str] = mapped_column(String(64), nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    application_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location_or_link: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class WriteAudit(Base):
    """Durable record of one approval-gated write attempt."""

    __tablename__ = "write_audits"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_write_audits_idempotency_key"),
        Index("ix_write_audits_task_id", "task_id"),
        Index("ix_write_audits_success", "success"),
    )

    execution_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    token_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    operator: Mapped[str] = mapped_column(String(200), nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    evidence_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    before_diff: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_diff: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    backup_ref: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    rollback_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )

    # Keep the persisted diff names explicit while accepting the executor's
    # shorter audit vocabulary at the storage boundary.
    id = synonym("execution_id")
    before = synonym("before_diff")
    after = synonym("after_diff")
    backup = synonym("backup_ref")
    rollback = synonym("rollback_payload")
    error = synonym("error_code")


class BrowserReviewReceipt(Base):
    """Durable authorization and observation receipt for one browser attempt."""

    __tablename__ = "browser_review_receipts"
    __table_args__ = (
        UniqueConstraint(
            "review_id",
            "target_id",
            "action_attempt_id",
            name="uq_browser_review_receipts_key",
        ),
    )

    review_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    action_attempt_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    observation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    normalized_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    application_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    entries: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), default="authorized", server_default="authorized", nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
        nullable=False,
    )


class BrowserOperation(Base):
    """Durable lifecycle record for one Edge browser-bridge operation."""

    __tablename__ = "browser_operations"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_browser_operations_idempotency_key",
        ),
        CheckConstraint(
            "status IN ('CONNECTING', 'DISPATCHED', 'NAVIGATING', "
            "'WAITING_FOR_LOGIN', 'EXTRACTING', 'VALIDATING', 'UPDATING', "
            "'SUCCEEDED', 'STATE_UNCLEAR', 'FAILED', 'CANCELLED')",
            name="ck_browser_operations_status",
        ),
        Index("ix_browser_operations_device_status", "device_id", "status"),
        Index("ix_browser_operations_updated_at", "updated_at"),
    )

    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    operation: Mapped[str] = mapped_column(String(128), nullable=False)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="CONNECTING", server_default="CONNECTING", nullable=False
    )
    command: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_event_sequence: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_outbox_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
        nullable=False,
    )

    id = synonym("operation_id")
    terminal_result = synonym("result")

    @property
    def state(self) -> str:
        return self.status


class BrowserBridgeDevice(Base):
    """Persisted presence state for one authenticated Edge device."""

    __tablename__ = "browser_bridge_devices"
    __table_args__ = (
        Index("ix_browser_bridge_devices_connected", "connected"),
    )

    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    connected: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    id = synonym("device_id")

    @property
    def status(self) -> str:
        return "connected" if self.connected else "disconnected"


class BrowserOperationEvent(Base):
    """Append-only state/event evidence received from the bridge."""

    __tablename__ = "browser_operation_events"
    __table_args__ = (
        UniqueConstraint(
            "operation_id",
            "sequence",
            name="uq_browser_operation_events_operation_sequence",
        ),
        CheckConstraint("sequence > 0", name="ck_browser_operation_events_sequence"),
        CheckConstraint(
            "status IN ('CONNECTING', 'DISPATCHED', 'NAVIGATING', "
            "'WAITING_FOR_LOGIN', 'EXTRACTING', 'VALIDATING', 'UPDATING', "
            "'SUCCEEDED', 'STATE_UNCLEAR', 'FAILED', 'CANCELLED')",
            name="ck_browser_operation_events_status",
        ),
        Index("ix_browser_operation_events_operation_sequence", "operation_id", "sequence"),
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("browser_operations.operation_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    event_type: Mapped[str] = mapped_column(
        String(64), default="state", server_default="state", nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )

    id = synonym("event_id")

    @property
    def state(self) -> str:
        return self.status


class BrowserOutbox(Base):
    """Durable outbound command waiting for an Edge device ACK."""

    __tablename__ = "browser_outbox"
    __table_args__ = (
        UniqueConstraint(
            "device_id",
            "sequence",
            name="uq_browser_outbox_device_sequence",
        ),
        CheckConstraint("sequence > 0", name="ck_browser_outbox_sequence"),
        Index("ix_browser_outbox_pending", "device_id", "acked_at", "sequence"),
        Index("ix_browser_outbox_operation", "operation_id"),
    )

    outbox_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("browser_operations.operation_id", ondelete="CASCADE"),
        nullable=False,
    )
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    ack_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ack_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    acked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )

    id = synonym("outbox_id")

    @property
    def acknowledged(self) -> bool:
        return self.acked_at is not None


class BrowserOutboxCursor(Base):
    """Per-device monotonic sequence allocator for the browser outbox."""

    __tablename__ = "browser_outbox_cursors"

    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    next_sequence: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )


class AutomationSchedule(Base):
    """One active or disabled local recurring automation."""

    __tablename__ = "automation_schedules"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "target_key",
            name="uq_automation_schedules_task_target",
        ),
        CheckConstraint("frequency = 'daily'", name="ck_automation_schedules_frequency"),
        Index("ix_automation_schedules_due", "active", "next_run_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    task_label: Mapped[str] = mapped_column(String(255), nullable=False)
    target_kind: Mapped[str] = mapped_column(
        String(64), default="all", server_default="all", nullable=False
    )
    target_key: Mapped[str] = mapped_column(
        String(255), default="*", server_default="*", nullable=False
    )
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_label: Mapped[str | None] = mapped_column(String(512), nullable=True)
    frequency: Mapped[str] = mapped_column(
        String(32), default="daily", server_default="daily", nullable=False
    )
    start_time: Mapped[Any] = mapped_column(Time, nullable=False)
    timezone_name: Mapped[str] = mapped_column(
        String(64), default="Asia/Shanghai", server_default="Asia/Shanghai", nullable=False
    )
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
        nullable=False,
    )


class AutomationExecution(Base):
    """Durable result for one scheduled automation occurrence."""

    __tablename__ = "automation_executions"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "scheduled_for",
            name="uq_automation_executions_schedule_time",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'blocked')",
            name="ck_automation_executions_status",
        ),
        Index("ix_automation_executions_schedule_started", "schedule_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("automation_schedules.id", ondelete="CASCADE"),
        nullable=False,
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default="running", server_default="running", nullable=False
    )
    thread_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Explicit aliases make the persistence role clear to callers that avoid names
# shared with the Pydantic domain models.
TaskRunRecord = TaskRun
ApprovalRecord = Approval
ToolCallRecord = ToolCall
CompanySnapshotRecord = CompanySnapshot
JobSnapshotRecord = JobSnapshot
JobAnalysisSnapshotRecord = JobAnalysisSnapshot
ApplicationSnapshotRecord = ApplicationSnapshot
ScheduleEventSnapshotRecord = ScheduleEventSnapshot
WriteAuditRecord = WriteAudit
BrowserReviewReceiptRecord = BrowserReviewReceipt
ConversationThreadRecord = ConversationThread
ConversationMessageRecord = ConversationMessage
BrowserBridgeDeviceRecord = BrowserBridgeDevice
BrowserOperationRecord = BrowserOperation
BrowserOperationEventRecord = BrowserOperationEvent
BrowserOutboxRecord = BrowserOutbox
BrowserOutboxCursorRecord = BrowserOutboxCursor
AutomationScheduleRecord = AutomationSchedule
AutomationExecutionRecord = AutomationExecution


__all__ = [
    "AutomationExecution",
    "AutomationExecutionRecord",
    "AutomationSchedule",
    "AutomationScheduleRecord",
    "Approval",
    "ApprovalRecord",
    "BrowserReviewReceipt",
    "BrowserReviewReceiptRecord",
    "AVAILABILITY_STATUSES",
    "CAPTURE_STATUSES",
    "BrowserOperation",
    "BrowserOperationRecord",
    "BrowserOperationEvent",
    "BrowserOperationEventRecord",
    "BrowserOutbox",
    "BrowserOutboxRecord",
    "BrowserOutboxCursor",
    "BrowserOutboxCursorRecord",
    "ApplicationSnapshot",
    "ApplicationSnapshotRecord",
    "Base",
    "BrowserBridgeDevice",
    "BrowserBridgeDeviceRecord",
    "CompanySnapshot",
    "CompanySnapshotRecord",
    "ConversationMessage",
    "ConversationMessageRecord",
    "ConversationThread",
    "ConversationThreadRecord",
    "JobAnalysisSnapshot",
    "JobAnalysisSnapshotRecord",
    "JobSnapshot",
    "JobSnapshotRecord",
    "ScheduleEventSnapshot",
    "ScheduleEventSnapshotRecord",
    "TaskRun",
    "TaskRunRecord",
    "ToolCall",
    "ToolCallRecord",
    "WriteAudit",
    "WriteAuditRecord",
]
