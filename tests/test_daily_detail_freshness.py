from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from packages.pipeline import daily
from packages.pipeline.daily import CrawlResult, DailyRecruitmentPipeline
from packages.recruitment_core import job_details
from packages.storage import JobAnalysisSnapshot, JobSnapshot, Storage


UTC = timezone.utc
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
URL = "https://example.test/jobs/native-1"
OLD_DETAIL = (
    "Responsibilities: maintain C++ services on Linux and investigate production failures. "
    "Requirements: C++ experience, Linux knowledge, and software engineering practice."
)
NEW_DETAIL = "C++ official detail changed after the latest source capture."


def _evidence(
    detail: str,
    url: str = URL,
    *,
    captured_at: datetime | None = NOW,
) -> dict[str, Any]:
    evidence = {
        "status": "complete",
        "method": "fixture_detail",
        "source_url": url,
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": sha256(detail.strip().encode("utf-8")).hexdigest(),
    }
    if captured_at is not None:
        evidence["captured_at"] = captured_at.isoformat()
    return evidence


def _sparse_job(**changes: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": "job-1",
        "native_job_id": "native-1",
        "source_job_id": "native-1",
        "title": "C++ Engineer",
        "city": "Shanghai",
        "detail_url": URL,
        "jd_url": URL,
        "jd_raw": "C++ Engineer Shanghai",
        "cohort": 2027,
        "cohort_status": "confirmed",
        "batch": "formal",
        "job_type": "campus",
    }
    job.update(changes)
    return job


def _work(job: dict[str, Any]) -> daily._CompanyWork:
    company = daily.PipelineCompany(
        id="co",
        name="Example Co",
        careers_url="https://example.test/campus",
        crawler_key="fake",
        integration_status="connected",
    )
    job.setdefault("company_id", company.id)
    job.setdefault("company", company.name)
    return daily._CompanyWork(company=company, accepted_jobs=[job])


def _stored(
    job: dict[str, Any],
    *,
    updated_at: datetime | None = NOW,
    captured_at: datetime | None = NOW,
) -> daily._ExistingSnapshot:
    return daily._ExistingSnapshot(
        job=SimpleNamespace(
            id=job["id"],
            company_id="co",
            title=job["title"],
            detail_url=job["detail_url"],
            jd_raw=OLD_DETAIL,
            capture_evidence=_evidence(
                OLD_DETAIL,
                job["detail_url"],
                captured_at=captured_at,
            ),
            native_job_id=job.get("native_job_id"),
            source_platform="fake",
            source_tenant=None,
            updated_at=updated_at,
            last_seen_at=NOW + timedelta(days=30),
        ),
        analysis=None,
    )


def _hydrator_reply(
    job: dict[str, Any],
    detail: str = OLD_DETAIL,
    *,
    captured_at: datetime = NOW,
) -> dict[str, Any]:
    url = job["detail_url"]
    return {
        "detail": detail,
        "status": "complete",
        "detail_url": url,
        "source": "fixture_detail",
        "identity_status": "matched",
        "capture_evidence": _evidence(detail, url, captured_at=captured_at),
    }


def _companies_file(path: Path) -> Path:
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


def _storage() -> Storage:
    storage = Storage.from_url("sqlite:///:memory:")
    storage.initialize()
    return storage


def _crawl(job: dict[str, Any]) -> CrawlResult:
    return CrawlResult(
        jobs=[job],
        source_url="https://example.test/campus",
        allowed_origins=["https://example.test"],
        pages_seen=1,
        total_pages=1,
        has_more=False,
    )


class _Matcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def match(self, job: dict[str, Any], *, existing_analysis: Any = None) -> dict[str, Any]:
        del existing_analysis
        self.calls.append(job["id"])
        return {"analysis_status": "complete", "match_score": 82}


