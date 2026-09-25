from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import Lock
from time import sleep
from typing import Any, Mapping

import yaml
from sqlalchemy import select

from packages.discovery.company_registry import CompanySourceRecord, CompanySourceRegistry
from packages.pipeline import CrawlResult, run_daily_pipeline
from packages.storage import CompanySnapshot, JobAnalysisSnapshot, JobSnapshot, Storage


UTC = timezone.utc
LIST_URL = "https://jobs.example.test/campus"


def _storage(tmp_path: Path) -> Storage:
    return Storage.from_url(
        f"sqlite:///{(tmp_path / 'agent.sqlite').as_posix()}",
        initialize=True,
    )


def _config(path: Path, *rows: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump({"companies": list(rows)}, allow_unicode=True), encoding="utf-8")
    return path


def _job(job_id: str, title: str, *, detail_url: str | None = None) -> dict[str, Any]:
    return {
        "id": job_id,
        "title": title,
        "city": "Shanghai",
        "detail_url": detail_url or f"{LIST_URL}/position/{job_id}",
        "cohort": 2027,
        "cohort_status": "confirmed",
        "batch": "formal",
    }


def _crawl(*jobs: dict[str, Any], complete: bool = True, scope_key: str = "scope-a") -> CrawlResult:
    return CrawlResult(
        jobs=list(jobs),
        source_url=LIST_URL,
        allowed_origins=("https://jobs.example.test",),
        pages_seen=1,
        total_pages=1,
        has_more=not complete,
        pagination_complete=complete,
        completeness_known=True,
        scope_key=scope_key,
    )


def _detail(job: Mapping[str, Any]) -> dict[str, Any]:
    text = f"Official detail for {job['title']}"
    url = str(job["detail_url"])
    return {
        "status": "complete",
        "detail": text,
        "detail_url": url,
        "source": "fixture",
        "capture_evidence": {
            "status": "complete",
            "method": "fixture",
            "source_url": url,
            "identity_verified": True,
            "terminal_observed": True,
            "remaining_controls": [],
            "content_sha256": sha256(text.encode("utf-8")).hexdigest(),
        },
    }


class FakeHydrator:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.failures = failures or set()

    def __call__(self, job: Mapping[str, Any]) -> dict[str, Any]:
        job_id = str(job["id"])
        self.calls.append(job_id)
        if job_id in self.failures:
            return {"status": "failed", "error_code": "detail_404", "detail_url": job["detail_url"]}
        return _detail(job)


class FakeMatcher:
    def __init__(self, failures: int = 0) -> None:
        self.calls: list[str] = []
        self.failures = failures

    def match(self, job: Mapping[str, Any], *, existing_analysis: Any = None) -> dict[str, Any]:
        assert existing_analysis is None
        self.calls.append(str(job["id"]))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("fixture matcher failure")
        return {
            "analysis_status": "complete",
            "match_score": 87,
            "summary": "fixture score",
            "recommendation": "recommend",
        }


def _company(company_id: str, name: str | None = None, *, status: str = "connected") -> dict[str, Any]:
    return {
        "id": company_id,
        "name": name or company_id,
        "careers_url": f"{LIST_URL}/{company_id}",
        "crawler": "fixture",
        "integration_status": status,
    }


