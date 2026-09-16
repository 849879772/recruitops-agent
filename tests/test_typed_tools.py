from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

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
from packages.tools import (
    ApplicationQueryInput,
    CompanyCoverageInput,
    JobDetailInput,
    JobSearchInput,
    ToolErrorCode,
    ToolStatus,
    TodayScheduleInput,
    application_query,
    company_coverage,
    job_detail,
    search_jobs,
    today_schedule,
)


class InMemoryRepository:
    def __init__(self) -> None:
        now = datetime(2026, 8, 19, 8, 0, tzinfo=timezone.utc)
        self.jobs = [
            Job(
                id="job-1",
                company_id="company-1",
                title="C++开发工程师",
                city="上海",
                detail_url="https://example.com/jobs/1",
                jd_raw="岗位职责\n任职要求",
                cohort=2027,
                cohort_status="confirmed",
                batch=RecruitmentBatch.FORMAL,
                match_score=88,
                first_seen_at=now,
                source="fixture-jobs",
                source_ref="jobs/job-1",
            )
        ]
        self.analysis = JobAnalysis(
            match_score=88,
            advantages=["C++"],
            gaps=["分布式"],
            summary="匹配",
        )
        self.companies = [
            Company(
                id="company-1",
                name="示例公司",
                aliases=["Example"],
                campus_url="https://example.com/campus",
                crawler_key="fixture",
                integration_status="connected",
                source="fixture-config",
                source_ref="companies[0]",
            ),
            Company(
                id="company-2",
                name="未接入公司",
                integration_status="not_connected",
                source="fixture-config",
                source_ref="companies[1]",
            ),
        ]
        self.applications = [
            Application(
                id="application-1",
                company_name="示例公司",
                job_title="C++开发工程师",
                job_id="job-1",
                stage=ApplicationStage.WRITTEN,
                idempotency_key="application:application-1",
                source="fixture-applications",
                source_ref="applications/application-1",
            ),
            Application(
                id="application-2",
                company_name="示例公司",
                job_title="算法工程师",
                job_id="job-2",
                stage=ApplicationStage.APPLIED,
                idempotency_key="application:application-2",
                source="fixture-applications",
                source_ref="applications/application-2",
            ),
        ]
        self.events = [
            ScheduleEvent(
                id="event-today",
                title="笔试 · 示例公司",
                event_date=date(2026, 8, 19),
                event_type="笔试",
                company_name="示例公司",
                job_title="C++开发工程师",
                application_stage=ApplicationStage.WRITTEN,
                starts_at=datetime(2026, 8, 19, 10, 0, tzinfo=timezone.utc),
                application_id="application-1",
                location_or_link="线上",
                source="fixture-applications",
                source_ref="applications/application-1/events/event-today",
            ),
            ScheduleEvent(
                id="event-other-day",
                title="面试 · 示例公司",
                event_date=date(2026, 8, 20),
                event_type="面试",
                company_name="示例公司",
                job_title="C++开发工程师",
                application_stage=ApplicationStage.INTERVIEW1,
                application_id="application-1",
                source="fixture-applications",
                source_ref="applications/application-1/events/event-other-day",
            ),
        ]
        self.calls: list[str] = []

    def search_jobs(self, **kwargs: object) -> JobPage:
        self.calls.append("search_jobs")
        items = self.jobs
        query = kwargs.get("query")
        if query:
            items = [job for job in items if str(query).casefold() in job.title.casefold()]
        first_seen_on = kwargs.get("first_seen_on")
        if first_seen_on:
            items = [
                job
                for job in items
                if job.first_seen_at is not None
                and job.first_seen_at.date() == first_seen_on
            ]
        return JobPage(
            items=items,
            total=len(items),
            limit=int(kwargs["limit"]),
            offset=int(kwargs["offset"]),
        )

    def get_job(self, job_id: str) -> JobDetail | None:
        self.calls.append("get_job")
        for job in self.jobs:
            if job.id == job_id:
                return JobDetail(job=job, analysis=self.analysis)
        return None

    def latest_job_seen_at(self) -> datetime | None:
        timestamps = [
            job.last_seen_at or job.first_seen_at
            for job in self.jobs
            if job.last_seen_at is not None or job.first_seen_at is not None
        ]
        return max(timestamps, default=None)

    def list_companies(self) -> list[Company]:
        self.calls.append("list_companies")
        return self.companies

    def list_applications(self) -> list[Application]:
        self.calls.append("list_applications")
        return self.applications

    def list_schedule(self, on_date: date | None = None) -> list[ScheduleEvent]:
        self.calls.append("list_schedule")
        return self.events

    def write_was_called(self) -> bool:
        return False


def test_all_five_tools_return_typed_success_with_evidence() -> None:
    repository = InMemoryRepository()

    schedule = today_schedule(
        TodayScheduleInput(on_date=date(2026, 8, 19)),
        repository,
    )
    jobs = search_jobs(JobSearchInput(query="C++"), repository)
    detail = job_detail(JobDetailInput(job_id="job-1"), repository)
    companies = company_coverage(CompanyCoverageInput(company_name="Example"), repository)
    application = application_query(
        ApplicationQueryInput(application_id="application-1"),
        repository,
    )
    responses = [schedule, jobs, detail, companies, application]
    assert all(response.success for response in responses)
    assert all(response.status is ToolStatus.SUCCESS for response in responses)
    assert all(response.read_only is True for response in responses)
    assert all(response.evidence for response in responses)
    assert all(response.timeout_ms > 0 and response.timed_out is False for response in responses)
    assert schedule.data is not None and len(schedule.data.events) == 1
    assert jobs.data is not None and jobs.data.items[0].id == "job-1"
    assert detail.data is not None and detail.data.analysis is not None
    assert companies.data is not None and companies.data.companies[0].name == "示例公司"
    assert application.data is not None and application.data.application is not None


