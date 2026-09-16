from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from packages.rag.lujie_resume import documents_from_lujie_sqlite


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "lujie.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE ResumeVersion (
            id TEXT PRIMARY KEY,
            jobId TEXT,
            content TEXT NOT NULL
        );
        CREATE TABLE Resume (
            id TEXT PRIMARY KEY,
            jobId TEXT,
            content TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()
    return path


def _insert(path: Path, table: str, record_id: str, content: object, job_id: str | None = None) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        f'INSERT INTO "{table}" (id, jobId, content) VALUES (?, ?, ?)',
        (record_id, job_id, content if isinstance(content, str) else json.dumps(content)),
    )
    connection.commit()
    connection.close()


def test_read_only_original_version_is_prioritized_and_pii_is_removed(tmp_path: Path) -> None:
    path = _database(tmp_path)
    original = {
        "basics": {
            "city": "Shanghai",
            "name": "Alice",
            "email": "alice@example.com",
            "phone": "13800000000",
            "links": ["https://example.com/alice"],
            "editor": {"draft": True},
        },
        "profile": {"summary": "Robotics engineer", "name": "Alice"},
        "education": [{"school": "Example University", "degree": "MSc"}],
        "experiences": [{"company": "Example Lab", "role": "Engineer"}],
        "skills": ["Python", "C++"],
        "_internal": "drop me",
        "editor": "drop me",
    }
    _insert(path, "ResumeVersion", "raw-1", original)
    _insert(
        path,
        "ResumeVersion",
        "optimized-1",
        {"profile": {"summary": "AI rewrite"}, "_optimizationMeta": {"model": "x"}},
    )
    _insert(
        path,
        "ResumeVersion",
        "job-bound-1",
        {"profile": {"summary": "Job-specific"}},
        job_id="job-1",
    )
    _insert(path, "Resume", "fallback-1", {"profile": {"summary": "Fallback"}})
    before = path.read_bytes()

    documents = documents_from_lujie_sqlite(path)

    assert path.read_bytes() == before
    assert len(documents) == 1
    document = documents[0]
    assert document.source == "candidate_evidence"
    assert document.source_ref == f"{path.resolve().as_posix()}#ResumeVersion/raw-1"
    assert document.metadata == {
        "domain": "candidate",
        "kind": "resume",
        "trust": "user_authored",
        "record_type": "ResumeVersion",
        "record_id": "raw-1",
    }
    exported = json.loads(document.content)
    assert exported["basics"] == {"city": "Shanghai"}
    assert exported["profile"] == {"summary": "Robotics engineer"}
    assert exported["education"] == [{"school": "Example University", "degree": "MSc"}]
    assert "alice@example.com" not in document.content
    assert "13800000000" not in document.content
    assert "example.com/alice" not in document.content
    assert "optimized-1" not in document.content
    assert "fallback-1" not in document.content


def test_falls_back_to_user_authored_resume_when_versions_are_not_eligible(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _insert(
        path,
        "ResumeVersion",
        "optimized-1",
        {"profile": {"summary": "AI rewrite"}, "_tailoringBaseResume": {"id": "raw"}},
    )
    _insert(
        path,
        "ResumeVersion",
        "job-bound-1",
        {"profile": {"summary": "Job-specific"}},
        job_id="job-1",
    )
    _insert(path, "Resume", "resume-1", {"profile": {"summary": "User authored"}})

    documents = documents_from_lujie_sqlite(path)

    assert len(documents) == 1
    assert documents[0].metadata["record_type"] == "Resume"
    assert documents[0].metadata["record_id"] == "resume-1"
    assert json.loads(documents[0].content) == {"profile": {"summary": "User authored"}}


def test_explicit_record_selection_excludes_unapproved_local_resumes(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _insert(path, "ResumeVersion", "approved", {"skills": ["C++"]})
    _insert(path, "ResumeVersion", "demo", {"skills": ["Demo skill"]})

    documents = documents_from_lujie_sqlite(
        path,
        approved_record_ids=["approved"],
    )

    assert len(documents) == 1
    assert documents[0].metadata["record_id"] == "approved"
    assert "Demo skill" not in documents[0].content


def test_empty_evidence_returns_empty_list(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _insert(
        path,
        "ResumeVersion",
        "pii-only",
        {
            "basics": {"name": "Alice", "email": "alice@example.com"},
            "name": "Alice",
            "_optimizationMeta": None,
        },
    )
    _insert(path, "Resume", "unused", {"profile": {"name": "Alice"}})

    assert documents_from_lujie_sqlite(path) == []


def test_damaged_json_raises_clear_value_error(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _insert(path, "ResumeVersion", "broken", '{"profile": ')

    with pytest.raises(ValueError, match="invalid JSON content.*ResumeVersion/broken"):
        documents_from_lujie_sqlite(path)


def test_missing_database_or_table_raises_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        documents_from_lujie_sqlite(tmp_path / "missing.db")

    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    with pytest.raises(ValueError, match="missing required table ResumeVersion"):
        documents_from_lujie_sqlite(path)