def test_title_first_matches_existing_by_company_and_title_only(tmp_path: Path) -> None:
    config = _config(
        tmp_path / "companies.yaml",
        _company("company-a", "Company A"),
        _company("company-b", "Company B"),
    )
    state = {
        "company-a": [
            _job("a-1", "C++ Software Engineer"),
            _job("a-2", "C++ Software Engineer", detail_url=f"{LIST_URL}/position/a-2"),
            _job("a-filtered", "Product Manager"),
            _job("a-intern", "C++ Software Intern"),
        ],
        "company-b": [_job("b-1", "C++ Software Engineer")],
    }
    crawler = lambda company: _crawl(*state[company.id])
    hydrator = FakeHydrator()
    matcher = FakeMatcher()
    now = datetime(2026, 9, 9, tzinfo=UTC)
    storage = _storage(tmp_path)

    first = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        jd_hydrator=hydrator,
        matcher=matcher,
        clock=lambda: now,
    )

    assert first.new_count == 2
    assert first.reused_count == 0
    assert len(hydrator.calls) == 2
    assert len(matcher.calls) == 2
    with storage.session() as session:
        rows = list(session.scalars(select(JobSnapshot).order_by(JobSnapshot.company_id)))
        analyses = list(session.scalars(select(JobAnalysisSnapshot)))
    assert [(row.company_id, row.title, row.capture_status, row.title_key) for row in rows] == [
        ("company-a", "C++ Software Engineer", "complete", "C++ Software Engineer"),
        ("company-b", "C++ Software Engineer", "complete", "C++ Software Engineer"),
    ]
    assert len(analyses) == 2

    original_ids = {row.company_id: row.id for row in rows}
    now += timedelta(minutes=5)
    state["company-a"] = [_job("a-new-id", "  C++   Software Engineer  ", detail_url=f"{LIST_URL}/position/new")]
    state["company-b"] = [_job("b-new-id", "C++ Software Engineer", detail_url=f"{LIST_URL}/position/b-new")]

    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        jd_hydrator=hydrator,
        matcher=matcher,
        clock=lambda: now,
    )

    assert second.new_count == 0
    assert second.reused_count == 2
    assert len(hydrator.calls) == 2
    assert len(matcher.calls) == 2
    with storage.session() as session:
        rows_after = list(session.scalars(select(JobSnapshot).order_by(JobSnapshot.company_id)))
        analyses_after = list(session.scalars(select(JobAnalysisSnapshot)))
    assert {row.company_id: row.id for row in rows_after} == original_ids
    assert all(row.match_score == 87 for row in rows_after)
    assert all(row.capture_status == "complete" for row in rows_after)
    assert len(analyses_after) == 2


def test_title_first_persists_detail_failure_and_keeps_company_partial(tmp_path: Path) -> None:
    config = _config(tmp_path / "companies.yaml", _company("company-a", "Company A"))
    state = {"jobs": [_job("a-1", "C++ Software Engineer")]}
    crawler = lambda _company: _crawl(*state["jobs"])
    hydrator = FakeHydrator({"a-1"})
    matcher = FakeMatcher()
    storage = _storage(tmp_path)
    now = datetime(2026, 9, 9, tzinfo=UTC)

    first = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        jd_hydrator=hydrator,
        matcher=matcher,
        clock=lambda: now,
    )

    assert first.new_count == 1
    assert first.failed_job_count == 1
    assert first.company_results[0].status == "partial"
    assert matcher.calls == []
    with storage.session() as session:
        row = session.scalar(select(JobSnapshot))
        assert row is not None
        assert row.capture_status == "failed"
        assert row.capture_failure_reason == "detail_404"
        assert row.jd_raw is None
        assert row.availability_status == "active"
        original_created_at = row.created_at
        original_first_seen_at = row.first_seen_at
        source_rows = list(session.scalars(select(CompanySourceRecord)))
    assert len(source_rows) == 1
    assert source_rows[0].status == "partial"

    now += timedelta(minutes=5)
    state["jobs"] = [_job("a-2", "C++ Software Engineer", detail_url=f"{LIST_URL}/position/a-2")]
    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        jd_hydrator=hydrator,
        matcher=matcher,
        clock=lambda: now,
    )

    assert second.new_count == 0
    assert second.reused_count == 1
    assert len(hydrator.calls) == 2
    assert matcher.calls == []
    assert second.company_results[0].status == "partial"
    with storage.session() as session:
        row = session.scalar(select(JobSnapshot))
        assert row is not None
        assert row.id == "a-1"
        assert row.capture_status == "failed"
        assert row.jd_raw is None
        assert row.updated_at.replace(tzinfo=UTC) == now
        assert row.created_at == original_created_at
        assert row.first_seen_at == original_first_seen_at
        source_rows = list(session.scalars(select(CompanySourceRecord)))
    assert source_rows[0].status == "partial"

    hydrator.failures.clear()
    third = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        jd_hydrator=hydrator,
        matcher=matcher,
        clock=lambda: now,
    )

    assert third.new_count == 0
    assert third.reused_count == 1
    assert len(hydrator.calls) == 3
    assert matcher.calls == ["a-1"]
    assert third.company_results[0].status == "complete"
    with storage.session() as session:
        row = session.scalar(select(JobSnapshot))
        assert row is not None
        assert row.id == "a-1"
        assert row.capture_status == "complete"
        assert row.jd_raw == "Official detail for C++ Software Engineer"
        assert row.capture_evidence["status"] == "complete"
        assert row.updated_at.replace(tzinfo=UTC) == now
        assert row.created_at == original_created_at
        assert row.first_seen_at == original_first_seen_at
        analysis = session.scalar(select(JobAnalysisSnapshot))
        assert analysis is not None
        assert analysis.match_score == 87
        source_rows = list(session.scalars(select(CompanySourceRecord)))
    assert source_rows[0].status == "complete"

    fourth = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        jd_hydrator=lambda _job: (_ for _ in ()).throw(
            AssertionError("a successful repaired detail must not be fetched again")
        ),
        matcher=matcher,
        clock=lambda: now + timedelta(minutes=5),
    )
    assert fourth.new_count == 0
    assert fourth.reused_count == 1
    assert len(hydrator.calls) == 3
    assert matcher.calls == ["a-1"]


