from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from packages.domain.models import Application, ApplicationStage
from packages.storage import Storage
from packages.storage.models import CompanySnapshot, JobSnapshot
from packages.storage.sync import upsert_application_snapshot
from scripts.import_title_first_catalog import import_preview


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_formal_import_filters_unusable_preserves_applications_and_is_idempotent(tmp_path: Path) -> None:
    preview = tmp_path / "preview"
    _write(preview / "companies.jsonl", [
        {"company_id": "valid", "company_name": "Valid", "status": "partial", "list_status": "complete", "sources": [{"source_key": "valid-source", "source_url": "https://jobs.example/campus", "capture": {"status": "complete", "raw_job_count": 2}}]},
        {"company_id": "invalid", "company_name": "Invalid", "status": "unusable", "list_status": "unusable", "sources": []},
    ])
    evidence = {"status": "complete", "identity_verified": True, "terminal_observed": True}
    _write(preview / "jobs-ready.jsonl", [{"company_id": "valid", "company": "Valid", "title": "AI Engineer", "detail_url": "https://jobs.example/1", "jd_raw": "official", "capture_status": "complete", "capture_evidence": evidence}])
    _write(preview / "jobs-failed.jsonl", [{"company_id": "valid", "company": "Valid", "title": "C++ Engineer", "detail_url": "https://jobs.example/2", "capture_status": "failed", "capture_failure_reason": "timeout"}])
    _write(preview / "existing-repairs-ready.jsonl", [{
        "id": "old-job", "company_id": "valid", "title": "Old AI Engineer",
        "detail_url": "https://jobs.example/old", "jd_raw": "repaired official JD",
        "capture_status": "complete", "capture_evidence": evidence,
    }])
    backup = tmp_path / "backup.dump"
    backup.write_bytes(b"verified backup")
    digest = sha256(backup.read_bytes()).hexdigest()
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'catalog.db'}", initialize=True)
    with storage.write_transaction() as session:
        session.add(CompanySnapshot(
            id="valid", name="Valid", aliases=[], campus_url="https://jobs.example/campus",
            crawler_key="test", integration_status="connected", source="test", source_ref="valid",
        ))
        session.add(JobSnapshot(
            id="old-job", company_id="valid", title="Old AI Engineer", city=None,
            detail_url="https://jobs.example/old", jd_raw=None, cohort=2027,
            cohort_status="confirmed", batch="formal", match_score=None,
            capture_status="failed", capture_failure_reason="old failure",
            availability_status="active", capture_evidence={}, source="test", source_ref="old-job",
        ))
        upsert_application_snapshot(session, Application(
            id="app-1", company_name="Old", job_title="Old Job", stage=ApplicationStage.APPLIED,
            idempotency_key="app-1", source="test", source_ref="app-1",
        ))

    preview_report = import_preview(storage, preview, backup=backup, backup_sha256=digest)
    assert preview_report["written"] is False
    assert preview_report["planned"]["new_jobs"] == 2
    first = import_preview(storage, preview, backup=backup, backup_sha256=digest, apply=True)
    second = import_preview(storage, preview, backup=backup, backup_sha256=digest, apply=True)

    assert first["applications"]["unchanged"] is True
    assert first["catalog_after"]["companies"] == 1
    assert first["catalog_after"]["jobs"] == 3
    assert second["planned"]["new_jobs"] == 0
    with storage.session() as session:
        companies = list(session.query(CompanySnapshot))
        jobs = {row.title: row for row in session.query(JobSnapshot)}
    assert [row.id for row in companies] == ["valid"]
    assert jobs["AI Engineer"].capture_status == "complete"
    assert jobs["C++ Engineer"].capture_status == "failed"
    assert jobs["C++ Engineer"].jd_raw is None
    assert jobs["Old AI Engineer"].capture_status == "complete"
    assert jobs["Old AI Engineer"].jd_raw == "repaired official JD"
