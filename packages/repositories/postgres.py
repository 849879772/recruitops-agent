from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from packages.domain.models import (
    Application,
    ApplicationPage,
    CompanyJobSummary,
    Company,
    Job,
    JobAnalysis,
    JobBrowseFacets,
    JobBrowseItem,
    JobBrowsePage,
    JobBrowseStats,
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
from packages.recruitment_core.job_filters import JOB_CATEGORY_LABELS, job_category


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
        self,
        *,
        query: str | None = None,
        company: str | None = None,
        category: str | None = None,
        platform: str | None = None,
        evaluation: str | None = None,
        score_band: str | None = None,
        first_seen_on: date | None = None,
        sort: str = "score",
        limit: int = 50,
        offset: int = 0,
    ) -> JobBrowsePage:
        """Return a compact, user-facing view of confirmed 2027 campus jobs."""

        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset cannot be negative")

        statement = (
            select(
                JobSnapshot.id.label("id"),
                JobSnapshot.company_id.label("company_id"),
                JobSnapshot.organization_id.label("job_organization_id"),
                JobSnapshot.title.label("title"),
                JobSnapshot.city.label("city"),
                JobSnapshot.detail_url.label("detail_url"),
                JobSnapshot.source_platform.label("source_platform"),
                JobSnapshot.batch.label("batch"),
                JobSnapshot.capture_status.label("capture_status"),
                JobSnapshot.capture_failure_reason.label("capture_failure_reason"),
                JobSnapshot.availability_status.label("availability_status"),
                JobSnapshot.match_score.label("job_match_score"),
                JobSnapshot.first_seen_at.label("first_seen_at"),
                CompanySnapshot.name.label("company_name"),
                CompanySnapshot.organization_id.label("company_organization_id"),
                CompanySnapshot.recruitment_unit_name.label("recruitment_unit_name"),
                CompanySnapshot.campus_url.label("campus_url"),
                JobAnalysisSnapshot.match_score.label("analysis_match_score"),
                JobAnalysisSnapshot.recommendation.label("recommendation"),
                JobAnalysisSnapshot.summary.label("summary"),
                JobAnalysisSnapshot.advantages.label("advantages"),
                JobAnalysisSnapshot.gaps.label("gaps"),
                JobAnalysisSnapshot.matched_directions.label("matched_directions"),
                JobAnalysisSnapshot.primary_match_direction.label("primary_match_direction"),
                JobAnalysisSnapshot.analysis_status.label("analysis_status"),
            )
            .outerjoin(CompanySnapshot, CompanySnapshot.id == JobSnapshot.company_id)
            .outerjoin(JobAnalysisSnapshot, JobAnalysisSnapshot.job_id == JobSnapshot.id)
            .where(
                JobSnapshot.cohort == 2027,
                func.lower(JobSnapshot.cohort_status) == "confirmed",
                JobSnapshot.batch.in_(
                    [RecruitmentBatch.FORMAL.value, RecruitmentBatch.EARLY.value]
                ),
                or_(
                    JobSnapshot.source_ref.is_(None),
                    ~JobSnapshot.source_ref.like("recruitops-offline:v1:inactive:%"),
                ),
            )
        )
        if first_seen_on is not None:
            statement = statement.where(func.date(JobSnapshot.first_seen_at) == first_seen_on)

        with self.storage.session() as session:
            rows = list(session.execute(statement).mappings())
            catalog_rows = list(
                session.execute(
                    select(
                        CompanySnapshot.id,
                        CompanySnapshot.name,
                        CompanySnapshot.organization_id,
                        CompanySnapshot.recruitment_unit_name,
                        CompanySnapshot.campus_url,
                    )
                ).mappings()
            )
            application_rows = list(
                session.execute(
                    select(
                        ApplicationSnapshot.job_id,
                        ApplicationSnapshot.stage,
                        ApplicationSnapshot.updated_at,
                    )
                    .where(ApplicationSnapshot.job_id.is_not(None))
                    .order_by(ApplicationSnapshot.updated_at.desc())
                )
            )

        application_stages: dict[str, str] = {}
        for job_id, stage, _updated_at in application_rows:
            if job_id:
                application_stages.setdefault(str(job_id), str(stage))

        catalog_by_name = {
            str(row["name"]).strip().casefold(): row
            for row in catalog_rows
            if row["name"]
        }
        canonical_organizations = {
            name: str(row["organization_id"])
            for name, row in catalog_by_name.items()
            if row["organization_id"]
        }
        from packages.config import get_settings
        from packages.candidate_profile.loader import load_candidate_profile
        from packages.matching.title_policy import screen_title_job
        profile_path = get_settings().candidate_profile_config
        profile = load_candidate_profile(profile_path) if profile_path.is_file() else None
        items: list[JobBrowseItem] = []
        company_metadata: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not screen_title_job({"title": row["title"]}, profile).eligible:
                continue
            company_id = str(row["company_id"])
            company_name = str(row["company_name"] or company_id)
            catalog_company = catalog_by_name.get(company_name.strip().casefold())
            # The company catalog is the canonical grouping source. Older job
            # snapshots can carry missing or stale organization IDs from before
            # reconciliation, which would otherwise split one company into rows.
            organization_id = (
                row["company_organization_id"]
                or canonical_organizations.get(company_name.strip().casefold())
                or row["job_organization_id"]
            )
            title = str(row["title"])
            detail_url = str(row["detail_url"])
            category_key = job_category({"title": title})
            match_score = row["job_match_score"]
            if match_score is None:
                match_score = row["analysis_match_score"]
            analysis_status = row["analysis_status"]
            if analysis_status and analysis_status != "complete":
                match_score = None
            if analysis_status in {"direction_out", "doctorate_only", "internship", "cohort_unconfirmed"}:
                # The current title policy already admitted this row. Legacy
                # model exclusions must not label a related unscored job as excluded.
                analysis_status = "eligible" if row["capture_status"] == "complete" else "jd_incomplete"
            item = JobBrowseItem(
                id=str(row["id"]),
                company_id=company_id,
                company_name=company_name,
                organization_id=str(organization_id) if organization_id else None,
                title=title,
                city=row["city"],
                detail_url=detail_url,
                category=category_key,
                category_label=JOB_CATEGORY_LABELS[category_key],
                platform=_platform_label(row["source_platform"], detail_url),
                batch=row["batch"],
                capture_status=row["capture_status"] or "unknown",
                capture_failure_reason=row["capture_failure_reason"] or "",
                availability_status=row["availability_status"] or "active",
                match_score=match_score,
                recommendation=row["recommendation"],
                summary=row["summary"],
                advantages=[str(value) for value in _json_list(row["advantages"])],
                gaps=[str(value) for value in _json_list(row["gaps"])],
                matched_directions=[str(value) for value in row["matched_directions"] or []],
                primary_match_direction=row["primary_match_direction"],
                analysis_status=analysis_status,
                first_seen_at=row["first_seen_at"],
                application_stage=application_stages.get(str(row["id"])),
            )
            items.append(item)
            company_metadata[company_id] = {
                "name": company_name,
                "organization_id": str(organization_id) if organization_id else None,
                "recruitment_unit_name": row["recruitment_unit_name"]
                or (catalog_company["recruitment_unit_name"] if catalog_company else None),
                "campus_url": row["campus_url"]
                or (catalog_company["campus_url"] if catalog_company else None),
            }

        summaries: dict[str, dict[str, Any]] = {}
        for item in items:
            metadata = company_metadata[item.company_id]
            key = item.organization_id or item.company_id
            summary = summaries.setdefault(
                key,
                {
                    "key": key,
                    "name": metadata["name"],
                    "company_ids": set(),
                    "recruitment_units": set(),
                    "campus_url": metadata["campus_url"],
                    "job_count": 0,
                    "scores": [],
                    "top_score": None,
                    "top_job": None,
                },
            )
            summary["company_ids"].add(item.company_id)
            if metadata["recruitment_unit_name"]:
                summary["recruitment_units"].add(str(metadata["recruitment_unit_name"]))
            if not summary["campus_url"] and metadata["campus_url"]:
                summary["campus_url"] = metadata["campus_url"]
            summary["job_count"] += 1
            if item.match_score is not None:
                summary["scores"].append(item.match_score)
                if summary["top_score"] is None or item.match_score > summary["top_score"]:
                    summary["top_score"] = item.match_score
                    summary["top_job"] = item.title

        company_summaries = [
            CompanyJobSummary(
                key=value["key"],
                name=value["name"],
                company_ids=sorted(value["company_ids"], key=str.casefold),
                recruitment_units=sorted(value["recruitment_units"], key=str.casefold),
                campus_url=value["campus_url"],
                job_count=value["job_count"],
                average_score=(
                    sum(value["scores"]) / len(value["scores"])
                    if value["scores"]
                    else None
                ),
                top_score=value["top_score"],
                top_job=value["top_job"],
            )
            for value in summaries.values()
        ]
        company_summaries.sort(
            key=lambda value: (
                value.average_score is None,
                -(value.average_score or 0),
                -value.job_count,
                value.name.casefold(),
            )
        )

        stats = JobBrowseStats(
            jobs=len(items),
            companies=len(company_summaries),
            high_match=sum(1 for item in items if (item.match_score or -1) >= 70),
            unscored=sum(1 for item in items if item.match_score is None),
            pending=sum(1 for item in items if _is_pending_score(item)),
            jd_incomplete=sum(1 for item in items if item.analysis_status == "jd_incomplete"),
            excluded=sum(1 for item in items if item.analysis_status in (
                "direction_out", "doctorate_only", "internship", "cohort_unconfirmed"
            )),
        )
        facets = JobBrowseFacets(
            companies=company_summaries,
            categories=dict(JOB_CATEGORY_LABELS),
            platforms=sorted({item.platform for item in items}, key=str.casefold),
        )
        featured = sorted(
            (item for item in items if (item.match_score or -1) >= 70),
            key=lambda item: (
                -(item.match_score or 0),
                -_timestamp(item.first_seen_at),
                item.company_name.casefold(),
            ),
        )[:10]

        normalized_query = (query or "").strip().casefold()
        normalized_company = (company or "").strip().casefold()
        normalized_platform = (platform or "").strip().casefold()
        filtered = []
        for item in items:
            if normalized_query:
                haystack = f"{item.company_name} {item.title} {item.city or ''}".casefold()
                if normalized_query not in haystack:
                    continue
            if normalized_company:
                company_values = {
                    item.company_id.casefold(),
                    item.company_name.casefold(),
                    (item.organization_id or "").casefold(),
                }
                if normalized_company not in company_values:
                    continue
            if category and item.category != category:
                continue
            if normalized_platform and item.platform.casefold() != normalized_platform:
                continue
            if evaluation == "scored" and item.match_score is None:
                continue
            if evaluation == "unscored" and item.match_score is not None:
                continue
            if evaluation == "pending" and not _is_pending_score(item):
                continue
            if evaluation == "jd_incomplete" and item.analysis_status != "jd_incomplete":
                continue
            if evaluation == "excluded" and item.analysis_status not in (
                "direction_out", "doctorate_only", "internship", "cohort_unconfirmed"
            ):
                continue
            if score_band:
                if item.match_score is None:
                    continue
                if score_band == "high" and item.match_score < 70:
                    continue
                if score_band == "medium" and not 60 <= item.match_score < 70:
                    continue
                if score_band == "low" and item.match_score >= 60:
                    continue
            filtered.append(item)

        if sort == "newest":
            filtered.sort(
                key=lambda item: (
                    -_timestamp(item.first_seen_at),
                    -(item.match_score if item.match_score is not None else -1),
                    item.company_name.casefold(),
                    item.title.casefold(),
                )
            )
        elif sort == "company":
            filtered.sort(
                key=lambda item: (
                    item.company_name.casefold(),
                    item.title.casefold(),
                    -_timestamp(item.first_seen_at),
                )
            )
        else:
            filtered.sort(
                key=lambda item: (
                    -(item.match_score if item.match_score is not None else -1),
                    -_timestamp(item.first_seen_at),
                    item.company_name.casefold(),
                    item.title.casefold(),
                )
            )

        return JobBrowsePage(
            items=filtered[offset : offset + limit],
            featured=featured,
            total=len(filtered),
            limit=limit,
            offset=offset,
            stats=stats,
            facets=facets,
        )

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

    def search_applications(self, *, limit: int = 50, offset: int = 0) -> ApplicationPage:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset must be non-negative")
        with self.storage.session() as session:
            total = int(session.scalar(select(func.count()).select_from(ApplicationSnapshot)) or 0)
            rows = list(
                session.scalars(
                    select(ApplicationSnapshot)
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