def test_failed_placeholder_not_in_current_list_is_not_retried(tmp_path: Path) -> None:
    config = _config(tmp_path / "companies.yaml", _company("company-a", "Company A"))
    state = {"jobs": [_job("a-1", "C++ Software Engineer")]}
    crawler = lambda _company: _crawl(*state["jobs"])
    hydrator = FakeHydrator({"a-1"})
    storage = _storage(tmp_path)

    first = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        jd_hydrator=hydrator,
        matcher=FakeMatcher(),
    )
    assert first.new_count == 1

    state["jobs"] = []
    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        jd_hydrator=lambda _job: (_ for _ in ()).throw(
            AssertionError("an absent failed placeholder must not be fetched")
        ),
        matcher=FakeMatcher(),
    )

    assert second.new_count == 0
    assert second.reused_count == 0
    assert hydrator.calls == ["a-1"]
    assert second.company_results[0].status == "partial"
    with storage.session() as session:
        row = session.get(JobSnapshot, "a-1")
        assert row is not None
        assert row.capture_status == "failed"
        source_rows = list(session.scalars(select(CompanySourceRecord)))
    assert source_rows[0].status == "partial"


def test_legacy_body_without_receipt_is_not_retried(tmp_path: Path) -> None:
    config = _config(tmp_path / "companies.yaml", _company("company-a", "Company A"))
    state = {"jobs": [_job("a-1", "C++ Software Engineer")]}
    storage = _storage(tmp_path)
    first_matcher = FakeMatcher()
    hydrator = FakeHydrator()

    first = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(*state["jobs"]),
        jd_hydrator=hydrator,
        matcher=first_matcher,
    )
    assert first.new_count == 1
    with storage.session() as session:
        row = session.get(JobSnapshot, "a-1")
        assert row is not None
        old_detail = row.jd_raw
        old_score = row.match_score
        row.capture_evidence = {}
        row.capture_status = "unknown"
        row.capture_failure_reason = ""
        session.commit()

    second_matcher = FakeMatcher()
    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job("a-new-id", "C++ Software Engineer")
        ),
        jd_hydrator=lambda _job: (_ for _ in ()).throw(
            AssertionError("legacy body without a receipt must remain reusable")
        ),
        matcher=second_matcher,
    )

    assert second.new_count == 0
    assert second.reused_count == 1
    assert second_matcher.calls == []
    with storage.session() as session:
        row = session.get(JobSnapshot, "a-1")
        assert row is not None
        assert row.jd_raw == old_detail
        assert row.match_score == old_score
        assert row.capture_evidence == {}
        assert row.capture_status == "unknown"


def test_same_title_prefers_successful_duplicate_without_detail_request(tmp_path: Path) -> None:
    config = _config(tmp_path / "companies.yaml", _company("company-a", "Company A"))
    storage = _storage(tmp_path)
    now = datetime(2026, 9, 9, tzinfo=UTC)
    hydrator = FakeHydrator()
    matcher = FakeMatcher()

    first = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(_job("a-success", "C++ Software Engineer")),
        jd_hydrator=hydrator,
        matcher=matcher,
        clock=lambda: now,
    )
    assert first.new_count == 1

    with storage.write_transaction() as session:
        session.add(
            JobSnapshot(
                id="a-failed",
                company_id="company-a",
                title="C++ Software Engineer",
                city="Shanghai",
                detail_url=f"{LIST_URL}/position/a-failed",
                jd_raw=None,
                cohort=2027,
                cohort_status="confirmed",
                batch="formal",
                match_score=None,
                first_seen_at=now,
                last_seen_at=now,
                organization_id="company-a",
                recruitment_unit_id="company-a",
                source_platform="fixture",
                native_job_id="native-failed",
                capture_status="failed",
                capture_failure_reason="detail_404",
                availability_status="active",
                title_key="C++ Software Engineer",
                capture_evidence={},
                created_at=now,
                updated_at=now,
                source="recruitops-agent.daily_pipeline",
                source_ref="duplicate:a-failed",
            )
        )

    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job("a-list-id", "C++ Software Engineer")
        ),
        jd_hydrator=lambda _job: (_ for _ in ()).throw(
            AssertionError("a successful same-title representative avoids retry")
        ),
        matcher=FakeMatcher(),
        clock=lambda: now,
    )

    assert second.new_count == 0
    assert second.reused_count == 1
    with storage.session() as session:
        success = session.get(JobSnapshot, "a-success")
        failed = session.get(JobSnapshot, "a-failed")
        assert success is not None
        assert failed is not None
        assert success.capture_status == "complete"
        assert failed.capture_status == "failed"
        assert session.scalar(select(JobAnalysisSnapshot).where(JobAnalysisSnapshot.job_id == "a-success")) is not None
    assert second.company_results[0].status == "complete"


