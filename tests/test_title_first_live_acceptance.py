from __future__ import annotations

from hashlib import sha256
from threading import Lock
from urllib.parse import urlsplit

import json

from scripts import run_title_first_live_acceptance as acceptance


def _row(*, existing: bool = False) -> dict:
    detail_url = "https://example.test/jobs/1"
    row = {
        "company_id": "company-1",
        "company_name": "Example Co",
        "title": "C++ Software Engineer",
        "detail_url": detail_url,
        "source_urls": ["https://example.test/campus"],
        "observations": [
            {
                "source_url": "https://example.test/campus",
                "raw_job": {"title": "C++ Software Engineer", "jd_url": detail_url},
            }
        ],
    }
    if existing:
        row.update(
            {
                "existing_job_ids": ["old-job-1"],
                "repair_target_job": {"id": "old-job-1", "cohort": 2027, "cohort_status": "confirmed"},
            }
        )
    return row


def _sample(*, existing: bool = False) -> dict:
    return acceptance._sample_record(
        0,
        "existing_empty" if existing else "missing_detail",
        _row(existing=existing),
        existing=existing,
        profile={},
    )


def _complete_response(job: dict) -> dict:
    detail = "Official C++ detail"
    return {
        "status": "complete",
        "source": "render",
        "detail": detail,
        "detail_url": job["detail_url"],
        "identity_status": "matched",
        "identity_evidence": ["title:C++ Software Engineer"],
        "capture_evidence": {
            "status": "complete",
            "method": "detail_dom",
            "source_url": job["detail_url"],
            "identity_verified": True,
            "terminal_observed": True,
            "remaining_controls": [],
            "content_sha256": sha256(detail.encode("utf-8")).hexdigest(),
        },
    }


def test_run_sample_requires_verified_official_capture(tmp_path) -> None:
    sample = _sample()
    domain = urlsplit(sample["request_job"]["detail_url"]).netloc
    result = acceptance.run_sample(
        sample,
        tmp_path,
        {domain: Lock()},
        3,
        fetch=lambda job, *, timeout_seconds: _complete_response(job),
    )

    assert result["success"] is True
    assert result["capture_assessment"]["complete"] is True
    assert result["validation"]["passed"] is True
    assert result["jd_sha256"] == result["capture_evidence"]["content_sha256"]


def test_sample_reapplies_current_company_interaction_recipe() -> None:
    sample = acceptance._sample_record(
        0,
        "existing_empty",
        _row(existing=True),
        existing=True,
        profile={},
        company_recipe={
            "entry_click_texts": ["校园招聘"],
            "detail_interaction": {"mode": "inline", "trigger_selector": ".job-card"},
        },
    )

    assert sample["request_job"]["entry_click_texts"] == ["校园招聘"]
    assert sample["request_job"]["detail_interaction"]["trigger_selector"] == ".job-card"


def test_run_sample_persists_stable_failure_and_identity_slot(tmp_path) -> None:
    sample = _sample()
    domain = urlsplit(sample["request_job"]["detail_url"]).netloc
    result = acceptance.run_sample(
        sample,
        tmp_path,
        {domain: Lock()},
        3,
        fetch=lambda job, *, timeout_seconds: {
            "status": "identity_mismatch",
            "detail": "",
            "detail_url": job["detail_url"],
            "identity_status": "mismatch",
            "identity_evidence": ["title:Other title"],
            "identity_diagnostic": {"status": "identity_mismatch", "observation_status": "observed"},
            "capture_evidence": {},
        },
    )

    assert result["success"] is False
    assert result["failure_reason"] == "identity_mismatch"
    assert result["request_detail_url"] == sample["detail_url"]
    assert result["identity"]["diagnostic"]["status"] == "identity_mismatch"


def test_run_sample_circuits_same_domain_after_captcha(tmp_path) -> None:
    first = _sample()
    second = _sample()
    second["index"] = 1
    second["job_id"] = "new-job-2"
    domain = urlsplit(first["request_job"]["detail_url"]).netloc
    barriers: dict[str, str] = {}
    calls = 0

    def fetch(job: dict, *, timeout_seconds: float) -> dict:
        nonlocal calls
        calls += 1
        return {"status": "captcha_required", "detail": "", "detail_url": job["detail_url"]}

    first_result = acceptance.run_sample(first, tmp_path, {domain: Lock()}, 3, fetch=fetch, domain_barriers=barriers)
    second_result = acceptance.run_sample(second, tmp_path, {domain: Lock()}, 3, fetch=fetch, domain_barriers=barriers)

    assert first_result["failure_reason"] == "captcha_required"
    assert second_result["status"] == "domain_blocked"
    assert second_result["failure_reason"] == "domain_blocked"
    assert calls == 1


