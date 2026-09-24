from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, update

from packages.repositories import job_browse
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import Storage
from packages.storage.models import ApplicationSnapshot, CompanySnapshot, JobAnalysisSnapshot, JobSnapshot


@pytest.fixture
def repository(tmp_path, monkeypatch):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'synthetic.db'}", initialize=True)
    monkeypatch.setattr("packages.config.get_settings", lambda: SimpleNamespace(candidate_profile_config=tmp_path / "profile.yaml"))
    with storage.engine.begin() as connection:
        connection.execute(CompanySnapshot.__table__.insert(), [
            {"id": "a", "name": "Alpha", "organization_id": "org-a", "integration_status": "connected", "source": "fixture"},
            {"id": "b", "name": "Beta", "organization_id": "org-b", "integration_status": "connected", "source": "fixture"},
        ])
    yield PostgresRecruitmentRepository(storage)
    storage.engine.dispose()


def _jobs(repository, titles):
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with repository.storage.engine.begin() as connection:
        connection.execute(JobSnapshot.__table__.insert(), [
            dict(id=f"job-{i:04}", company_id="a" if i % 2 == 0 else "b", title=title,
                 city="上海", detail_url=f"https://example.test/jobs/{i}",
                 cohort=2027, cohort_status="confirmed", batch="formal",
                 capture_status="complete", match_score=i % 101,
                 first_seen_at=now + timedelta(seconds=i), source="fixture", source_ref=f"job:{i}")
            for i, title in enumerate(titles)
        ])


def test_title_policy_applies_before_sql_page_and_total(repository):
    titles = ["销售专员"] * 220 + ["C++ 软件工程师", "AI 工程师", "AI实习生", "AI博士", "AI工程师（博士优先）", "Sailing工程师"]
    _jobs(repository, titles)
    page = repository.browse_jobs(limit=1, include_summary=False)
    assert page.total == 3
    assert len(page.items) == 1
    assert page.stats is page.facets is None
    assert not page.summary_included
    assert page.featured == []
    pages = [repository.browse_jobs(limit=1, offset=i, include_summary=False).items[0].id for i in range(3)]
    assert len(set(pages)) == 3


@pytest.mark.parametrize("sort", ["score", "newest", "company"])
def test_sql_page_order_and_totals_match_previous_python_sort(repository, sort):
    _jobs(repository, ["C++ 软件工程师"] * 125)
    all_items = repository.browse_jobs(limit=500, include_summary=False).items
    if sort == "score":
        key = lambda item: (-(item.match_score if item.match_score is not None else -1), -item.first_seen_at.timestamp(), item.company_name.casefold(), item.title.casefold())
    elif sort == "newest":
        key = lambda item: (-item.first_seen_at.timestamp(), -(item.match_score if item.match_score is not None else -1), item.company_name.casefold(), item.title.casefold())
    else:
        key = lambda item: (item.company_name.casefold(), item.title.casefold(), -item.first_seen_at.timestamp())
    expected = [item.id for item in sorted(all_items, key=key)]
    actual = []
    for offset in range(0, 125, 13):
        page = repository.browse_jobs(sort=sort, limit=13, offset=offset, include_summary=False)
        assert page.total == 125
        actual.extend(item.id for item in page.items)
    assert actual == expected


def test_literal_search_unicode_and_zero_score(repository):
    _jobs(repository, ["C++ 100%_工程师", "C++ Straße 工程师", "C++ École 工程师", "C++ 100ab工程师"])
    assert [item.id for item in repository.browse_jobs(query="  100%_  ", include_summary=False).items] == ["job-0000"]
    assert repository.browse_jobs(query="STRASSE", include_summary=False).total == 1
    assert repository.browse_jobs(query="éCOLE", include_summary=False).total == 1
    zero = repository.browse_jobs(query="100%_", evaluation="scored", score_band="low").items[0]
    assert zero.match_score == 0


def test_warm_page_does_not_rebuild_summary_or_load_all_analysis_text(repository, monkeypatch):
    _jobs(repository, ["C++ 软件工程师"] * 250)
    repository.browse_jobs()
    monkeypatch.setattr(job_browse, "_summarize", lambda *_: pytest.fail("warm page rebuilt summary"))
    monkeypatch.setattr("packages.matching.title_policy.screen_title_job", lambda *_: pytest.fail("unchanged titles rescreened"))
    statements = []
    def record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)
    event.listen(repository.storage.engine, "before_cursor_execute", record_sql)
    try:
        page = repository.browse_jobs(limit=20, offset=200, include_summary=False)
    finally:
        event.remove(repository.storage.engine, "before_cursor_execute", record_sql)
    assert len(page.items) == 20
    analyses = [sql for sql in statements if "job_analysis_snapshots.advantages" in sql]
    assert len(analyses) == 1
    assert "LIMIT" in analyses[0]
    assert not any("SELECT DISTINCT job_snapshots.title" in sql for sql in statements)


