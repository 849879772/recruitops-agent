from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import packages.pipeline.daily as daily
from packages.pipeline import DailyRecruitmentPipeline, PipelineError, PipelineInterrupted
from packages.storage import JobAnalysisSnapshot, JobSnapshot
from test_title_first_pipeline import (
    FakeHydrator, FakeMatcher, _company, _config, _crawl, _detail, _job, _storage,
)


def _pipeline(tmp_path, **kwargs):
    config = tmp_path / "companies.yaml"
    if not config.exists():
        _config(config, _company("a"), _company("b"))
    return DailyRecruitmentPipeline(
        companies_path=config,
        checkpoint_path=tmp_path / "checkpoint.json",
        crawler=kwargs.pop("crawler", lambda company: _crawl(_job(company.id, "C++ Engineer"))),
        jd_hydrator=kwargs.pop("jd_hydrator", FakeHydrator()),
        matcher=kwargs.pop("matcher", FakeMatcher()),
        max_concurrency=1,
        **kwargs,
    )


def _rows(storage):
    with storage.session() as session:
        return {row.id: row for row in session.scalars(select(JobSnapshot))}


def test_stop_during_company_crawl_commits_admitted_rows_before_checkpoint(tmp_path):
    storage = _storage(tmp_path)
    stop = Event()

    def crawler(company):
        stop.set()
        return _crawl(_job(company.id, "C++ Engineer"))

    pipeline = _pipeline(tmp_path, storage=storage, stop_requested=stop, crawler=crawler)
    with pytest.raises(PipelineInterrupted):
        pipeline.run()
    row = _rows(storage)["a"]
    assert row.capture_status == "pending"
    assert row.match_score is None
    assert row.jd_raw is None
    checkpoint = json.loads(pipeline.checkpoint_path.read_text(encoding="utf-8"))
    assert set(checkpoint["companies"]) == {"a"}
    assert checkpoint["companies"]["a"]["status"] == "complete"


