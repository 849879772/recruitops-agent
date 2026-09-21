"""Offline recovery regressions; all state lives in pytest's temporary directory."""

from dataclasses import replace
from hashlib import sha256
import json

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
import yaml

from packages.discovery.company_registry import CompanySourceRecord, CompanySourceRegistry
from packages.pipeline import CrawlResult, DailyRecruitmentPipeline
from packages.storage import JobAnalysisSnapshot, JobSnapshot, Storage


URL = "https://fixture.example.test/campus"


def job(job_id, title="C++ Engineer"):
    return {
        "id": job_id, "title": title, "detail_url": f"{URL}/position/{job_id}",
        "cohort": 2027, "cohort_status": "confirmed", "batch": "formal",
    }


def crawl(*jobs):
    return CrawlResult(
        jobs=jobs, source_url=URL, allowed_origins=["https://fixture.example.test"],
        pages_seen=1, total_pages=1, pagination_complete=True, completeness_known=True,
    )


def detail(row, text=None):
    text = text or f"Official responsibilities for {row['title']}"
    return {
        "status": "complete", "detail": text, "detail_url": row["detail_url"],
        "capture_evidence": {
            "status": "complete", "method": "fixture", "source_url": row["detail_url"],
            "identity_verified": True, "terminal_observed": True, "remaining_controls": [],
            "content_sha256": sha256(text.encode()).hexdigest(),
        },
    }


@pytest.fixture
def setup_pipeline(tmp_path):
    storage = Storage.from_url(f"sqlite:///{(tmp_path / 'fixture.db').as_posix()}", initialize=True)
    config = tmp_path / "companies.yaml"
    checkpoint = tmp_path / "checkpoint.json"

    def build(company_ids=("a",), **overrides):
        config.write_text(yaml.safe_dump({"companies": [
            {"id": key, "name": key, "careers_url": f"{URL}/{key}",
             "crawler": "fixture", "integration_status": "connected"}
            for key in company_ids
        ]}), encoding="utf-8")
        kwargs = dict(
            companies_path=config, storage=storage, checkpoint_path=checkpoint,
            crawler=lambda company: crawl(job(company.id)), jd_hydrator=detail,
            max_concurrency=1, checkpoint_batch_size=1,
        )
        kwargs.update(overrides)
        return DailyRecruitmentPipeline(**kwargs)

    return build, storage, checkpoint


def forbidden(*args, **kwargs):
    pytest.fail("unexpected external/repeated operation")


def hydration_path(checkpoint, company_id="a"):
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    directory = checkpoint.parent / f".jd-{payload['hydration_checkpoint_id'][:12]}"
    return directory / (sha256(company_id.encode()).hexdigest()[:32] + ".json")


def hydration_work(checkpoint, company_id="a"):
    return json.loads(hydration_path(checkpoint, company_id).read_text(encoding="utf-8"))


def legacy_hydration_path(checkpoint, company_id="a"):
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    directory = checkpoint.with_name(
        f"{checkpoint.name}.hydration-{payload['hydration_checkpoint_id']}"
    )
    return directory / (sha256(company_id.encode()).hexdigest() + ".json")


@pytest.mark.parametrize("long_reason", [False, True])
def test_source_attempt_failure_preserves_jobs_and_pending_scores(
    setup_pipeline, monkeypatch, long_reason,
):
    build, storage, checkpoint = setup_pipeline
    reason = "crawler_error:" + "x" * 200
    attempted = []
    scored = []

    def record_attempt(self, record_id, **kwargs):
        attempted.append(kwargs)
        if long_reason:
            assert kwargs["reason_code"] == reason
            raise ValueError("reason_code exceeds 128")
        raise OperationalError("record_attempt", {}, RuntimeError("fixture database failure"))

    def matcher(row, **kwargs):
        scored.append(row["id"])
        return {"analysis_status": "complete", "match_score": 87}

    crawler = lambda company: (
        replace(crawl(), error_code=reason) if long_reason and company.id == "a"
        else crawl(job(company.id))
    )
    with monkeypatch.context() as patch:
        patch.setattr(CompanySourceRegistry, "record_attempt", record_attempt)
        with pytest.raises((ValueError, OperationalError)):
            build(("a", "b"), crawler=crawler, matcher=matcher).run()

    expected = {"b"} if long_reason else {"a", "b"}
    with storage.session() as session:
        rows = list(session.scalars(select(JobSnapshot)))
        analyses = list(session.scalars(select(JobAnalysisSnapshot)))
        sources = list(session.scalars(select(CompanySourceRecord)))
    assert {row.id for row in rows} == expected
    assert all(row.capture_status == "complete" and row.jd_raw for row in rows)
    assert {row.job_id for row in analyses} == expected
    assert all(row.analysis_status == "pending" for row in analyses)
    assert all(row.status != "complete" for row in sources)
    assert len(attempted) == 1
    assert scored == []
    assert hydration_work(checkpoint, "b")["detail_success_count"] == 1
    assert hydration_work(checkpoint, "b")["hydration_results"]

    if not long_reason:
        for _ in range(2):
            build(("a", "b"), crawler=forbidden, jd_hydrator=forbidden,
                  matcher=matcher, resume_from_checkpoint=True).run()
        assert sorted(scored) == ["a", "b"]
        with storage.session() as session:
            assert len(list(session.scalars(select(JobSnapshot)))) == 2


