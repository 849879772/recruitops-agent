from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import threading
import time
from typing import Any

import pytest
import yaml
from sqlalchemy import func, select

from packages.matching import (
    AnalysisDecision,
    AnalysisOutcome,
    AnalysisRecord,
    AnalysisStatus,
    DecisionAction,
)
from packages.pipeline import (
    CrawlResult,
    DailyRecruitmentPipeline,
    MatchingServiceAdapter,
    PipelineError,
    run_daily_pipeline,
)
from packages.discovery.company_registry import CompanySourceRegistry
from packages.storage import (
    CompanySnapshot,
    JobAnalysisSnapshot,
    JobSnapshot,
    Storage,
)


UTC = timezone.utc
COMPLETE_JD = (
    "Responsibilities: Develop and maintain C++ services on Linux, write unit tests, "
    "and investigate production failures. "
    "Requirements: Experience with C++ and Linux, knowledge of networking and data structures."
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


def _job(job_id: str, *, jd_raw: str = COMPLETE_JD) -> dict[str, Any]:
    job = {
        "id": job_id,
        "title": f"C++ Engineer {job_id}",
        "city": "Shanghai",
        "detail_url": f"https://example.test/jobs/{job_id}",
        "jd_raw": jd_raw,
        "cohort": 2027,
        "cohort_status": "confirmed",
        "batch": "formal",
        "job_type": "campus",
    }
    if jd_raw == COMPLETE_JD:
        job["capture_evidence"] = _capture_evidence(jd_raw, job["detail_url"])
    return job


def _crawl_result(*jobs: dict[str, Any], **overrides: Any) -> CrawlResult:
    values: dict[str, Any] = {
        "jobs": list(jobs),
        "source_url": "https://example.test/campus",
        "allowed_origins": ["https://example.test"],
        "pages_seen": 1,
        "total_pages": 1,
        "has_more": False,
    }
    values.update(overrides)
    return CrawlResult(**values)


def _fixture_hydrator(job: dict[str, Any]) -> dict[str, Any]:
    detail = job.get("jd_raw") or COMPLETE_JD
    detail_url = str(job["detail_url"])
    return {
        "detail": detail,
        "status": "complete",
        "detail_url": detail_url,
        "source": "fixture_detail",
        "capture_evidence": _capture_evidence(detail, detail_url),
    }


class FakeMatcher:
    def __init__(self, *, failing_ids: set[str] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.failing_ids = failing_ids or set()

    def match(self, job: dict[str, Any], *, existing_analysis: Any = None) -> dict[str, Any]:
        self.calls.append((job["id"], existing_analysis))
        if job["id"] in self.failing_ids:
            raise RuntimeError("fake matcher failure")
        return {
            "analysis_status": "complete",
            "match_score": 88,
            "advantages": ["evidence"],
            "gaps": [],
            "summary": "matched",
            "recommendation": "recommend",
            "matched_directions": ["cpp_software"],
        }


def test_pipeline_selects_agent_companies_and_writes_one_transaction(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "connected",
            "name": "Connected Co",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
        {
            "id": "disconnected",
            "name": "Disconnected Co",
            "careers_url": "https://example.test/other",
            "crawler": "fake",
            "integration_status": "pending",
        },
        {"id": "manual", "name": "Manual Co", "integration_status": "connected"},
    )
    storage = _storage()
    matcher = FakeMatcher()
    crawler_calls: list[str] = []
    write_calls = 0
    source_write_calls = 0
    original_write_transaction = storage.write_transaction
    original_upsert_source = CompanySourceRegistry.upsert_source
    original_record_attempt = CompanySourceRegistry.record_attempt

    def tracked_write_transaction() -> Any:
        nonlocal write_calls
        write_calls += 1
        return original_write_transaction()

    storage.write_transaction = tracked_write_transaction  # type: ignore[method-assign]

    def tracked_upsert_source(self, *args: Any, **kwargs: Any) -> Any:
        nonlocal source_write_calls
        source_write_calls += 1
        return original_upsert_source(self, *args, **kwargs)

    def tracked_record_attempt(self, *args: Any, **kwargs: Any) -> Any:
        nonlocal source_write_calls
        source_write_calls += 1
        return original_record_attempt(self, *args, **kwargs)

    CompanySourceRegistry.upsert_source = tracked_upsert_source  # type: ignore[method-assign]
    CompanySourceRegistry.record_attempt = tracked_record_attempt  # type: ignore[method-assign]

    def crawler(company: Any) -> CrawlResult:
        crawler_calls.append(company.name)
        return _crawl_result(_job("job-1"))

    try:
        result = run_daily_pipeline(
            companies_path=config,
            storage=storage,
            crawler=crawler,
            matcher=matcher,
            max_concurrency=2,
        )
    finally:
        CompanySourceRegistry.upsert_source = original_upsert_source  # type: ignore[method-assign]
        CompanySourceRegistry.record_attempt = original_record_attempt  # type: ignore[method-assign]

    assert result.selected_companies == 1
    assert result.skipped_companies == ("Disconnected Co", "Manual Co")
    assert result.new_count == 1
    assert result.reused_count == 0
    assert result.written is True
    assert crawler_calls == ["Connected Co"]
    assert len(matcher.calls) == 1
    assert source_write_calls == 6
    # Title-first saves all admitted rows before scoring, then persists the
    # completed score in a second transaction.
    assert write_calls - source_write_calls == 2
    with storage.session() as session:
        # The title-first run registers every configured company, including
        # skipped and currently unusable entries, before crawling candidates.
        assert session.scalar(select(func.count()).select_from(CompanySnapshot)) == 3
        assert {
            snapshot.id for snapshot in session.scalars(select(CompanySnapshot)).all()
        } == {"connected", "disconnected", "manual"}
        assert session.scalar(select(func.count()).select_from(JobSnapshot)) == 1
        assert session.scalar(select(func.count()).select_from(JobAnalysisSnapshot)) == 1


@pytest.mark.parametrize(
    ("careers_url", "expected_reason"),
    [
        ("not-an-http-url", "invalid_entry"),
        ("https://wj.qq.com/s2/example", "form_application_only"),
    ],
)
def test_generic_render_entry_gate_fails_before_crawler_start(
    tmp_path: Path,
    careers_url: str,
    expected_reason: str,
) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": careers_url,
            "crawler": "render",
            "integration_status": "connected",
        },
    )
    crawler_calls: list[str] = []

    result = run_daily_pipeline(
        companies_path=config,
        crawler=lambda company: crawler_calls.append(company.name) or _crawl_result(),
        matcher=FakeMatcher(),
        dry_run=True,
    )

    assert crawler_calls == []
    assert result.failed_company_count == 1
    assert result.failure_reasons == {expected_reason: 1}
    assert result.company_results[0].failure_reason == expected_reason