def test_isolated_persistence_reuses_same_jobs_and_sources(tmp_path) -> None:
    samples = [_sample(existing=True), _sample(existing=False)]
    for sample in samples:
        sample["source_urls"] = ["https://example.test/campus?sourceToken=secret"]
    samples[1]["index"] = 1
    samples[1]["job_id"] = "new-job-1"
    results = [
        {
            "job_id": samples[0]["job_id"],
            "company": samples[0]["company"],
            "success": False,
            "failure_reason": "identity_mismatch",
            "response": {"detail": "", "status": "identity_mismatch"},
            "capture_evidence": {},
        },
        {
            "job_id": samples[1]["job_id"],
            "company": samples[1]["company"],
            "success": True,
            "failure_reason": None,
            "response": _complete_response(samples[1]["request_job"]),
            "capture_evidence": _complete_response(samples[1]["request_job"])["capture_evidence"],
        },
    ]
    storage = acceptance._seed_database(tmp_path / "acceptance.sqlite", samples)
    first = acceptance._persist_pass(storage, samples, results)
    first_snapshot = acceptance.database_snapshot(storage)
    second = acceptance._persist_pass(storage, samples, results)
    second_snapshot = acceptance.database_snapshot(storage)

    assert first["counts"]["jobs"] == {"updated": 1, "inserted": 1}
    assert second["counts"]["jobs"] == {"reused": 2}
    assert second["counts"]["company_sources"] == {"reused": 1}
    assert first["formal_database_writes"] == 0
    assert first["isolated_database_write_transactions"] == 1
    assert first_snapshot["counts"] == second_snapshot["counts"]
    assert first_snapshot["hashes"] == second_snapshot["hashes"]
    assert first_snapshot["old_job_ids"] == ["old-job-1"]
    assert first_snapshot["hashes"]["applications"] == second_snapshot["hashes"]["applications"]


def test_prepare_retry_selects_only_requested_failed_results(tmp_path, monkeypatch) -> None:
    previous = tmp_path / "previous"
    output = tmp_path / "retry"
    previous.mkdir()
    output.mkdir()
    samples = [_sample(), _sample()]
    samples[0]["stratum"] = "missing_detail"
    samples[1]["index"] = 1
    samples[1]["platform"] = "beisen"
    samples[1]["stratum"] = "existing_empty"
    samples[1]["company_id"] = "configured-company"
    monkeypatch.setattr(
        acceptance,
        "_load_company_recipes",
        lambda: {
            "configured-company": {
                "entry_click_texts": ["校园招聘"],
                "detail_interaction": {"mode": "inline", "trigger_selector": ".job-card"},
            }
        },
    )
    (previous / "selection.json").write_text(
        json.dumps({"samples": samples, "inputs": [], "input_counts": {}}),
        encoding="utf-8",
    )
    for index, result in enumerate(
        [
            {"index": 0, "success": False, "platform": "custom_render", "failure_reason": "content_incomplete"},
            {"index": 1, "success": False, "platform": "beisen", "failure_reason": "identity_mismatch"},
        ]
    ):
        (previous / f"result-{index:03}.json").write_text(json.dumps(result), encoding="utf-8")

    manifest = acceptance.prepare_retry(
        previous,
        output,
        platforms={"beisen"},
        statuses={"identity_mismatch"},
        strata={"existing_empty"},
    )

    assert len(manifest["samples"]) == 1
    assert manifest["samples"][0]["index"] == 0
    assert manifest["samples"][0]["historical_failure_reason"] == "identity_mismatch"
    assert manifest["samples"][0]["request_job"]["entry_click_texts"] == ["校园招聘"]
    assert manifest["samples"][0]["input_job"]["detail_interaction"]["mode"] == "inline"

    empty = acceptance.prepare_retry(
        previous,
        tmp_path / "wrong-stratum",
        platforms={"beisen"},
        strata={"missing_detail"},
    )
    assert empty["samples"] == []