def test_fresh_persisted_detail_reuses_across_runs_without_refresh_or_rescore(
    tmp_path: Path,
) -> None:
    config = _companies_file(tmp_path / "companies.yaml")
    storage = _storage()
    matcher = _Matcher()
    calls: list[str] = []

    def hydrate(job: dict[str, Any]) -> dict[str, Any]:
        calls.append(job["id"])
        return _hydrator_reply(job)

    first = DailyRecruitmentPipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(_sparse_job()),
        matcher=matcher,
        jd_hydrator=hydrate,
        clock=lambda: NOW,
    ).run()
    assert first.new_count == 1
    assert calls == ["job-1"]
    with storage.session() as session:
        stored = session.get(JobSnapshot, "job-1")
        assert stored is not None
        first_capture = dict(stored.capture_evidence)

    second = DailyRecruitmentPipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(_sparse_job()),
        matcher=matcher,
        jd_hydrator=lambda _job: (_ for _ in ()).throw(AssertionError("fresh detail must reuse")),
        clock=lambda: NOW + timedelta(hours=1),
    ).run()

    assert second.reused_count == 1
    assert len(matcher.calls) == 1
    # Existing title-first rows do not emit the legacy detail-reuse diagnostic.
    assert second.company_results[0].jd_results == ()
    with storage.session() as session:
        stored = session.get(JobSnapshot, "job-1")
        assert stored is not None
        assert stored.jd_raw == OLD_DETAIL
        assert stored.capture_evidence == first_capture
        assert stored.last_seen_at.replace(tzinfo=UTC) == NOW + timedelta(hours=1)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("title", "title_changed"),
        ("native_job_id", "native_id_changed"),
        ("detail_url", "detail_url_changed"),
    ],
)
def test_identity_change_forces_detail_refresh(
    change: str,
    reason: str,
) -> None:
    current = _sparse_job()
    stored_job = dict(current)
    if change == "title":
        current["title"] = "C++ Platform Engineer"
    elif change == "native_job_id":
        current["native_job_id"] = "native-2"
        current["source_job_id"] = "native-2"
    else:
        current["detail_url"] = "https://example.test/jobs/native-2"
        current["jd_url"] = current["detail_url"]
    work = _work(current)
    calls: list[str] = []
    pipeline = DailyRecruitmentPipeline(
        jd_hydrator=lambda job: calls.append(job["id"]) or _hydrator_reply(job),
        clock=lambda: NOW,
    )

    pipeline._prepare_job_details([work], {"job-1": _stored(stored_job)})

    assert calls == ["job-1"]
    assert work.jd_results[0]["detail_reuse"]["refresh_reason"] == reason


def test_stale_detail_refreshes_after_default_24_hour_ttl() -> None:
    current = _sparse_job()
    work = _work(current)
    calls: list[str] = []
    pipeline = DailyRecruitmentPipeline(
        jd_hydrator=lambda job: calls.append(job["id"])
        or _hydrator_reply(job, NEW_DETAIL, captured_at=NOW + timedelta(hours=25)),
        clock=lambda: NOW + timedelta(hours=25),
    )

    pipeline._prepare_job_details(
        [work], {"job-1": _stored(current, updated_at=NOW, captured_at=NOW)}
    )

    assert calls == ["job-1"]
    assert current["jd_raw"] == NEW_DETAIL
    assert work.jd_results[0]["detail_reuse"]["refresh_reason"] == "capture_stale"


def test_missing_persisted_capture_time_forces_refresh() -> None:
    current = _sparse_job()
    work = _work(current)
    calls: list[str] = []
    pipeline = DailyRecruitmentPipeline(
        jd_hydrator=lambda job: calls.append(job["id"]) or _hydrator_reply(job),
        clock=lambda: NOW,
    )

    pipeline._prepare_job_details(
        [work], {"job-1": _stored(current, updated_at=NOW, captured_at=None)}
    )

    assert calls == ["job-1"]
    assert work.jd_results[0]["detail_reuse"]["refresh_reason"] == "capture_time_missing"


