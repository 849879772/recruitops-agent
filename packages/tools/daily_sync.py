"""Narrow MCP boundary for the complete local daily recruitment sync."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from packages.scheduler.tasks import TaskType

from .operations import OperationalTaskRunInput, OperationalTaskRunner
from .typed import EvidenceSource, ToolErrorCode, ToolInput, ToolModel, ToolResponse, ToolStatus


class DailyRecruitmentSyncInput(ToolInput):
    thread_id: str | None = Field(default=None, min_length=1, max_length=255)
    turn_id: str | None = Field(default=None, min_length=1, max_length=255)
    dry_run: bool = False
    company_ids: list[str] = Field(default_factory=list, max_length=10)
    source_record_ids: list[str] = Field(default_factory=list, max_length=10)
    mode: Literal["full", "crawl_only", "score_only", "resume"] = "full"
    resume_run_id: str | None = Field(default=None, min_length=8, max_length=128)
    company_batch_limit: int | None = Field(
        default=None, ge=1, le=5000,
        description="本轮最多尝试的公司数；达到上限后保存断点并暂停，后续用 resume 继续原范围。",
    )

    @field_validator("company_ids", "source_record_ids")
    @classmethod
    def unique_company_ids(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            normalized = value.strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @field_validator("resume_run_id")
    @classmethod
    def normalize_resume_run_id(cls, value: str | None) -> str | None:
        return value.strip() if value else None

    @model_validator(mode="after")
    def validate_resume_mode(self) -> "DailyRecruitmentSyncInput":
        if self.mode == "resume" and not self.resume_run_id:
            raise ValueError("resume_run_id is required for resume mode")
        if self.mode != "resume" and self.resume_run_id:
            raise ValueError("resume_run_id is only valid for resume mode")
        if self.company_ids and self.source_record_ids:
            raise ValueError("company_ids and source_record_ids cannot be combined")
        if self.company_batch_limit and self.mode == "score_only":
            raise ValueError("company_batch_limit is only valid for crawl modes")
        return self


class DailyRecruitmentSyncData(ToolModel):
    run_id: str
    run_status: str
    dry_run: bool
    attempts: int = Field(ge=0)
    company_ids: list[str] = Field(default_factory=list)
    source_record_ids: list[str] = Field(default_factory=list)
    mode: Literal["full", "crawl_only", "score_only", "resume"] = "full"
    resume_run_id: str | None = None
    company_batch_limit: int | None = None
    result: Any = None
    error: str | None = None
    current_step: str | None = None
    step_count: int = Field(default=0, ge=0)
    progress: dict[str, Any] | None = None


class DailyRecruitmentSyncResponse(ToolResponse[DailyRecruitmentSyncData]):
    read_only: Literal[False] = False


class DailyRecruitmentSyncStatusInput(ToolInput):
    run_id: str = Field(min_length=8, max_length=128)


class DailyRecruitmentSyncStatusResponse(ToolResponse[DailyRecruitmentSyncData]):
    pass


def _compact_status_result(value: Any) -> Any:
    """Keep Agent polling responses bounded while preserving acceptance evidence."""

    if not isinstance(value, dict):
        return value
    pipeline = value.get("pipeline")
    if not isinstance(pipeline, dict):
        pipeline = value
    if not any(
        key in pipeline
        for key in ("selected_companies", "scoped_company_ids", "companies")
    ):
        return value
    scalar_keys = (
        "status", "sync_status", "dry_run", "total_companies",
        "selected_companies", "crawled_companies", "new", "changed",
        "reused", "rejected", "failed", "failed_companies", "failed_jobs",
        "filtered", "written", "analysis_enabled", "scoring_candidates",
        "scored", "scoring_failed", "unscored",
    )
    compact = {key: pipeline[key] for key in scalar_keys if key in pipeline}
    for key in ("rejection_reasons", "failure_reasons", "scoped_company_ids"):
        if key in pipeline:
            compact[key] = pipeline[key]
    companies = pipeline.get("companies")
    if isinstance(companies, list):
        company_keys = (
            "company_id", "company_name", "status", "raw_job_count",
            "accepted_job_count", "new_count", "changed_count", "reused_count",
            "rejected_count", "failed_count", "filtered_count",
            "rejection_reasons", "filtered_reasons", "failure_reason", "run_reason",
            "list_complete", "detail_success_count", "detail_failure_count",
        )
        compact["companies"] = [
            {key: row[key] for key in company_keys if key in row}
            for row in companies
            if isinstance(row, dict)
        ]
    if value is not pipeline:
        compact["run_status"] = value.get("status")
        compact["warnings"] = value.get("warnings") or []
        compact["error"] = value.get("error")
    return compact


def run_daily_recruitment_sync(
    request: DailyRecruitmentSyncInput,
    runner: OperationalTaskRunner,
) -> DailyRecruitmentSyncResponse:
    operation_request = OperationalTaskRunInput(
        thread_id=request.thread_id,
        turn_id=request.turn_id,
        task_id=TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value,
        dry_run=request.dry_run,
        company_ids=request.company_ids,
        source_record_ids=request.source_record_ids,
        mode=request.mode,
        resume_run_id=request.resume_run_id,
        company_batch_limit=request.company_batch_limit,
        timeout_ms=request.timeout_ms,
    )
    result = (
        runner.run(operation_request, execute_dry_run=True)
        if request.dry_run
        else runner.start(operation_request)
    )
    if result.data is None:
        return DailyRecruitmentSyncResponse(
            tool_name="daily_recruitment_sync",
            status=result.status,
            success=False,
            evidence=result.evidence,
            error_code=result.error_code or ToolErrorCode.INTERNAL_ERROR,
            error_message=result.error_message or "Daily recruitment sync failed.",
            timeout_ms=request.timeout_ms,
            timed_out=result.timed_out,
            elapsed_ms=result.elapsed_ms,
            read_only=False,
        )
    return DailyRecruitmentSyncResponse(
        tool_name="daily_recruitment_sync",
        status=ToolStatus.SUCCESS if result.success else ToolStatus.FAILURE,
        success=result.success,
        data=DailyRecruitmentSyncData(
            run_id=result.data.run_id,
            run_status=result.data.run_status,
            dry_run=result.data.dry_run,
            attempts=result.data.attempts,
            company_ids=result.data.company_ids,
            source_record_ids=result.data.source_record_ids,
            mode=result.data.mode,
            resume_run_id=result.data.resume_run_id,
            company_batch_limit=result.data.company_batch_limit,
            result=result.data.result,
            error=result.data.error,
            current_step=None,
            step_count=0,
        ),
        evidence=[
            EvidenceSource(
                source="agent_daily_sync",
                source_ref=f"run:{result.data.run_id}",
            )
        ],
        error_code=None if result.success else (result.error_code or ToolErrorCode.INTERNAL_ERROR),
        error_message=None if result.success else (result.error_message or result.data.error),
        timeout_ms=request.timeout_ms,
        timed_out=result.timed_out,
        elapsed_ms=result.elapsed_ms,
        read_only=False,
    )


def get_daily_recruitment_sync_status(
    request: DailyRecruitmentSyncStatusInput,
    runner: OperationalTaskRunner,
) -> DailyRecruitmentSyncStatusResponse:
    payload = runner.background_status(request.run_id)
    if payload is None:
        return DailyRecruitmentSyncStatusResponse(
            tool_name="daily_recruitment_sync_status",
            status=ToolStatus.NO_RESULTS,
            success=False,
            error_code=ToolErrorCode.NOT_FOUND,
            error_message="Daily recruitment sync run was not found in this runtime.",
            timeout_ms=request.timeout_ms,
            elapsed_ms=0,
        )
    return DailyRecruitmentSyncStatusResponse(
        tool_name="daily_recruitment_sync_status",
        status=ToolStatus.SUCCESS,
        success=True,
        data=DailyRecruitmentSyncData(
            run_id=str(payload["run_id"]),
            run_status=str(payload["run_status"]),
            dry_run=bool(payload["dry_run"]),
            attempts=int(payload["attempts"]),
            company_ids=list(payload.get("company_ids") or []),
            source_record_ids=list(payload.get("source_record_ids") or []),
            mode=payload.get("mode") or "full",
            resume_run_id=payload.get("resume_run_id"),
            company_batch_limit=payload.get("company_batch_limit"),
            result=_compact_status_result(payload.get("result")),
            error=payload.get("error"),
            current_step=payload.get("current_step"),
            step_count=int(payload.get("step_count") or 0),
            progress=payload.get("progress"),
        ),
        evidence=[
            EvidenceSource(source="agent_daily_sync", source_ref=f"run:{request.run_id}")
        ],
        timeout_ms=request.timeout_ms,
        elapsed_ms=0,
    )


__all__ = [
    "DailyRecruitmentSyncData",
    "DailyRecruitmentSyncInput",
    "DailyRecruitmentSyncResponse",
    "DailyRecruitmentSyncStatusInput",
    "DailyRecruitmentSyncStatusResponse",
    "get_daily_recruitment_sync_status",
    "run_daily_recruitment_sync",
]
