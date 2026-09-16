import json
from hashlib import sha256

import pytest

from scripts import eval_offerbiu_detail_capture as evaluation


def test_capture_eval_uses_confirmed_rows_and_no_model_or_database(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps({"samples": [{"company": "Example", "crawl": {"raw_jobs": [
        {"title": "Unknown", "jd_url": "https://example.com/unknown", "cohort_status": "unconfirmed"},
        {"title": "C++ Engineer", "jd_url": "https://example.com/jobs/1", "cohort": 2027,
         "cohort_status": "confirmed"},
    ]}}]}), encoding="utf-8")
    calls = []

    def fetch(job, *, timeout_seconds):
        calls.append(job)
        assert timeout_seconds == 5
        return {"detail": "C++", "status": "complete", "capture_evidence": {
            "status": "complete", "method": "official_api", "source_url": job["jd_url"],
            "identity_verified": True, "terminal_observed": True, "remaining_controls": [],
            "content_sha256": sha256(b"C++").hexdigest(),
        }}

    report = evaluation.evaluate([path], tmp_path / ".data/evals/check", limit=1, timeout=5, fetch=fetch)
    assert len(calls) == report["tested_jobs"] == report["capture_complete"] == 1
    assert report["db_writes"] == report["model_calls"] == 0
    assert report["full_company_validation"] is False


def test_output_cannot_escape_eval_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="under .data/evals"):
        evaluation.evaluate([], tmp_path / "unexpected", limit=1)
