from __future__ import annotations

import json
from pathlib import Path

from scripts.build_title_first_import_preview import build_preview


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_preview_merges_verified_retry_and_reconciles_company_title(tmp_path: Path) -> None:
    replay = tmp_path / "replay"
    acceptance = tmp_path / "acceptance"
    retry = tmp_path / "retry"
    _write(replay / "successful-jd-reuse.jsonl", [{"proposed_job": {
        "company_id": "new-a", "company": "Acme", "title": "Robot Engineer",
        "jd_raw": "official", "capture_status": "complete",
    }}])
    _write(replay / "new-failures.jsonl", [{
        "company_id": "new-a", "title": "C++ Engineer",
        "placeholder": {"company_id": "new-a", "company": "Acme", "title": "C++ Engineer"},
    }])
    _write(replay / "missing-jd.jsonl", [{
        "company_id": "new-a", "title": "AI Engineer",
        "pending_job": {
            "company_id": "new-a", "company": "Acme", "title": "AI Engineer",
            "capture_status": "pending",
        },
    }])
    _write(replay / "existing-repairs.jsonl", [])
    _write(acceptance / "company-status.jsonl", [{"company_id": "new-a", "status": "complete"}])
    _write(retry / "result-000.json", [{
        "company_id": "new-a", "title": "C++ Engineer", "success": True,
        "response": {"detail": "verified detail", "detail_url": "https://example.test/2"},
        "capture_evidence": {"status": "complete", "identity_verified": True},
    }][0:1])
    _write(retry / "result-001.json", [{
        "company_id": "new-a", "title": "AI Engineer", "success": False,
        "status": "identity_mismatch", "failure_reason": "identity_mismatch",
    }])
    catalog = {
        "companies": [{"id": "old-a", "name": "Acme", "aliases": ["Acme"]}],
        "jobs": [{"id": "old-job", "company_id": "old-a", "title": "Robot Engineer"}],
        "analyses": [{"job_id": "old-job", "match_score": 88}],
    }

    files, report = build_preview(replay, acceptance, [retry], catalog)

    assert report["counts"]["jobs-ready"] == 2
    assert report["reconciliation_actions"] == {"reuse_existing_scored": 1, "insert_new": 1}
    assert files["jobs-ready"][1]["jd_raw"] == "verified detail"
    assert files["jobs-ready"][1]["capture_status"] == "complete"
    assert files["jobs-failed"] == [{
        "company_id": "new-a",
        "company": "Acme",
        "title": "AI Engineer",
        "capture_status": "failed",
        "capture_failure_reason": "identity_mismatch",
        "acceptance_evidence": str((retry / "result-001.json").resolve()),
    }]


def test_preview_excludes_jobs_for_unusable_entry_company(tmp_path: Path) -> None:
    replay = tmp_path / "replay"
    acceptance = tmp_path / "acceptance"
    _write(replay / "successful-jd-reuse.jsonl", [
        {"company_id": "valid", "proposed_job": {
            "company_id": "valid", "company": "Valid", "title": "AI Engineer",
            "jd_raw": "official", "capture_status": "complete",
        }},
        {"company_id": "invalid", "proposed_job": {
            "company_id": "invalid", "company": "Invalid", "title": "Robot Engineer",
            "jd_raw": "official", "capture_status": "complete",
        }},
    ])
    _write(replay / "new-failures.jsonl", [{
        "company_id": "invalid", "title": "C++ Engineer",
        "placeholder": {"company_id": "invalid", "company": "Invalid", "title": "C++ Engineer"},
    }])
    _write(replay / "missing-jd.jsonl", [])
    _write(replay / "existing-repairs.jsonl", [])
    _write(acceptance / "company-status.jsonl", [
        {"company_id": "valid", "status": "complete", "list_status": "complete"},
        {"company_id": "invalid", "status": "unusable", "list_status": "unusable"},
    ])

    files, report = build_preview(replay, acceptance, [], {"companies": [], "jobs": [], "analyses": []})

    assert [row["company_id"] for row in files["companies"]] == ["valid"]
    assert [row["company_id"] for row in files["jobs-ready"]] == ["valid"]
    assert files["jobs-failed"] == []
    assert report["counts"]["companies"] == 1
