from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from threading import Lock
from urllib.parse import urlsplit

import pytest

from scripts import verify_title_first_retry_samples as pilot


def _receipt(detail: str, source_url: str) -> dict[str, object]:
    return {
        "status": "complete",
        "method": "fixture_detail",
        "source_url": source_url,
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": sha256(detail.encode("utf-8")).hexdigest(),
    }


def _sample(*, index: int = 0, detail_url: str = "https://example.test/jobs/old-1") -> dict:
    job = {
        "id": "old-1",
        "company": "Example Co",
        "company_id": "company-1",
        "title": "C++ Engineer",
        "detail_url": detail_url,
        "jd_url": detail_url,
    }
    return {
        "index": index,
        "stratum": "existing_empty",
        "job": job,
        "existing_job_ids": [job["id"]],
        "historical_row": {},
    }


def _run_sample(sample: dict, output, fetch):
    domain = urlsplit(sample["job"]["detail_url"]).netloc
    return pilot.run_sample(sample, output, {domain: Lock()}, timeout=3, fetch=fetch)


def test_run_sample_accepts_detail_with_valid_capture_receipt(tmp_path) -> None:
    sample = _sample()
    detail = "Official C++ detail"
    calls = []

    def fetch(job, *, timeout_seconds):
        calls.append((job["id"], timeout_seconds))
        return {
            "status": "complete",
            "detail": detail,
            "capture_evidence": _receipt(detail, job["detail_url"]),
        }

    result = _run_sample(sample, tmp_path, fetch)

    assert calls == [("old-1", 3)]
    assert result["capture_complete"] is True
    assert result["assessment_reason"] == "capture_complete"
    assert result["failure_reason"] is None
    assert result["response"]["capture_evidence"]["content_sha256"] == sha256(
        detail.encode("utf-8")
    ).hexdigest()


def test_run_sample_rejects_detail_with_bad_capture_hash(tmp_path) -> None:
    sample = _sample()
    detail = "Official C++ detail"

    def fetch(job, *, timeout_seconds):
        return {
            "status": "complete",
            "detail": detail,
            "capture_evidence": {
                **_receipt(detail, job["detail_url"]),
                "content_sha256": sha256(b"tampered detail").hexdigest(),
            },
        }

    result = _run_sample(sample, tmp_path, fetch)

    assert result["capture_complete"] is False
    assert result["assessment_reason"] == "capture_content_changed"
    assert result["failure_reason"] is not None


def test_run_sample_exception_result_preserves_original_job_identity(tmp_path) -> None:
    sample = _sample(detail_url="https://example.test/jobs/original")
    original_job = dict(sample["job"])

    def fetch(job, *, timeout_seconds):
        raise RuntimeError("fixture fetch failed")

    result = _run_sample(sample, tmp_path, fetch)

    assert result["response"] == {
        "status": "exception",
        "error_type": "RuntimeError",
        "error": "fixture fetch failed",
        "detail": "",
    }
    assert {key: result["job"][key] for key in ("id", "title", "detail_url")} == {
        key: original_job[key] for key in ("id", "title", "detail_url")
    }
    assert result["capture_complete"] is False
    assert result["failure_reason"] == "RuntimeError"


def test_run_sample_resume_reuses_saved_result_without_fetch(tmp_path) -> None:
    sample = _sample()
    calls = 0

    def fetch(job, *, timeout_seconds):
        nonlocal calls
        calls += 1
        detail = "Resume-safe detail"
        return {
            "status": "complete",
            "detail": detail,
            "capture_evidence": _receipt(detail, job["detail_url"]),
        }

    first = _run_sample(sample, tmp_path, fetch)

    def must_not_fetch(job, *, timeout_seconds):
        raise AssertionError("resume fetched an existing result")

    second = _run_sample(sample, tmp_path, must_not_fetch)

    assert calls == 1
    assert second == first
    assert json.loads((tmp_path / "result-000.json").read_text(encoding="utf-8")) == first


def _history_row(
    company_id: str,
    company_name: str,
    title: str,
    detail_url: str,
    *,
    existing_job_ids: list[str] | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "company_id": company_id,
        "company_name": company_name,
        "title": title,
        "detail_url": detail_url,
        "existing_job_ids": existing_job_ids or [],
        "observations": [
            {
                "source_url": f"{urlsplit(detail_url).scheme}://{urlsplit(detail_url).netloc}/campus",
                "raw_job": {"title": title, "detail_url": detail_url},
            }
        ],
    }
    if reason is not None:
        row["capture_failure_reason"] = reason
    return row