def test_incremental_hydration_checkpoint_survives_interruption(setup_pipeline):
    build, storage, checkpoint = setup_pipeline
    jobs = [job("one", "C++ Engineer"), job("two", "Qt Engineer")]

    def interrupt(stage, completed, total):
        if stage == "jd" and completed == 1:
            raise RuntimeError("fixture interruption")

    with pytest.raises(RuntimeError, match="fixture interruption"):
        build(crawler=lambda _: crawl(*jobs), progress_callback=interrupt).run()
    entry = json.loads(checkpoint.read_text(encoding="utf-8"))["companies"]["a"]
    assert entry["attempts"] == 1
    work = hydration_work(checkpoint)
    assert work["detail_success_count"] == 1
    assert len(work["hydration_results"]) == 1
    completed_id = work["jd_results"][0]["job_id"]
    with storage.session() as session:
        assert session.scalar(select(JobSnapshot)) is None
        assert session.scalar(select(CompanySourceRecord)).status != "complete"

    calls = []

    def hydrate(row):
        calls.append(row["id"])
        return detail(row)

    result = build(crawler=forbidden, jd_hydrator=hydrate, resume_from_checkpoint=True).run()
    assert calls == [row["id"] for row in jobs if row["id"] != completed_id]
    assert result.new_count == 2
    assert result.company_results[0].detail_success_count == 2
    entry = json.loads(checkpoint.read_text(encoding="utf-8"))["companies"]["a"]
    assert entry["attempts"] == 1
    assert hydration_work(checkpoint)["detail_success_count"] == 2
    assert len(hydration_work(checkpoint)["jd_results"]) == 2


@pytest.mark.parametrize("partial_list", [False, True])
def test_database_failure_keeps_hydration_for_resume_without_false_completion(
    setup_pipeline, monkeypatch, partial_list,
):
    build, storage, checkpoint = setup_pipeline
    listing = replace(crawl(job("one")), has_more=partial_list, pagination_complete=not partial_list)

    def fail_persist(*args, **kwargs):
        raise OperationalError("persist jobs", {}, RuntimeError("fixture write failure"))

    with monkeypatch.context() as patch:
        patch.setattr(DailyRecruitmentPipeline, "_persist_title_first", fail_persist)
        with pytest.raises(OperationalError):
            build(crawler=lambda _: listing).run()
    with storage.session() as session:
        assert session.scalar(select(JobSnapshot)) is None
        assert session.scalar(select(CompanySourceRecord)).status != "complete"

    result = build(
        crawler=(lambda _: listing) if partial_list else forbidden,
        jd_hydrator=forbidden, resume_from_checkpoint=True,
    ).run()
    assert result.new_count == 1
    assert result.company_results[0].status == ("partial" if partial_list else "complete")
    entry = json.loads(checkpoint.read_text(encoding="utf-8"))["companies"]["a"]
    assert entry["attempts"] == (2 if partial_list else 1)
    assert hydration_work(checkpoint)["detail_success_count"] == 1


def test_failed_hydration_is_retried_and_counts_are_not_accumulated(setup_pipeline):
    build, storage, checkpoint = setup_pipeline
    failed = lambda row: {"status": "failed", "error_code": "fixture_detail_failure"}
    first = build(jd_hydrator=failed).run()
    assert first.company_results[0].status == "partial"
    assert first.company_results[0].detail_failure_count == 1
    work = hydration_work(checkpoint)
    assert work["detail_failure_count"] == 1
    assert work["jd_results"][0]["status"] == "failed"
    second = build(crawler=forbidden, resume_from_checkpoint=True).run()
    assert second.company_results[0].status == "complete"
    assert second.company_results[0].detail_failure_count == 0
    assert second.company_results[0].detail_success_count == 1
    with storage.session() as session:
        assert len(list(session.scalars(select(JobSnapshot)))) == 1


