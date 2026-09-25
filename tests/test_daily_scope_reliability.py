from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import func, select

from packages.domain import ApplicationStage
from packages.pipeline import CrawlResult, run_daily_pipeline
from packages.pipeline.offline import (
    JobVisibility,
    decode_offline_state,
    reconcile_offline_jobs,
)
from packages.storage import (
    ApplicationSnapshot,
    CompanySnapshot,
    JobAnalysisSnapshot,
    JobSnapshot,
    Storage,
)


UTC = timezone.utc
COMPLETE_JD = (
    "Responsibilities: Develop and maintain C++ services on Linux, write unit tests, "
    "and investigate production failures. Requirements: Experience with C++ and Linux, "
    "knowledge of networking and data structures."
)


def _capture_evidence(detail: str, source_url: str) -> dict[str, Any]:
    normalized = detail.strip()
    return {
        "status": "complete",
        "method": "fixture_detail",
        "source_url": source_url,
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": sha256(normalized.encode("utf-8")).hexdigest(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def _storage() -> Storage:
    storage = Storage.from_url("sqlite:///:memory:")
    storage.initialize()
    return storage


def _write_companies(path: Path, *rows: dict[str, Any]) -> Path:
    path.write_text(
        yaml.safe_dump({"companies": list(rows)}, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _company(company_id: str) -> dict[str, Any]:
    return {
        "id": company_id,
        "name": f"Company {company_id}",
        "careers_url": "https://example.test/campus",
        "crawler": "fake",
        "integration_status": "connected",
    }


def _job(job_id: str) -> dict[str, Any]:
    job = {
        "id": job_id,
        "title": f"C++ Engineer {job_id}",
        "city": "Shanghai",
        "detail_url": f"https://example.test/jobs/{job_id}",
        "jd_raw": COMPLETE_JD,
        "cohort": 2027,
        "cohort_status": "confirmed",
        "batch": "formal",
        "job_type": "campus",
    }
    job["capture_evidence"] = _capture_evidence(COMPLETE_JD, job["detail_url"])
    return job


def _crawl(*jobs: dict[str, Any], **overrides: Any) -> CrawlResult:
    values: dict[str, Any] = {
        "jobs": list(jobs),
        "source_url": "https://example.test/campus",
        "allowed_origins": ["https://example.test"],
        "pages_seen": 1,
        "total_pages": 1,
        "has_more": False,
        "pagination_complete": True,
        "completeness_known": True,
    }
    values.update(overrides)
    return CrawlResult(**values)


def _hydrate(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "complete",
        "detail": job["jd_raw"],
        "detail_url": job["detail_url"],
        "source": "fixture_detail",
        "capture_evidence": job["capture_evidence"],
    }


class _Matcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def match(self, job: dict[str, Any], *, existing_analysis: Any = None) -> dict[str, Any]:
        self.calls.append((job["id"], existing_analysis))
        return {
            "analysis_status": "complete",
            "match_score": 88,
            "advantages": ["evidence"],
            "gaps": [],
            "summary": "matched",
            "recommendation": "recommend",
            "matched_directions": ["cpp_software"],
        }


def _offline_state(storage: Storage, job_id: str) -> tuple[Any, str | None, Any]:
    with storage.session() as session:
        job = session.get(JobSnapshot, job_id)
        assert job is not None
        return decode_offline_state(job.source_ref), job.source_ref, job.last_seen_at


def test_scoped_two_company_crawl_failure_does_not_block_valid_write(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        _company("broken"),
        _company("healthy"),
        _company("outside"),
    )
    storage = _storage()
    crawled: list[str] = []

    def crawler(company: Any) -> CrawlResult:
        crawled.append(company.id)
        if company.id == "broken":
            raise RuntimeError("crawler unavailable")
        if company.id == "healthy":
            return _crawl(_job("healthy-job"))
        raise AssertionError(f"out-of-scope company was crawled: {company.id}")

    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        matcher=_Matcher(),
        jd_hydrator=_hydrate,
        company_ids=["broken", "healthy"],
        max_concurrency=2,
    )

    assert sorted(crawled) == ["broken", "healthy"]
    assert result.total_companies == 2
    assert result.selected_companies == 2
    assert result.crawled_companies == 1
    assert result.failed_company_count == 1
    assert result.failure_reasons == {"crawler_failed": 1}
    assert result.new_job_ids == ("healthy-job",)
    company_results = {item.company_id: item for item in result.company_results}
    assert company_results["broken"].failure_reason == "crawler_failed"
    assert company_results["healthy"].status == "complete"

    with storage.session() as session:
        healthy_job = session.get(JobSnapshot, "healthy-job")
        assert healthy_job is not None
        assert healthy_job.company_id == "healthy"
        assert session.get(JobAnalysisSnapshot, "healthy-job") is not None
        assert session.get(JobSnapshot, "outside-job") is None


def test_failed_company_is_excluded_from_offline_missing_while_complete_company_is_reconciled(
    tmp_path: Path,
) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        _company("broken"),
        _company("healthy"),
    )
    storage = _storage()
    initial = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda company: _crawl(_job(f"{company.id}-old")),
        matcher=_Matcher(),
        jd_hydrator=_hydrate,
        company_ids=["broken", "healthy"],
    )
    assert initial.new_job_ids == ("broken-old", "healthy-old")
    broken_before = _offline_state(storage, "broken-old")

    def second_crawl(company: Any) -> CrawlResult:
        if company.id == "broken":
            raise RuntimeError("broken company crawl failed")
        return _crawl()

    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=second_crawl,
        matcher=_Matcher(),
        jd_hydrator=None,
        company_ids=["broken", "healthy"],
    )
    offline = reconcile_offline_jobs(
        storage,
        company_runs=second.company_results,
        observed_at=datetime(2026, 9, 7, 8, tzinfo=UTC),
        grace_runs=2,
        grace_days=0,
    )

    assert offline.processed_company_ids == ("healthy",)
    assert offline.skipped_company_ids == ("broken",)
    assert offline.missing_count == 1
    assert offline.inactive_count == 0
    assert [plan.job_id for plan in offline.plans] == ["healthy-old"]

    broken_after = _offline_state(storage, "broken-old")
    healthy_after = _offline_state(storage, "healthy-old")
    assert broken_after == broken_before
    assert healthy_after[0].status == JobVisibility.MISSING.value
    assert healthy_after[0].missing_runs == 1