def test_dedicated_adapter_is_not_blocked_by_generic_entry_gate(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": "https://example.test/",
            "crawler": "moka",
            "integration_status": "connected",
        },
    )
    crawler_calls: list[str] = []

    result = run_daily_pipeline(
        companies_path=config,
        crawler=lambda company: crawler_calls.append(company.name)
        or _crawl_result(_job("job-1")),
        matcher=FakeMatcher(),
        dry_run=True,
    )

    assert crawler_calls == ["Example Co"]
    assert result.failed_company_count == 0
    assert result.new_count == 1


@pytest.mark.parametrize(
    ("crawler_key", "careers_url", "expected_reason"),
    [
        ("fake", "https://wj.qq.com/s2/example", "form_application_only"),
        ("fake", "not-an-http-url", "invalid_entry"),
    ],
)
def test_empty_crawl_uses_entry_diagnosis_for_failure_reason(
    tmp_path: Path,
    crawler_key: str,
    careers_url: str,
    expected_reason: str,
) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": careers_url,
            "crawler": crawler_key,
            "integration_status": "connected",
        },
    )
    crawler_calls: list[str] = []

    result = run_daily_pipeline(
        companies_path=config,
        crawler=lambda company: crawler_calls.append(company.name) or _crawl_result(),
        matcher=FakeMatcher(),
        dry_run=True,
    )

    assert crawler_calls == []
    assert result.failure_reasons == {expected_reason: 1}
    assert result.company_results[0].failure_reason == expected_reason


def test_second_run_reuses_same_job_and_changed_jd_is_scored_again(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    storage = _storage()
    matcher = FakeMatcher()
    current = {"value": _crawl_result(_job("job-1"))}

    def crawler(_company: Any) -> CrawlResult:
        return current["value"]

    pipeline = DailyRecruitmentPipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        matcher=matcher,
    )
    first = pipeline.run(legacy=True)
    second = pipeline.run(legacy=True)
    assert first.new_count == 1
    assert second.reused_count == 1
    assert second.new_count == 0
    assert second.changed_count == 0
    assert len(matcher.calls) == 1

    changed_jd = (
        "Responsibilities: Develop C++ robot controllers and own Linux performance tuning. "
        "Requirements: Experience with C++ and Linux, knowledge of robotics and ROS."
    )
    current["value"] = _crawl_result(
        _job("job-1", jd_raw=changed_jd)
        | {"capture_evidence": _capture_evidence(changed_jd, "https://example.test/jobs/job-1")}
    )
    third = pipeline.run(legacy=True)
    assert third.changed_count == 1
    assert third.reused_count == 0
    assert len(matcher.calls) == 2
    with storage.session() as session:
        analysis = session.get(JobAnalysisSnapshot, "job-1")
    assert analysis is not None
    assert analysis.match_score == 88