@pytest.mark.parametrize("changed", ["receipt", "hash", "identity", "policy", "listing", "internship"])
def test_checkpoint_reuse_revalidates_receipts_and_screening(setup_pipeline, monkeypatch, changed):
    build, storage, checkpoint = setup_pipeline

    def interrupt(stage, completed, total):
        if stage == "jd" and completed == total:
            raise RuntimeError("fixture interruption")

    hydrator = (lambda row: detail(row, "This is an internship position.")) if changed == "internship" else detail
    with pytest.raises(RuntimeError, match="fixture interruption"):
        build(jd_hydrator=hydrator, progress_callback=interrupt).run()
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    work = payload["companies"]["a"]["work"]
    sidecar = hydration_path(checkpoint)
    details = hydration_work(checkpoint)
    receipt = next(iter(details["hydration_results"].values()))
    if changed == "receipt":
        receipt["capture_evidence"]["content_sha256"] = "bad"
    elif changed == "hash":
        receipt["detail_sha256"] = "bad"
        receipt["capture_evidence"] = {}
    elif changed == "identity":
        receipt["capture_evidence"]["title"] = "Another Job"
    elif changed == "policy":
        monkeypatch.setattr("packages.pipeline.daily._TITLE_FIRST_CAPTURE_POLICY", "fixture-new-policy")
    elif changed == "listing":
        work["accepted_jobs"][0]["detail_url"] += "-changed"
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    sidecar.write_text(json.dumps(details), encoding="utf-8")
    calls = []

    def hydrate(row):
        calls.append(row["id"])
        return detail(row)

    result = build(crawler=forbidden, jd_hydrator=hydrate,
                   matcher=forbidden if changed == "internship" else None,
                   resume_from_checkpoint=True).run()
    assert calls == ([] if changed == "internship" else ["a"])
    assert result.new_count == (0 if changed == "internship" else 1)
    with storage.session() as session:
        assert len(list(session.scalars(select(JobSnapshot)))) == result.new_count


def test_hydration_batches_do_not_rewrite_list_checkpoint(setup_pipeline, monkeypatch):
    build, storage, checkpoint = setup_pipeline
    list_bytes = []
    sidecar_writes = []
    original = DailyRecruitmentPipeline._write_hydration_checkpoint

    def observe_write(self, work, **kwargs):
        sidecar_writes.append(work.company.id)
        assert checkpoint.read_bytes() == list_bytes[0]
        return original(self, work, **kwargs)

    def progress(stage, completed, total):
        if stage == "jd" and completed == 0:
            list_bytes.append(checkpoint.read_bytes())

    monkeypatch.setattr(DailyRecruitmentPipeline, "_write_hydration_checkpoint", observe_write)
    build(("a", "b"), crawler=lambda company: crawl(*[
        job(f"{company.id}-{index}", f"C++ Engineer {index}") for index in range(3)
    ]), checkpoint_batch_size=2, progress_callback=progress).run()
    assert checkpoint.read_bytes() == list_bytes[0]
    assert len(sidecar_writes) <= 6
    for company_id in ("a", "b"):
        work = hydration_work(checkpoint, company_id)
        assert len(work["hydration_results"]) == 3
        assert "accepted_jobs" not in work


def test_old_list_checkpoint_upgrades_without_recrawl(setup_pipeline):
    build, storage, checkpoint = setup_pipeline

    def interrupt(stage, completed, total):
        if stage == "jd":
            raise RuntimeError("fixture list-only interruption")

    with pytest.raises(RuntimeError):
        build(progress_callback=interrupt).run()
    payload = json.loads(checkpoint.read_text(encoding="utf-8"))
    payload.pop("hydration_checkpoint_id")
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    result = build(crawler=forbidden, resume_from_checkpoint=True).run()
    upgraded = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert upgraded["version"] == payload["version"] == 1
    assert upgraded["companies"] == payload["companies"]
    assert result.new_count == 1
    assert hydration_work(checkpoint)["detail_success_count"] == 1


def test_fresh_run_does_not_reuse_previous_run_sidecars(setup_pipeline):
    build, storage, checkpoint = setup_pipeline

    def interrupt(stage, completed, total):
        if stage == "jd" and completed == total:
            raise RuntimeError("fixture interruption")

    with pytest.raises(RuntimeError):
        build(progress_callback=interrupt).run()
    previous = hydration_path(checkpoint)
    calls = []
    build(jd_hydrator=lambda row: calls.append(row["id"]) or detail(row)).run()
    assert calls == ["a"]
    assert hydration_path(checkpoint) != previous


