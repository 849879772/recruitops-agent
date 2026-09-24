from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, or_, select

from packages.domain.models import (
    Application,
    ApplicationPage,
    ApplicationStage,
    Company,
    Job,
    JobAnalysis,
    JobBrowseItem,
    JobBrowsePage,
    JobDetail,
    JobPage,
    RecruitmentBatch,
    ScheduleEvent,
)
from packages.storage import Storage
from packages.storage.models import (
    ApplicationSnapshot,
    CompanySnapshot,
    JobAnalysisSnapshot,
    JobSnapshot,
    ScheduleEventSnapshot,
)


def _json_list(value: str | list[Any] | None) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return [value]
    return parsed if isinstance(parsed, list) else [parsed]


def _platform_label(source_platform: str | None, detail_url: str) -> str:
    value = (source_platform or "").strip()
    normalized = value.casefold()
    url = (detail_url or "").casefold()
    if "moka" in normalized or "mokahr.com" in url:
        return "Moka"
    if "beisen" in normalized or "zhiye.com" in url:
        return "北森"
    if "feishu" in normalized or "lark" in normalized or "feishu.cn" in url or "mioffice" in url:
        return "飞书"
    if "hotjob" in normalized or "hotjob.cn" in url:
        return "Hotjob"
    if "moseeker" in normalized:
        return "Moseeker"
    if "ourats" in normalized:
        return "OurATS"
    return "自建"


def _timestamp(value: datetime | None) -> float:
    if value is None:
        return 0.0
    try:
        return value.timestamp()
    except (OSError, OverflowError, ValueError):
        return 0.0


def _is_pending_score(item: JobBrowseItem) -> bool:
    return (
        item.analysis_status == "eligible"
        and item.match_score is None
        and item.capture_status == "complete"
    )