def test_company_coverage_bounds_unfiltered_result_size() -> None:
    repository = InMemoryRepository()
    repository.companies = [
        Company(
            id=f"company-{index}",
            name=f"Company {index}",
            integration_status="connected",
            source="fixture-config",
            source_ref=f"companies[{index}]",
        )
        for index in range(75)
    ]

    first_page = company_coverage(CompanyCoverageInput(), repository)
    second_page = company_coverage(
        CompanyCoverageInput(limit=25, offset=50), repository
    )

    assert first_page.success is True and first_page.data is not None
    assert first_page.data.total == 75
    assert len(first_page.data.companies) == 50
    assert second_page.success is True and second_page.data is not None
    assert len(second_page.data.companies) == 25


def test_schedule_and_job_search_report_no_results_without_guessing() -> None:
    repository = InMemoryRepository()

    schedule = today_schedule(
        TodayScheduleInput(on_date=date(2026, 8, 21)),
        repository,
    )
    jobs = search_jobs(JobSearchInput(query="不存在的岗位"), repository)

    assert schedule.status is ToolStatus.NO_RESULTS
    assert schedule.error_code is ToolErrorCode.NO_RESULTS
    assert schedule.data is not None and schedule.data.events == []
    assert jobs.status is ToolStatus.NO_RESULTS
    assert jobs.error_code is ToolErrorCode.NO_RESULTS
    assert jobs.data is not None and jobs.data.items == []


def test_application_query_marks_zero_and_multiple_matches_explicitly() -> None:
    repository = InMemoryRepository()

    missing = application_query(ApplicationQueryInput(query="不存在"), repository)
    ambiguous = application_query(ApplicationQueryInput(company_name="示例公司"), repository)

    assert missing.status is ToolStatus.NO_RESULTS
    assert missing.error_code is ToolErrorCode.NO_RESULTS
    assert missing.data is not None and missing.data.application is None
    assert ambiguous.status is ToolStatus.AMBIGUOUS
    assert ambiguous.error_code is ToolErrorCode.AMBIGUOUS_MATCH
    assert ambiguous.data is not None
    assert ambiguous.data.application is None
    assert len(ambiguous.data.matches) == 2


def test_application_query_can_explicitly_list_all_matches() -> None:
    response = application_query(
        ApplicationQueryInput(list_all=True),
        InMemoryRepository(),
    )

    assert response.status is ToolStatus.SUCCESS
    assert response.data is not None
    assert response.data.application is None
    assert len(response.data.matches) == 2
    assert response.data.total == 2
    assert response.data.company_count == 1


def test_application_query_matches_company_alias_and_title_without_job_code() -> None:
    repository = InMemoryRepository()
    repository.applications.append(
        Application(
            id="application-h3c",
            company_name="新华三集团",
            job_title="软件开发工程师-C/C++(J18705)",
            stage=ApplicationStage.APPLIED,
            idempotency_key="application:application-h3c",
            source="fixture-applications",
            source_ref="applications/application-h3c",
        )
    )

    by_company = application_query(
        ApplicationQueryInput(company_name="新华三技术"),
        repository,
    )
    by_title = application_query(
        ApplicationQueryInput(job_title="软件开发工程师-C/C++"),
        repository,
    )

    assert by_company.status is ToolStatus.SUCCESS
    assert by_company.data is not None
    assert by_company.data.application is not None
    assert by_company.data.application.id == "application-h3c"
    assert by_title.status is ToolStatus.SUCCESS
    assert by_title.data is not None
    assert by_title.data.application is not None
    assert by_title.data.application.id == "application-h3c"


def test_application_query_can_exclude_rejected_and_withdrawn_records() -> None:
    repository = InMemoryRepository()
    repository.applications.extend(
        [
            Application(
                id="application-rejected",
                company_name="已挂公司",
                job_title="软件工程师",
                stage=ApplicationStage.REJECTED,
                idempotency_key="application:application-rejected",
                source="fixture-applications",
                source_ref="applications/application-rejected",
            ),
            Application(
                id="application-withdrawn",
                company_name="已撤回公司",
                job_title="算法工程师",
                stage=ApplicationStage.WITHDRAWN,
                idempotency_key="application:application-withdrawn",
                source="fixture-applications",
                source_ref="applications/application-withdrawn",
            ),
        ]
    )

    response = application_query(
        ApplicationQueryInput(list_all=True, exclude_terminal=True),
        repository,
    )

    assert response.status is ToolStatus.SUCCESS
    assert response.data is not None
    assert {item.id for item in response.data.matches} == {
        "application-1",
        "application-2",
    }
    assert response.data.total == 2
    assert response.data.company_count == 1
    assert response.data.excluded_terminal == 2


def test_inputs_reject_unknown_fields_and_invalid_timeout() -> None:
    with pytest.raises(ValidationError):
        JobSearchInput(timeout_ms=0)
    with pytest.raises(ValidationError):
        JobDetailInput(job_id="job-1", mutating=True)