def test_capture_identity_evidence_mismatch_forces_refresh() -> None:
    current = _sparse_job()
    work = _work(current)
    previous = _stored(current)
    previous.job.capture_evidence = {
        **_evidence(OLD_DETAIL),
        "identity_evidence": ["native_id:native-2", "title:C++ Engineer"],
    }
    calls: list[str] = []
    pipeline = DailyRecruitmentPipeline(
        jd_hydrator=lambda job: calls.append(job["id"]) or _hydrator_reply(job),
        clock=lambda: NOW,
    )

    pipeline._prepare_job_details([work], {"job-1": previous})

    assert calls == ["job-1"]
    assert work.jd_results[0]["detail_reuse"]["refresh_reason"] == "capture_identity_mismatch"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("native_job_id", "native-2"),
        ("native_id", "native-2"),
        ("post_id", "native-2"),
        ("title", "Other title"),
    ],
)
def test_top_level_capture_identity_conflict_is_rejected_without_nested_evidence(
    field_name: str,
    value: str,
) -> None:
    evidence = _evidence(OLD_DETAIL)
    evidence[field_name] = value

    assert not daily._capture_identity_matches_job(_sparse_job(), evidence)


def test_future_persisted_capture_time_forces_refresh_even_with_current_updated_at() -> None:
    current = _sparse_job()
    work = _work(current)
    calls: list[str] = []
    pipeline = DailyRecruitmentPipeline(
        jd_hydrator=lambda job: calls.append(job["id"]) or _hydrator_reply(job),
        clock=lambda: NOW,
    )

    pipeline._prepare_job_details(
        [work],
        {
            "job-1": _stored(
                current,
                updated_at=NOW,
                captured_at=NOW + timedelta(minutes=1),
            )
        },
    )

    assert calls == ["job-1"]
    assert work.jd_results[0]["detail_reuse"]["refresh_reason"] == "capture_time_future"


@pytest.mark.parametrize(
    ("captured_at", "reason"),
    [
        (None, "capture_time_missing"),
        (NOW + timedelta(minutes=1), "capture_time_future"),
    ],
)
def test_current_complete_receipt_without_valid_time_forces_refresh(
    captured_at: datetime | None,
    reason: str,
) -> None:
    current = _sparse_job(
        jd_raw=OLD_DETAIL,
        capture_evidence=_evidence(OLD_DETAIL, captured_at=captured_at),
    )
    original_capture = dict(current["capture_evidence"])
    work = _work(current)
    seen_capture_inputs: list[dict[str, Any]] = []

    def hydrate(job: dict[str, Any]) -> dict[str, Any]:
        seen_capture_inputs.append(dict(job["capture_evidence"]))
        return _hydrator_reply(job, NEW_DETAIL, captured_at=NOW)

    pipeline = DailyRecruitmentPipeline(jd_hydrator=hydrate, clock=lambda: NOW)
    pipeline._prepare_job_details([work], {})

    assert seen_capture_inputs == [{}]
    assert current["jd_raw"] == NEW_DETAIL
    assert work.jd_results[0]["detail_reuse"]["refresh_reason"] == reason
    assert original_capture.get("captured_at") == (
        None if captured_at is None else captured_at.isoformat()
    )