def test_repeating_the_same_scoped_batch_reuses_jobs_and_analysis(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        _company("alpha"),
        _company("beta"),
        _company("outside"),
    )
    storage = _storage()
    crawler_calls: list[str] = []
    matcher = _Matcher()

    def crawler(company: Any) -> CrawlResult:
        crawler_calls.append(company.id)
        return _crawl(_job(f"{company.id}-job"))

    first = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        matcher=matcher,
        jd_hydrator=_hydrate,
        company_ids=["alpha", "beta"],
    )
    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        matcher=matcher,
        jd_hydrator=None,
        company_ids=["alpha", "beta"],
    )

    assert first.new_job_ids == ("alpha-job", "beta-job")
    assert second.scoped_company_ids == ("alpha", "beta")
    assert second.new_count == 0
    assert second.changed_count == 0
    assert second.reused_job_ids == ("alpha-job", "beta-job")
    assert sorted(crawler_calls) == ["alpha", "alpha", "beta", "beta"]
    assert sorted(job_id for job_id, _existing in matcher.calls) == [
        "alpha-job",
        "beta-job",
    ]
    assert all(existing is None for _job_id, existing in matcher.calls)

    with storage.session() as session:
        assert session.scalar(select(func.count()).select_from(JobSnapshot)) == 2
        assert session.scalar(select(func.count()).select_from(JobAnalysisSnapshot)) == 2
        assert session.scalar(select(func.count()).select_from(CompanySnapshot)) == 2
        assert session.get(JobSnapshot, "outside-job") is None


