from __future__ import annotations

from datetime import date, datetime, time, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AuditFields(StrictModel):
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    source: str
    source_ref: str | None = None


class RecruitmentBatch(StrEnum):
    FORMAL = "formal"
    EARLY = "early"
    INTERNSHIP = "internship"
    UNKNOWN = "unknown"


class ApplicationStage(StrEnum):
    INTERESTED = "interested"
    APPLIED = "applied"
    ASSESSMENT = "assessment"
    WRITTEN = "written"
    INTERVIEW1 = "interview1"
    INTERVIEW2 = "interview2"
    INTERVIEW3 = "interview3"
    HR = "hr"
    OFFER = "offer"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Company(AuditFields):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    campus_url: str | None = None
    crawler_key: str | None = None
    integration_status: str
    organization_id: str | None = None
    recruitment_unit_name: str | None = None
    source_identity: str | None = None


class Job(AuditFields):
    id: str
    company_id: str
    title: str
    city: str | None = None
    detail_url: str = Field(min_length=1)
    jd_raw: str | None = None
    cohort: int | None = None
    cohort_status: str = "unconfirmed"
    batch: RecruitmentBatch = RecruitmentBatch.UNKNOWN
    match_score: int | None = Field(default=None, ge=0, le=100)
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    organization_id: str | None = None
    recruitment_unit_id: str | None = None
    recruitment_campaign_id: str | None = None
    source_platform: str | None = None
    source_tenant: str | None = None
    native_job_id: str | None = None
    normalized_detail_url: str | None = None
    business_key: str | None = None
    capture_status: str = "unknown"
    capture_failure_reason: str = ""
    availability_status: str = "active"
    title_key: str | None = None
    capture_evidence: dict[str, Any] = Field(default_factory=dict)


class JobAnalysis(StrictModel):
    match_score: int | None = Field(default=None, ge=0, le=100)
    advantages: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    summary: str | None = None
    recommendation: str | None = None
    score_breakdown: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    evidence_level: str | None = None
    matched_directions: list[str] = Field(default_factory=list)
    primary_match_direction: str | None = None
    analysis_status: str | None = None
    model: str | None = None
    analysis_version: str | None = None
    prompt_version: str | None = None
    content_fingerprint: str | None = None
    profile_fingerprint: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    filter_reasons: list[str] = Field(default_factory=list)
    refusal_reason: str | None = None
    error_code: str | None = None
    analyzed_at: datetime | None = None


class JobDetail(StrictModel):
    job: Job
    analysis: JobAnalysis | None = None


class Application(AuditFields):
    id: str
    company_name: str
    job_title: str
    job_id: str | None = None
    record_url: str | None = None
    stage: ApplicationStage
    idempotency_key: str
    note: str | None = None
    stage_history: list[dict[str, Any]] = Field(default_factory=list)
    source_stage: str | None = None
    source_status: str | None = None
    source_status_synced_at: datetime | None = None


class ScheduleEvent(AuditFields):
    id: str
    title: str
    event_date: date | None = None
    status: Literal["pending", "completed", "ignored"] = "pending"
    time_kind: Literal["appointment", "deadline", "unspecified"] = "appointment"
    event_time: time | None = None
    event_type: str
    company_name: str
    job_title: str
    application_stage: ApplicationStage
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    application_id: str | None = None
    location_or_link: str | None = None
    note: str | None = None


class TaskRun(AuditFields):
    id: str
    task_type: str
    status: TaskStatus = TaskStatus.PENDING
    user_request: str
    current_step: str | None = None
    step_count: int = Field(default=0, ge=0)
    max_steps: int = Field(default=12, ge=1, le=50)
    error_code: str | None = None


class Approval(AuditFields):
    id: str
    task_id: str
    operation: str
    preview: dict[str, Any]
    status: ApprovalStatus = ApprovalStatus.PENDING
    idempotency_key: str
    decided_at: datetime | None = None


class ToolCall(AuditFields):
    id: str
    task_id: str
    tool_name: str
    arguments: dict[str, Any]
    result_summary: str | None = None
    success: bool | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = None


class ServiceStatus(StrictModel):
    status: str
    mode: str


class JobPage(StrictModel):
    items: list[Job]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class JobBrowseItem(StrictModel):
    id: str
    company_id: str
    company_name: str
    organization_id: str | None = None
    title: str
    city: str | None = None
    detail_url: str
    category: str
    category_label: str
    platform: str
    batch: RecruitmentBatch
    capture_status: str = "unknown"
    capture_failure_reason: str = ""
    availability_status: str = "active"
    match_score: int | None = Field(default=None, ge=0, le=100)
    recommendation: str | None = None
    summary: str | None = None
    advantages: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    matched_directions: list[str] = Field(default_factory=list)
    primary_match_direction: str | None = None
    analysis_status: str | None = None
    first_seen_at: datetime | None = None
    application_stage: ApplicationStage | None = None


class CompanyJobSummary(StrictModel):
    key: str
    name: str
    company_ids: list[str] = Field(default_factory=list)
    recruitment_units: list[str] = Field(default_factory=list)
    campus_url: str | None = None
    job_count: int = Field(ge=0)
    average_score: float | None = Field(default=None, ge=0, le=100)
    top_score: int | None = Field(default=None, ge=0, le=100)
    top_job: str | None = None


class JobBrowseStats(StrictModel):
    jobs: int = Field(ge=0)
    companies: int = Field(ge=0)
    high_match: int = Field(ge=0)
    unscored: int = Field(ge=0)
    pending: int = Field(default=0, ge=0)
    jd_incomplete: int = Field(default=0, ge=0)
    excluded: int = Field(default=0, ge=0)


class JobBrowseFacets(StrictModel):
    companies: list[CompanyJobSummary] = Field(default_factory=list)
    categories: dict[str, str] = Field(default_factory=dict)
    platforms: list[str] = Field(default_factory=list)


class JobBrowsePage(StrictModel):
    items: list[JobBrowseItem]
    featured: list[JobBrowseItem] = Field(default_factory=list)
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    stats: JobBrowseStats
    facets: JobBrowseFacets


class ApplicationPage(StrictModel):
    items: list[Application]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
