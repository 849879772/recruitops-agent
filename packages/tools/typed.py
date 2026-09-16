from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from time import perf_counter
from typing import TYPE_CHECKING, Any, Callable, Generic, Literal, TypeVar
import unicodedata

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from packages.domain.models import (
    Application,
    ApplicationStage,
    Company,
    Job,
    JobAnalysis,
    JobDetail,
    JobPage,
    RecruitmentBatch,
    ScheduleEvent,
)
if TYPE_CHECKING:
    from packages.repositories.base import RecruitmentRepository
else:
    RecruitmentRepository = Any


class ToolStatus(StrEnum):
    SUCCESS = "success"
    NO_RESULTS = "no_results"
    AMBIGUOUS = "ambiguous"
    FAILURE = "failure"


class ToolErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    NO_RESULTS = "no_results"
    AMBIGUOUS_MATCH = "ambiguous_match"
    SOURCE_UNAVAILABLE = "source_unavailable"
    TIMEOUT = "timeout"
    INCOMPLETE_JD = "incomplete_jd"
    COHORT_NOT_CONFIRMED = "cohort_not_confirmed"
    INELIGIBLE_BATCH = "ineligible_batch"
    READ_ONLY_VIOLATION = "read_only_violation"
    INTERNAL_ERROR = "internal_error"
    UNTRUSTED_WEB_CONTENT = "untrusted_web_content"
    INVALID_SOURCE = "invalid_source"
    PAGINATION_INCOMPLETE = "pagination_incomplete"
    PAGINATION_EVIDENCE_MISSING = "pagination_evidence_missing"


# Keep the shorter name available to callers that use the repository vocabulary.
ErrorCode = ToolErrorCode


class ToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolInput(ToolModel):
    timeout_ms: int = Field(default=5_000, ge=1, le=120_000)


class EvidenceSource(ToolModel):
    source: str = Field(min_length=1)
    source_ref: str | None = None


class TodayScheduleInput(ToolInput):
    on_date: date = Field(validation_alias=AliasChoices("on_date", "date"))


class CapabilitiesInput(ToolInput):
    pass


class JobSearchInput(ToolInput):
    query: str | None = None
    company: str | None = None
    cohort: int | None = Field(default=None, ge=1, le=9_999)
    cohort_status: str | None = None
    recruitment_track: str | None = None
    first_seen_on: date | None = None
    batches: list[RecruitmentBatch] | None = None
    min_score: int | None = Field(default=None, ge=0, le=100)
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class JobDetailInput(ToolInput):
    job_id: str = Field(min_length=1)


class CompanyCoverageInput(ToolInput):
    company_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("company_name", "company", "name"),
    )
    integration_status: str | None = Field(
        default=None,
        validation_alias=AliasChoices("integration_status", "status"),
    )
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class ApplicationQueryInput(ToolInput):
    application_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("application_id", "id"),
    )
    company_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("company_name", "company"),
    )
    job_title: str | None = None
    job_id: str | None = None
    stage: ApplicationStage | None = None
    query: str | None = None
    list_all: bool = False
    exclude_terminal: bool = False


class TodayScheduleData(ToolModel):
    on_date: date
    events: list[ScheduleEvent] = Field(default_factory=list)


class CapabilitiesData(ToolModel):
    capabilities: list[str] = Field(default_factory=list)
    safety_boundary: str


class JobSearchData(ToolModel):
    items: list[Job]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    requested_first_seen_on: date | None = None
    data_as_of: datetime | None = None
    freshness_status: Literal["current", "stale", "unknown"] = "unknown"


class JobDetailData(ToolModel):
    job: Job
    analysis: JobAnalysis | None = None


class CompanyCoverageData(ToolModel):
    companies: list[Company]
    total: int = Field(ge=0)
    connected: int = Field(ge=0)
    not_connected: int = Field(ge=0)


class ApplicationQueryData(ToolModel):
    matches: list[Application] = Field(default_factory=list)
    application: Application | None = None
    total: int = Field(default=0, ge=0)
    company_count: int = Field(default=0, ge=0)
    excluded_terminal: int = Field(default=0, ge=0)


T = TypeVar("T", bound=BaseModel)


class ToolResponse(ToolModel, Generic[T]):
    tool_name: str
    status: ToolStatus
    success: bool
    data: T | None = None
    evidence: list[EvidenceSource] = Field(min_length=1)
    error_code: ToolErrorCode | None = None
    error_message: str | None = None
    timeout_ms: int = Field(ge=1)
    timed_out: bool = False
    elapsed_ms: int = Field(ge=0)
    read_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_status_fields(self) -> ToolResponse[T]:
        expected_success = self.status == ToolStatus.SUCCESS
        if self.success != expected_success:
            raise ValueError("success must agree with status")
        if self.status == ToolStatus.SUCCESS and self.error_code is not None:
            raise ValueError("successful responses cannot contain an error code")
        if self.status != ToolStatus.SUCCESS and self.error_code is None:
            raise ValueError("non-success responses require an error code")
        return self


