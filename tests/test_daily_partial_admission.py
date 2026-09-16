from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import func, select

from packages.domain import ApplicationStage
from packages.pipeline import CrawlResult, run_daily_pipeline
from packages.pipeline.offline import (
    CompanyRunObservation,
    JobVisibility,
    decode_offline_state,
    reconcile_offline_jobs,
)
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import (
    ApplicationSnapshot,
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


def _write_company(path: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "companies": [
                    {
                        "id": "co",
                        "name": "Example Co",
                        "careers_url": "https://example.test/campus",
                        "crawler": "fake",
                        "integration_status": "connected",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _job(
    job_id: str,
    *,
    detail_url: str = "https://example.test/jobs/one",
    jd_raw: str = COMPLETE_JD,
    title: str = "C++ Software Engineer",
) -> dict[str, Any]:
    job = {
        "id": job_id,
        "title": title,
        "city": "Shanghai",
        "detail_url": detail_url,
        "jd_raw": jd_raw,
        "cohort": 2027,
        "cohort_status": "confirmed",
        "batch": "formal",
        "job_type": "campus",
    }
    if jd_raw == COMPLETE_JD:
        job["capture_evidence"] = _capture_evidence(jd_raw, detail_url)
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


def test_invalid_row_does_not_contaminate_complete_pagination_evidence(
    tmp_path: Path,
) -> None:
    config = _write_company(tmp_path / "companies.yaml")

    result = run_daily_pipeline(
        companies_path=config,
        dry_run=True,
        crawler=lambda _company: _crawl(
            _job("valid"),
            {},
            advertised_total=2,
        ),
        matcher=_Matcher(),
    )

    company = result.company_results[0]
    assert company.failure_reason is None
    assert company.accepted_job_count == 1
    assert company.rejection_reasons == {"invalid_job": 1}
    assert company.crawl_evidence["advertised_total"] == 2


class _Matcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def match(self, job: dict[str, Any], *, existing_analysis: Any = None) -> dict[str, Any]:
        self.calls.append(job["id"])
        return {
            "analysis_status": "complete",
            "match_score": 88,
            "advantages": ["evidence"],
            "gaps": [],
            "summary": "matched",
            "recommendation": "recommend",
            "matched_directions": ["cpp_software"],
        }


_APPLICATION_FIELDS = (
    "id",
    "company_name",
    "job_title",
    "job_id",
    "record_url",
    "stage",
    "idempotency_key",
    "note",
    "stage_history",
    "source_stage",
    "source_status",
    "source_status_synced_at",
    "created_at",
    "updated_at",
    "source",
    "source_ref",
)


def _application_state(storage: Storage, application_id: str = "application-1") -> tuple[tuple[str, Any], ...]:
    with storage.session() as session:
        application = session.get(ApplicationSnapshot, application_id)
        assert application is not None
        values: list[tuple[str, Any]] = []
        for field in _APPLICATION_FIELDS:
            value = getattr(application, field)
            values.append((field, list(value) if isinstance(value, list) else value))
        return tuple(values)


def _visible_job_ids(storage: Storage) -> set[str]:
    page = PostgresRecruitmentRepository(storage).search_jobs(limit=50, offset=0)
    return {job.id for job in page.items}


def test_partial_coverage_admits_valid_rows_but_rejects_invalid_rows(tmp_path: Path) -> None:
    config = _write_company(tmp_path / "companies.yaml")
    storage = _storage()
    with storage.write_transaction() as session:
        session.add(
            ApplicationSnapshot(
                id="application-1",
                company_name="Example Co",
                job_title="C++ Software Engineer",
                job_id="good",
                stage=ApplicationStage.APPLIED.value,
                idempotency_key="application:good",
                source="fixture",
                source_ref="application-1",
            )
        )
    invalid = _job("bad", detail_url="https://other.test/jobs/bad")
    matcher = _Matcher()

    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job("good"),
            invalid,
            pages_seen=1,
            total_pages=2,
            has_more=True,
            pagination_complete=False,
            completeness_known=True,
        ),
        matcher=matcher,
    )

    company = result.company_results[0]
    assert result.new_job_ids == ("good",)
    assert company.accepted_job_count == 1
    assert company.failure_reason == "pagination_incomplete"
    assert company.status == "partial"
    assert company.crawl_evidence["pagination_state"] == "incomplete"
    assert result.rejection_reasons["detail_origin_not_allowed"] == 1
    assert "pagination_incomplete" not in company.rejection_reasons
    assert matcher.calls == ["good"]
    with storage.session() as session:
        assert session.get(JobSnapshot, "good") is not None
        assert session.get(JobSnapshot, "bad") is None
        application = session.get(ApplicationSnapshot, "application-1")
        assert application is not None
        assert application.stage == ApplicationStage.APPLIED.value
        assert application.job_id == "good"


def test_unknown_coverage_keeps_row_path_without_marking_company_complete(tmp_path: Path) -> None:
    config = _write_company(tmp_path / "companies.yaml")
    storage = _storage()

    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job("unknown"),
            pages_seen=0,
            total_pages=None,
            has_more=False,
            pagination_complete=None,
            completeness_known=False,
        ),
        matcher=_Matcher(),
    )

    company = result.company_results[0]
    assert result.new_job_ids == ("unknown",)
    assert company.accepted_job_count == 1
    assert company.failure_reason == "pagination_unknown"
    assert company.status == "partial"
    assert result.crawled_companies == 0
    assert company.crawl_evidence["pagination_state"] == "unknown"
    with storage.session() as session:
        assert session.get(JobSnapshot, "unknown") is not None