def test_changed_native_id_reuses_location_independent_business_identity(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "unit-a",
            "name": "Example Co",
            "organization_id": "org-example",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    storage = _storage()
    current = {"id": "ats-old", "city": "上海"}

    def crawler(_company: Any) -> CrawlResult:
        return _crawl_result(
            {
                "id": current["id"],
                "title": "软件开发工程师",
                "city": current["city"],
                "detail_url": "https://example.test/jobs/42?utm_source=oc",
                "jd_raw": "Complete responsibilities and requirements for this role.",
                "capture_evidence": _capture_evidence(
                    "Complete responsibilities and requirements for this role.",
                    "https://example.test/jobs/42?utm_source=oc",
                ),
                "cohort": 2027,
                "cohort_status": "confirmed",
                "batch": "formal",
                "job_type": "campus",
            }
        )

    first = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        matcher=FakeMatcher(),
        legacy=True,
    )
    current.update({"id": "ats-new", "city": "北京"})
    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=crawler,
        matcher=FakeMatcher(),
        legacy=True,
    )

    assert first.new_job_ids == ("ats-old",)
    assert second.changed_job_ids == ("ats-old",)
    with storage.session() as session:
        jobs = list(session.scalars(select(JobSnapshot)))
    assert len(jobs) == 1
    assert jobs[0].id == "ats-old"
    assert jobs[0].business_key


def test_same_run_deduplicates_different_native_ids_by_business_key(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "unit-a",
            "name": "Example Co",
            "organization_id": "org-example",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    storage = _storage()
    matcher = FakeMatcher()
    first = _job("native-a") | {
        "title": "软件开发工程师",
        "city": "上海",
        "detail_url": "https://example.test/jobs/42?utm_source=oc",
        "capture_evidence": _capture_evidence(
            COMPLETE_JD,
            "https://example.test/jobs/42?utm_source=oc",
        ),
    }
    second = _job("native-b") | {
        "title": "软件开发工程师",
        "city": "北京",
        "detail_url": "https://example.test/jobs/42",
        "capture_evidence": _capture_evidence(
            COMPLETE_JD,
            "https://example.test/jobs/42",
        ),
    }

    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl_result(first, second),
        matcher=matcher,
        legacy=True,
    )

    assert result.new_count == 1
    assert len(matcher.calls) == 1
    with storage.session() as session:
        jobs = list(session.scalars(select(JobSnapshot)))
    assert len(jobs) == 1
    assert jobs[0].business_key