class TodayScheduleResponse(ToolResponse[TodayScheduleData]):
    pass


class CapabilitiesResponse(ToolResponse[CapabilitiesData]):
    pass


class JobSearchResponse(ToolResponse[JobSearchData]):
    pass


class JobDetailResponse(ToolResponse[JobDetailData]):
    pass


class CompanyCoverageResponse(ToolResponse[CompanyCoverageData]):
    pass


class ApplicationQueryResponse(ToolResponse[ApplicationQueryData]):
    pass


@dataclass(frozen=True)
class _Outcome(Generic[T]):
    status: ToolStatus
    data: T | None
    error_code: ToolErrorCode | None = None
    error_message: str | None = None
    evidence_values: tuple[object, ...] = ()


def _outcome(
    status: ToolStatus,
    data: T | None,
    *,
    error_code: ToolErrorCode | None = None,
    error_message: str | None = None,
    evidence_values: tuple[object, ...] = (),
) -> _Outcome[T]:
    return _Outcome(
        status=status,
        data=data,
        error_code=error_code,
        error_message=error_message,
        evidence_values=evidence_values,
    )


def _collect_evidence(tool_name: str, *values: object) -> list[EvidenceSource]:
    evidence: list[EvidenceSource] = []
    seen: set[tuple[str, str | None]] = set()

    def add(source: object, source_ref: object = None) -> None:
        if source is None:
            return
        source_text = str(source).strip()
        if not source_text:
            return
        ref_text = str(source_ref).strip() if source_ref is not None else None
        key = (source_text, ref_text)
        if key not in seen:
            seen.add(key)
            evidence.append(EvidenceSource(source=source_text, source_ref=ref_text))

    add("recruitment_repository", tool_name)

    def visit(value: object) -> None:
        if isinstance(value, BaseModel):
            add(getattr(value, "source", None), getattr(value, "source_ref", None))
            for field_name in type(value).model_fields:
                visit(getattr(value, field_name, None))
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item)

    for value in values:
        visit(value)
    return evidence


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((perf_counter() - started) * 1_000)))


def _execute(
    request: ToolInput,
    tool_name: str,
    response_type: type[ToolResponse[T]],
    operation: Callable[[], _Outcome[T]],
) -> ToolResponse[T]:
    started = perf_counter()
    try:
        result = operation()
    except TimeoutError:
        elapsed_ms = _elapsed_ms(started)
        return response_type(
            tool_name=tool_name,
            status=ToolStatus.FAILURE,
            success=False,
            evidence=_collect_evidence(tool_name),
            error_code=ToolErrorCode.TIMEOUT,
            error_message="The read operation timed out.",
            timeout_ms=request.timeout_ms,
            timed_out=True,
            elapsed_ms=elapsed_ms,
            read_only=True,
        )
    except Exception as exc:  # Repository errors must remain typed failures.
        elapsed_ms = _elapsed_ms(started)
        message = str(exc).strip() or "The read source was unavailable."
        return response_type(
            tool_name=tool_name,
            status=ToolStatus.FAILURE,
            success=False,
            evidence=_collect_evidence(tool_name),
            error_code=ToolErrorCode.SOURCE_UNAVAILABLE,
            error_message=message,
            timeout_ms=request.timeout_ms,
            timed_out=False,
            elapsed_ms=elapsed_ms,
            read_only=True,
        )

    elapsed_ms = _elapsed_ms(started)
    if (perf_counter() - started) * 1_000 > request.timeout_ms:
        return response_type(
            tool_name=tool_name,
            status=ToolStatus.FAILURE,
            success=False,
            evidence=_collect_evidence(tool_name),
            error_code=ToolErrorCode.TIMEOUT,
            error_message="The read operation exceeded its timeout budget.",
            timeout_ms=request.timeout_ms,
            timed_out=True,
            elapsed_ms=elapsed_ms,
            read_only=True,
        )

    return response_type(
        tool_name=tool_name,
        status=result.status,
        success=result.status == ToolStatus.SUCCESS,
        data=result.data,
        evidence=_collect_evidence(tool_name, result.data, *result.evidence_values),
        error_code=result.error_code,
        error_message=result.error_message,
        timeout_ms=request.timeout_ms,
        timed_out=False,
        elapsed_ms=elapsed_ms,
        read_only=True,
    )