@pytest.mark.parametrize(
    "error_code",
    ["login_required", "captcha_required", "crawler_failed", "identity_mismatch"],
)
def test_non_pagination_error_never_admits_rows(tmp_path: Path, error_code: str) -> None:
    config = _write_company(tmp_path / f"companies-{error_code}.yaml")
    storage = _storage()
    matcher = _Matcher()

    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job(error_code),
            pagination_complete=False,
            completeness_known=True,
            error_code=error_code,
        ),
        matcher=matcher,
    )

    company = result.company_results[0]
    assert company.failure_reason == error_code
    assert company.accepted_job_count == 0
    assert matcher.calls == []
    with storage.session() as session:
        assert session.get(JobSnapshot, error_code) is None


def test_explicit_pagination_error_can_keep_audited_rows(tmp_path: Path) -> None:
    config = _write_company(tmp_path / "companies.yaml")
    storage = _storage()

    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job("pagination-error"),
            pagination_complete=False,
            completeness_known=True,
            error_code="pagination_incomplete",
        ),
        matcher=_Matcher(),
    )

    assert result.new_job_ids == ("pagination-error",)
    assert result.company_results[0].failure_reason == "pagination_incomplete"
    with storage.session() as session:
        assert session.get(JobSnapshot, "pagination-error") is not None


def test_partial_bad_detail_does_not_replace_existing_complete_jd(tmp_path: Path) -> None:
    config = _write_company(tmp_path / "companies.yaml")
    storage = _storage()
    original = _job("stable")
    first = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(original),
        matcher=_Matcher(),
    )
    assert first.new_count == 1

    hydration_calls: list[str] = []

    def should_not_hydrate(_job: dict[str, Any]) -> str:
        hydration_calls.append("unexpected")
        raise AssertionError("a stored complete JD must be reused")

    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job("stable", jd_raw=""),
            pages_seen=1,
            total_pages=2,
            has_more=True,
            pagination_complete=False,
            completeness_known=True,
        ),
        matcher=_Matcher(),
        jd_hydrator=should_not_hydrate,
    )

    assert second.company_results[0].failure_reason == "pagination_incomplete"
    assert second.reused_count == 1
    assert hydration_calls == []
    with storage.session() as session:
        stored = session.get(JobSnapshot, "stable")
        assert stored is not None
        assert stored.jd_raw == COMPLETE_JD


