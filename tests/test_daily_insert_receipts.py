from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

import packages.pipeline.daily as daily
from packages.pipeline import DailyRecruitmentPipeline, PipelineInterrupted, run_daily_pipeline
from packages.storage import JobSnapshot
from packages.storage.sync import upsert_job_snapshot
from test_title_first_pipeline import FakeHydrator, FakeMatcher, _company, _config, _crawl, _job, _storage


def test_overlapping_sources_report_nine_real_inserts_from_thirty_one_observations(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    config = _config(tmp_path / "companies.yaml", _company("old-source"))
    jobs = [_job(f"stable-{index}", f"C++ Engineer {index}") for index in range(27)]
    run_daily_pipeline(
        companies_path=config, storage=storage,
        crawler=lambda _company: _crawl(*jobs[:18]),
        jd_hydrator=FakeHydrator(), matcher=FakeMatcher(),
    )
    with storage.session() as session:
        original = {row.id: (row.company_id, row.source_ref, row.match_score, row.jd_raw)
                    for row in session.scalars(select(JobSnapshot))}

    _config(config, _company("alias-a"), _company("alias-b"))
    hydrator = FakeHydrator()
    matcher = FakeMatcher()
    result = run_daily_pipeline(
        companies_path=config, storage=storage,
        crawler=lambda company: _crawl(*(jobs if company.id == "alias-a" else jobs[-4:])),
        jd_hydrator=hydrator, matcher=matcher, max_concurrency=1,
    )

    assert sum(item.raw_job_count for item in result.company_results) == 31
    assert result.new_count == len(result.new_job_ids) == 9
    assert set(result.new_job_ids) == {f"stable-{index}" for index in range(18, 27)}
    assert set(hydrator.calls) == set(result.new_job_ids)
    assert len(hydrator.calls) == len(matcher.calls) == 9
    assert result.job_write_statistics == {
        "inserted_count": 9, "updated_count": 18, "unique_written_count": 27,
        "new_complete_count": 9, "new_pending_count": 0, "new_failed_count": 0,
        "new_inactive_count": 0, "predicted_insert_count": None,
        "basis": "committed_insert_receipts", "dry_run": False,
    }
    assert sum(item.new_count for item in result.company_results) == 9
    with storage.session() as session:
        rows = list(session.scalars(select(JobSnapshot)))
    assert len(rows) == 27
    assert {row.id: (row.company_id, row.source_ref, row.match_score, row.jd_raw)
            for row in rows if row.id in original} == original


def test_explicit_native_ids_keep_distinct_same_title_jobs(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    config = _config(tmp_path / "companies.yaml", _company("source"))
    jobs = [{**_job(f"id-{index}", "C++ Engineer"), "native_job_id": f"native-{index}"}
            for index in range(2)]
    hydrator = FakeHydrator()
    matcher = FakeMatcher()
    first = run_daily_pipeline(
        companies_path=config, storage=storage, crawler=lambda _: _crawl(*jobs),
        jd_hydrator=hydrator, matcher=matcher,
    )
    second = run_daily_pipeline(
        companies_path=config, storage=storage, crawler=lambda _: _crawl(*reversed(jobs)),
        jd_hydrator=hydrator, matcher=matcher,
    )
    assert first.new_count == 2
    assert second.new_count == 0
    assert second.reused_count == 2
    assert len(hydrator.calls) == len(matcher.calls) == 2
    with storage.session() as session:
        rows = list(session.scalars(select(JobSnapshot)))
    assert {row.native_job_id for row in rows} == {"native-0", "native-1"}
    assert all(row.match_score == 87 for row in rows)


def test_unique_legacy_list_snapshot_is_upgraded_to_native_detail_without_duplicate(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    config = _config(tmp_path / "companies.yaml", _company("source"))
    run_daily_pipeline(
        companies_path=config, storage=storage,
        crawler=lambda _: _crawl(_job("legacy-id", "C++ Engineer", detail_url="https://jobs.example.test/campus")),
        jd_hydrator=FakeHydrator({"legacy-id"}), matcher=FakeMatcher(),
    )
    hydrated = FakeHydrator()
    native_job = {**_job("new-native-id", "C++ Engineer"), "native_job_id": "official-id"}
    result = run_daily_pipeline(
        companies_path=config, storage=storage, crawler=lambda _: _crawl(native_job),
        jd_hydrator=hydrated, matcher=FakeMatcher(),
    )
    assert result.new_count == 0
    assert result.reused_count == 1
    assert hydrated.calls == ["legacy-id"]
    with storage.session() as session:
        rows = list(session.scalars(select(JobSnapshot)))
    assert len(rows) == 1
    assert rows[0].id == "legacy-id"
    assert rows[0].native_job_id == "official-id"
    assert rows[0].detail_url == native_job["detail_url"]
    assert rows[0].capture_status == "complete"


def test_two_new_native_ids_do_not_both_bind_one_legacy_snapshot(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    config = _config(tmp_path / "companies.yaml", _company("source"))
    run_daily_pipeline(
        companies_path=config, storage=storage,
        crawler=lambda _: _crawl(_job("legacy-id", "C++ Engineer")),
        jd_hydrator=FakeHydrator({"legacy-id"}), matcher=FakeMatcher(),
    )
    jobs = [{**_job(f"new-{index}", "C++ Engineer"), "native_job_id": f"native-{index}"} for index in range(2)]
    result = run_daily_pipeline(
        companies_path=config, storage=storage, crawler=lambda _: _crawl(*jobs),
        jd_hydrator=FakeHydrator(), matcher=FakeMatcher(),
    )
    assert result.new_count == 2
    with storage.session() as session:
        assert {row.id for row in session.scalars(select(JobSnapshot))} == {"legacy-id", "new-0", "new-1"}


def test_resume_only_counts_inserts_committed_in_that_invocation(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    config = _config(tmp_path / "companies.yaml", _company("a"), _company("b"))
    options = dict(
        companies_path=config, storage=storage, checkpoint_path=tmp_path / "checkpoint.json",
        crawler=lambda company: _crawl(_job(company.id, "C++ Engineer")),
        jd_hydrator=FakeHydrator(), matcher=FakeMatcher(), max_concurrency=1,
    )
    initial = DailyRecruitmentPipeline(**options, company_batch_limit=1)
    with pytest.raises(PipelineInterrupted):
        initial.run()
    assert initial._inserted_job_companies == {"a": "a"}
    resumed = DailyRecruitmentPipeline(**options, resume_from_checkpoint=True).run()
    assert resumed.new_count == 1
    assert resumed.new_job_ids == ("b",)
    repeated = DailyRecruitmentPipeline(**options, resume_from_checkpoint=True).run()
    assert repeated.new_count == 0
    assert repeated.job_write_statistics["inserted_count"] == 0
    with storage.session() as session:
        assert len(list(session.scalars(select(JobSnapshot)))) == 2


def test_dry_run_marks_prediction_without_any_insert_receipt(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    config = _config(tmp_path / "companies.yaml", _company("a"), _company("b"))
    result = run_daily_pipeline(
        companies_path=config, storage=storage,
        crawler=lambda _: _crawl(_job("shared-id", "C++ Engineer")),
        jd_hydrator=FakeHydrator(), matcher=FakeMatcher(), dry_run=True,
    )
    assert result.new_count == 1
    assert result.job_write_statistics["inserted_count"] == 0
    assert result.job_write_statistics["unique_written_count"] == 0
    assert result.job_write_statistics["predicted_insert_count"] == 1
    assert result.job_write_statistics["basis"] == "predicted_unique_candidates"
    with storage.session() as session:
        assert list(session.scalars(select(JobSnapshot))) == []


def test_shared_failed_capture_is_attempted_once_and_keeps_both_sources_partial(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    config = _config(tmp_path / "companies.yaml", _company("a"), _company("b"))
    hydrator = FakeHydrator({"shared-id"})
    result = run_daily_pipeline(
        companies_path=config, storage=storage,
        crawler=lambda _: _crawl(_job("shared-id", "C++ Engineer")),
        jd_hydrator=hydrator, matcher=FakeMatcher(),
    )
    assert hydrator.calls == ["shared-id"]
    assert result.new_count == result.failed_job_count == 1
    assert result.job_write_statistics["new_failed_count"] == 1
    assert result.job_write_statistics["new_complete_count"] == 0
    assert result.job_write_statistics["new_pending_count"] == 0
    assert all(item.status == "partial" for item in result.company_results)
    assert all(item.detail_failure_count == 1 for item in result.company_results)


def test_rolled_back_insert_never_publishes_a_commit_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = _storage(tmp_path)
    config = _config(tmp_path / "companies.yaml", _company("a"))
    pipeline = DailyRecruitmentPipeline(companies_path=config, storage=storage)
    work = daily._CompanyWork(company=daily.load_companies(config)[0])
    candidates = [daily._TitleFirstCandidate(
        work=work, job={**_job(f"id-{index}", f"C++ Engineer {index}"), "capture_status": "pending"},
        title_key=f"C++ Engineer {index}",
    ) for index in range(2)]
    original = daily.upsert_job_snapshot

    def fail_second(session, model, **kwargs):
        if model.id == "id-1":
            raise RuntimeError("fixture rollback")
        return original(session, model, **kwargs)

    monkeypatch.setattr(daily, "upsert_job_snapshot", fail_second)
    with pytest.raises(RuntimeError, match="fixture rollback"):
        pipeline._persist_title_first(
            companies=(work.company,), existing_updates=(), new_candidates=candidates,
            scored_candidates=(), inactive_ids=(), dry_run=False,
        )
    assert pipeline._inserted_job_companies == {}
    assert pipeline._written_job_ids == set()
    with storage.session() as session:
        assert list(session.scalars(select(JobSnapshot))) == []


def test_conflict_receipt_preserves_completed_snapshot_identity_atomically(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    config = _config(tmp_path / "companies.yaml", _company("original"), _company("alias"))
    companies = daily.load_companies(config)
    pipeline = DailyRecruitmentPipeline(companies_path=config, storage=storage)
    candidate = daily._TitleFirstCandidate(
        work=daily._CompanyWork(company=companies[0]),
        job={**_job("stable-id", "C++ Engineer"), "capture_status": "complete", "jd_raw": "complete JD"},
        title_key="C++ Engineer",
    )
    now = daily._timestamp(pipeline.clock)
    original = pipeline._title_first_job_model(candidate, now=now).model_copy(update={"match_score": 88})
    with storage.write_transaction() as session:
        for company in companies:
            session.add(daily.CompanySnapshot(
                id=company.id, name=company.name, aliases=[], created_at=now, updated_at=now,
                source="fixture", integration_status="connected",
            ))
        session.flush()
        assert upsert_job_snapshot(session, original, capture_status="complete", return_inserted=True) is True
    stale = original.model_copy(update={
        "company_id": "alias", "jd_raw": None, "detail_url": "https://jobs.example.test/stale",
        "source_ref": "wrong-source", "match_score": None,
    })
    with storage.write_transaction() as session:
        assert upsert_job_snapshot(
            session, stale, capture_status="failed", capture_failure_reason="timeout",
            return_inserted=True, preserve_existing_identity=True, preserve_existing_score=True,
        ) is False
    with storage.session() as session:
        row = session.get(JobSnapshot, "stable-id")
        assert row.company_id == "original"
        assert row.jd_raw == "complete JD"
        assert row.detail_url == str(original.detail_url)
        assert row.source_ref == original.source_ref
        assert row.capture_status == "complete"
        assert row.match_score == 88

    enriched = stale.model_copy(update={"native_job_id": "native-id", "source_tenant": "tenant", "business_key": "business"})
    with storage.write_transaction() as session:
        assert upsert_job_snapshot(
            session, enriched, capture_status="failed", return_inserted=True,
            preserve_existing_identity=True, preserve_existing_score=True,
        ) is False
    with storage.session() as session:
        row = session.get(JobSnapshot, "stable-id")
        assert row.company_id == "original"
        assert row.native_job_id == "native-id"
        assert row.source_tenant == "tenant"
        assert row.business_key == "business"
        assert row.capture_status == "complete"
