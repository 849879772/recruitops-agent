"""Paged SQL job reads with a bounded, revision-aware title/summary cache.

Title policy stays in Python because its Unicode/ASCII-boundary and doctorate
exceptions must match ingestion exactly. Only distinct titles are screened; large
analysis text and JSON are loaded only for the requested page/featured rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from threading import RLock
from time import monotonic
from types import SimpleNamespace
from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy import Text, and_, any_, bindparam, case, func, or_, select
from sqlalchemy.dialects.postgresql import ARRAY

from packages.domain.models import (
    CompanyJobSummary, JobBrowseFacets, JobBrowseItem, JobBrowsePage,
    JobBrowseStats, RecruitmentBatch,
)
from packages.storage.models import (
    ApplicationSnapshot, CompanySnapshot, JobAnalysisSnapshot, JobSnapshot,
)
from packages.recruitment_core.job_filters import JOB_CATEGORY_LABELS, job_category

_CACHE_TTL_SECONDS = 15.0
_CACHE_LOCK = RLock()
_CACHES: WeakKeyDictionary = WeakKeyDictionary()
_LEGACY_EXCLUSIONS = ("direction_out", "doctorate_only", "internship", "cohort_unconfirmed")


@dataclass
class _Cache:
    lock: Any = field(default_factory=RLock)
    signature: Any = None
    created: float = 0
    titles: dict[str, str] = field(default_factory=dict)
    title_decisions: dict[str, str | None] = field(default_factory=dict)
    profile_key: Any = None
    fold_overrides: dict[str, str] = field(default_factory=dict)
    catalog: dict[str, dict[str, Any]] = field(default_factory=dict)
    summaries: dict[Any, Any] = field(default_factory=dict)


def _cache_for(engine) -> _Cache:
    with _CACHE_LOCK:
        return _CACHES.setdefault(engine, _Cache())


def _base_predicates():
    return (
        JobSnapshot.cohort == 2027,
        func.lower(JobSnapshot.cohort_status) == "confirmed",
        JobSnapshot.batch.in_(("formal", "early")),
        or_(JobSnapshot.source_ref.is_(None),
            ~JobSnapshot.source_ref.like("recruitops-offline:v1:inactive:%")),
    )


def _revision(session):
    # Every normal write updates updated_at. Counts additionally detect inserts/
    # deletes, and TTL covers external writes that preserve old timestamps.
    expressions = []
    for model in (JobSnapshot, JobAnalysisSnapshot, CompanySnapshot):
        expressions.extend((
            select(func.count()).select_from(model).scalar_subquery(),
            select(func.max(model.updated_at)).scalar_subquery(),
        ))
    return tuple(session.execute(select(*expressions)).one())


def _ensure_index(cache, session):
    from packages.config import get_settings
    from packages.candidate_profile.loader import load_candidate_profile
    from packages.matching.title_policy import screen_title_job

    path = get_settings().candidate_profile_config
    exists = path.is_file()
    profile_key = (str(path.resolve()), sha256(path.read_bytes()).hexdigest() if exists else None)
    signature = (_revision(session), profile_key)
    if cache.signature == signature and monotonic() - cache.created < _CACHE_TTL_SECONDS:
        return
    profile = load_candidate_profile(path) if exists else None
    if cache.profile_key != profile_key or len(cache.title_decisions) > 50_000:
        cache.title_decisions = {}
    cache.profile_key = profile_key
    titles = session.scalars(select(JobSnapshot.title).where(*_base_predicates()).distinct())
    cache.titles = {}
    for title in titles:
        if title not in cache.title_decisions:
            cache.title_decisions[title] = (
                job_category({"title": title})
                if screen_title_job({"title": title}, profile).eligible else None
            )
        if cache.title_decisions[title] is not None:
            cache.titles[title] = cache.title_decisions[title]
    cache.catalog = {
        str(row["name"]).strip().casefold(): dict(row)
        for row in session.execute(select(
            CompanySnapshot.id, CompanySnapshot.name, CompanySnapshot.organization_id,
            CompanySnapshot.recruitment_unit_name, CompanySnapshot.campus_url,
        )).mappings() if row["name"]
    }
    # SQLite lower() handles ASCII only; PostgreSQL lower() also differs from
    # Python casefold for e.g. ß. Small exceptional-value maps preserve the old
    # Unicode matching/sorting semantics without replacing the title policy.
    values = set(cache.titles)
    for row in cache.catalog.values():
        values.update(str(value) for value in (row["name"], row["organization_id"]) if value)
    for row in session.execute(select(
        JobSnapshot.company_id, JobSnapshot.city, JobSnapshot.organization_id,
    ).where(*_base_predicates()).distinct()):
        values.update(str(value) for value in row if value)
    values.update(value.strip() for value in tuple(values))
    cache.fold_overrides = {
        value: value.casefold() for value in values
        if value.casefold() != re.sub(r"[A-Z]", lambda match: match.group().lower(), value)
    }
    cache.signature = signature
    cache.created = monotonic()
    cache.summaries = {}


def _full_statement():
    return (
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


def _effective_score():
    status = JobAnalysisSnapshot.analysis_status
    return case(
        (and_(status.is_not(None), status != "", status != "complete"), None),
        else_=func.coalesce(JobSnapshot.match_score, JobAnalysisSnapshot.match_score),
    )


def _fold(expression, cache):
    return (case(cache.fold_overrides, value=expression, else_=func.lower(expression))
            if cache.fold_overrides else func.lower(expression))


def _effective_status():
    return case(
        (JobAnalysisSnapshot.analysis_status.in_(_LEGACY_EXCLUSIONS),
         case((JobSnapshot.capture_status == "complete", "eligible"), else_="jd_incomplete")),
        else_=JobAnalysisSnapshot.analysis_status,
    )


def _platform():
    source = func.lower(func.coalesce(JobSnapshot.source_platform, ""))
    url = func.lower(JobSnapshot.detail_url)
    return case(
        (or_(source.contains("moka"), url.contains("mokahr.com")), "Moka"),
        (or_(source.contains("beisen"), url.contains("zhiye.com")), "北森"),
        (or_(source.contains("feishu"), source.contains("lark"), url.contains("feishu.cn"), url.contains("mioffice")), "飞书"),
        (or_(source.contains("hotjob"), url.contains("hotjob.cn")), "Hotjob"),
        (source.contains("moseeker"), "Moseeker"),
        (source.contains("ourats"), "OurATS"),
        else_="自建",
    )


def _organization(row, catalog):
    name = str(row["company_name"] or row["company_id"]).strip().casefold()
    canonical = catalog.get(name) or {}
    return row["company_organization_id"] or canonical.get("organization_id") or row["job_organization_id"]


def _row_item(row, cache, stages, *, compact=False):
    from .postgres import _json_list, _platform_label
    organization = _organization(row, cache.catalog)
    status = row["analysis_status"]
    score = row["job_match_score"] if row["job_match_score"] is not None else row["analysis_match_score"]
    if status and status != "complete":
        score = None
    if status in _LEGACY_EXCLUSIONS:
        status = "eligible" if row["capture_status"] == "complete" else "jd_incomplete"
    category = cache.titles[row["title"]]
    values = dict(
        id=str(row["id"]), company_id=str(row["company_id"]),
        company_name=str(row["company_name"] or row["company_id"]),
        organization_id=str(organization) if organization else None,
        title=str(row["title"]), city=row["city"], detail_url=str(row["detail_url"]),
        category=category, category_label=JOB_CATEGORY_LABELS[category],
        platform=_platform_label(row["source_platform"], str(row["detail_url"])),
        batch=row["batch"], capture_status=row["capture_status"] or "unknown",
        capture_failure_reason=row.get("capture_failure_reason") or "",
        availability_status=row["availability_status"] or "active",
        match_score=score, analysis_status=status, first_seen_at=row["first_seen_at"],
        application_stage=stages.get(str(row["id"])),
    )
    if compact:
        return SimpleNamespace(**values)
    return JobBrowseItem(**values, recommendation=row["recommendation"], summary=row["summary"],
        advantages=[str(value) for value in _json_list(row["advantages"])],
        gaps=[str(value) for value in _json_list(row["gaps"])],
        matched_directions=[str(value) for value in row["matched_directions"] or []],
        primary_match_direction=row["primary_match_direction"])


def _summarize(items, company_metadata):
    from .postgres import _is_pending_score, _timestamp
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


    return stats, facets, [item.id for item in featured]


def _summary(session, base, cache, first_seen_on):
    if first_seen_on not in cache.summaries:
        excluded = {"advantages", "gaps", "summary", "recommendation",
                    "matched_directions", "primary_match_direction", "capture_failure_reason"}
        columns = [column for column in base.selected_columns if column.key not in excluded]
        items, metadata = [], {}
        for row in session.execute(base.with_only_columns(*columns)).mappings():
            item = _row_item(row, cache, {}, compact=True)
            items.append(item)
            canonical = cache.catalog.get(item.company_name.strip().casefold()) or {}
            metadata[item.company_id] = {
                "name": item.company_name, "organization_id": item.organization_id,
                "recruitment_unit_name": row["recruitment_unit_name"] or canonical.get("recruitment_unit_name"),
                "campus_url": row["campus_url"] or canonical.get("campus_url"),
            }
        # Only a few date scopes are useful; cap memory even for arbitrary API dates.
        if len(cache.summaries) >= 8:
            cache.summaries.pop(next(iter(cache.summaries)))
        cache.summaries[first_seen_on] = _summarize(items, metadata)
    return cache.summaries[first_seen_on]


def _application_stages(session, identifiers):
    if not identifiers:
        return {}
    stages = {}
    for job_id, stage in session.execute(select(
        ApplicationSnapshot.job_id, ApplicationSnapshot.stage,
    ).where(ApplicationSnapshot.job_id.in_(identifiers)).order_by(
        ApplicationSnapshot.updated_at.desc(), ApplicationSnapshot.id,
    )):
        stages.setdefault(str(job_id), str(stage))
    return stages


def _title_membership(titles, dialect):
    # One bound array/JSON value avoids thousands of placeholders on every page
    # and remains below driver parameter limits for large real-world catalogs.
    values = tuple(titles)
    if dialect == "postgresql":
        return JobSnapshot.title == any_(bindparam(None, list(values), type_=ARRAY(Text)))
    if dialect == "sqlite":
        value_table = func.json_each(json.dumps(values, ensure_ascii=False)).table_valued("value")
        return JobSnapshot.title.in_(select(value_table.c.value))
    return JobSnapshot.title.in_(values)


def browse_jobs(repository, *, query=None, company=None, category=None, platform=None,
                evaluation=None, score_band=None, first_seen_on=None, sort="score",
                limit=50, offset=0, include_summary=True):
    if limit < 1 or offset < 0:
        raise ValueError("limit must be positive and offset cannot be negative")
    cache = _cache_for(repository.storage.engine)
    with cache.lock, repository.storage.session() as session:
        _ensure_index(cache, session)
        dialect = session.bind.dialect.name
        base = _full_statement().where(_title_membership(cache.titles, dialect))
        if first_seen_on is not None:
            base = base.where(func.date(JobSnapshot.first_seen_at) == first_seen_on)
        score, status = _effective_score(), _effective_status()
        company_name = func.coalesce(func.nullif(CompanySnapshot.name, ""), JobSnapshot.company_id)
        predicates = []
        normalized_query = (query or "").strip().casefold()
        if normalized_query:
            # autoescape preserves literal %, _ and the escape character itself.
            haystack = (_fold(company_name, cache) + " " + _fold(JobSnapshot.title, cache)
                        + " " + _fold(func.coalesce(JobSnapshot.city, ""), cache))
            predicates.append(haystack.contains(normalized_query, autoescape=True))
        normalized_company = (company or "").strip().casefold()
        if normalized_company:
            canonical_orgs = {name: row["organization_id"] for name, row in cache.catalog.items() if row["organization_id"]}
            organization = func.coalesce(
                func.nullif(CompanySnapshot.organization_id, ""),
                case(canonical_orgs, value=_fold(func.trim(company_name), cache)) if canonical_orgs else None,
                JobSnapshot.organization_id,
            )
            predicates.append(or_(_fold(JobSnapshot.company_id, cache) == normalized_company,
                                  _fold(company_name, cache) == normalized_company,
                                  _fold(organization, cache) == normalized_company))
        if category:
            predicates.append(_title_membership(
                (title for title, key in cache.titles.items() if key == category), dialect,
            ))
        if platform and platform.strip():
            predicates.append(func.lower(_platform()) == platform.strip().casefold())
        if evaluation == "scored":
            predicates.append(score.is_not(None))
        elif evaluation == "unscored":
            predicates.append(score.is_(None))
        elif evaluation == "pending":
            predicates.append(and_(status == "eligible", score.is_(None), JobSnapshot.capture_status == "complete"))
        elif evaluation == "jd_incomplete":
            predicates.append(status == "jd_incomplete")
        elif evaluation == "excluded":
            predicates.append(status.in_(_LEGACY_EXCLUSIONS))
        if score_band:
            predicates.append(score.is_not(None))
            if score_band == "high":
                predicates.append(score >= 70)
            elif score_band == "medium":
                predicates.append(and_(score >= 60, score < 70))
            elif score_band == "low":
                predicates.append(score < 60)
        filtered = base.where(*predicates)
        total = int(session.scalar(select(func.count()).select_from(
            filtered.with_only_columns(JobSnapshot.id).order_by(None).subquery())) or 0)
        # Explicit ID tie-break keeps adjacent pages stable even when every
        # displayed sort key is identical.
        name_sort, title_sort = _fold(company_name, cache), _fold(JobSnapshot.title, cache)
        if session.bind.dialect.name == "postgresql":
            name_sort, title_sort = name_sort.collate("C"), title_sort.collate("C")
        newest, highest = JobSnapshot.first_seen_at.desc().nullslast(), score.desc().nullslast()
        order = ((newest, highest, name_sort, title_sort) if sort == "newest" else
                 (name_sort, title_sort, newest) if sort == "company" else
                 (highest, newest, name_sort, title_sort))
        rows = list(session.execute(filtered.order_by(*order, JobSnapshot.id).limit(limit).offset(offset)).mappings())
        stats = facets = None
        featured_rows = []
        if include_summary:
            cached_stats, cached_facets, featured_ids = _summary(session, base, cache, first_seen_on)
            stats, facets = cached_stats.model_copy(deep=True), cached_facets.model_copy(deep=True)
            if featured_ids:
                indexed = {row["id"]: row for row in session.execute(
                    base.where(JobSnapshot.id.in_(featured_ids))).mappings()}
                featured_rows = [indexed[key] for key in featured_ids if key in indexed]
        stages = _application_stages(session, {row["id"] for row in [*rows, *featured_rows]})
        return JobBrowsePage(
            items=[_row_item(row, cache, stages) for row in rows],
            featured=[_row_item(row, cache, stages) for row in featured_rows],
            total=total, limit=limit, offset=offset, stats=stats, facets=facets,
            summary_included=include_summary,
        )
