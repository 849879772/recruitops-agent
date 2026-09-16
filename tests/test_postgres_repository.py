from datetime import date, datetime, timezone

from sqlalchemy.dialects import postgresql

from packages.domain.models import (
    Application,
    ApplicationStage,
    Company,
    Job,
    JobAnalysis,
    JobDetail,
    RecruitmentBatch,
    ScheduleEvent,
)
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import SnapshotSyncService, Storage
from packages.storage.models import JobAnalysisSnapshot, JobSnapshot


class FixtureRepository:
    def __init__(self) -> None:
        now = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
        self.company = Company(
            id="acme",
            name="示例科技",
            aliases=["Acme"],
            campus_url="https://example.com/campus",
            crawler_key="moka",
            integration_status="connected",
            organization_id="org-acme",
            recruitment_unit_name="示例科技",
            source="fixture",
            source_ref="company:acme",
        )
        self.detail = JobDetail(
            job=Job(
                id="job-1",
                company_id="acme",
                title="C++机器人软件工程师",
                city="上海",
                detail_url="https://example.com/jobs/1",
                jd_raw="负责机器人软件开发",
                cohort=2027,
                cohort_status="confirmed",
                batch=RecruitmentBatch.FORMAL,
                match_score=88,
                first_seen_at=now,
                last_seen_at=now,
                source="fixture",
                source_ref="job:1",
            ),
            analysis=JobAnalysis(
                match_score=88,
                advantages=["C++"],
                gaps=["无"],
                matched_directions=["C++软件开发"],
                model="fake",
                analyzed_at=now,
            ),
        )
        self.application = Application(
            id="app-1",
            company_name="示例科技",
            job_title="C++机器人软件工程师",
            job_id="job-1",
            stage=ApplicationStage.APPLIED,
            idempotency_key="app:1",
            source="fixture",
            source_ref="application:1",
        )
        self.event = ScheduleEvent(
            id="event-1",
            title="笔试",
            event_date=date(2026, 8, 21),
            event_type="written",
            company_name="示例科技",
            job_title="C++机器人软件工程师",
            application_stage=ApplicationStage.WRITTEN,
            source="fixture",
            source_ref="event:1",
        )

    def list_companies(self):
        return [self.company]

    def search_jobs(self, *, limit=50, offset=0, **_kwargs):
        from packages.domain.models import JobPage

        return JobPage(items=[self.detail.job][offset : offset + limit], total=1, limit=limit, offset=offset)

    def get_job(self, job_id):
        return self.detail if job_id == "job-1" else None

    def list_applications(self):
        return [self.application]

    def list_schedule(self, on_date=None):
        return [self.event] if on_date in (None, self.event.event_date) else []


def _repository() -> PostgresRecruitmentRepository:
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    SnapshotSyncService(storage, FixtureRepository()).sync_all(hydrate_job_details=True)
    return PostgresRecruitmentRepository(storage)


def test_reads_independent_snapshot_data() -> None:
    repository = _repository()

    page = repository.search_jobs(
        query="机器人",
        company="示例",
        cohort=2027,
        cohort_status="confirmed",
        batches=(RecruitmentBatch.FORMAL,),
        min_score=80,
    )

    assert page.total == 1
    assert page.items[0].id == "job-1"
    detail = repository.get_job("job-1")
    assert detail is not None
    assert detail.analysis is not None
    assert detail.analysis.advantages == ["C++"]
    assert repository.list_companies()[0].crawler_key == "moka"
    assert repository.list_applications()[0].job_id == "job-1"
    application_page = repository.search_applications(limit=1, offset=0)
    assert application_page.total == 1
    assert application_page.items[0].job_id == "job-1"
    assert repository.list_schedule(date(2026, 8, 21))[0].title == "笔试"


def test_filters_and_missing_rows() -> None:
    repository = _repository()

    assert repository.search_jobs(cohort=2026).total == 0
    assert repository.search_jobs(recruitment_track="early_batch").total == 0
    assert repository.search_jobs(first_seen_on=date(2026, 8, 20)).total == 1
    assert repository.get_job("missing") is None


def test_aggregates_job_counts_by_company() -> None:
    repository = _repository()

    assert repository.job_counts_by_company() == {"acme": 1}


def test_browse_jobs_returns_compact_2027_facets_and_application_state() -> None:
    repository = _repository()

    page = repository.browse_jobs(
        query="机器人",
        company="acme",
        category="robotics",
        platform="自建",
        evaluation="scored",
        score_band="high",
        limit=50,
    )

    assert page.total == 1
    assert page.stats.jobs == 1
    assert page.stats.companies == 1
    assert page.stats.high_match == 1
    assert page.items[0].company_name == "示例科技"
    assert page.items[0].category_label == "机器人与具身智能"
    assert page.items[0].application_stage == ApplicationStage.APPLIED
    assert page.facets.companies[0].job_count == 1
    assert page.facets.companies[0].top_job == "C++机器人软件工程师"