def test_resume_reads_legacy_long_hydration_sidecar(setup_pipeline):
    build, storage, checkpoint = setup_pipeline

    def interrupt(stage, completed, total):
        if stage == "jd" and completed == total:
            raise RuntimeError("fixture interruption")

    with pytest.raises(RuntimeError, match="fixture interruption"):
        build(progress_callback=interrupt).run()
    current = hydration_path(checkpoint)
    legacy = legacy_hydration_path(checkpoint)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    current.replace(legacy)

    result = build(
        crawler=forbidden,
        jd_hydrator=forbidden,
        resume_from_checkpoint=True,
    ).run()

    assert result.new_count == 1


def test_hydration_sidecar_uses_bounded_names(setup_pipeline):
    build, _storage, checkpoint = setup_pipeline
    pipeline = build()
    sidecar = pipeline._hydration_checkpoint_path("company-" + "x" * 200)

    assert sidecar.parent.parent == checkpoint.parent
    assert sidecar.parent.name.startswith(".jd-")
    assert len(sidecar.parent.name) == 16
    assert len(sidecar.name) == 37


def test_dry_run_leaves_list_and_sidecar_unchanged(setup_pipeline):
    build, storage, checkpoint = setup_pipeline

    def interrupt(stage, completed, total):
        if stage == "jd" and completed == total:
            raise RuntimeError("fixture interruption")

    with pytest.raises(RuntimeError):
        build(progress_callback=interrupt).run()
    sidecar = hydration_path(checkpoint)
    before = checkpoint.read_bytes(), sidecar.read_bytes()
    writes = []
    storage.pre_write_hook = lambda _: writes.append(True)
    result = build(crawler=forbidden, jd_hydrator=forbidden, resume_from_checkpoint=True).run(dry_run=True)
    assert not result.written
    assert writes == []
    assert (checkpoint.read_bytes(), sidecar.read_bytes()) == before


def test_restored_hydration_scores_once_with_current_screening(setup_pipeline):
    build, storage, checkpoint = setup_pipeline

    def interrupt(stage, completed, total):
        if stage == "jd" and completed == total:
            raise RuntimeError("fixture interruption")

    with pytest.raises(RuntimeError):
        build(progress_callback=interrupt).run()
    calls = []

    class Matcher:
        def analyze_title_first(self, row, profile, *, screening, existing_analysis=None):
            assert screening.eligible
            assert row["jd_raw"] == "Official responsibilities for C++ Engineer"
            calls.append(row["id"])
            return {"analysis_status": "complete", "match_score": 88}

    # Current profile rejects the title even though the stored receipt is valid.
    rejected = build(
        crawler=forbidden, jd_hydrator=forbidden, matcher=forbidden,
        profile={"matching": {"title_keywords": ["Java"]}}, resume_from_checkpoint=True,
    ).run()
    assert rejected.new_count == 0
    for _ in range(2):
        build(crawler=forbidden, jd_hydrator=forbidden, matcher=Matcher(),
              resume_from_checkpoint=True).run()
    assert calls == ["a"]


def test_sidecar_write_failure_propagates_and_preserves_previous_batch(setup_pipeline, monkeypatch):
    build, storage, checkpoint = setup_pipeline
    original = DailyRecruitmentPipeline._write_hydration_checkpoint
    writes = []

    def fail_second_batch(self, work, **kwargs):
        writes.append(work.company.id)
        if len(writes) == 2:
            raise OSError("fixture disk full")
        return original(self, work, **kwargs)

    jobs = [job("one", "C++ Engineer"), job("two", "Qt Engineer")]
    with monkeypatch.context() as patch:
        patch.setattr(DailyRecruitmentPipeline, "_write_hydration_checkpoint", fail_second_batch)
        with pytest.raises(OSError, match="disk full"):
            build(crawler=lambda _: crawl(*jobs)).run()
    saved = hydration_work(checkpoint)
    assert saved["detail_success_count"] == 1
    completed_id = saved["jd_results"][0]["job_id"]
    with storage.session() as session:
        assert session.scalar(select(JobSnapshot)) is None
        assert session.scalar(select(CompanySourceRecord)).status != "complete"
    calls = []
    build(crawler=forbidden, resume_from_checkpoint=True,
          jd_hydrator=lambda row: calls.append(row["id"]) or detail(row)).run()
    assert calls == [row["id"] for row in jobs if row["id"] != completed_id]