def test_audit_rejects_non_2027_incomplete_and_ineligible_jobs(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    accepted = _job("accepted")
    missing_title = _job("missing-title") | {"title": ""}
    bad_url = _job("bad-url") | {"detail_url": "javascript:bad"}
    old_cohort = _job("old") | {"cohort": 2026}
    unconfirmed = _job("unconfirmed") | {"cohort_status": "unconfirmed"}
    internship = _job("intern") | {"batch": "internship"}
    result = run_daily_pipeline(
        companies_path=config,
        storage=_storage(),
        crawler=lambda _company: _crawl_result(
            accepted, missing_title, bad_url, old_cohort, unconfirmed, internship
        ),
        matcher=FakeMatcher(),
        legacy=True,
    )

    assert result.new_count == 3
    assert result.rejected_count == 3
    assert result.filtered_count == 2
    assert result.rejection_reasons["missing_title"] == 1
    assert result.rejection_reasons["invalid_detail_url"] == 1
    assert result.rejection_reasons["ineligible_batch"] == 1


def test_company_results_count_job_filter_reasons_independently(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co-a",
            "name": "Company A",
            "careers_url": "https://example.test/campus/jobs",
            "crawler": "fake",
            "integration_status": "connected",
        },
        {
            "id": "co-b",
            "name": "Company B",
            "careers_url": "https://example.test/campus/jobs",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    statuses = {
        "direction": "direction_out",
        "doctorate": "doctorate_only",
        "intern": "internship",
        "sparse": "jd_incomplete",
    }

    class FilteringMatcher:
        def match(
            self,
            job: dict[str, Any],
            *,
            existing_analysis: Any = None,
        ) -> dict[str, Any]:
            assert existing_analysis is None
            status = statuses[job["id"]]
            return {
                "analysis_status": status,
                "filter_reasons": [status],
            }

    def crawler(company: Any) -> CrawlResult:
        job_ids = (
            ("direction", "intern", "sparse")
            if company.id == "co-a"
            else ("doctorate",)
        )
        return _crawl_result(*[_job(job_id) for job_id in job_ids])

    result = run_daily_pipeline(
        companies_path=config,
        storage=_storage(),
        crawler=crawler,
        matcher=FilteringMatcher(),
        jd_hydrator=None,
        legacy=True,
    )

    companies = {item.company_id: item for item in result.company_results}
    assert companies["co-a"].filtered_count == 3
    assert companies["co-a"].filtered_reasons == {
        "direction_out": 1,
        "internship": 1,
        "jd_incomplete": 1,
    }
    assert companies["co-b"].filtered_count == 1
    assert companies["co-b"].filtered_reasons == {"doctorate_only": 1}
    serialized = {item["company_id"]: item for item in result.to_dict()["companies"]}
    assert serialized["co-a"]["filtered_reasons"] == companies["co-a"].filtered_reasons


def test_target_direction_sparse_jd_is_hydrated_before_matching(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    storage = _storage()
    sparse = _job("cpp", jd_raw="C++软件开发工程师 上海") | {
        "title": "C++软件开发工程师"
    }
    hydrated = (
        "岗位职责: 负责 Linux 平台 C++ 软件模块设计、开发和自动化测试。"
        "任职要求: 熟悉 C++, 多线程, 数据结构和软件工程实践, 有完整项目经验。"
    )
    calls: list[str] = []

    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl_result(sparse),
        matcher=FakeMatcher(),
        jd_hydrator=lambda job: calls.append(job["id"]) or {
            "detail": hydrated,
            "status": "complete",
            "source": "fixture_detail",
            "detail_url": job["detail_url"],
            "identity_status": "matched",
            "capture_evidence": _capture_evidence(hydrated, job["detail_url"]),
        },
    )

    assert result.new_count == 1
    assert calls == ["cpp"]
    with storage.session() as session:
        stored_jd = session.get(JobSnapshot, "cpp").jd_raw
        assert "Linux" in stored_jd
        assert len(stored_jd) > 50

    second = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl_result(sparse),
        matcher=FakeMatcher(),
        jd_hydrator=lambda _job: (_ for _ in ()).throw(
            AssertionError("stored complete JD should be reused")
        ),
    )
    assert second.reused_count == 1


def test_jd_hydration_diagnostic_is_reported_per_company(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    sparse = _job("cpp", jd_raw="C++软件开发工程师 上海") | {
        "title": "C++软件开发工程师"
    }
    storage = _storage()

    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl_result(sparse),
        matcher=FakeMatcher(),
        jd_hydrator=lambda _job: {
            "detail": "",
            "status": "api_variant_unsupported",
            "source": "moka_official",
        },
    )

    company = result.company_results[0]
    assert company.status == "partial"
    assert company.detail_failure_count == 1
    assert company.jd_results[0]["status"] == "failed"
    assert company.jd_results[0]["failure_reason"] == "api_variant_unsupported"
    assert result.failure_reasons["api_variant_unsupported"] == 1
    with storage.session() as session:
        row = session.get(JobSnapshot, "cpp")
    assert row is not None
    assert row.capture_status == "failed"
    assert row.capture_failure_reason == "api_variant_unsupported"


def test_incomplete_pagination_admits_valid_rows_but_keeps_company_failed(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    storage = _storage()
    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl_result(
            _job("partial"), pages_seen=1, total_pages=2, has_more=True
        ),
        matcher=FakeMatcher(),
    )

    assert result.new_count == 1
    assert result.failed_company_count == 1
    assert result.failure_reasons["pagination_incomplete"] == 1
    assert "pagination_incomplete" not in result.rejection_reasons
    assert result.company_results[0].accepted_job_count == 1
    with storage.session() as session:
        assert session.get(JobSnapshot, "partial") is not None


def test_company_and_job_failures_are_isolated_and_concurrency_is_bounded(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        *[
            {
                "id": f"co-{index}",
                "name": f"Company {index}",
                "careers_url": "https://example.test/campus",
                "crawler": "fake",
                "integration_status": "connected",
            }
            for index in range(4)
        ],
    )
    lock = threading.Lock()
    active = 0
    max_active = 0

    def crawler(company: Any) -> CrawlResult:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.02)
            if company.name == "Company 0":
                raise RuntimeError("unavailable")
            return _crawl_result(_job(company.id))
        finally:
            with lock:
                active -= 1

    result = run_daily_pipeline(
        companies_path=config,
        storage=_storage(),
        crawler=crawler,
        matcher=FakeMatcher(),
        max_concurrency=2,
    )

    assert max_active <= 2
    assert result.selected_companies == 4
    assert result.failed_company_count == 1
    assert result.new_count == 3
    assert result.failure_reasons["crawler_failed"] == 1