def test_stale_complete_input_uses_actual_hydrator_and_failed_refresh_preserves_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _companies_file(tmp_path / "companies.yaml")
    storage = _storage()
    matcher = _Matcher()
    base_now = datetime.now(UTC)

    first = DailyRecruitmentPipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(_sparse_job()),
        matcher=matcher,
        jd_hydrator=lambda job: _hydrator_reply(job, OLD_DETAIL, captured_at=base_now),
        clock=lambda: base_now,
    ).run(legacy=True)
    assert first.new_count == 1

    stale_capture = _evidence(
        OLD_DETAIL,
        captured_at=base_now - timedelta(hours=25),
    )
    current = _sparse_job(jd_raw=OLD_DETAIL, capture_evidence=stale_capture)
    request_capture_inputs: list[dict[str, Any]] = []
    request_identity_inputs: list[tuple[str, str, str, str]] = []
    network_urls: list[str] = []

    def mocked_render(url: str, **_kwargs: Any) -> str:
        network_urls.append(url)
        return ""

    monkeypatch.setattr(job_details, "render_page", mocked_render)

    def real_hydrator(job: dict[str, Any]) -> Any:
        request_capture_inputs.append(dict(job.get("capture_evidence") or {}))
        request_identity_inputs.append(
            (
                job["native_job_id"],
                job["title"],
                job["detail_url"],
                job["jd_url"],
            )
        )
        return job_details.fetch_full_job_description_result(job)

    second = DailyRecruitmentPipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(current),
        matcher=matcher,
        jd_hydrator=real_hydrator,
        clock=lambda: base_now + timedelta(hours=25),
    ).run(legacy=True)

    assert request_capture_inputs == [{}]
    assert request_identity_inputs == [("native-1", "C++ Engineer", URL, URL)]
    assert network_urls == [URL]
    assert second.failed_job_count == 1
    row = second.company_results[0].jd_results[0]
    assert row["status"] == "render_failed"
    assert row["source"] == "render"
    assert row["detail_reuse"]["preserved_old_value"] is True
    assert current["capture_evidence"] == stale_capture
    with storage.session() as session:
        stored = session.get(JobSnapshot, "job-1")
        assert stored is not None
        assert stored.jd_raw == OLD_DETAIL
        assert stored.capture_evidence["captured_at"] == base_now.isoformat()
        assert stored.updated_at.replace(tzinfo=UTC) == base_now
        assert stored.last_seen_at.replace(tzinfo=UTC) == base_now
        analysis = session.get(JobAnalysisSnapshot, "job-1")
        assert analysis is not None
        assert analysis.match_score == 82
        assert analysis.analysis_status == "complete"


def test_actual_hydrator_success_receipt_is_fresh_against_live_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html = """
    <html><body><main>
      <h1>C++ Engineer</h1>
      <div>Responsibilities: maintain C++ services on Linux and investigate production failures.
      Requirements: C++ experience, Linux knowledge, and software engineering practice.</div>
    </main></body></html>
    """
    monkeypatch.setattr(job_details, "render_page", lambda _url, **_kwargs: html)
    current = _sparse_job()
    work = _work(current)
    clock_reads: list[datetime] = []

    def live_clock() -> datetime:
        value = datetime.now(UTC)
        clock_reads.append(value)
        return value

    pipeline = DailyRecruitmentPipeline(
        jd_hydrator=job_details.fetch_full_job_description_result,
        clock=live_clock,
    )
    pipeline._prepare_job_details([work], {})

    assert len(clock_reads) >= 2
    assert current["jd_raw"]
    assert current["capture_evidence"]["captured_at"]
    assert work.jd_results[0]["status"] == "complete"
    assert work.jd_results[0]["source"] == "render"


def test_hydration_identity_diagnostics_are_transparent() -> None:
    current = _sparse_job()
    work = _work(current)
    identity_diagnostic = {
        "requested_job_id": "native-1",
        "requested_title": "C++ Engineer",
        "observed_job_ids": ["native-1"],
        "observed_titles": ["C++ Engineer"],
        "source": "official_api",
        "url": URL,
        "failed_step": None,
        "exception": {"type": None, "detail": None},
    }

    def hydrate(job: dict[str, Any]) -> dict[str, Any]:
        result = _hydrator_reply(job, NEW_DETAIL)
        result["identity_diagnostic"] = identity_diagnostic
        result["error_detail"] = "identity response matched"
        result["capture_evidence"]["identity_diagnostic"] = identity_diagnostic
        return result

    pipeline = DailyRecruitmentPipeline(jd_hydrator=hydrate, clock=lambda: NOW)
    pipeline._prepare_job_details([work], {})

    diagnostic = work.jd_results[0]
    assert diagnostic["identity_diagnostic"] == identity_diagnostic
    assert diagnostic["error_detail"] == "identity response matched"
    assert diagnostic["capture_evidence"]["identity_diagnostic"] == identity_diagnostic