_APPLICATION_COLUMNS = tuple(ApplicationSnapshot.__table__.columns.keys())


def _application_state(storage: Storage) -> dict[str, Any]:
    with storage.session() as session:
        application = session.get(ApplicationSnapshot, "application-1")
        assert application is not None
        return {
            column: deepcopy(getattr(application, column))
            for column in _APPLICATION_COLUMNS
        }


def test_daily_write_failure_rolls_back_current_batch_and_preserves_admitted_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _write_companies(tmp_path / "companies.yaml", _company("company"))
    storage = _storage()
    created_at = datetime(2026, 9, 1, 8, tzinfo=UTC)
    updated_at = datetime(2026, 9, 2, 8, tzinfo=UTC)
    with storage.write_transaction() as session:
        session.add(
            ApplicationSnapshot(
                id="application-1",
                company_name="Original Company",
                job_title="Original Engineer",
                job_id="original-job",
                record_url="https://example.test/applications/1",
                stage=ApplicationStage.APPLIED.value,
                idempotency_key="application:original",
                note="keep every field",
                stage_history=[{"stage": "interested"}, {"stage": "applied"}],
                source_stage="applied",
                source_status="submitted",
                source_status_synced_at=created_at,
                created_at=created_at,
                updated_at=updated_at,
                source="fixture",
                source_ref="fixture:application:1",
            )
        )
    before = _application_state(storage)

    import packages.pipeline.daily as daily

    original_upsert = daily.upsert_job_analysis_snapshot
    analysis_calls = 0

    def fail_on_second_analysis(session: Any, job: Any, analysis: Any) -> None:
        nonlocal analysis_calls
        analysis_calls += 1
        if analysis_calls == 2:
            application = session.get(ApplicationSnapshot, "application-1")
            assert application is not None
            application.company_name = "Mutated Company"
            application.job_title = "Mutated Engineer"
            application.job_id = "mutated-job"
            application.record_url = "https://example.test/applications/mutated"
            application.stage = ApplicationStage.REJECTED.value
            application.idempotency_key = "application:mutated"
            application.note = "must roll back"
            application.stage_history = [{"stage": "rejected"}]
            application.source_stage = "rejected"
            application.source_status = "rejected"
            application.source_status_synced_at = created_at
            application.created_at = created_at.replace(day=3)
            application.updated_at = created_at.replace(day=4)
            application.source = "mutated-source"
            application.source_ref = "mutated:application:1"
            session.flush()
            raise RuntimeError("mid-transaction failure")
        original_upsert(session, job, analysis)

    monkeypatch.setattr(daily, "upsert_job_analysis_snapshot", fail_on_second_analysis)

    with pytest.raises(RuntimeError, match="mid-transaction failure"):
        run_daily_pipeline(
            companies_path=config,
            storage=storage,
            crawler=lambda _company: _crawl(_job("first"), _job("second")),
            matcher=_Matcher(),
            jd_hydrator=_hydrate,
            checkpoint_batch_size=2,
            max_concurrency=1,
            match_max_concurrency=1,
        )

    assert analysis_calls == 2
    assert _application_state(storage) == before
    with storage.session() as session:
        # Listing admission committed before hydration. The failed detail batch
        # rolls back its two details/analyses while preserving those pending rows.
        assert session.scalar(select(func.count()).select_from(CompanySnapshot)) == 1
        rows = list(session.scalars(select(JobSnapshot)))
        assert {row.id for row in rows} == {"first", "second"}
        assert all(row.capture_status == "pending" for row in rows)
        assert all(row.jd_raw is None and row.match_score is None for row in rows)
        assert session.scalar(select(func.count()).select_from(JobAnalysisSnapshot)) == 0