def test_matcher_failure_does_not_discard_other_jobs(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    storage = _storage()
    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl_result(_job("good"), _job("bad")),
        matcher=FakeMatcher(failing_ids={"bad"}),
    )

    assert result.new_job_ids == ("bad", "good")
    assert result.failed_job_count == 1
    assert result.failed_job_ids == ("bad",)
    with storage.session() as session:
        good = session.get(JobSnapshot, "good")
        bad = session.get(JobSnapshot, "bad")
        bad_analysis = session.get(JobAnalysisSnapshot, "bad")
    assert good is not None
    assert bad is not None
    assert bad_analysis is not None
    assert bad_analysis.analysis_status == "pending"


def test_dry_run_never_opens_write_transaction(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    storage = _storage()
    write_calls = 0
    original_write_transaction = storage.write_transaction

    def tracked_write_transaction() -> Any:
        nonlocal write_calls
        write_calls += 1
        return original_write_transaction()

    storage.write_transaction = tracked_write_transaction  # type: ignore[method-assign]
    result = run_daily_pipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl_result(_job("dry")),
        matcher=FakeMatcher(),
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.new_count == 1
    assert result.written is False
    assert write_calls == 0
    with storage.session() as session:
        assert session.scalar(select(func.count()).select_from(CompanySnapshot)) == 0
        assert session.scalar(select(func.count()).select_from(JobSnapshot)) == 0


def test_matching_service_adapter_passes_the_completed_analyze_contract() -> None:
    calls: list[tuple[Any, Any, Any]] = []
    profile = {"direction": "cpp_software"}

    class FakeService:
        def analyze(self, job: Any, actual_profile: Any, *, existing_analysis: Any = None) -> Any:
            calls.append((job, actual_profile, existing_analysis))
            return {"analysis_status": "complete", "match_score": 70}

    adapter = MatchingServiceAdapter(FakeService(), profile)
    existing = {"analysis_status": "complete"}
    result = adapter.match({"id": "job-1"}, existing_analysis=existing)

    assert result["match_score"] == 70
    assert calls == [({"id": "job-1"}, profile, existing)]


def test_pipeline_adapts_a_real_analysis_outcome_shape(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    record = AnalysisRecord(
        job_id="outcome-job",
        analysis_status=AnalysisStatus.COMPLETE,
        match_score=77,
        analysis_version="test-v1",
        prompt_version="test-p1",
        content_fingerprint="a" * 64,
        profile_fingerprint="b" * 64,
    )
    outcome = AnalysisOutcome(
        decision=AnalysisDecision(
            action=DecisionAction.ANALYZE,
            reason="new",
            analysis_version="test-v1",
            prompt_version="test-p1",
            content_fingerprint="a" * 64,
            profile_fingerprint="b" * 64,
        ),
        result=record,
    )

    class OutcomeMatcher:
        def match(self, job: dict[str, Any], *, existing_analysis: Any = None) -> AnalysisOutcome:
            assert job["id"] == "outcome-job"
            assert existing_analysis is None
            return outcome

    result = run_daily_pipeline(
        companies_path=config,
        storage=_storage(),
        crawler=lambda _company: _crawl_result(_job("outcome-job")),
        matcher=OutcomeMatcher(),
    )

    assert result.new_count == 1


def test_matching_service_adapter_owns_versioned_reuse_and_persists_metadata(
    tmp_path: Path,
) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    storage = _storage()
    calls: list[Any] = []

    class VersionedService:
        def analyze(self, job: Any, profile: Any, *, existing_analysis: Any = None) -> Any:
            calls.append(existing_analysis)
            action = DecisionAction.REUSE if existing_analysis else DecisionAction.ANALYZE
            record = AnalysisRecord(
                job_id=job["id"],
                analysis_status=AnalysisStatus.COMPLETE,
                match_score=81,
                analysis_version="test-v2",
                prompt_version="prompt-v3",
                content_fingerprint="c" * 64,
                profile_fingerprint="d" * 64,
                input_tokens=12,
                output_tokens=4,
            )
            return AnalysisOutcome(
                decision=AnalysisDecision(
                    action=action,
                    reason="same" if existing_analysis else "new",
                    analysis_version="test-v2",
                    prompt_version="prompt-v3",
                    content_fingerprint="c" * 64,
                    profile_fingerprint="d" * 64,
                ),
                result=record,
            )

    pipeline = DailyRecruitmentPipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl_result(_job("versioned")),
        matcher=MatchingServiceAdapter(VersionedService(), {"skills": ["C++"]}),
    )

    first = pipeline.run(legacy=True)
    second = pipeline.run(legacy=True)

    assert first.new_count == 1
    assert second.reused_count == 1
    assert len(calls) == 2
    assert calls[0] is None
    assert calls[1]["analysis_version"] == "test-v2"
    assert calls[1]["profile_fingerprint"] == "d" * 64
    with storage.session() as session:
        row = session.get(JobAnalysisSnapshot, "versioned")
        assert row.analysis_version == "test-v2"
        assert row.prompt_version == "prompt-v3"
        assert row.input_tokens == 12
        assert row.output_tokens == 4


def test_companies_yaml_validation_and_dry_run_without_storage(tmp_path: Path) -> None:
    path = tmp_path / "companies.yaml"
    path.write_text("companies: nope\n", encoding="utf-8")
    with pytest.raises(ValueError):
        DailyRecruitmentPipeline(companies_path=path, matcher=FakeMatcher()).run(dry_run=True)


def test_daily_pipeline_can_limit_a_run_to_explicit_company_ids(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "company-1",
            "name": "Company 1",
            "careers_url": "https://example.test/campus/1",
            "crawler": "fake",
            "integration_status": "connected",
        },
        {
            "id": "company-2",
            "name": "Company 2",
            "careers_url": "https://example.test/campus/2",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    seen: list[str] = []

    def crawl(company):
        seen.append(company.id)
        return _crawl_result()

    result = run_daily_pipeline(
        companies_path=config,
        dry_run=True,
        crawler=crawl,
        matcher=FakeMatcher(),
        company_ids=["company-2"],
    )

    assert seen == ["company-2"]
    assert result.total_companies == 1
    assert result.selected_companies == 1
    assert result.skipped_companies == ()
    assert result.scoped_company_ids == ("company-2",)
    assert result.to_dict()["scoped_company_ids"] == ["company-2"]


def test_daily_pipeline_rejects_unknown_company_scope(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "company-1",
            "name": "Company 1",
            "careers_url": "https://example.test/campus/1",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )

    with pytest.raises(PipelineError, match="requested company IDs are not configured"):
        run_daily_pipeline(
            companies_path=config,
            dry_run=True,
            crawler=lambda _company: _crawl_result(),
            matcher=FakeMatcher(),
            company_ids=["missing-company"],
        )

    valid = _write_companies(
        tmp_path / "valid.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    result = run_daily_pipeline(
        companies_path=valid,
        crawler=lambda _company: _crawl_result(_job("memory")),
        matcher=FakeMatcher(),
        dry_run=True,
    )
    assert result.new_count == 1
    assert result.written is False


def test_matching_runs_concurrently_and_checkpoints_batches(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    storage = _storage()
    lock = threading.Lock()
    active = 0
    peak_active = 0
    write_calls = 0
    source_write_calls = 0
    original_write_transaction = storage.write_transaction
    original_upsert_source = CompanySourceRegistry.upsert_source
    original_record_attempt = CompanySourceRegistry.record_attempt

    class ConcurrentMatcher(FakeMatcher):
        def match(self, job: dict[str, Any], *, existing_analysis: Any = None) -> dict[str, Any]:
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                time.sleep(0.03)
                return super().match(job, existing_analysis=existing_analysis)
            finally:
                with lock:
                    active -= 1

    def tracked_write_transaction() -> Any:
        nonlocal write_calls
        write_calls += 1
        return original_write_transaction()

    storage.write_transaction = tracked_write_transaction  # type: ignore[method-assign]

    def tracked_upsert_source(self, *args: Any, **kwargs: Any) -> Any:
        nonlocal source_write_calls
        source_write_calls += 1
        return original_upsert_source(self, *args, **kwargs)

    def tracked_record_attempt(self, *args: Any, **kwargs: Any) -> Any:
        nonlocal source_write_calls
        source_write_calls += 1
        return original_record_attempt(self, *args, **kwargs)

    CompanySourceRegistry.upsert_source = tracked_upsert_source  # type: ignore[method-assign]
    CompanySourceRegistry.record_attempt = tracked_record_attempt  # type: ignore[method-assign]
    jobs = [_job(f"job-{index}") for index in range(8)]
    progress: list[tuple[str, int, int]] = []
    try:
        result = run_daily_pipeline(
            companies_path=config,
            storage=storage,
            crawler=lambda _company: _crawl_result(*jobs),
            matcher=ConcurrentMatcher(),
            match_max_concurrency=4,
            checkpoint_batch_size=3,
            progress_callback=lambda stage, completed, total: progress.append(
                (stage, completed, total)
            ),
        )
    finally:
        CompanySourceRegistry.upsert_source = original_upsert_source  # type: ignore[method-assign]
        CompanySourceRegistry.record_attempt = original_record_attempt  # type: ignore[method-assign]

    assert peak_active >= 2
    assert result.new_count == 8
    assert source_write_calls == 2
    assert write_calls - source_write_calls == 4
    assert [item for item in progress if item[0] == "matching"] == [
        ("matching", 3, 8),
        ("matching", 6, 8),
        ("matching", 8, 8),
    ]
    assert ("companies", 0, 1) in progress
    assert ("companies", 1, 1) in progress
    with storage.session() as session:
        assert session.scalar(select(func.count()).select_from(JobAnalysisSnapshot)) == 8


def test_provider_payment_error_trips_matching_circuit_breaker(tmp_path: Path) -> None:
    config = _write_companies(
        tmp_path / "companies.yaml",
        {
            "id": "co",
            "name": "Example Co",
            "careers_url": "https://example.test/campus",
            "crawler": "fake",
            "integration_status": "connected",
        },
    )
    storage = _storage()
    calls = 0
    lock = threading.Lock()

    class QuotaMatcher:
        def match(self, job: dict[str, Any], *, existing_analysis: Any = None) -> dict[str, Any]:
            nonlocal calls
            with lock:
                calls += 1
                current = calls
            if current > 1:
                time.sleep(0.05)
            return {
                "analysis_status": "failed",
                "error_code": "http_402",
                "summary": "quota exhausted",
            }

    with pytest.raises(PipelineError, match="http_402"):
        run_daily_pipeline(
            companies_path=config,
            storage=storage,
            crawler=lambda _company: _crawl_result(
                *[_job(f"job-{index}") for index in range(20)]
            ),
            matcher=QuotaMatcher(),
            match_max_concurrency=4,
            checkpoint_batch_size=3,
        )

    assert calls <= 5
    with storage.session() as session:
        persisted_failures = session.scalar(
            select(func.count())
            .select_from(JobAnalysisSnapshot)
            .where(JobAnalysisSnapshot.error_code == "http_402")
        )
    assert 1 <= persisted_failures <= 5


@pytest.mark.parametrize("url", ["https://example.test/", "https://example.test/#/jobs", "https://career.example.test/index.html"])
def test_recruitment_root_is_observed_before_discovery_failure(tmp_path: Path, url: str) -> None:
    config = _write_companies(tmp_path / "companies.yaml", {
        "id": "co", "name": "Example Co", "careers_url": url,
        "crawler": "render", "integration_status": "connected",
    })
    calls = []
    result = run_daily_pipeline(
        companies_path=config, dry_run=True, matcher=FakeMatcher(), jd_hydrator=None,
        crawler=lambda company: calls.append(company.careers_url) or _crawl_result(_job("one")),
    )
    assert calls == [url]
    assert result.new_count == 1


@pytest.mark.parametrize(("evidence", "expected"), [
    ({"completeness_known": False, "pagination_complete": False}, "pagination_unknown"),
    ({"completeness_known": True, "pagination_complete": False, "pages_seen": 1, "total_pages": 3}, "pagination_incomplete"),
    ({"completeness_known": True, "pagination_complete": True, "advertised_total": 2}, "pagination_incomplete"),
    ({"completeness_known": True, "pagination_complete": True}, None),
])
def test_production_evidence_transport_controls_acceptance(tmp_path: Path, monkeypatch, evidence, expected) -> None:
    import packages.pipeline.daily as daily

    config = _write_companies(tmp_path / "companies.yaml", {
        "id": "co", "name": "Example Co", "careers_url": "https://example.test/campus",
        "crawler": "fake", "integration_status": "connected",
    })
    monkeypatch.setattr(daily, "crawl_company_result_isolated", lambda *_a, **_k: {
        "jobs": [_job("one")], "pages_seen": 1, "has_more": False,
        "termination_reasons": ["observed_fixture"], **evidence,
        "crawl_source_url": "https://example.test/campus",
        "entry_attempts": [{"source_url": "https://example.test/", "raw_job_count": 0}],
    })
    result = run_daily_pipeline(
        companies_path=config,
        dry_run=True,
        matcher=FakeMatcher(),
        jd_hydrator=_fixture_hydrator,
    )
    company = result.company_results[0]
    assert company.raw_job_count == 1
    assert company.failure_reason == expected
    # Pagination describes company-level coverage. A row that independently
    # passes identity, origin, cohort and field audits remains admissible.
    assert company.accepted_job_count == 1
    assert company.crawl_evidence["termination_reasons"] == ["observed_fixture"]
    assert company.crawl_evidence["crawl_source_url"] == "https://example.test/campus"
    assert company.crawl_evidence["entry_attempts"][0]["raw_job_count"] == 0


def test_verified_empty_list_is_not_an_adapter_failure(tmp_path: Path) -> None:
    config = _write_companies(tmp_path / "companies.yaml", {
        "id": "co", "name": "Example Co", "careers_url": "https://example.test/campus",
        "crawler": "moka", "integration_status": "connected",
    })
    result = run_daily_pipeline(companies_path=config, dry_run=True, crawler=lambda _c: _crawl_result())
    assert result.failed_company_count == 0
    assert result.company_results[0].run_reason == "activity_empty"
    assert result.company_results[0].observed_job_ids == ()


@pytest.mark.parametrize("reply", ["short", {"detail": "short", "status": "complete"}, {"detail": "", "status": "timeout"}])
def test_rejected_hydration_always_has_exactly_one_diagnostic(tmp_path: Path, reply) -> None:
    config = _write_companies(tmp_path / "companies.yaml", {
        "id": "co", "name": "Example Co", "careers_url": "https://example.test/campus",
        "crawler": "fake", "integration_status": "connected",
    })
    job = {**_job("one", jd_raw=""), "title": "软件开发工程师"}
    result = run_daily_pipeline(
        companies_path=config, dry_run=True, crawler=lambda _c: _crawl_result(job),
        matcher=FakeMatcher(), jd_hydrator=lambda _job: reply, legacy=True,
    )
    company = result.company_results[0]
    assert len(company.jd_results) == 1
    assert sum(company.rejection_reasons.values()) == 1
    status = "timeout" if isinstance(reply, dict) and reply["status"] == "timeout" else "content_incomplete"
    assert company.jd_results[0]["status"] == status


def test_hydration_resolved_url_and_attempts_survive_pipeline(tmp_path: Path) -> None:
    config = _write_companies(tmp_path / "companies.yaml", {
        "id": "co", "name": "Example Co", "careers_url": "https://example.test/campus",
        "crawler": "fake", "integration_status": "connected",
    })
    job = {**_job("one", jd_raw=""), "title": "软件开发工程师"}
    detail = "岗位职责: 负责 Linux 平台 C++ 软件模块设计、开发和自动化测试。任职要求: 熟悉 C++, 多线程, 数据结构和软件工程实践, 有完整项目经验。"
    storage = _storage()
    result = run_daily_pipeline(
        companies_path=config, storage=storage, crawler=lambda _c: _crawl_result(job), matcher=FakeMatcher(),
        jd_hydrator=lambda _j: {"detail": detail, "status": "complete", "detail_url": "https://example.test/details/123", "source": "official_api", "attempts": ["list:resolved", "api:complete"], "identity_status": "matched", "identity_evidence": ["native_id:123"], "capture_evidence": _capture_evidence(detail, "https://example.test/details/123")},
    )
    diagnostic = result.company_results[0].jd_results[0]
    assert diagnostic["source"] == "official_api"
    assert diagnostic["attempts"] == ["list:resolved", "api:complete"]
    assert diagnostic["identity_status"] == "matched"
    assert diagnostic["identity_evidence"] == ["native_id:123"]
    with storage.session() as session:
        assert session.get(JobSnapshot, "one").detail_url == "https://example.test/details/123"


def test_sparse_known_other_direction_does_not_trigger_hydration(tmp_path: Path) -> None:
    config = _write_companies(tmp_path / "companies.yaml", {
        "id": "co", "name": "Example Co", "careers_url": "https://example.test/campus",
        "crawler": "fake", "integration_status": "connected",
    })
    calls = []
    job = {**_job("cpp", jd_raw=""), "title": "C++软件开发工程师"}
    result = run_daily_pipeline(
        companies_path=config, dry_run=True, crawler=lambda _c: _crawl_result(job),
        profile={"target_directions": ["llm_agent"]},
        jd_hydrator=lambda _j: calls.append(_j) or "",
    )
    assert calls == []
    assert result.company_results[0].filtered_reasons["direction_out"] == 1