def test_partial_and_unknown_rounds_skip_offline_missing_and_preserve_application(
    tmp_path: Path,
) -> None:
    config = _write_company(tmp_path / "companies.yaml")
    storage = _storage()
    matcher = _Matcher()
    initial = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job("old", detail_url="https://example.test/jobs/old")
        ),
        matcher=matcher,
    )
    assert initial.new_job_ids == ("old",)
    with storage.write_transaction() as session:
        session.add(
            ApplicationSnapshot(
                id="application-1",
                company_name="Example Co",
                job_title="C++ Software Engineer",
                job_id="old",
                stage=ApplicationStage.APPLIED.value,
                idempotency_key="application:old",
                note="keep this application",
                stage_history=[{"stage": "applied"}],
                source_stage="applied",
                source_status="submitted",
                source="fixture",
                source_ref="application-1",
            )
        )
    before_application = _application_state(storage)
    seen = datetime(2026, 9, 6, 8, tzinfo=UTC)

    partial = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job(
                "new-partial",
                detail_url="https://example.test/jobs/new-partial",
                title="C++ New Partial Engineer",
            ),
            pages_seen=1,
            total_pages=2,
            has_more=True,
            pagination_complete=False,
            completeness_known=True,
        ),
        matcher=matcher,
    )
    partial_offline = reconcile_offline_jobs(
        storage,
        company_runs=partial.company_results,
        observed_at=seen,
        grace_runs=1,
        grace_days=0,
    )

    unknown = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job(
                "new-unknown",
                detail_url="https://example.test/jobs/new-unknown",
                title="C++ New Unknown Engineer",
            ),
            pages_seen=0,
            total_pages=None,
            has_more=False,
            pagination_complete=None,
            completeness_known=False,
        ),
        matcher=matcher,
    )
    unknown_offline = reconcile_offline_jobs(
        storage,
        company_runs=unknown.company_results,
        observed_at=seen + timedelta(days=1),
        grace_runs=1,
        grace_days=0,
    )

    for reconciliation in (partial_offline, unknown_offline):
        assert reconciliation.written is False
        assert reconciliation.skipped_company_ids == ("co",)
        assert reconciliation.missing_count == 0
        assert reconciliation.inactive_count == 0
        assert reconciliation.plans == ()
    assert partial.new_job_ids == ("new-partial",)
    assert unknown.new_job_ids == ("new-unknown",)
    assert _visible_job_ids(storage) == {"old", "new-partial", "new-unknown"}
    with storage.session() as session:
        for job_id in ("old", "new-partial", "new-unknown"):
            job = session.get(JobSnapshot, job_id)
            assert job is not None
            state = decode_offline_state(job.source_ref)
            assert state.status == JobVisibility.ACTIVE.value
            assert state.missing_runs == 0
    assert _application_state(storage) == before_application


def test_partial_rerun_dedupes_and_reuses_complete_analysis(tmp_path: Path) -> None:
    config = _write_company(tmp_path / "companies.yaml")
    storage = _storage()
    matcher = _Matcher()
    crawl = lambda _company: _crawl(
        _job("repeat", detail_url="https://example.test/jobs/repeat"),
        pages_seen=1,
        total_pages=2,
        has_more=True,
        pagination_complete=False,
        completeness_known=True,
    )

    first = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawl,
        matcher=matcher,
    )
    with storage.session() as session:
        analysis = session.get(JobAnalysisSnapshot, "repeat")
        assert analysis is not None
        baseline = (analysis.match_score, analysis.summary, analysis.recommendation)
        assert session.scalar(select(func.count()).select_from(JobSnapshot)) == 1

    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawl,
        matcher=matcher,
    )

    assert first.new_job_ids == ("repeat",)
    assert second.reused_job_ids == ("repeat",)
    assert second.new_count == 0
    assert second.changed_count == 0
    assert len(matcher.calls) == 1
    with storage.session() as session:
        analysis = session.get(JobAnalysisSnapshot, "repeat")
        assert analysis is not None
        assert (analysis.match_score, analysis.summary, analysis.recommendation) == baseline
        assert session.scalar(select(func.count()).select_from(JobSnapshot)) == 1