def today_schedule(
    request: TodayScheduleInput,
    repository: RecruitmentRepository,
) -> TodayScheduleResponse:
    def operation() -> _Outcome[TodayScheduleData]:
        raw_events = repository.list_schedule(on_date=request.on_date)
        events: list[ScheduleEvent] = []
        for raw_event in raw_events:
            event = ScheduleEvent.model_validate(raw_event)
            if event.event_date == request.on_date:
                events.append(event)
        data = TodayScheduleData(on_date=request.on_date, events=events)
        if not events:
            return _outcome(
                ToolStatus.NO_RESULTS,
                data,
                error_code=ToolErrorCode.NO_RESULTS,
                error_message="No schedule events exist for the requested date.",
            )
        return _outcome(ToolStatus.SUCCESS, data)

    return _execute(request, "today_schedule", TodayScheduleResponse, operation)


def describe_capabilities(request: CapabilitiesInput) -> CapabilitiesResponse:
    def operation() -> _Outcome[CapabilitiesData]:
        return _outcome(
            ToolStatus.SUCCESS,
            CapabilitiesData(
                capabilities=[
                    "查询 27 届岗位、今日新增和公司接入情况",
                    "查询投递记录、今日笔面试日程和招聘邮件",
                    "按明确指令添加或修改本地待办、日历事项；处理招聘邮件时自动生成待办",
                    "解释岗位匹配依据并检索爬虫接入经验",
                    "生成投递状态复核、邮件关联和定时计划的只读预览",
                    "按明确指令运行受控的本地招聘运营任务",
                ],
                safety_boundary=(
                    "默认只读；用户明确要求添加或修改本地待办即为该操作授权，无需额外审批。"
                    "投递阶段写入仍须通过证据校验；其他受控操作沿用既有审批规则。"
                ),
            ),
        )

    return _execute(request, "capabilities", CapabilitiesResponse, operation)


def search_jobs(
    request: JobSearchInput,
    repository: RecruitmentRepository,
) -> JobSearchResponse:
    def operation() -> _Outcome[JobSearchData]:
        page = JobPage.model_validate(
            repository.search_jobs(
                query=request.query,
                company=request.company,
                cohort=request.cohort,
                cohort_status=request.cohort_status,
                recruitment_track=request.recruitment_track,
                first_seen_on=request.first_seen_on,
                batches=tuple(request.batches) if request.batches else None,
                min_score=request.min_score,
                limit=request.limit,
                offset=request.offset,
            )
        )
        latest_seen = None
        latest_seen_reader = getattr(repository, "latest_job_seen_at", None)
        if callable(latest_seen_reader):
            latest_seen = latest_seen_reader()
        freshness_status: Literal["current", "stale", "unknown"] = "unknown"
        if request.first_seen_on is not None and latest_seen is not None:
            freshness_status = (
                "current"
                if latest_seen.date() >= request.first_seen_on
                else "stale"
            )
        data = JobSearchData(
            items=page.items,
            total=page.total,
            limit=page.limit,
            offset=page.offset,
            requested_first_seen_on=request.first_seen_on,
            data_as_of=latest_seen,
            freshness_status=freshness_status,
        )
        if not data.items:
            return _outcome(
                ToolStatus.NO_RESULTS,
                data,
                error_code=ToolErrorCode.NO_RESULTS,
                error_message="No jobs matched the search criteria.",
            )
        return _outcome(ToolStatus.SUCCESS, data)

    return _execute(request, "search_jobs", JobSearchResponse, operation)


def job_detail(
    request: JobDetailInput,
    repository: RecruitmentRepository,
) -> JobDetailResponse:
    def operation() -> _Outcome[JobDetailData]:
        raw_detail = repository.get_job(request.job_id)
        if raw_detail is None:
            return _outcome(
                ToolStatus.NO_RESULTS,
                None,
                error_code=ToolErrorCode.NOT_FOUND,
                error_message=f"Job {request.job_id!r} was not found.",
            )
        detail = JobDetail.model_validate(raw_detail)
        return _outcome(
            ToolStatus.SUCCESS,
            JobDetailData(job=detail.job, analysis=detail.analysis),
        )

    return _execute(request, "job_detail", JobDetailResponse, operation)