def test_crash_after_list_commit_before_checkpoint_replays_idempotently(tmp_path, monkeypatch):
    storage = _storage(tmp_path)
    pipeline = _pipeline(tmp_path, storage=storage)

    def fail(*args, **kwargs):
        assert _rows(storage)["a"].capture_status == "pending"
        raise PipelineError("checkpoint unavailable")

    monkeypatch.setattr(pipeline, "_write_company_checkpoint", fail)
    with pytest.raises(PipelineError, match="checkpoint unavailable"):
        pipeline.run()
    checkpoint = json.loads(pipeline.checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["companies"] == {}
    hydrator = FakeHydrator()
    resumed = _pipeline(tmp_path, storage=storage, resume_from_checkpoint=True, jd_hydrator=hydrator)
    resumed.run()
    assert set(_rows(storage)) == {"a", "b"}
    assert sorted(hydrator.calls) == ["a", "b"]
    assert all(row.match_score == 87 for row in _rows(storage).values())


def test_database_failure_does_not_advance_company_checkpoint(tmp_path, monkeypatch):
    storage = _storage(tmp_path)
    pipeline = _pipeline(tmp_path, storage=storage)

    def fail(*args, **kwargs):
        raise OSError("database is read-only")

    monkeypatch.setattr(daily, "upsert_job_snapshot", fail)
    with pytest.raises(OSError, match="read-only"):
        pipeline.run()
    checkpoint = json.loads(pipeline.checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["companies"] == {}
    assert _rows(storage) == {}


def test_stop_during_details_commits_before_receipt_and_resumes_without_refetch(tmp_path):
    storage = _storage(tmp_path)
    stop = Event()

    def hydrate(job):
        stop.set()
        return _detail(job)

    pipeline = _pipeline(tmp_path, storage=storage, stop_requested=stop,
                         jd_hydrator=hydrate, detail_max_concurrency=1)
    with pytest.raises(PipelineInterrupted):
        pipeline.run()
    rows = _rows(storage)
    assert rows["a"].capture_status == "complete"
    assert rows["b"].capture_status == "pending"
    with storage.session() as session:
        assert session.get(JobAnalysisSnapshot, "a").analysis_status == "pending"
    receipt = json.loads(pipeline._hydration_checkpoint_path("a").read_text(encoding="utf-8"))
    assert receipt["detail_success_count"] == 1
    hydrator = FakeHydrator()
    _pipeline(tmp_path, storage=storage, jd_hydrator=hydrator, resume_from_checkpoint=True).run()
    assert hydrator.calls == ["b"]
    assert all(row.match_score == 87 for row in _rows(storage).values())


def test_detail_commit_survives_sidecar_write_failure(tmp_path, monkeypatch):
    storage = _storage(tmp_path)
    pipeline = _pipeline(tmp_path, storage=storage, checkpoint_batch_size=1, detail_max_concurrency=1)

    def fail(work, **kwargs):
        assert _rows(storage)[work.company.id].capture_status == "complete"
        raise PipelineError("sidecar unavailable")

    monkeypatch.setattr(pipeline, "_write_hydration_checkpoint", fail)
    with pytest.raises(PipelineError, match="sidecar unavailable"):
        pipeline.run()
    hydrator = FakeHydrator()
    _pipeline(tmp_path, storage=storage, jd_hydrator=hydrator, resume_from_checkpoint=True).run()
    assert hydrator.calls == ["b"]


@pytest.mark.parametrize("damage", ["json", "infinite_count"])
def test_corrupt_sidecar_does_not_discard_other_company_receipts(tmp_path, damage):
    storage = _storage(tmp_path)
    pipeline = _pipeline(tmp_path, storage=storage)
    pipeline.run()
    sidecar = pipeline._hydration_checkpoint_path("a")
    content = "{damaged"
    if damage == "infinite_count":
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        payload["detail_success_count"] = float("inf")
        content = json.dumps(payload)
    sidecar.write_text(content, encoding="utf-8")
    resumed = _pipeline(tmp_path, storage=storage, resume_from_checkpoint=True)
    _, works = resumed._load_company_checkpoint(daily.load_companies(resumed.companies_path), dry_run=False)
    assert not works["a"].hydration_results
    assert works["b"].hydration_results


def test_atomic_checkpoint_retries_and_keeps_previous_valid_backup(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    daily._atomic_checkpoint(path, {"version": 1, "value": "old"})
    replace = Path.replace
    attempts = []

    def intermittent(self, target):
        if target == path:
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError("busy scanner")
        return replace(self, target)

    monkeypatch.setattr(Path, "replace", intermittent)
    daily._atomic_checkpoint(path, {"version": 1, "value": "new"})
    assert len(attempts) == 3
    assert json.loads(path.read_text())["value"] == "new"
    assert json.loads(path.with_name("receipt.json.bak").read_text())["value"] == "old"


def test_permanent_checkpoint_failure_stops_after_bounded_retries(tmp_path, monkeypatch):
    path = tmp_path / "receipt.json"
    attempts = []

    def fail(self, target):
        attempts.append(1)
        raise PermissionError("read-only directory")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(PipelineError, match="stopping without advancing recovery"):
        daily._atomic_checkpoint(path, {"version": 1})
    assert len(attempts) == 3
    assert not path.exists()


def test_resume_uses_valid_backup_but_rejects_unverifiable_scope(tmp_path):
    storage = _storage(tmp_path)
    pipeline = _pipeline(tmp_path, storage=storage)
    pipeline.run()
    pipeline.checkpoint_path.write_text("broken", encoding="utf-8")
    resumed = _pipeline(tmp_path, storage=storage, resume_from_checkpoint=True)
    entries, _ = resumed._load_company_checkpoint(daily.load_companies(resumed.companies_path), dry_run=False)
    assert set(entries) == {"a"}
    payload = json.loads(pipeline.checkpoint_path.with_name("checkpoint.json.bak").read_text(encoding="utf-8"))
    payload["scope_digest"] = "different scope"
    pipeline.checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PipelineError, match="frozen scope"):
        resumed._load_company_checkpoint(daily.load_companies(resumed.companies_path), dry_run=False)
    payload["version"] = 999
    pipeline.checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PipelineError, match="unsupported format"):
        resumed._load_company_checkpoint(daily.load_companies(resumed.companies_path), dry_run=False)
    pipeline.checkpoint_path.write_text("broken", encoding="utf-8")
    pipeline.checkpoint_path.with_name("checkpoint.json.bak").write_text("broken", encoding="utf-8")
    with pytest.raises(PipelineError, match="unreadable"):
        resumed._load_company_checkpoint(daily.load_companies(resumed.companies_path), dry_run=False)


def test_company_progress_counts_failed_attempts_and_does_not_double_count_retries(tmp_path):
    storage = _storage(tmp_path)
    progress = []

    def crawler(company):
        return _crawl(_job(company.id, "C++ Engineer"), complete=company.id == "b")

    callback = lambda stage, completed, total: progress.append((stage, completed, total))
    _pipeline(tmp_path, storage=storage, crawler=crawler, progress_callback=callback,
              checkpoint_batch_size=1).run()
    assert [item for item in progress if item[0] == "companies"][-1] == ("companies", 2, 2)
    progress.clear()
    _pipeline(tmp_path, storage=storage, crawler=crawler, progress_callback=callback,
              checkpoint_batch_size=1, resume_from_checkpoint=True).run()
    assert {item[1] for item in progress if item[0] == "companies"} == {2}


def test_transient_company_retry_follows_first_pass_and_commits_one_unique_count(tmp_path, monkeypatch):
    storage = _storage(tmp_path)
    calls = []
    progress = []
    monkeypatch.setattr(daily, "_SHORT_RETRY_COOLDOWN_SECONDS", 0)

    def crawler(company):
        calls.append(company.id)
        if company.id == "a" and calls.count("a") == 1:
            return daily.CrawlResult(error_code="connection_error")
        return _crawl(_job(company.id, "C++ Engineer"))

    pipeline = _pipeline(
        tmp_path, storage=storage, crawler=crawler,
        progress_callback=lambda *args: progress.append(args), checkpoint_batch_size=1,
    )
    pipeline.run()
    assert calls == ["a", "b", "a"]
    assert max(value for stage, value, _ in progress if stage == "companies") == 2
    assert pipeline.complete_company_count == 2
    entries = json.loads(pipeline.checkpoint_path.read_text(encoding="utf-8"))["companies"]
    assert entries["a"]["attempts"] == 2
    assert entries["b"]["attempts"] == 1
    assert set(_rows(storage)) == {"a", "b"}


def test_company_retry_budget_is_three_and_permanent_errors_do_not_retry(tmp_path, monkeypatch):
    storage = _storage(tmp_path)
    monkeypatch.setattr(daily, "_SHORT_RETRY_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(daily, "_DELAYED_RETRY_COOLDOWN_SECONDS", 0)
    calls = []

    def crawler(company):
        calls.append(company.id)
        if company.id == "a":
            return daily.CrawlResult(error_code="connection_error")
        return daily.CrawlResult(error_code="login_required")

    pipeline = _pipeline(tmp_path, storage=storage, crawler=crawler)
    pipeline.run()
    assert calls == ["a", "b", "a", "a"]
    entries = json.loads(pipeline.checkpoint_path.read_text(encoding="utf-8"))["companies"]
    assert entries["a"]["attempts"] == 3
    assert entries["b"]["attempts"] == 1
    assert pipeline.complete_company_count == 0

    resumed_calls = []

    def resumed_crawler(company):
        resumed_calls.append(company.id)
        return daily.CrawlResult(error_code="login_required")

    _pipeline(
        tmp_path, storage=storage, crawler=resumed_crawler,
        resume_from_checkpoint=True,
    ).run()
    assert resumed_calls == ["b"]
    assert json.loads(pipeline.checkpoint_path.read_text(encoding="utf-8"))["companies"]["a"]["attempts"] == 3


def test_retry_stage_budget_leaves_failed_receipt_for_future_run(tmp_path, monkeypatch):
    storage = _storage(tmp_path)
    monkeypatch.setattr(daily, "_SHORT_RETRY_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(daily, "_COMPANY_RETRY_STAGE_MAX_TASKS", 0)
    calls = []

    def crawler(company):
        calls.append(company.id)
        return daily.CrawlResult(error_code="connection_error")

    pipeline = _pipeline(tmp_path, storage=storage, crawler=crawler)
    pipeline.run()
    assert calls == ["a", "b"]
    entries = json.loads(pipeline.checkpoint_path.read_text(encoding="utf-8"))["companies"]
    assert entries["a"]["status"] == entries["b"]["status"] == "failed"
    assert entries["a"]["attempts"] == entries["b"]["attempts"] == 1


def test_partial_company_rows_survive_failed_delayed_retry(tmp_path, monkeypatch):
    storage = _storage(tmp_path)
    _config(tmp_path / "companies.yaml", _company("a"))
    monkeypatch.setattr(daily, "_DELAYED_RETRY_COOLDOWN_SECONDS", 0)
    calls = []

    def crawler(company):
        calls.append(company.id)
        if len(calls) == 1:
            first = _crawl(_job(company.id, "C++ Engineer"), complete=False)
            return replace(first, termination_reasons=("page_request_failed",))
        return daily.CrawlResult(error_code="timeout")

    pipeline = _pipeline(tmp_path, storage=storage, crawler=crawler)
    pipeline.run()
    assert calls == ["a", "a"]
    entry = json.loads(pipeline.checkpoint_path.read_text(encoding="utf-8"))["companies"]["a"]
    assert entry["status"] == "partial"
    assert len(entry["work"]["accepted_jobs"]) == 1
    assert "a" in _rows(storage)


def test_host_fair_admission_does_not_fill_slots_with_one_domain(tmp_path):
    config = tmp_path / "companies.yaml"
    same_host = [_company(key) for key in ("a", "b", "c")]
    different_host = {**_company("d"), "careers_url": "https://other.example.test/jobs"}
    _config(config, *same_host, different_host)
    other_started = Event()
    calls = []
    lock = Lock()

    def crawler(company):
        with lock:
            calls.append(company.id)
        if company.id in {"a", "b"}:
            assert other_started.wait(3), "another domain was starved"
        if company.id == "d":
            other_started.set()
        return _crawl(_job(company.id, "C++ Engineer"))

    pipeline = DailyRecruitmentPipeline(
        companies_path=config, crawler=crawler, max_concurrency=3,
    )
    pipeline._crawl_companies(daily.load_companies(config))
    assert set(calls[:3]) == {"a", "b", "d"}


def test_company_evidence_keeps_bounded_resource_wait_timing(tmp_path):
    pipeline = _pipeline(tmp_path, crawler=lambda company: {
        "jobs": [], "source_url": company.careers_url,
        "pages_seen": 1, "total_pages": 1,
        "pagination_complete": True, "completeness_known": True,
        "resource_timing": {
            "browser_wait_seconds": 1.25, "http_wait_seconds": float("inf"),
            "browser_acquisitions": 2, "http_acquisitions": -1,
            "unbounded_detail": "ignored",
        },
    })
    company = daily.load_companies(pipeline.companies_path)[0]
    work = pipeline._crawl_one(company)
    assert work.crawl_evidence["resource_timing"] == {
        "browser_wait_seconds": 1.25, "browser_acquisitions": 2,
    }


@pytest.mark.parametrize("phase", ["detail", "score"])
def test_time_flush_commits_small_batch_while_another_worker_is_still_running(tmp_path, monkeypatch, phase):
    storage = _storage(tmp_path)
    committed = Event()

    def hydrate(job):
        if phase == "detail" and job["id"] == "b":
            assert committed.wait(3), "completed detail batch was not flushed on time"
        return _detail(job)

    class Matcher(FakeMatcher):
        def match(self, job, *, existing_analysis=None):
            if phase == "score" and job["id"] == "b":
                assert committed.wait(3), "completed scoring batch was not flushed on time"
            return super().match(job, existing_analysis=existing_analysis)

    pipeline = _pipeline(tmp_path, storage=storage, jd_hydrator=hydrate, matcher=Matcher(),
                         detail_max_concurrency=2, match_max_concurrency=2,
                         checkpoint_batch_size=25, checkpoint_interval_seconds=0.02)
    persist = pipeline._persist_title_first

    def observed(**kwargs):
        value = persist(**kwargs)
        row = _rows(storage).get("a")
        if row and ((phase == "detail" and row.capture_status == "complete")
                    or (phase == "score" and row.match_score == 87)):
            committed.set()
        return value

    monkeypatch.setattr(pipeline, "_persist_title_first", observed)
    result = pipeline.run()
    assert committed.is_set()
    assert result.failed_job_count == 0
    assert result.scored_count == 2


@pytest.mark.parametrize("status", [401, 402, 403])
def test_fatal_provider_error_stops_scoring_but_keeps_captured_rows(tmp_path, status):
    storage = _storage(tmp_path)
    calls = []

    def matcher(job):
        calls.append(job["id"])
        error = RuntimeError("provider authorization failed")
        error.response = SimpleNamespace(status_code=status)
        raise error

    with pytest.raises(PipelineError, match=f"http_{status}"):
        _pipeline(tmp_path, storage=storage, matcher=matcher, match_max_concurrency=1).run()
    assert len(calls) == 1
    assert all(row.capture_status == "complete" for row in _rows(storage).values())
    assert all(row.match_score is None for row in _rows(storage).values())


def test_missing_score_is_failed_and_does_not_become_zero(tmp_path):
    storage = _storage(tmp_path)
    result = _pipeline(tmp_path, storage=storage,
                       matcher=lambda job: {"analysis_status": "complete", "summary": "no score"}).run()
    assert result.scored_count == 0
    assert result.failure_reasons["invalid_match_score"] == 2
    assert all(row.match_score is None for row in _rows(storage).values())


def test_listing_replay_cannot_overwrite_existing_detail_or_score(tmp_path):
    storage = _storage(tmp_path)
    pipeline = _pipeline(tmp_path, storage=storage)
    pipeline.run()
    before = _rows(storage)["a"]
    company = daily.load_companies(pipeline.companies_path)[0]
    # A stale pre-run view is realistic after a crash or overlapping worker.
    work = daily._CompanyWork(company=company, accepted_jobs=[_job("a", "C++ Engineer")])
    pipeline._persist_title_first_listing(work, (), dry_run=False)
    after = _rows(storage)["a"]
    assert (after.jd_raw, after.capture_status, after.match_score, after.availability_status) == (
        before.jd_raw, "complete", 87, "active",
    )
    with storage.session() as session:
        assert session.get(JobAnalysisSnapshot, "a").match_score == 87


def test_default_concurrency_pools_are_independent(tmp_path):
    pipeline = DailyRecruitmentPipeline(companies_path=tmp_path / "unused.yaml")
    assert (pipeline.max_concurrency, pipeline.detail_max_concurrency, pipeline.match_max_concurrency) == (10, 10, 6)
    pipeline = DailyRecruitmentPipeline(max_concurrency=1, detail_max_concurrency=3, match_max_concurrency=2)
    assert (pipeline.max_concurrency, pipeline.detail_max_concurrency, pipeline.match_max_concurrency) == (1, 3, 2)


def test_new_detail_exclusion_retains_evidence_and_leaves_existing_job_active(tmp_path):
    storage = _storage(tmp_path)
    _config(tmp_path / "companies.yaml", _company("a"))
    _pipeline(tmp_path, storage=storage).run()
    old = _rows(storage)["a"]
    _config(tmp_path / "companies.yaml", _company("a"), _company("b"))

    def internship(job):
        result = _detail(job)
        result["detail"] = "This is an internship position."
        result["capture_evidence"]["content_sha256"] = sha256(result["detail"].encode()).hexdigest()
        return result

    result = _pipeline(tmp_path, storage=storage, jd_hydrator=internship).run()
    assert result.scored_count == 0
    rows = _rows(storage)
    assert (rows["a"].jd_raw, rows["a"].match_score, rows["a"].availability_status) == (
        old.jd_raw, 87, "active",
    )
    assert rows["b"].availability_status == "inactive"
    assert rows["b"].capture_failure_reason.startswith("excluded:")
    assert rows["b"].jd_raw == "This is an internship position."
    _pipeline(tmp_path, storage=storage, resume_from_checkpoint=True).run()
    assert _rows(storage)["b"].availability_status == "inactive"


def test_stale_exclusion_candidate_cannot_deactivate_completed_old_job(tmp_path):
    storage = _storage(tmp_path)
    pipeline = _pipeline(tmp_path, storage=storage)
    pipeline.run()
    old = _rows(storage)["a"]
    company = daily.load_companies(pipeline.companies_path)[0]
    candidate = daily._TitleFirstCandidate(
        work=daily._CompanyWork(company=company), title_key="C++ Engineer",
        job={**_job("a", "C++ Engineer"), "jd_raw": "new excluded text",
             "capture_status": "complete", "capture_failure_reason": "excluded:internship",
             "availability_status": "inactive"},
    )
    pipeline._persist_title_first(companies=(), existing_updates=(), new_candidates=(candidate,),
                                  scored_candidates=(), inactive_ids=(), dry_run=False)
    row = _rows(storage)["a"]
    assert (row.jd_raw, row.match_score, row.availability_status) == (old.jd_raw, 87, "active")


def test_partial_resume_uses_current_detail_url_for_existing_job_identity(tmp_path):
    storage = _storage(tmp_path)
    _config(tmp_path / "companies.yaml", _company("a"))
    calls = []
    current_url = "https://jobs.example.test/campus/position/a-old"

    def crawler(company):
        return _crawl(_job("a", "C++ Engineer", detail_url=current_url), complete=False)

    def hydrate(job):
        calls.append(job["detail_url"])
        if job["detail_url"].endswith("old"):
            return {"status": "failed", "error_code": "detail_404"}
        return _detail(job)

    _pipeline(tmp_path, storage=storage, crawler=crawler, jd_hydrator=hydrate).run()
    current_url = "https://jobs.example.test/campus/position/a-fixed"
    _pipeline(tmp_path, storage=storage, crawler=crawler, jd_hydrator=hydrate,
              resume_from_checkpoint=True).run()
    assert [url.rsplit("/", 1)[-1] for url in calls] == ["a-old", "a-fixed"]
    assert _rows(storage)["a"].detail_url == current_url
    assert _rows(storage)["a"].capture_status == "complete"


def test_invalid_main_checkpoint_counter_is_explicit_error(tmp_path):
    storage = _storage(tmp_path)
    pipeline = _pipeline(tmp_path, storage=storage)
    pipeline.run()
    payload = json.loads(pipeline.checkpoint_path.read_text(encoding="utf-8"))
    payload["companies"]["a"]["work"]["detail_success_count"] = float("inf")
    pipeline.checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")
    resumed = _pipeline(tmp_path, storage=storage, resume_from_checkpoint=True)
    with pytest.raises(PipelineError, match="invalid work: a"):
        resumed._load_company_checkpoint(daily.load_companies(resumed.companies_path), dry_run=False)
