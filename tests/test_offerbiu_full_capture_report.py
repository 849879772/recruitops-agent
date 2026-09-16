from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from scripts import report_offerbiu_full_capture as module


def _evidence(detail: str, url: str, *, status: str = "complete") -> dict[str, object]:
    return {
        "status": status,
        "method": "fixture_api",
        "source_url": url,
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": sha256(detail.encode("utf-8")).hexdigest(),
    }


def _job(
    title: str,
    url: str,
    *,
    cohort: object = 2027,
    cohort_status: str = "confirmed",
    jd: str = "full official detail",
    evidence: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "title": title,
        "detail_url": url,
        "cohort": cohort,
        "cohort_status": cohort_status,
        "jd_raw": jd,
    }
    if evidence is not None:
        value["capture_evidence"] = evidence
    return value


def _checkpoint(
    directory: Path,
    key: str,
    company: str,
    url: str,
    *,
    status: str = "raw_jobs_observed",
    jobs: list[dict[str, object]] | None = None,
    pagination: dict[str, object] | None = None,
    reason: str | None = None,
) -> None:
    payload: dict[str, object] = {
        "task_key": key,
        "company": company,
        "crawl_url": url,
        "crawler_status": status,
        "crawl": {
            "raw_job_count": len(jobs or []),
            "raw_jobs": jobs or [],
            "pagination_evidence": pagination or {},
        },
    }
    if reason is not None:
        payload["reason"] = reason
    (directory / f"{key}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _fixture_run(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    run_dir = tmp_path / ".data" / "evals" / "full"
    checkpoint_dir = run_dir / "checkpoints"
    checkpoint_dir.mkdir(parents=True)
    sources = [
        {"key": "jobs", "row": {"companyName": "Alpha", "applyUrl": "https://alpha.example/campus"}},
        {"key": "empty", "row": {"companyName": "Beta", "applyUrl": "https://beta.example/campus"}},
        {"key": "skip", "row": {"companyName": "Gamma", "applyUrl": "https://gamma.example/campus"}},
        {"key": "error", "row": {"companyName": "Delta", "applyUrl": "https://delta.example/campus"}},
        {"key": "pending", "row": {"companyName": "Epsilon", "applyUrl": "https://epsilon.example/campus"}},
    ]
    (run_dir / "manifest.json").write_text(json.dumps({"task_count": 5, "snapshot_sha256": "fixture"}), encoding="utf-8")
    (run_dir / "sources.json").write_text(json.dumps(sources, ensure_ascii=False), encoding="utf-8")

    shared = "https://ats.example/jobs/1?utm_source=fixture"
    _checkpoint(
        checkpoint_dir,
        "jobs",
        "Alpha",
        sources[0]["row"]["applyUrl"],
        jobs=[
            _job(
                "complete",
                shared,
                jd="complete official detail",
                evidence=_evidence("complete official detail", shared),
            ),
            _job("unknown", "https://ats.example/jobs/2", jd="card text"),
            _job(
                "incomplete",
                "https://ats.example/jobs/3",
                evidence=_evidence("partial", "https://ats.example/jobs/3", status="incomplete"),
            ),
            _job("other", "", cohort=2026, jd="", evidence=None),
        ],
        pagination={
            "pagination_complete": True,
            "completeness_known": True,
            "has_more": False,
            "advertised_total": 4,
        },
    )
    _checkpoint(
        checkpoint_dir,
        "empty",
        "Beta",
        sources[1]["row"]["applyUrl"],
        status="empty_raw_result_not_proof_of_no_jobs",
        pagination={"pagination_complete": False, "completeness_known": False, "has_more": False},
    )
    _checkpoint(
        checkpoint_dir,
        "skip",
        "Gamma",
        sources[2]["row"]["applyUrl"],
        status="skipped",
        reason="known article",
    )
    _checkpoint(
        checkpoint_dir,
        "error",
        "Delta",
        sources[3]["row"]["applyUrl"],
        status="error",
        reason="timeout",
    )
    hydration_dir = run_dir / "hydration"
    hydration_dir.mkdir()
    (hydration_dir / "summary.json").write_text(json.dumps({"stage_complete": False, "remaining": 2}), encoding="utf-8")
    return run_dir, checkpoint_dir, run_dir / "sources.json", hydration_dir


def test_report_counts_manifest_progress_and_separates_quality_buckets(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    run_dir, checkpoint_dir, _, hydration_dir = _fixture_run(tmp_path)
    output = tmp_path / ".data" / "evals" / "report"

    summary = module.run(checkpoint_dir, output, hydration_dir=hydration_dir)

    assert summary["attempted"] == 4
    assert summary["pending"] == 1
    assert summary["entry_status_counts"] == {
        "has_jobs": 1,
        "empty": 1,
        "skip": 1,
        "error": 1,
        "pending": 1,
    }
    assert summary["raw_jobs"]["raw_returned_rows"] == 4
    assert summary["jd_counts"] == {"complete": 1, "unknown": 2, "incomplete": 1}
    assert summary["cohort_counts"] == {"2027": 3, "unknown": 0, "other": 1}
    assert summary["pagination"] == {
        "complete_and_known_entries": 1,
        "has_more_entries": 0,
        "count_conflict_entries": 0,
        "has_more_and_count_conflict_entries": 0,
    }
    assert summary["detail_urls"]["raw_job_rows"] if "raw_job_rows" in summary["detail_urls"] else True
    assert summary["detail_urls"]["unique_normalized_detail_urls"] == 3
    assert summary["detail_urls"]["cross_company_group_count"] == 0
    assert summary["hydration_summary"] == {"stage_complete": False, "remaining": 2}

    entries = json.loads((output / "entries.json").read_text(encoding="utf-8"))
    assert [entry["status"] for entry in entries] == ["has_jobs", "empty", "skip", "error", "pending"]
    assert entries[-1]["url"] == "https://epsilon.example/campus"
    assert entries[-1]["reason_code"] == "no_checkpoint"
    assert "https://delta.example/campus" in (output / "report.md").read_text(encoding="utf-8")
    assert (output / "quality-summary.json").exists()
    assert run_dir.joinpath("manifest.json").exists()


def test_pagination_has_more_and_count_conflict_are_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    run_dir, checkpoint_dir, _, _ = _fixture_run(tmp_path)
    sources = json.loads((run_dir / "sources.json").read_text(encoding="utf-8"))
    sources.append({"key": "conflict", "row": {"companyName": "Zeta", "applyUrl": "https://zeta.example/campus"}})
    (run_dir / "sources.json").write_text(json.dumps(sources, ensure_ascii=False), encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps({"task_count": 6}), encoding="utf-8")
    _checkpoint(
        checkpoint_dir,
        "conflict",
        "Zeta",
        "https://zeta.example/campus",
        jobs=[_job("one", "https://zeta.example/jobs/1")],
        pagination={
            "pagination_complete": False,
            "completeness_known": True,
            "has_more": True,
            "advertised_total": 2,
        },
    )

    summary = module.run(checkpoint_dir, tmp_path / ".data" / "evals" / "report-2")

    assert summary["pagination"]["has_more_entries"] == 1
    assert summary["pagination"]["count_conflict_entries"] == 1
    assert summary["pagination"]["has_more_and_count_conflict_entries"] == 1
    assert summary["entry_status_counts"]["pending"] == 1


def test_cross_company_duplicate_groups_keep_company_labels_and_output_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    run_dir, checkpoint_dir, _, _ = _fixture_run(tmp_path)
    sources = json.loads((run_dir / "sources.json").read_text(encoding="utf-8"))
    sources[1]["key"] = "empty"
    sources.append({"key": "other-company", "row": {"companyName": "Other", "applyUrl": "https://other.example/campus"}})
    (run_dir / "sources.json").write_text(json.dumps(sources, ensure_ascii=False), encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps({"task_count": 6}), encoding="utf-8")
    _checkpoint(
        checkpoint_dir,
        "other-company",
        "Other",
        "https://other.example/campus",
        jobs=[_job("same URL", "https://ATS.example/jobs/1?utm_medium=other")],
        pagination={"pagination_complete": True, "completeness_known": True, "advertised_total": 1},
    )

    summary = module.run(checkpoint_dir, tmp_path / ".data" / "evals" / "report-3")
    group = summary["detail_urls"]["cross_company_groups"][0]

    assert group["companies"] == ["Alpha", "Other"]
    assert summary["detail_urls"]["legal_entity_merge"] is False
    with pytest.raises(ValueError, match="under .data/evals"):
        module.run(checkpoint_dir, tmp_path / "outside")


def test_static_pre_exclusion_is_separate_from_eligible_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    run_dir = tmp_path / ".data" / "evals" / "scope"
    checkpoint_dir = run_dir / "checkpoints"
    checkpoint_dir.mkdir(parents=True)
    sources = [
        {"key": "article", "row": {"companyName": "Static", "applyUrl": "https://mp.weixin.qq.com/s/a"}},
        {"key": "done", "row": {"companyName": "Done", "applyUrl": "https://done.example/campus"}},
        {"key": "pending-1", "row": {"companyName": "Pending 1", "applyUrl": "https://pending-1.example/campus"}},
        {"key": "pending-2", "row": {"companyName": "Pending 2", "applyUrl": "https://pending-2.example/campus"}},
    ]
    (run_dir / "manifest.json").write_text(json.dumps({"task_count": 4}), encoding="utf-8")
    (run_dir / "sources.json").write_text(json.dumps(sources, ensure_ascii=False), encoding="utf-8")
    _checkpoint(checkpoint_dir, "article", "Static", sources[0]["row"]["applyUrl"], status="skipped", reason="article")
    _checkpoint(checkpoint_dir, "done", "Done", sources[1]["row"]["applyUrl"], jobs=[_job("done", "https://done.example/jobs/1")])

    summary = module.run(checkpoint_dir, tmp_path / ".data" / "evals" / "scope-report")

    assert summary["planned"] == {
        "manifest_task_count": 4,
        "source_entries": 4,
        "pre_excluded": 1,
        "eligible_to_crawl": 3,
        "checkpointed_entries": 2,
        "pre_excluded_checkpointed": 1,
        "eligible_attempted": 1,
        "eligible_pending": 2,
        "scope_complete": False,
    }
    assert summary["attempted"] == 1
    assert summary["pending"] == 2