def test_current_complete_official_content_wins_over_stored_detail() -> None:
    current = _sparse_job(
        jd_raw=NEW_DETAIL,
        capture_evidence=_evidence(
            NEW_DETAIL,
            captured_at=NOW + timedelta(hours=30),
        ),
    )
    work = _work(current)
    calls: list[str] = []
    pipeline = DailyRecruitmentPipeline(
        jd_hydrator=lambda job: calls.append(job["id"]) or _hydrator_reply(job),
        clock=lambda: NOW + timedelta(hours=30),
    )

    pipeline._prepare_job_details([work], {"job-1": _stored(current, updated_at=NOW)})

    assert calls == []
    assert current["jd_raw"] == NEW_DETAIL


def test_current_receipt_does_not_use_updated_at_as_missing_stored_capture_time(
    tmp_path: Path,
) -> None:
    config = _companies_file(tmp_path / "companies.yaml")
    storage = _storage()
    current = _sparse_job(
        jd_raw=OLD_DETAIL,
        capture_evidence=_evidence(OLD_DETAIL, captured_at=NOW),
    )
    matcher = _Matcher()

    first = DailyRecruitmentPipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(_sparse_job()),
        matcher=matcher,
        jd_hydrator=lambda job: _hydrator_reply(job, OLD_DETAIL, captured_at=NOW),
        clock=lambda: NOW,
    ).run()
    assert first.new_count == 1

    with storage.session() as session:
        stored = session.get(JobSnapshot, "job-1")
        assert stored is not None
        stored.capture_evidence = {}
        stored.updated_at = NOW + timedelta(hours=1)
        session.commit()

    second = DailyRecruitmentPipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(current),
        matcher=matcher,
        jd_hydrator=lambda _job: (_ for _ in ()).throw(
            AssertionError("fresh current receipt must not refresh")
        ),
        clock=lambda: NOW,
    ).run()

    assert second.reused_count == 1
    assert second.company_results[0].jd_results == ()
    with storage.session() as session:
        stored = session.get(JobSnapshot, "job-1")
        assert stored is not None
        # Existing title-first rows only advance presence state; the listing
        # receipt does not replace persisted detail evidence.
        assert stored.capture_evidence == {}
        assert stored.last_seen_at.replace(tzinfo=UTC) == NOW


def test_repeated_list_capture_does_not_renew_persisted_capture_time(tmp_path: Path) -> None:
    config = _companies_file(tmp_path / "companies.yaml")
    storage = _storage()
    matcher = _Matcher()

    first = DailyRecruitmentPipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(_sparse_job()),
        matcher=matcher,
        jd_hydrator=lambda job: _hydrator_reply(job),
        clock=lambda: NOW,
    ).run()
    assert first.new_count == 1
    with storage.session() as session:
        stored = session.get(JobSnapshot, "job-1")
        assert stored is not None
        first_capture = dict(stored.capture_evidence)

    repeated_capture = {
        **_evidence(OLD_DETAIL, captured_at=NOW),
        "list_seen_at": (NOW + timedelta(hours=25)).isoformat(),
    }
    second = DailyRecruitmentPipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(
            _sparse_job(jd_raw=OLD_DETAIL, capture_evidence=repeated_capture)
        ),
        matcher=matcher,
        jd_hydrator=lambda _job: (_ for _ in ()).throw(
            AssertionError("identical list content must not refresh")
        ),
        clock=lambda: NOW + timedelta(hours=1),
    ).run()

    assert second.reused_count == 1
    assert len(matcher.calls) == 1
    assert second.company_results[0].jd_results == ()
    with storage.session() as session:
        stored = session.get(JobSnapshot, "job-1")
        assert stored is not None
        assert stored.capture_evidence == first_capture
        assert stored.last_seen_at.replace(tzinfo=UTC) == NOW + timedelta(hours=1)