def test_retry_failure_preserves_old_detail_and_analysis(tmp_path: Path) -> None:
    config = _config(tmp_path / "companies.yaml", _company("company-a", "Company A"))
    storage = _storage(tmp_path)
    now = datetime(2026, 9, 9, tzinfo=UTC)
    first_hydrator = FakeHydrator()
    first_matcher = FakeMatcher()

    first = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(_job("a-old", "C++ Software Engineer")),
        jd_hydrator=first_hydrator,
        matcher=first_matcher,
        clock=lambda: now,
    )
    assert first.new_count == 1
    with storage.session() as session:
        row = session.get(JobSnapshot, "a-old")
        analysis = session.get(JobAnalysisSnapshot, "a-old")
        assert row is not None
        assert analysis is not None
        old_detail = row.jd_raw
        old_evidence = dict(row.capture_evidence)
        old_analysis = {
            "match_score": analysis.match_score,
            "summary": analysis.summary,
            "recommendation": analysis.recommendation,
            "analysis_status": analysis.analysis_status,
        }
        row.capture_status = "failed"
        row.capture_failure_reason = "stale_capture"
        row.capture_evidence = {**old_evidence, "status": "failed"}
        session.commit()

    retry_hydrator = FakeHydrator({"a-old"})
    retry_matcher = FakeMatcher()
    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job("a-new-id", "C++ Software Engineer")
        ),
        jd_hydrator=retry_hydrator,
        matcher=retry_matcher,
        clock=lambda: now + timedelta(minutes=5),
    )

    assert second.new_count == 0
    assert second.reused_count == 1
    assert retry_hydrator.calls == ["a-old"]
    assert retry_matcher.calls == []
    with storage.session() as session:
        row = session.get(JobSnapshot, "a-old")
        analysis = session.get(JobAnalysisSnapshot, "a-old")
        assert row is not None
        assert analysis is not None
        assert row.jd_raw == old_detail
        assert row.capture_evidence == {**old_evidence, "status": "failed"}
        assert row.capture_status == "failed"
        assert row.capture_failure_reason == "detail_404"
        assert row.match_score == old_analysis["match_score"]
        assert {
            "match_score": analysis.match_score,
            "summary": analysis.summary,
            "recommendation": analysis.recommendation,
            "analysis_status": analysis.analysis_status,
        } == old_analysis


def test_retry_with_valid_job_score_does_not_create_analysis_marker(tmp_path: Path) -> None:
    config = _config(tmp_path / "companies.yaml", _company("company-a", "Company A"))
    storage = _storage(tmp_path)
    now = datetime(2026, 9, 9, tzinfo=UTC)

    first = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(_job("a-old", "C++ Software Engineer")),
        jd_hydrator=FakeHydrator(),
        matcher=FakeMatcher(),
        clock=lambda: now,
    )
    assert first.new_count == 1
    with storage.session() as session:
        row = session.get(JobSnapshot, "a-old")
        analysis = session.get(JobAnalysisSnapshot, "a-old")
        assert row is not None
        assert analysis is not None
        row.match_score = 91
        row.organization_id = "organization-kept"
        row.recruitment_unit_id = "unit-kept"
        row.native_job_id = "native-kept"
        row.jd_raw = None
        row.capture_evidence = {}
        row.capture_status = "failed"
        row.capture_failure_reason = "detail_missing"
        session.delete(analysis)
        session.commit()

    retry_matcher = FakeMatcher()
    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job("a-new-id", "C++ Software Engineer", detail_url=f"{LIST_URL}/position/new")
        ),
        jd_hydrator=FakeHydrator(),
        matcher=retry_matcher,
        clock=lambda: now + timedelta(minutes=5),
    )

    assert second.new_count == 0
    assert second.reused_count == 1
    assert retry_matcher.calls == []
    with storage.session() as session:
        row = session.get(JobSnapshot, "a-old")
        analysis = session.get(JobAnalysisSnapshot, "a-old")
        assert row is not None
        assert row.jd_raw == "Official detail for C++ Software Engineer"
        assert row.capture_status == "complete"
        assert row.match_score == 91
        assert row.organization_id == "organization-kept"
        assert row.recruitment_unit_id == "unit-kept"
        assert row.native_job_id == "native-kept"
        assert analysis is None