def test_scores_jobs_company_and_profile_changes_invalidate_summary(repository, tmp_path):
    _jobs(repository, ["C++ 软件工程师", "AI 工程师"])
    assert repository.browse_jobs().stats.high_match == 0
    with repository.storage.write_transaction() as session:
        session.get(JobSnapshot, "job-0000").match_score = 98
    assert repository.browse_jobs().stats.high_match == 1
    with repository.storage.write_transaction() as session:
        session.get(CompanySnapshot, "a").name = "Renamed"
    assert any(company.name == "Renamed" for company in repository.browse_jobs().facets.companies)
    (tmp_path / "profile.yaml").write_text("profile:\n  matching:\n    title_keywords: [AI]\n", encoding="utf-8")
    assert repository.browse_jobs().total == 1
    (tmp_path / "profile.yaml").write_text("profile:\n  matching:\n    title_keywords: [C++]\n", encoding="utf-8")
    assert repository.browse_jobs().items[0].id == "job-0000"
    with repository.storage.write_transaction() as session:
        session.delete(session.get(JobSnapshot, "job-0000"))
    assert repository.browse_jobs().total == 0


def test_cache_ttl_covers_external_updates_with_preserved_timestamps(repository, monkeypatch):
    _jobs(repository, ["C++ 工程师"])
    assert repository.browse_jobs().stats.high_match == 0
    with repository.storage.engine.begin() as connection:
        connection.execute(update(JobSnapshot).values(match_score=90, updated_at=JobSnapshot.updated_at))
    cache = job_browse._cache_for(repository.storage.engine)
    monkeypatch.setattr(job_browse, "monotonic", lambda: cache.created + 16)
    assert repository.browse_jobs().stats.high_match == 1


def test_failed_analysis_invalidates_effective_score_without_stale_summary(repository):
    _jobs(repository, ["C++ 工程师"])
    with repository.storage.write_transaction() as session:
        session.get(JobSnapshot, "job-0000").match_score = 95
    assert repository.browse_jobs().stats.high_match == 1
    with repository.storage.write_transaction() as session:
        session.add(JobAnalysisSnapshot(job_id="job-0000", analysis_status="failed", source="fixture"))
    page = repository.browse_jobs()
    assert page.stats.high_match == 0
    assert page.stats.unscored == 1
    assert page.items[0].match_score is None


def test_concurrent_filters_do_not_share_mutable_results(repository):
    _jobs(repository, ["C++ 软件工程师"] * 100)
    def fetch(company):
        page = repository.browse_jobs(company=company, limit=8, offset=8)
        page.facets.companies.clear()
        return page.total, {item.company_id for item in page.items}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, ["a", "b"] * 5))
    assert results == [(50, {company}) for company in ["a", "b"] * 5]
    assert len(repository.browse_jobs().facets.companies) == 2


def _applications(repository):
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with repository.storage.engine.begin() as connection:
        connection.execute(ApplicationSnapshot.__table__.insert(), [
            dict(id=f"app-{i:04}", company_name=("RemoteCompany" if i == 230 else "Alpha" if i % 2 else "Beta"),
                 job_title=("100%_ Role" if i == 231 else "C++ Engineer"),
                 stage="applied" if i % 2 else "offer", updated_at=now - timedelta(seconds=i),
                 idempotency_key=f"fixture:{i}", source="fixture", source_ref=f"app:{i}")
            for i in range(240)
        ])


def test_application_search_finds_records_after_200_and_counts_entire_scope(repository):
    _applications(repository)
    page = repository.search_applications(query=" remoteCOMPANY ", limit=20)
    assert [item.id for item in page.items] == ["app-0230"]
    assert page.total == 1 and page.unfiltered_total == 240
    assert page.stage_counts == {"offer": 1}
    assert repository.search_applications(query="C++ Engineer").total == 239
    assert repository.search_applications(query="  ", stage="offer").total == 120
    assert repository.search_applications(query="%_").items[0].id == "app-0231"
    assert repository.search_applications(query="missing").total == 0


def test_application_stage_groups_and_stable_pagination(repository):
    _applications(repository)
    page = repository.search_applications(query="Engineer", stages=("offer",), limit=7, offset=7)
    assert page.total == 120
    assert page.stage_counts == {"offer": 120, "applied": 119}
    assert all(item.stage == "offer" for item in page.items)
    ids = [item.id for offset in range(0, 240, 31) for item in repository.search_applications(limit=31, offset=offset).items]
    assert len(ids) == len(set(ids)) == 240
    assert repository.search_applications(stages=()).total == 0
    with pytest.raises(ValueError, match="cannot be combined"):
        repository.search_applications(stage="offer", stages=("applied",))
    with pytest.raises(ValueError, match="unknown"):
        repository.search_applications(stage="not-a-stage")


def test_application_search_does_not_expand_sharp_s_against_sql_lower(repository):
    _applications(repository)
    with repository.storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "app-0001").company_name = "Straße"
    assert repository.search_applications(query=" STRAßE ").items[0].id == "app-0001"