def company_coverage(
    request: CompanyCoverageInput,
    repository: RecruitmentRepository,
) -> CompanyCoverageResponse:
    def operation() -> _Outcome[CompanyCoverageData]:
        companies = [Company.model_validate(company) for company in repository.list_companies()]
        if request.company_name:
            needle = request.company_name.casefold()
            companies = [
                company
                for company in companies
                if company.name.casefold() == needle
                or any(alias.casefold() == needle for alias in company.aliases)
            ]
        if request.integration_status:
            status = request.integration_status.casefold()
            companies = [
                company
                for company in companies
                if company.integration_status.casefold() == status
            ]
        total = len(companies)
        connected = sum(item.integration_status.casefold() == "connected" for item in companies)
        data = CompanyCoverageData(
            companies=companies[request.offset:request.offset + request.limit],
            total=total,
            connected=connected,
            not_connected=total - connected,
        )
        if not data.companies:
            return _outcome(
                ToolStatus.NO_RESULTS,
                data,
                error_code=ToolErrorCode.NO_RESULTS,
                error_message="No companies matched the coverage query.",
            )
        return _outcome(ToolStatus.SUCCESS, data)

    return _execute(request, "company_coverage", CompanyCoverageResponse, operation)


_COMPANY_SUFFIXES = (
    "股份有限公司",
    "有限责任公司",
    "有限公司",
    "控股集团",
    "集团",
    "科技",
    "技术",
    "公司",
)


def _normalized_lookup_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum() or character in "+#")


def _company_lookup_stem(value: str) -> str:
    normalized = _normalized_lookup_text(value)
    changed = True
    while changed and len(normalized) > 2:
        changed = False
        for suffix in _COMPANY_SUFFIXES:
            normalized_suffix = _normalized_lookup_text(suffix)
            if normalized.endswith(normalized_suffix) and len(normalized) > len(normalized_suffix):
                normalized = normalized[: -len(normalized_suffix)]
                changed = True
                break
    return normalized


def _lookup_matches(actual: str, expected: str, *, company: bool = False) -> bool:
    normalize = _company_lookup_stem if company else _normalized_lookup_text
    actual_value = normalize(actual)
    expected_value = normalize(expected)
    if not actual_value or not expected_value:
        return False
    return (
        actual_value == expected_value
        or expected_value in actual_value
        or actual_value in expected_value
    )


def _application_matches(application: Application, request: ApplicationQueryInput) -> bool:
    if request.exclude_terminal and application.stage in {
        ApplicationStage.REJECTED,
        ApplicationStage.WITHDRAWN,
    }:
        return False
    if request.application_id and application.id != request.application_id:
        return False
    if request.company_name and not _lookup_matches(
        application.company_name,
        request.company_name,
        company=True,
    ):
        return False
    if request.job_title and not _lookup_matches(application.job_title, request.job_title):
        return False
    if request.job_id and application.job_id != request.job_id:
        return False
    if request.stage and application.stage != request.stage:
        return False
    if request.query:
        needle = _normalized_lookup_text(request.query)
        haystack = _normalized_lookup_text(" ".join(
            [
                application.id,
                application.company_name,
                application.job_title,
                application.job_id or "",
                application.stage.value,
            ]
        ))
        if needle not in haystack:
            return False
    return True


def application_query(
    request: ApplicationQueryInput,
    repository: RecruitmentRepository,
) -> ApplicationQueryResponse:
    def operation() -> _Outcome[ApplicationQueryData]:
        applications = [
            Application.model_validate(application)
            for application in repository.list_applications()
        ]
        excluded_terminal = sum(
            application.stage in {ApplicationStage.REJECTED, ApplicationStage.WITHDRAWN}
            for application in applications
        ) if request.exclude_terminal else 0
        matches = [
            application
            for application in applications
            if _application_matches(application, request)
        ]
        data = ApplicationQueryData(
            matches=matches,
            application=None if request.list_all or len(matches) != 1 else matches[0],
            total=len(matches),
            company_count=len({application.company_name for application in matches}),
            excluded_terminal=excluded_terminal,
        )
        if not matches:
            return _outcome(
                ToolStatus.NO_RESULTS,
                data,
                error_code=ToolErrorCode.NO_RESULTS,
                error_message="No application matched the query.",
            )
        if len(matches) > 1 and not request.list_all:
            return _outcome(
                ToolStatus.AMBIGUOUS,
                data,
                error_code=ToolErrorCode.AMBIGUOUS_MATCH,
                error_message="Multiple applications matched; no application was selected.",
            )
        return _outcome(
            ToolStatus.SUCCESS,
            data,
        )

    return _execute(request, "application_query", ApplicationQueryResponse, operation)


# Request aliases keep the public vocabulary consistent across adapters.
JobSearchRequest = JobSearchInput
JobDetailRequest = JobDetailInput
CompanyCoverageRequest = CompanyCoverageInput
ApplicationQueryRequest = ApplicationQueryInput
TodayScheduleRequest = TodayScheduleInput

# Verb-oriented aliases are intentionally thin: all execution remains in the five tools above.
get_today_schedule = today_schedule
job_search = search_jobs
get_job_detail = job_detail
get_company_coverage = company_coverage
query_applications = application_query
