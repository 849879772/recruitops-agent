from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import pytest
import yaml
from sqlalchemy import func, select

from packages.migration import (
    LegacyMigrationError,
    LegacySourcePaths,
    run_migration,
    verify_source_read_only,
)
from packages.storage import (
    ApplicationSnapshot,
    CompanySnapshot,
    JobAnalysisSnapshot,
    JobSnapshot,
    ScheduleEventSnapshot,
    Storage,
)


UTC = timezone.utc


def _fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "legacy"
    data = source / "data"
    data.mkdir(parents=True)
    (source / "config.yaml").write_text(
        """
companies:
  - name: Legacy Co
    careers_url: https://legacy.example/campus
    crawler: fixture
    aliases: [Legacy]
  - name: Manual Co
""".strip()
        + "\n",
        encoding="utf-8",
    )
    database = data / "jobs.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                company TEXT,
                title TEXT,
                city TEXT,
                jd_url TEXT,
                jd_raw TEXT,
                source TEXT,
                crawled_at TEXT,
                last_seen_at TEXT,
                cohort INTEGER,
                cohort_status TEXT,
                recruitment_track TEXT
            );
            CREATE TABLE job_analysis (
                job_id INTEGER UNIQUE,
                match_score INTEGER,
                advantages TEXT,
                gaps TEXT,
                summary TEXT,
                recommendation TEXT,
                score_breakdown TEXT,
                evidence TEXT,
                evidence_level TEXT,
                matched_directions TEXT,
                primary_match_direction TEXT,
                analysis_status TEXT,
                model TEXT,
                analyzed_at TEXT
            );
            """
        )
        connection.execute(
            """
            INSERT INTO jobs VALUES
            (101, 'Legacy Co', 'Python Engineer', 'Shanghai',
             'https://legacy.example/jobs/101', 'legacy jd', 'legacy-crawler',
             '2026-08-19T01:00:00+00:00', '2026-08-20T02:00:00+00:00',
             2027, 'confirmed', 'formal')
            """
        )
        connection.execute(
            """
            INSERT INTO job_analysis VALUES
            (101, 91, '["Python"]', '["Distributed systems"]', 'Strong fit',
             'Recommend', '{"skills": 0.95}',
             '[{"source": "jd", "text": "Python"}]', 'verified',
             '["software"]', 'software', 'complete', 'fixture-model',
             '2026-08-20T03:00:00+00:00')
            """
        )
        connection.commit()
    (data / "applications.json").write_text(
        json.dumps(
            [
                {
                    "id": 7,
                    "job_id": 101,
                    "company": "Legacy Co",
                    "title": "Python Engineer",
                    "current_stage": "written",
                    "stages": [{"stage": "applied", "date": "2026-08-18"}],
                    "events": [
                        {
                            "id": 33,
                            "event_type": "written",
                            "event_date": "2026-08-21",
                            "event_time": "19:30",
                            "created_at": "2026-08-20T04:00:00+00:00",
                            "note": "online",
                        }
                    ],
                    "record_url": "https://legacy.example/applications/7",
                    "note": "keep",
                    "applied_at": "2026-08-18",
                    "updated_at": "2026-08-20T05:00:00+00:00",
                    "source_stage": "written",
                    "source_status": "Written test",
                    "source_status_synced_at": "2026-08-20T06:00:00+00:00",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return source, database, data / "applications.json"


def _agent_database(tmp_path: Path) -> Path:
    database = tmp_path / "agent.sqlite"
    storage = Storage.from_url(f"sqlite:///{database}")
    storage.initialize()
    storage.engine.dispose()
    return database


def test_dry_run_reads_legacy_inputs_without_writing_source_or_output(tmp_path: Path) -> None:
    source, database, applications = _fixture(tmp_path)
    output = tmp_path / "agent" / "config" / "companies.yaml"
    source_files = {
        source / "config.yaml": (source / "config.yaml").read_bytes(),
        database: database.read_bytes(),
        applications: applications.read_bytes(),
    }

    report = run_migration(
        source_root=source,
        database_url=f"sqlite:///{_agent_database(tmp_path)}",
        companies_output=output,
        mode="dry-run",
    )

    assert report.mode == "dry-run"
    assert report.as_dict()["companies"] == 2
    assert report.as_dict()["jobs"] == 1
    assert report.as_dict()["analyses"] == 1
    assert report.as_dict()["applications"] == 1
    assert report.as_dict()["schedule_events"] == 1
    assert report.source_read_only_verified is True
    assert report.source_unchanged is True
    assert report.database_written is False
    assert report.companies_yaml_written is False
    assert not output.exists()
    assert {path: path.read_bytes() for path in source_files} == source_files

    check = verify_source_read_only(LegacySourcePaths.from_root(source))
    assert check == {"sqlite_mode": "ro", "source_unchanged": True, "files_checked": 3}


def test_apply_preserves_ids_times_scores_and_is_idempotent(tmp_path: Path) -> None:
    source, database, applications = _fixture(tmp_path)
    target = _agent_database(tmp_path)
    output = tmp_path / "agent" / "config" / "companies.yaml"
    source_files = {
        source / "config.yaml": (source / "config.yaml").read_bytes(),
        database: database.read_bytes(),
        applications: applications.read_bytes(),
    }

    first = run_migration(
        source_root=source,
        database_url=f"sqlite:///{target}",
        companies_output=output,
        mode="apply",
    )
    second = run_migration(
        source_root=source,
        database_url=f"sqlite:///{target}",
        companies_output=output,
        mode="apply",
    )

    assert first.as_dict() == second.as_dict()
    assert first.database_written is True
    assert first.companies_yaml_written is True
    exported = yaml.safe_load(output.read_text(encoding="utf-8"))
    assert set(exported) == {"companies"}
    assert [row["id"] for row in exported["companies"]] == ["config-0", "config-1"]
    assert "profile" not in exported and "deepseek" not in exported

    storage = Storage.from_url(f"sqlite:///{target}")
    try:
        with storage.session() as session:
            assert session.scalar(select(func.count()).select_from(CompanySnapshot)) == 2
            assert session.scalar(select(func.count()).select_from(JobSnapshot)) == 1
            assert session.scalar(select(func.count()).select_from(JobAnalysisSnapshot)) == 1
            assert session.scalar(select(func.count()).select_from(ApplicationSnapshot)) == 1
            assert session.scalar(select(func.count()).select_from(ScheduleEventSnapshot)) == 1

            job = session.scalar(select(JobSnapshot).where(JobSnapshot.id == "101"))
            analysis = session.scalar(
                select(JobAnalysisSnapshot).where(JobAnalysisSnapshot.job_id == "101")
            )
            application = session.scalar(
                select(ApplicationSnapshot).where(ApplicationSnapshot.id == "7")
            )
            event = session.scalar(
                select(ScheduleEventSnapshot).where(ScheduleEventSnapshot.id == "33")
            )
            assert job is not None and job.match_score == 91
            assert job.company_id == "config-0"
            assert job.first_seen_at == datetime(2026, 8, 19, 1, 0)
            assert job.last_seen_at == datetime(2026, 8, 20, 2, 0)
            assert analysis is not None and analysis.match_score == 91
            assert analysis.analyzed_at == datetime(2026, 8, 20, 3, 0)
            assert application is not None and application.idempotency_key == "application:7"
            assert application.stage == "written"
            assert event is not None and event.event_time.isoformat() == "19:30:00"
            assert event.created_at == datetime(2026, 8, 20, 4, 0)
    finally:
        storage.engine.dispose()

    assert {path: path.read_bytes() for path in source_files} == source_files


def test_apply_creates_a_stable_historical_company_for_unconfigured_jobs(tmp_path: Path) -> None:
    source, database, _applications = _fixture(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO jobs VALUES
            (102, 'Historical Co', 'C++ Engineer', 'Beijing',
             'https://historical.example/jobs/102', 'historical jd', 'legacy-crawler',
             '2026-08-19T01:00:00+00:00', '2026-08-20T02:00:00+00:00',
             2027, 'confirmed', 'formal')
            """
        )
        connection.commit()

    target = _agent_database(tmp_path)
    output = tmp_path / "agent" / "config" / "companies.yaml"
    report = run_migration(
        source_root=source,
        database_url=f"sqlite:///{target}",
        companies_output=output,
        mode="apply",
    )

    storage = Storage.from_url(f"sqlite:///{target}")
    try:
        with storage.session() as session:
            company = session.scalar(
                select(CompanySnapshot).where(CompanySnapshot.name == "Historical Co")
            )
            job = session.scalar(select(JobSnapshot).where(JobSnapshot.id == "102"))
            assert company is not None
            assert company.integration_status == "not_connected"
            assert job is not None and job.company_id == company.id
    finally:
        storage.engine.dispose()

    exported = yaml.safe_load(output.read_text(encoding="utf-8"))["companies"]
    assert any(row["name"] == "Historical Co" for row in exported)
    assert report.warnings == ("created 1 historical companies from jobs.db",)


def test_apply_requires_existing_agent_snapshot_tables(tmp_path: Path) -> None:
    source, _, _ = _fixture(tmp_path)
    target = tmp_path / "uninitialized.sqlite"
    output = tmp_path / "companies.yaml"

    with pytest.raises(LegacyMigrationError, match="snapshot tables are missing"):
        run_migration(
            source_root=source,
            database_url=f"sqlite:///{target}",
            companies_output=output,
            mode="apply",
        )

    assert not output.exists()
