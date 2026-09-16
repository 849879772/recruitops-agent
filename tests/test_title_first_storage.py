from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text

from packages.domain.models import Job, RecruitmentBatch
from packages.discovery.company_registry import CompanySourceRegistry
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import JobSnapshot, Storage, create_storage_engine, initialize_schema
from packages.storage.sync import upsert_job_snapshot
from scripts.apply_migrations import split_sql


UTC = timezone.utc


def _storage() -> Storage:
    engine = create_storage_engine("sqlite+pysqlite:///:memory:")
    initialize_schema(engine)
    return Storage(engine)


def _job(**changes: object) -> Job:
    values = {
        "id": "job-1",
        "company_id": "company-1",
        "title": "Python Engineer",
        "detail_url": "https://example.test/jobs/1",
        "batch": RecruitmentBatch.FORMAL,
        "source": "fixture",
        "source_ref": "job-1",
        "created_at": datetime(2026, 9, 9, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 9, tzinfo=UTC),
    }
    values.update(changes)
    return Job(**values)


def test_title_first_fields_roundtrip_and_legacy_upsert_preserves_them() -> None:
    storage = _storage()
    with storage.write_transaction() as session:
        upsert_job_snapshot(
            session,
            _job(),
            capture_status="failed",
            capture_failure_reason="detail_timeout",
            availability_status="inactive",
            title_key="Python Engineer",
        )

    with storage.session() as session:
        row = session.get(JobSnapshot, "job-1")
        assert row is not None
        assert row.capture_status == "failed"
        assert row.capture_failure_reason == "detail_timeout"
        assert row.availability_status == "inactive"
        assert row.title_key == "Python Engineer"

    # Older domain objects do not carry the new fields. Their normal sync path
    # must not erase metadata that a title-first run already persisted.
    with storage.write_transaction() as session:
        upsert_job_snapshot(session, _job(title="Python Engineer (updated)"))

    repository = PostgresRecruitmentRepository(storage)
    assert repository.get_job_snapshot("job-1") == {
        "id": "job-1",
        "company_id": "company-1",
        "title": "Python Engineer (updated)",
        "city": None,
        "detail_url": "https://example.test/jobs/1",
        "capture_status": "failed",
        "capture_failure_reason": "detail_timeout",
        "availability_status": "inactive",
        "title_key": "Python Engineer",
        "match_score": None,
    }


def test_title_first_migration_is_sqlite_compatible_and_non_destructive(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    engine = create_storage_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE job_snapshots (
                id VARCHAR(255) PRIMARY KEY,
                company_id VARCHAR(255) NOT NULL,
                title VARCHAR(512) NOT NULL,
                source_ref VARCHAR(512)
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE company_source_records (
                id VARCHAR(64) PRIMARY KEY,
                company_name VARCHAR(255) NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            "INSERT INTO job_snapshots (id, company_id, title, source_ref) "
            "VALUES ('legacy', 'company-1', 'Legacy title', 'legacy-ref')"
        )
        connection.exec_driver_sql(
            "INSERT INTO company_source_records (id, company_name) "
            "VALUES ('source-1', 'Legacy company')"
        )
        migration = Path(__file__).parents[1] / "migrations/020_title_first_capture.sql"
        for statement in split_sql(migration.read_text(encoding="utf-8")):
            connection.exec_driver_sql(statement)

    columns = {item["name"]: item for item in inspect(engine).get_columns("job_snapshots")}
    source_columns = {
        item["name"]: item for item in inspect(engine).get_columns("company_source_records")
    }
    assert {"capture_status", "capture_failure_reason", "availability_status", "title_key"} <= set(columns)
    assert "company_id" in source_columns

    with engine.connect() as connection:
        legacy = connection.execute(
            text(
                "SELECT capture_status, capture_failure_reason, availability_status, title_key "
                "FROM job_snapshots WHERE id = 'legacy'"
            )
        ).one()
    assert legacy == ("unknown", "", "active", None)
    engine.dispose()


def test_source_registry_binding_is_optional_and_roundtrips(tmp_path: Path) -> None:
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'sources.db'}", initialize=True)
    registry = CompanySourceRegistry(storage)
    source = registry.upsert_source(
        source="offerbiu",
        source_record_id="source-1",
        company_name="Bound company",
        company_id="company-1",
        source_url="https://offerbiu.example/sources",
        entry_url="https://jobs.example/company-1",
    )
    assert source["company_id"] == "company-1"

    refreshed = registry.upsert_source(
        source="offerbiu",
        source_record_id="source-1",
        company_name="Bound company",
        source_url="https://offerbiu.example/sources",
        entry_url="https://jobs.example/other",
    )
    assert refreshed["id"] == source["id"]
    assert refreshed["company_id"] == "company-1"
