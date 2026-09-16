from __future__ import annotations

import json
from pathlib import Path

from scripts.build_title_first_company_acceptance import build_acceptance


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_company_acceptance_applies_each_success_once(tmp_path: Path) -> None:
    replay = tmp_path / "replay"
    retry_a = tmp_path / "retry-a"
    retry_b = tmp_path / "retry-b"
    replay.mkdir()
    retry_a.mkdir()
    retry_b.mkdir()
    _write_jsonl(replay / "company-status.jsonl", [{
        "company_id": "c1",
        "status": "partial",
        "list_status": "complete",
        "source_count": 2,
        "detail_failure_count": 1,
        "detail_pending_count": 1,
        "existing_detail_failure_count": 0,
        "existing_detail_pending_count": 0,
        "existing_unrediscovered_failure_count": 0,
        "existing_unrediscovered_pending_count": 0,
    }])
    _write_jsonl(replay / "new-failures.jsonl", [{"company_id": "c1", "title": "C++ Engineer"}])
    _write_jsonl(replay / "missing-jd.jsonl", [{"company_id": "c1", "title": "Robot Engineer"}])
    _write_jsonl(replay / "existing-repairs.jsonl", [])
    success = {"company_id": "c1", "title": "C++ Engineer", "success": True, "status": "complete"}
    (retry_a / "result-000.json").write_text(json.dumps(success), encoding="utf-8")
    (retry_b / "result-000.json").write_text(json.dumps(success), encoding="utf-8")

    companies, report = build_acceptance(replay, [retry_a, retry_b])

    assert report["unique_successful_retries"] == 1
    assert report["recovered"] == {"new-failures": 1}
    assert report["statuses"] == {"partial": 1}
    assert companies[0]["detail_failure_count"] == 0
    assert companies[0]["detail_pending_count"] == 1


def test_company_acceptance_excludes_unusable_entry_companies(tmp_path: Path) -> None:
    replay = tmp_path / "replay"
    replay.mkdir()
    _write_jsonl(replay / "company-status.jsonl", [
        {
            "company_id": "valid-failed",
            "status": "failed",
            "list_status": "failed",
            "source_count": 1,
        },
        {
            "company_id": "invalid-entry",
            "status": "unusable",
            "list_status": "unusable",
            "source_count": 1,
        },
    ])
    for name in ("new-failures", "missing-jd", "existing-repairs"):
        _write_jsonl(replay / f"{name}.jsonl", [])

    companies, report = build_acceptance(replay, [])

    assert [row["company_id"] for row in companies] == ["valid-failed"]
    assert report["statuses"] == {"failed": 1}