def test_pending_score_marker_resumes_without_refetching_or_queueing_history(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "companies.yaml", _company("company-a", "Company A"))
    state = {"jobs": [_job("a-1", "C++ Software Engineer")]}
    crawler = lambda _company: _crawl(*state["jobs"])
    hydrator = FakeHydrator()
    matcher = FakeMatcher(failures=1)
    storage = _storage(tmp_path)

    first = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        jd_hydrator=hydrator,
        matcher=matcher,
    )

    assert first.new_count == 1
    assert first.failed_job_count == 1
    with storage.session() as session:
        row = session.scalar(select(JobSnapshot))
        analysis = session.scalar(select(JobAnalysisSnapshot))
        assert row is not None
        assert analysis is not None
        assert analysis.analysis_status == "pending"
        assert analysis.analysis_version == "title-first-pending-v1"

    state["jobs"] = [_job("a-new-id", " C++   Software Engineer ")]
    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        jd_hydrator=hydrator,
        matcher=matcher,
    )

    assert second.new_count == 0
    assert second.reused_count == 1
    assert len(hydrator.calls) == 1
    assert len(matcher.calls) == 2
    with storage.session() as session:
        row = session.scalar(select(JobSnapshot))
        analysis = session.scalar(select(JobAnalysisSnapshot))
        assert row is not None
        assert analysis is not None
        assert analysis.analysis_status == "complete"
        assert row.match_score == 87


def test_scoped_run_scores_existing_complete_job_without_pending_marker(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path / "companies.yaml", _company("company-a", "Company A"))
    storage = _storage(tmp_path)
    hydrator = FakeHydrator()

    first = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(_job("a-1", "C++ Software Engineer")),
        jd_hydrator=hydrator,
        matcher=None,
        company_ids=["company-a"],
    )
    assert first.new_count == 1
    assert first.analysis_enabled is False
    assert first.scoring_candidate_count == 1
    assert first.unscored_count == 1

    matcher = FakeMatcher()
    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(_job("a-new-id", "C++ Software Engineer")),
        jd_hydrator=hydrator,
        matcher=matcher,
        company_ids=["company-a"],
    )

    assert second.new_count == 0
    assert second.reused_count == 1
    assert second.analysis_enabled is True
    assert second.scoring_candidate_count == 1
    assert second.scored_count == 1
    assert second.unscored_count == 0
    assert len(hydrator.calls) == 1
    assert len(matcher.calls) == 1
    with storage.session() as session:
        row = session.get(JobSnapshot, "a-1")
        analysis = session.get(JobAnalysisSnapshot, "a-1")
        assert row is not None
        assert row.match_score == 87
        assert analysis is not None
        assert analysis.analysis_status == "complete"


def test_title_first_detail_workers_are_bounded(tmp_path: Path) -> None:
    config = _config(tmp_path / "companies.yaml", _company("company-a", "Company A"))
    jobs = [_job(f"a-{index}", f"C++ Engineer {index}") for index in range(6)]
    storage = _storage(tmp_path)

    class BoundedHydrator:
        def __init__(self) -> None:
            self.active = 0
            self.peak = 0
            self.lock = Lock()

        def __call__(self, job: Mapping[str, Any]) -> dict[str, Any]:
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                sleep(0.01)
                return _detail(job)
            finally:
                with self.lock:
                    self.active -= 1

    hydrator = BoundedHydrator()
    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(*jobs),
        jd_hydrator=hydrator,
        matcher=FakeMatcher(),
        max_concurrency=2,
        detail_max_concurrency=2,
    )

    assert result.new_count == len(jobs)
    assert hydrator.peak <= 2