def test_run_preserves_source_job_id_as_native_identity_across_runs(tmp_path: Path) -> None:
    config = _companies_file(tmp_path / "companies.yaml")
    storage = _storage()
    matcher = _Matcher()

    def crawler_job() -> dict[str, Any]:
        job = _sparse_job()
        job.pop("native_job_id")
        job["source_job_id"] = "official-1"
        return job

    first = DailyRecruitmentPipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(crawler_job()),
        matcher=matcher,
        jd_hydrator=lambda job: _hydrator_reply(job),
        clock=lambda: NOW,
    ).run()
    assert first.new_count == 1

    with storage.session() as session:
        stored = session.get(JobSnapshot, "job-1")
        assert stored is not None
        assert stored.native_job_id == "official-1"

    second = DailyRecruitmentPipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(crawler_job()),
        matcher=matcher,
        jd_hydrator=lambda _job: (_ for _ in ()).throw(
            AssertionError("fresh stored detail must reuse")
        ),
        clock=lambda: NOW + timedelta(hours=1),
    ).run()

    assert second.reused_count == 1
    assert len(matcher.calls) == 1


def test_failed_stale_refresh_does_not_overwrite_valid_old_value(tmp_path: Path) -> None:
    config = _companies_file(tmp_path / "companies.yaml")
    storage = _storage()
    matcher = _Matcher()
    calls: list[str] = []

    first = DailyRecruitmentPipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(_sparse_job()),
        matcher=matcher,
        jd_hydrator=lambda job: _hydrator_reply(job),
        clock=lambda: NOW,
    ).run(legacy=True)
    assert first.new_count == 1

    def fail(_job: dict[str, Any]) -> dict[str, Any]:
        calls.append("refresh")
        return {"detail": "", "status": "fetch_failed", "capture_evidence": {}}

    second = DailyRecruitmentPipeline(
        companies_path=config,
        storage=storage,
        crawler=lambda _company: _crawl(_sparse_job()),
        matcher=matcher,
        jd_hydrator=fail,
        clock=lambda: NOW + timedelta(hours=25),
    ).run(legacy=True)

    assert calls == ["refresh"]
    assert second.failed_job_count == 1
    assert second.failure_reasons == {"detail_refresh_failed": 1}
    assert len(matcher.calls) == 1
    diagnostic = second.company_results[0].jd_results[0]["detail_reuse"]
    assert diagnostic["refresh_reason"] == "capture_stale"
    assert diagnostic["preserved_old_value"] is True
    assert second.company_results[0].jd_results[0]["status"] == "fetch_failed"
    with storage.session() as session:
        stored = session.get(JobSnapshot, "job-1")
        assert stored is not None
        assert stored.jd_raw == OLD_DETAIL
        assert stored.capture_evidence == _evidence(OLD_DETAIL)
        assert stored.updated_at.replace(tzinfo=UTC) == NOW
        analysis = session.get(JobAnalysisSnapshot, "job-1")
        assert analysis is not None
        assert analysis.match_score == 82
        assert analysis.analysis_status == "complete"


def test_run_cache_and_cross_run_storage_reuse_are_distinct() -> None:
    calls: list[str] = []

    def reusable_reply(job: dict[str, Any]) -> dict[str, Any]:
        result = _hydrator_reply(job)
        result["identity_evidence"] = ["native_id:123", "title:C++ Engineer"]
        result["capture_evidence"] = {
            **result["capture_evidence"],
            "method": "official_api",
        }
        return result

    pipeline = DailyRecruitmentPipeline(
        max_concurrency=4,
        jd_hydrator=lambda job: calls.append(job["id"]) or reusable_reply(job),
        clock=lambda: NOW,
    )

    works = []
    shared_url = "https://jobs.bytedance.com/campus/position/123/detail"
    for index in range(2):
        job = _sparse_job(
            id=f"job-{index}",
            native_job_id="123",
            source_job_id="123",
            detail_url=shared_url,
            jd_url=shared_url,
        )
        works.append(_work(job))
    pipeline._prepare_job_details(works, {})

    assert len(calls) == 1
    modes = [item["detail_reuse"].get("mode") for work in works for item in work.jd_results]
    assert "leader" in modes
    assert any(mode in {"cache", "singleflight"} for mode in modes)
    assert all(item["detail_reuse"].get("mode") != "storage" for work in works for item in work.jd_results)