class PostgresRecruitmentRepository:
    """Read the Agent-owned recruitment snapshot tables.

    The name reflects the production backend, while the implementation remains
    SQLAlchemy-portable so the same contract can be exercised with SQLite tests.
    """

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

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
    ) -> JobPage:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset cannot be negative")

        statement = select(JobSnapshot)
        # Offline reconciliation uses a clear source-ref marker so ordinary
        # searches hide only jobs that passed both missing-run and age grace.
        predicates = [
            or_(
                JobSnapshot.source_ref.is_(None),
                ~JobSnapshot.source_ref.like("recruitops-offline:v1:inactive:%"),
            )
        ]
        normalized_query = (query or "").strip().casefold()
        normalized_company = (company or "").strip().casefold()
        if normalized_query or normalized_company:
            statement = statement.outerjoin(
                CompanySnapshot,
                CompanySnapshot.id == JobSnapshot.company_id,
            )
        if normalized_query:
            pattern = f"%{normalized_query}%"
            predicates.append(
                or_(
                    func.lower(JobSnapshot.title).like(pattern),
                    func.lower(func.coalesce(JobSnapshot.city, "")).like(pattern),
                    func.lower(func.coalesce(CompanySnapshot.name, JobSnapshot.company_id)).like(
                        pattern
                    ),
                )
            )
        if normalized_company:
            pattern = f"%{normalized_company}%"
            predicates.append(
                or_(
                    func.lower(JobSnapshot.company_id).like(pattern),
                    func.lower(func.coalesce(CompanySnapshot.name, "")).like(pattern),
                )
            )
        if cohort is not None:
            predicates.append(JobSnapshot.cohort == cohort)
        if cohort_status:
            predicates.append(func.lower(JobSnapshot.cohort_status) == cohort_status.casefold())
        if first_seen_on is not None:
            predicates.append(func.date(JobSnapshot.first_seen_at) == first_seen_on)
        selected_batches = batches
        if recruitment_track:
            track = recruitment_track.casefold()
            aliases = {
                "early_batch": RecruitmentBatch.EARLY,
                "formal": RecruitmentBatch.FORMAL,
                "internship": RecruitmentBatch.INTERNSHIP,
            }
            selected = aliases.get(track)
            if selected is not None:
                selected_batches = (selected,)
        if selected_batches:
            predicates.append(JobSnapshot.batch.in_([item.value for item in selected_batches]))
        if min_score is not None:
            predicates.append(JobSnapshot.match_score >= min_score)
        if predicates:
            statement = statement.where(*predicates)

        count_statement = select(func.count()).select_from(statement.order_by(None).subquery())
        statement = statement.order_by(
            JobSnapshot.match_score.desc().nullslast(),
            JobSnapshot.first_seen_at.desc().nullslast(),
            JobSnapshot.id,
        ).limit(limit).offset(offset)
        with self.storage.session() as session:
            total = int(session.scalar(count_statement) or 0)
            rows = list(session.scalars(statement).all())
        return JobPage(
            items=[self._job(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    def list_source_jobs(
        self, source_record_id: str, *, page: int = 1, page_size: int = 30
    ) -> dict[str, Any] | None:
        """List related saved jobs for a source's bound company.

        This intentionally does not apply the normal cohort, score, or
        availability filters. A source without a company binding is still a
        valid source record, but it has no safe job scope to expose.
        """

        if page < 1 or page_size < 1 or page_size > 100:
            raise ValueError("invalid pagination")

        # Import lazily because company_registry imports the storage package,
        # whose sync module imports the repository protocol package.
        from packages.discovery.company_registry import CompanySourceRecord
        from packages.config import get_settings
        from packages.candidate_profile.loader import load_candidate_profile
        from packages.matching.title_policy import screen_title_job
        path = get_settings().candidate_profile_config
        profile = load_candidate_profile(path) if path.is_file() else None

        with self.storage.session() as session:
            source = session.get(CompanySourceRecord, source_record_id)
            if source is None:
                return None
            if not source.company_id:
                return {"items": [], "total": 0, "page": page, "page_size": page_size}

            statement = select(JobSnapshot).where(
                JobSnapshot.company_id == source.company_id
            )
            rows = [row for row in session.scalars(statement.order_by(JobSnapshot.title, JobSnapshot.id))
                    if screen_title_job({"title": row.title}, profile).eligible]
            total = len(rows)
            rows = rows[(page - 1) * page_size:page * page_size]

        return {
            "items": [self._source_job(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def get_job_snapshot(self, job_id: str) -> dict[str, Any] | None:
        """Return the persisted job fields needed by read-only consumers."""

        with self.storage.session() as session:
            row = session.get(JobSnapshot, job_id)
            return self._source_job(row) if row is not None else None

    def browse_jobs(
        self, *, query: str | None = None, company: str | None = None,
        category: str | None = None, platform: str | None = None,
        evaluation: str | None = None, score_band: str | None = None,
        first_seen_on: date | None = None, sort: str = "score",
        limit: int = 50, offset: int = 0, include_summary: bool = True,
    ) -> JobBrowsePage:
        from .job_browse import browse_jobs
        return browse_jobs(self, query=query, company=company, category=category,
            platform=platform, evaluation=evaluation, score_band=score_band,
            first_seen_on=first_seen_on, sort=sort, limit=limit, offset=offset,
            include_summary=include_summary)

    def get_job(self, job_id: str) -> JobDetail | None:
        with self.storage.session() as session:
            job = session.get(JobSnapshot, job_id)
            if job is None:
                return None
            analysis = session.get(JobAnalysisSnapshot, job_id)
            return JobDetail(
                job=self._job(job),
                analysis=self._analysis(analysis) if analysis is not None else None,
            )

    def latest_job_seen_at(self) -> datetime | None:
        statement = select(
            func.max(
                func.coalesce(
                    JobSnapshot.last_seen_at,
                    JobSnapshot.updated_at,
                    JobSnapshot.first_seen_at,
                )
            )
        )
        with self.storage.session() as session:
            return session.scalar(statement)

    def job_counts_by_company(self) -> dict[str, int]:
        """Return all persisted company job counts with one aggregate query."""

        statement = (
            select(JobSnapshot.company_id, func.count(JobSnapshot.id))
            .where(
                or_(
                    JobSnapshot.source_ref.is_(None),
                    ~JobSnapshot.source_ref.like("recruitops-offline:v1:inactive:%"),
                )
            )
            .group_by(JobSnapshot.company_id)
        )
        with self.storage.session() as session:
            return {str(company_id): int(count) for company_id, count in session.execute(statement)}

    def list_companies(self) -> list[Company]:
        with self.storage.session() as session:
            rows = list(session.scalars(select(CompanySnapshot).order_by(CompanySnapshot.name)))
        return [
            Company(
                id=row.id,
                name=row.name,
                aliases=list(row.aliases or []),
                campus_url=row.campus_url,
                crawler_key=row.crawler_key,
                integration_status=row.integration_status,
                organization_id=row.organization_id,
                recruitment_unit_name=row.recruitment_unit_name,
                source_identity=row.source_identity,
                created_at=row.created_at,
                updated_at=row.updated_at,
                source=row.source,
                source_ref=row.source_ref,
            )
            for row in rows
        ]

    def list_applications(self) -> list[Application]:
        with self.storage.session() as session:
            rows = list(
                session.scalars(
                    select(ApplicationSnapshot).order_by(ApplicationSnapshot.updated_at.desc())
                )
            )
        return [
            Application(
                id=row.id,
                company_name=row.company_name,
                job_title=row.job_title,
                job_id=row.job_id,
                record_url=row.record_url,
                stage=row.stage,
                idempotency_key=row.idempotency_key,
                note=row.note,
                stage_history=list(row.stage_history or []),
                source_stage=row.source_stage,
                source_status=row.source_status,
                source_status_synced_at=row.source_status_synced_at,
                created_at=row.created_at,
                updated_at=row.updated_at,
                source=row.source,
                source_ref=row.source_ref,
            )
            for row in rows
        ]

    def search_applications(
        self, *, query: str | None = None, stage: str | None = None,
        stages: tuple[str, ...] | None = None, limit: int = 50, offset: int = 0,
    ) -> ApplicationPage:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset must be non-negative")
        if stage is not None and stages is not None:
            raise ValueError("stage and stages cannot be combined")
        selected_stages = (stage,) if stage is not None else stages
        if selected_stages is not None:
            selected_stages = tuple(dict.fromkeys(str(value).strip() for value in selected_stages))
            if any(value not in {item.value for item in ApplicationStage} for value in selected_stages):
                raise ValueError("unknown application stage")
        predicates = []
        # Match SQL lower(), not Python casefold(): casefold expands e.g. ß
        # and would make even an exact "Straße" query miss the stored value.
        normalized_query = (query or "").strip().lower()
        if normalized_query:
            predicates.append(or_(
                func.lower(ApplicationSnapshot.company_name).contains(normalized_query, autoescape=True),
                func.lower(ApplicationSnapshot.job_title).contains(normalized_query, autoescape=True),
            ))
        with self.storage.session() as session:
            unfiltered_total = int(session.scalar(select(func.count()).select_from(ApplicationSnapshot)) or 0)
            stage_counts = {str(key): int(count) for key, count in session.execute(
                select(ApplicationSnapshot.stage, func.count()).where(*predicates)
                .group_by(ApplicationSnapshot.stage)
            )}
            if selected_stages is not None:
                predicates.append(ApplicationSnapshot.stage.in_(selected_stages))
                total = sum(stage_counts.get(stage, 0) for stage in selected_stages)
            else:
                total = sum(stage_counts.values())
            rows = list(
                session.scalars(
                    select(ApplicationSnapshot)
                    .where(*predicates)
                    .order_by(ApplicationSnapshot.updated_at.desc(), ApplicationSnapshot.id)
                    .limit(limit)
                    .offset(offset)
                )
            )
        return ApplicationPage(
            items=[
                Application(
                    id=row.id,
                    company_name=row.company_name,
                    job_title=row.job_title,
                    job_id=row.job_id,
                    record_url=row.record_url,
                    stage=row.stage,
                    idempotency_key=row.idempotency_key,
                    note=row.note,
                    stage_history=list(row.stage_history or []),
                    source_stage=row.source_stage,
                    source_status=row.source_status,
                    source_status_synced_at=row.source_status_synced_at,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    source=row.source,
                    source_ref=row.source_ref,
                )
                for row in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
            stage_counts=stage_counts,
            unfiltered_total=unfiltered_total,
        )

    def list_schedule(self, on_date: date | None = None) -> list[ScheduleEvent]:
        statement = select(ScheduleEventSnapshot)
        if on_date is not None:
            statement = statement.where(ScheduleEventSnapshot.event_date == on_date)
        statement = statement.order_by(
            ScheduleEventSnapshot.event_date,
            ScheduleEventSnapshot.event_time,
            ScheduleEventSnapshot.id,
        )
        with self.storage.session() as session:
            rows = list(session.scalars(statement))
        return [
            ScheduleEvent(
                id=row.id,
                title=row.title,
                event_date=row.event_date,
                status=row.status,
                time_kind=row.time_kind,
                event_time=row.event_time,
                event_type=row.event_type,
                company_name=row.company_name,
                job_title=row.job_title,
                application_stage=row.application_stage,
                starts_at=row.starts_at,
                ends_at=row.ends_at,
                application_id=row.application_id,
                location_or_link=row.location_or_link,
                note=row.note,
                created_at=row.created_at,
                updated_at=row.updated_at,
                source=row.source,
                source_ref=row.source_ref,
            )
            for row in rows
        ]

    @staticmethod
    def _job(row: JobSnapshot) -> Job:
        values: dict[str, Any] = {
            "id": row.id,
            "company_id": row.company_id,
            "title": row.title,
            "city": row.city,
            "detail_url": row.detail_url,
            "jd_raw": row.jd_raw,
            "cohort": row.cohort,
            "cohort_status": row.cohort_status,
            "batch": row.batch,
            "match_score": row.match_score,
            "first_seen_at": row.first_seen_at,
            "last_seen_at": row.last_seen_at,
            "organization_id": row.organization_id,
            "recruitment_unit_id": row.recruitment_unit_id,
            "recruitment_campaign_id": row.recruitment_campaign_id,
            "source_platform": row.source_platform,
            "source_tenant": row.source_tenant,
            "native_job_id": row.native_job_id,
            "normalized_detail_url": row.normalized_detail_url,
            "business_key": row.business_key,
            "capture_evidence": dict(row.capture_evidence or {}),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "source": row.source,
            "source_ref": row.source_ref,
        }
        # The storage contract can land before the domain response model. If
        # that model later declares these fields, pass them through without
        # making the current strict model accept unknown keys.
        for field_name in (
            "capture_status",
            "capture_failure_reason",
            "availability_status",
            "title_key",
        ):
            if field_name in Job.model_fields:
                values[field_name] = getattr(row, field_name)
        return Job(**values)

    @staticmethod
    def _source_job(row: JobSnapshot) -> dict[str, Any]:
        """Serialize a job without applying the ordinary jobs-page filters."""

        return {
            "id": row.id,
            "company_id": row.company_id,
            "title": row.title,
            "city": row.city,
            "detail_url": row.detail_url,
            "capture_status": row.capture_status or "unknown",
            "capture_failure_reason": row.capture_failure_reason or "",
            "availability_status": row.availability_status or "active",
            "title_key": row.title_key,
            "match_score": row.match_score,
        }

    @staticmethod
    def _analysis(row: JobAnalysisSnapshot) -> JobAnalysis:
        return JobAnalysis(
            match_score=row.match_score,
            advantages=[str(item) for item in _json_list(row.advantages)],
            gaps=[str(item) for item in _json_list(row.gaps)],
            summary=row.summary,
            recommendation=row.recommendation,
            score_breakdown=dict(row.score_breakdown or {}),
            evidence=list(row.evidence or []),
            evidence_level=row.evidence_level,
            matched_directions=list(row.matched_directions or []),
            primary_match_direction=row.primary_match_direction,
            analysis_status=row.analysis_status,
            model=row.model,
            analysis_version=row.analysis_version,
            prompt_version=row.prompt_version,
            content_fingerprint=row.content_fingerprint,
            profile_fingerprint=row.profile_fingerprint,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            filter_reasons=list(row.filter_reasons or []),
            refusal_reason=row.refusal_reason,
            error_code=row.error_code,
            analyzed_at=row.analyzed_at,
        )


__all__ = ["PostgresRecruitmentRepository"]