def test_complete_scope_inactivates_missing_titles_but_partial_scope_does_not(tmp_path: Path) -> None:
    config = _config(tmp_path / "companies.yaml", _company("company-a", "Company A"))
    states = [
        _crawl(_job("cpp-1", "C++ Software Engineer"), _job("qt-1", "Qt Developer")),
        _crawl(_job("cpp-2", "C++ Software Engineer"), complete=False),
        _crawl(_job("cpp-3", "C++ Software Engineer")),
        _crawl(_job("cpp-4", "C++ Software Engineer"), _job("qt-2", "Qt Developer")),
    ]
    hydrator = FakeHydrator()
    matcher = FakeMatcher()
    storage = _storage(tmp_path)

    def crawler(_company: Any) -> CrawlResult:
        return states.pop(0)

    for expected in (2, 0, 0, 0):
        result = run_daily_pipeline(
            companies_path=config,
            storage=storage,
            crawler=crawler,
            jd_hydrator=hydrator,
            matcher=matcher,
        )
        assert result.new_count == expected
        if expected == 0 and len(states) == 2:
            with storage.session() as session:
                qt = session.scalar(
                    select(JobSnapshot).where(JobSnapshot.title_key == "Qt Developer")
                )
                assert qt is not None
                assert qt.availability_status == "active"
        if expected == 0 and len(states) == 1:
            with storage.session() as session:
                qt = session.scalar(
                    select(JobSnapshot).where(JobSnapshot.title_key == "Qt Developer")
                )
                assert qt is not None
                assert qt.availability_status == "inactive"

    with storage.session() as session:
        qt_rows = list(session.scalars(select(JobSnapshot).where(JobSnapshot.title_key == "Qt Developer")))
    assert len(qt_rows) == 1
    assert qt_rows[0].availability_status == "active"
    assert len(hydrator.calls) == 2
    assert len(matcher.calls) == 2


def test_all_companies_and_empty_complete_lists_are_recorded(tmp_path: Path) -> None:
    config = _config(
        tmp_path / "companies.yaml",
        _company("filtered", "Filtered Co"),
        _company("empty", "Empty Co"),
        _company("pending", "Pending Co", status="pending"),
        {
            "id": "unusable",
            "name": "Unusable Co",
            "careers_url": "not-a-url",
            "crawler": "fixture",
            "integration_status": "connected",
        },
    )
    results = {
        "filtered": _crawl(_job("filtered-1", "Product Manager")),
        "empty": _crawl(),
    }
    hydrator = FakeHydrator()
    matcher = FakeMatcher()
    storage = _storage(tmp_path)
    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda company: results[company.id],
        jd_hydrator=hydrator,
        matcher=matcher,
    )

    assert result.total_companies == 4
    assert result.new_count == 0
    assert result.company_results[0].status == "complete"
    assert result.company_results[1].status == "complete"
    assert hydrator.calls == []
    assert matcher.calls == []
    with storage.session() as session:
        companies = list(session.scalars(select(CompanySnapshot)))
        sources = list(session.scalars(select(CompanySourceRecord)))
    assert {row.id for row in companies} == {"filtered", "empty", "pending", "unusable"}
    assert {row.company_name for row in sources} == {
        "Filtered Co",
        "Empty Co",
        "Pending Co",
        "Unusable Co",
    }
    source_status = {row.company_name: row.status for row in sources}
    assert source_status["Filtered Co"] == "complete"
    assert source_status["Empty Co"] == "complete"
    assert source_status["Pending Co"] == "pending"
    assert source_status["Unusable Co"] == "unusable"
    source_counts = {row.company_name: row.job_count for row in sources}
    assert source_counts["Filtered Co"] == 0
    assert source_counts["Empty Co"] == 0


def test_dry_run_does_not_write_storage_or_source_registry(tmp_path: Path) -> None:
    config = _config(tmp_path / "companies.yaml", _company("company-a", "Company A"))
    storage = _storage(tmp_path)
    writes: list[str | None] = []
    storage.pre_write_hook = lambda engine: writes.append(engine.url.database)
    hydrator = FakeHydrator()
    matcher = FakeMatcher()

    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(_job("a-1", "C++ Software Engineer")),
        jd_hydrator=hydrator,
        matcher=matcher,
        dry_run=True,
    )

    assert result.written is False
    assert writes == []
    with storage.session() as session:
        assert session.scalar(select(CompanySnapshot)) is None
        assert session.scalar(select(JobSnapshot)) is None
        assert session.scalar(select(CompanySourceRecord)) is None