def test_browse_jobs_keeps_today_filter_typed_and_excludes_other_dates() -> None:
    repository = _repository()

    assert repository.browse_jobs(first_seen_on=date(2026, 8, 20)).total == 1
    assert repository.browse_jobs(first_seen_on=date(2026, 8, 21)).total == 0


def test_browse_separates_pending_incomplete_and_excluded_without_stale_scores() -> None:
    repository = _repository()
    statuses = ("eligible", "jd_incomplete", "direction_out", "doctorate_only", "internship", "failed")
    with repository.storage.transaction() as session:
        for status in statuses:
            session.add(JobSnapshot(
                id=status, company_id="acme", title=("销售专员" if status == "direction_out" else
                    "软件工程师（博士）" if status == "doctorate_only" else
                    "软件工程师（实习）" if status == "internship" else f"软件工程师 {status}"),
                detail_url=f"https://example.com/jobs/{status}", cohort=2027,
                cohort_status="confirmed", batch="formal", match_score=99,
                capture_status="complete" if status == "eligible" else "unknown",
                source="fixture", source_ref=f"job:{status}",
            ))
            session.flush()
            session.add(JobAnalysisSnapshot(
                job_id=status, match_score=99, analysis_status=status,
                source="fixture", source_ref=f"analysis:{status}",
            ))
    page = repository.browse_jobs()
    assert page.total == 4
    assert page.stats.unscored == 3
    assert page.stats.pending == 1
    assert page.stats.jd_incomplete == 1
    assert page.stats.excluded == 0
    assert [j.id for j in repository.browse_jobs(evaluation="pending").items] == ["eligible"]
    assert [j.id for j in repository.browse_jobs(evaluation="jd_incomplete").items] == ["jd_incomplete"]
    assert repository.browse_jobs(evaluation="excluded").total == 0
    assert repository.browse_jobs(evaluation="scored").total == 1
    assert repository.browse_jobs(evaluation="unscored").total == 3
    assert all(j.match_score is None for j in page.items if j.id != "job-1")
    assert page.facets.companies[0].top_score == 88
    assert [j.id for j in page.featured] == ["job-1"]
    assert repository.list_applications()[0].job_id == "job-1"


def test_browse_does_not_count_unverified_eligible_job_as_pending_score() -> None:
    repository = _repository()
    with repository.storage.transaction() as session:
        session.add(JobSnapshot(
            id="legacy-eligible", company_id="acme", title="AI算法工程师",
            detail_url="https://example.com/jobs/legacy-eligible", cohort=2027,
            cohort_status="confirmed", batch="formal", capture_status="unknown",
            source="fixture", source_ref="job:legacy-eligible",
        ))
        session.flush()
        session.add(JobAnalysisSnapshot(
            job_id="legacy-eligible", analysis_status="eligible",
            source="fixture", source_ref="analysis:legacy-eligible",
        ))

    page = repository.browse_jobs()

    assert page.stats.pending == 0
    assert repository.browse_jobs(evaluation="pending").total == 0


def test_browse_jobs_groups_legacy_company_ids_with_the_canonical_company() -> None:
    repository = _repository()
    now = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
    with repository.storage.transaction() as session:
        session.add(
            JobSnapshot(
                id="legacy-job",
                company_id="示例科技",
                title="软件开发工程师",
                city="上海",
                detail_url="https://example.com/jobs/legacy",
                cohort=2027,
                cohort_status="confirmed",
                batch="formal",
                first_seen_at=now,
                last_seen_at=now,
                source="fixture",
                source_ref="job:legacy",
            )
        )

    page = repository.browse_jobs(limit=50)

    assert page.stats.companies == 1
    assert page.facets.companies[0].key == "org-acme"
    assert page.facets.companies[0].company_ids == ["acme", "示例科技"]
    assert page.facets.companies[0].job_count == 2


def test_first_seen_filter_keeps_a_typed_date_for_postgresql() -> None:
    captured_values: list[object] = []

    class EmptyRows:
        @staticmethod
        def all() -> list[object]:
            return []

    class CapturingSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def scalar(self, statement) -> int:
            captured_values.extend(
                statement.compile(dialect=postgresql.dialect()).params.values()
            )
            return 0

        @staticmethod
        def scalars(_statement) -> EmptyRows:
            return EmptyRows()

    class CapturingStorage:
        @staticmethod
        def session() -> CapturingSession:
            return CapturingSession()

    requested = date(2026, 8, 21)
    repository = PostgresRecruitmentRepository(CapturingStorage())  # type: ignore[arg-type]
    result = repository.search_jobs(first_seen_on=requested)

    assert result.total == 0
    assert requested in captured_values
    assert requested.isoformat() not in captured_values