def test_daily_and_offline_dry_run_perform_zero_writes(tmp_path: Path) -> None:
    config = _write_company(tmp_path / "companies.yaml")
    storage = _storage()
    initial = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job("existing", detail_url="https://example.test/jobs/existing")
        ),
        matcher=_Matcher(),
    )
    assert initial.new_job_ids == ("existing",)
    writes: list[object] = []
    storage.pre_write_hook = lambda engine: writes.append(engine)

    dry_daily = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job("dry-partial", detail_url="https://example.test/jobs/dry-partial"),
            pages_seen=1,
            total_pages=2,
            has_more=True,
            pagination_complete=False,
            completeness_known=True,
        ),
        matcher=_Matcher(),
        dry_run=True,
    )
    dry_offline = reconcile_offline_jobs(
        storage,
        company_runs=[CompanyRunObservation("co", frozenset())],
        observed_at=datetime(2026, 9, 7, 8, tzinfo=UTC),
        grace_runs=1,
        grace_days=0,
        dry_run=True,
    )

    assert dry_daily.written is False
    assert dry_offline.written is False
    assert dry_offline.plans
    assert writes == []
    with storage.session() as session:
        assert session.get(JobSnapshot, "dry-partial") is None
        existing = session.get(JobSnapshot, "existing")
        assert existing is not None
        state = decode_offline_state(existing.source_ref)
        assert state.status == JobVisibility.ACTIVE.value
        assert state.missing_runs == 0


def test_same_host_different_registered_ats_project_is_rejected(tmp_path: Path) -> None:
    config = tmp_path / "companies.yaml"
    expected_url = "https://app.mokahr.com/campus_apply/expected/100#/jobs"
    wrong_url = "https://app.mokahr.com/campus_apply/other-project/200#/jobs"
    config.write_text(
        yaml.safe_dump(
            {
                "companies": [
                    {
                        "id": "co",
                        "name": "Example Co",
                        "careers_url": expected_url,
                        "crawler": "moka",
                        "integration_status": "connected",
                        "source_identity": "moka:expected",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    storage = _storage()
    matcher = _Matcher()

    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job("wrong-project", detail_url="https://app.mokahr.com/jobs/one"),
            source_url=expected_url,
            allowed_origins=["https://app.mokahr.com"],
            effective_source_urls=[wrong_url],
            source_runs=[
                {
                    "source_url": expected_url,
                    "effective_source_url": wrong_url,
                    "pagination_complete": True,
                }
            ],
        ),
        matcher=matcher,
    )

    company = result.company_results[0]
    assert company.failure_reason == "identity_mismatch"
    assert company.accepted_job_count == 0
    assert company.crawl_evidence["expected_source_identities"] == [
        "moka:expected"
    ]
    assert company.crawl_evidence["observed_source_identities"] == [
        "moka:other-project"
    ]
    assert matcher.calls == []
    with storage.session() as session:
        assert session.get(JobSnapshot, "wrong-project") is None


def test_registered_ats_project_allows_pagination_variants(tmp_path: Path) -> None:
    config = tmp_path / "companies.yaml"
    expected_url = "https://app.mokahr.com/campus_apply/expected/100#/jobs"
    page_two = "https://app.mokahr.com/campus_apply/expected/100#/jobs?page=2"
    config.write_text(
        yaml.safe_dump(
            {
                "companies": [
                    {
                        "id": "co",
                        "name": "Example Co",
                        "careers_url": expected_url,
                        "crawler": "moka",
                        "integration_status": "connected",
                        "source_identity": "moka:expected",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    storage = _storage()

    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _job("expected-project", detail_url="https://app.mokahr.com/jobs/one"),
            source_url=expected_url,
            allowed_origins=["https://app.mokahr.com"],
            effective_source_urls=[page_two],
            source_runs=[
                {
                    "source_url": expected_url,
                    "effective_source_url": page_two,
                    "pagination_complete": True,
                }
            ],
        ),
        matcher=_Matcher(),
    )

    assert result.new_job_ids == ("expected-project",)
    assert result.company_results[0].failure_reason is None