def _write_jsonl(path, rows) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_prepare_inputs(source, *, old_count: int) -> tuple[list[dict], list[dict], list[dict]]:
    old_rows = [
        _history_row(
            f"old-company-{index}",
            f"Old Company {index}",
            f"Old Title {index}",
            f"https://old-{index}.test/jobs/{index}",
            existing_job_ids=[f"old-job-{index}"],
        )
        for index in range(old_count)
    ]
    reasons = [f"failure-{index}" for index in range(9)]
    failure_rows = []
    for reason in reasons:
        failure_rows.extend(
            [
                _history_row(
                    f"{reason}-company-1",
                    f"{reason} Company 1",
                    f"{reason} title 1",
                    f"https://{reason}-1.test/jobs/1",
                    reason=reason,
                ),
                _history_row(
                    f"{reason}-company-1",
                    f"{reason} Company 1",
                    f"{reason} title duplicate",
                    f"https://{reason}-duplicate.test/jobs/2",
                    reason=reason,
                ),
                _history_row(
                    f"{reason}-company-2",
                    f"{reason} Company 2",
                    f"{reason} title 2",
                    f"https://{reason}-2.test/jobs/2",
                    reason=reason,
                ),
                _history_row(
                    f"{reason}-company-3",
                    f"{reason} Company 3",
                    f"{reason} title 3",
                    f"https://{reason}-3.test/jobs/3",
                    reason=reason,
                ),
            ]
        )
    missing_rows = [
        _history_row(
            f"missing-company-{index}",
            f"Missing Company {index}",
            f"Missing title {index}",
            f"https://missing-{index}.test/jobs/{index}",
        )
        for index in range(8)
    ]
    missing_rows.insert(
        1,
        _history_row(
            "missing-company-duplicate",
            "Missing Duplicate",
            "Missing duplicate domain",
            "https://missing-0.test/jobs/duplicate",
        ),
    )
    return old_rows, failure_rows, missing_rows


def test_prepare_selects_all_old_bounded_failures_and_missing_domains(tmp_path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    old_rows, failure_rows, missing_rows = _write_prepare_inputs(source, old_count=14)
    _write_jsonl(source / "existing-repairs.jsonl", old_rows)
    _write_jsonl(source / "new-failures.jsonl", failure_rows)
    _write_jsonl(source / "missing-jd.jsonl", missing_rows)

    manifest = pilot.prepare(source, output)
    samples = manifest["samples"]
    strata = Counter(sample["stratum"] for sample in samples)

    assert len(samples) == 40
    assert strata["existing_empty"] == 14
    assert strata["missing_detail"] == 8
    failure_samples = [sample for sample in samples if sample["stratum"] in {row["capture_failure_reason"] for row in failure_rows}]
    failure_counts = Counter(sample["stratum"] for sample in failure_samples)
    assert failure_counts == Counter({f"failure-{index}": 2 for index in range(9)})
    for reason in failure_counts:
        companies = [sample["job"]["company_id"] for sample in failure_samples if sample["stratum"] == reason]
        assert len(companies) == len(set(companies)) == 2

    missing_samples = [sample for sample in samples if sample["stratum"] == "missing_detail"]
    assert len({urlsplit(sample["job"]["detail_url"]).netloc for sample in missing_samples}) == 8
    assert {sample["job"]["id"] for sample in samples if sample["stratum"] == "existing_empty"} == {
        f"old-job-{index}" for index in range(14)
    }
    assert json.loads((output / "selection.json").read_text(encoding="utf-8")) == manifest


def test_prepare_rejects_pilot_over_40_samples(tmp_path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    old_rows, failure_rows, missing_rows = _write_prepare_inputs(source, old_count=15)
    _write_jsonl(source / "existing-repairs.jsonl", old_rows)
    _write_jsonl(source / "new-failures.jsonl", failure_rows)
    _write_jsonl(source / "missing-jd.jsonl", missing_rows)

    with pytest.raises(ValueError, match="40-job bound"):
        pilot.prepare(source, output)
