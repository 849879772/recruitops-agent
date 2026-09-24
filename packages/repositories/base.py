from datetime import date, datetime
from typing import Protocol

from packages.domain.models import (
    Application,
    ApplicationPage,
    Company,
    JobDetail,
    JobPage,
    RecruitmentBatch,
    ScheduleEvent,
)


class RecruitmentRepository(Protocol):
    def search_jobs(
        self,
        *,
        query: str | None = None,
        company: str | None = None,
        cohort: int | None = None,
        cohort_status: str | None = None,
        recruitment_track: str | None = None,
        first_seen_on: date | None = None,
        batches: tuple[RecruitmentBatch, ...] | None = None,
        min_score: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> JobPage: ...

    def get_job(self, job_id: str) -> JobDetail | None: ...

    def latest_job_seen_at(self) -> datetime | None: ...

    def list_companies(self) -> list[Company]: ...

    def list_applications(self) -> list[Application]: ...

    def search_applications(
        self, *, query: str | None = None, stage: str | None = None,
        stages: tuple[str, ...] | None = None, limit: int = 50, offset: int = 0,
    ) -> ApplicationPage: ...

    def list_schedule(self, on_date: date | None = None) -> list[ScheduleEvent]: ...
