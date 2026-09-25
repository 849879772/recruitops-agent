"""Large crawl checkpoints keep listing payloads in durable per-company receipts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import packages.pipeline.daily as daily
from packages.pipeline.company_checkpoint import company_receipt_path
from packages.pipeline.daily import DailyRecruitmentPipeline, PipelineCompany, PipelineError
from packages.scheduler.runtime import _company_checkpoint_progress, _initialize_empty_company_checkpoint


def _scope(size=100):
    return [
        PipelineCompany(
            id=f"company-{index:04d}", name=f"Company {index}",
            careers_url=f"https://example-{index}.test/jobs", crawler_key="json_api",
            integration_status="connected",
        )
        for index in range(size)
    ]


def _pipeline(tmp_path, *, resume=False):
    return DailyRecruitmentPipeline(
        companies_path=tmp_path / "companies.yaml",
        checkpoint_path=tmp_path / "checkpoint.json",
        resume_from_checkpoint=resume,
    )


def _work(company, marker="x"):
    return daily._CompanyWork(
        company=company, raw_job_count=1, list_complete=True,
        accepted_jobs=[{"id": "one", "title": "Engineer", "detail": marker * 30_000}],
    )


def test_compact_receipt_path_does_not_exceed_normal_desktop_checkpoint_path():
    checkpoint = Path(
        "D:/RecruitOps1/.data/.desktop-runtime-tests/shell-runtime-example/runtime/"
        + "daily-checkpoint-" + "a" * 32 + ".json"
    )
    sidecar = company_receipt_path(checkpoint, "b" * 32, "company-example")
    assert len(str(sidecar)) <= len(str(checkpoint))


def test_large_scope_index_stays_small_and_restores_receipts(tmp_path):
    scope = _scope()
    pipeline = _pipeline(tmp_path)
    entries, _ = pipeline._load_company_checkpoint(scope, dry_run=False)
    assert pipeline._company_checkpoint_version == 2
    for company in scope[:6]:
        pipeline._write_company_checkpoint(entries, _work(company), attempts=1, dry_run=False)

    index = json.loads(pipeline.checkpoint_path.read_text(encoding="utf-8"))
    assert index["version"] == 2
    assert len(index["companies"]) == 6
    assert pipeline.checkpoint_path.stat().st_size < 40_000
    assert _company_checkpoint_progress(pipeline.checkpoint_path)["attempted_unique"] == 6
    receipt = company_receipt_path(
        pipeline.checkpoint_path, index["hydration_checkpoint_id"], scope[0].id,
    )
    assert receipt.is_file()
    assert len(str(receipt)) - len(str(pipeline.checkpoint_path)) < 60
    assert pipeline.checkpoint_path.stat().st_size * 10 < sum(
        company_receipt_path(pipeline.checkpoint_path, index["hydration_checkpoint_id"], company.id).stat().st_size
        for company in scope[:6]
    )

    resumed = _pipeline(tmp_path, resume=True)
    restored, works = resumed._load_company_checkpoint(scope, dry_run=False)
    assert len(restored) == 6
    assert len(works[scope[0].id].accepted_jobs[0]["detail"]) == 30_000
    assert resumed._company_checkpoint_version == 2


def test_indexed_receipt_missing_or_corrupt_fails_closed(tmp_path):
    scope = _scope()
    pipeline = _pipeline(tmp_path)
    entries, _ = pipeline._load_company_checkpoint(scope, dry_run=False)
    pipeline._write_company_checkpoint(entries, _work(scope[0]), attempts=1, dry_run=False)
    index = json.loads(pipeline.checkpoint_path.read_text(encoding="utf-8"))
    receipt = company_receipt_path(
        pipeline.checkpoint_path, index["hydration_checkpoint_id"], scope[0].id,
    )
    receipt.write_text("broken", encoding="utf-8")
    with pytest.raises(PipelineError, match="indexed company receipt is missing or corrupt"):
        _pipeline(tmp_path, resume=True)._load_company_checkpoint(scope, dry_run=False)


def test_crash_between_receipt_and_index_recovers_previous_generation(tmp_path, monkeypatch):
    scope = _scope()
    pipeline = _pipeline(tmp_path)
    entries, _ = pipeline._load_company_checkpoint(scope, dry_run=False)
    pipeline._write_company_checkpoint(entries, _work(scope[0], "a"), attempts=1, dry_run=False)
    original_write = daily._atomic_checkpoint

    def crash_on_index(path, payload):
        if path == pipeline.checkpoint_path:
            raise PipelineError("synthetic index write failure")
        original_write(path, payload)

    monkeypatch.setattr(daily, "_atomic_checkpoint", crash_on_index)
    with pytest.raises(PipelineError, match="synthetic index write failure"):
        pipeline._write_company_checkpoint(entries, _work(scope[0], "b"), attempts=2, dry_run=False)
    monkeypatch.setattr(daily, "_atomic_checkpoint", original_write)

    resumed = _pipeline(tmp_path, resume=True)
    restored, works = resumed._load_company_checkpoint(scope, dry_run=False)
    assert restored[scope[0].id]["attempts"] == 1
    assert works[scope[0].id].accepted_jobs[0]["detail"] == "a" * 30_000
    resumed._write_company_checkpoint(restored, _work(scope[0], "c"), attempts=2, dry_run=False)
    second_resume = _pipeline(tmp_path, resume=True)
    latest, works = second_resume._load_company_checkpoint(scope, dry_run=False)
    assert latest[scope[0].id]["attempts"] == 2
    assert works[scope[0].id].accepted_jobs[0]["detail"] == "c" * 30_000


def test_preinitialized_large_scope_is_resumable_before_first_company(tmp_path):
    import yaml

    scope = _scope()
    config = tmp_path / "companies.yaml"
    config.write_text(yaml.safe_dump({"companies": [company.crawler_config() for company in scope]}), encoding="utf-8")
    checkpoint = tmp_path / "checkpoint.json"
    _initialize_empty_company_checkpoint(checkpoint, config)
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["version"] == 2
    restored, works = _pipeline(tmp_path, resume=True)._load_company_checkpoint(scope, dry_run=False)
    assert restored == works == {}


def test_preinitialized_subset_reuses_exact_frozen_scope(tmp_path):
    import yaml

    scope = _scope(101)
    config = tmp_path / "companies.yaml"
    config.write_text(yaml.safe_dump({"companies": [company.crawler_config() for company in scope]}), encoding="utf-8")
    checkpoint = tmp_path / "checkpoint.json"
    subset = scope[1:]
    _initialize_empty_company_checkpoint(checkpoint, config, tuple(company.id for company in subset))
    initialized = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert initialized["company_ids"] == [company.id for company in subset]
    fresh = _pipeline(tmp_path)
    fresh._load_company_checkpoint(subset, dry_run=False)
    assert fresh._hydration_checkpoint_id == initialized["hydration_checkpoint_id"]
    assert _pipeline(tmp_path, resume=True)._load_company_checkpoint(subset, dry_run=False)[0] == {}


def test_existing_nonempty_v1_large_scope_still_resumes(tmp_path):
    scope = _scope()
    pipeline = _pipeline(tmp_path)
    pipeline._checkpoint_scope_digest = daily.company_scope_digest(scope)
    pipeline._checkpoint_company_ids = tuple(company.id for company in scope)
    work = _work(scope[0])
    daily._atomic_checkpoint(pipeline.checkpoint_path, {
        "version": 1,
        "company_ids": list(pipeline._checkpoint_company_ids),
        "scope_digest": pipeline._checkpoint_scope_digest,
        "companies": {scope[0].id: {
            "status": "complete", "attempts": 1,
            "work": daily._checkpoint_work_payload(work),
        }},
    })
    resumed = _pipeline(tmp_path, resume=True)
    entries, works = resumed._load_company_checkpoint(scope, dry_run=False)
    assert resumed._company_checkpoint_version == 2
    assert entries[scope[0].id]["status"] == "complete"
    assert works[scope[0].id].accepted_jobs[0]["title"] == "Engineer"
    assert json.loads(pipeline.checkpoint_path.read_text(encoding="utf-8"))["version"] == 2
    assert json.loads(pipeline.checkpoint_path.with_name("checkpoint.json.bak").read_text(encoding="utf-8"))["version"] == 1


def test_failed_legacy_promotion_keeps_v1_resumable(tmp_path, monkeypatch):
    scope = _scope()
    pipeline = _pipeline(tmp_path)
    daily._atomic_checkpoint(pipeline.checkpoint_path, {
        "version": 1,
        "company_ids": [company.id for company in scope],
        "scope_digest": daily.company_scope_digest(scope),
        "hydration_checkpoint_id": "a" * 32,
        "companies": {scope[0].id: {
            "status": "complete", "attempts": 1,
            "work": daily._checkpoint_work_payload(_work(scope[0])),
        }},
    })
    original_write = daily._atomic_checkpoint

    def fail_index(path, payload):
        if path == pipeline.checkpoint_path and payload.get("version") == 2:
            raise PipelineError("synthetic migration failure")
        original_write(path, payload)

    monkeypatch.setattr(daily, "_atomic_checkpoint", fail_index)
    resumed = _pipeline(tmp_path, resume=True)
    entries, works = resumed._load_company_checkpoint(scope, dry_run=False)
    assert resumed._company_checkpoint_version == 1
    assert scope[0].id in entries and scope[0].id in works
    assert json.loads(pipeline.checkpoint_path.read_text(encoding="utf-8"))["version"] == 1
    monkeypatch.setattr(daily, "_atomic_checkpoint", original_write)
    promoted = _pipeline(tmp_path, resume=True)
    promoted._load_company_checkpoint(scope, dry_run=False)
    assert promoted._company_checkpoint_version == 2
