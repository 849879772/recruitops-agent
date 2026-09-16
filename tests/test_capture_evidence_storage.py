from datetime import datetime, timezone

from packages.domain.models import Job, RecruitmentBatch
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import JobSnapshot, Storage, create_storage_engine, initialize_schema
from packages.storage.sync import upsert_job_snapshot


UTC = timezone.utc


def _storage() -> Storage:
    engine = create_storage_engine("sqlite:///:memory:")
    initialize_schema(engine)
    return Storage(engine)


def _job(**changes: object) -> Job:
    values = {
        "id": "job-1",
        "company_id": "company-1",
        "title": "Python Engineer",
        "detail_url": "https://example.test/jobs/1",
        "batch": RecruitmentBatch.FORMAL,
        "source": "test",
        "source_ref": "job-1",
        "created_at": datetime(2026, 9, 8, tzinfo=UTC),
        "updated_at": datetime(2026, 9, 8, tzinfo=UTC),
    }
    values.update(changes)
    return Job(**values)


def test_capture_evidence_roundtrips_through_sqlite_and_repository() -> None:
    storage = _storage()
    evidence = {
        "status": "complete",
        "method": "rendered_detail",
        "source_url": "https://example.test/jobs/1",
        "identity_verified": True,
        "terminal_observed": True,
        "remaining_controls": [],
        "content_sha256": "abc123",
    }

    with storage.write_transaction() as session:
        upsert_job_snapshot(session, _job(capture_evidence=evidence))
        updated_evidence = {**evidence, "content_sha256": "updated-sha"}
        upsert_job_snapshot(session, _job(capture_evidence=updated_evidence))

    with storage.session() as session:
        stored = session.get(JobSnapshot, "job-1")

    assert stored is not None
    assert stored.capture_evidence == updated_evidence

    repository = PostgresRecruitmentRepository(storage)
    result = repository.get_job("job-1")
    assert result is not None
    assert result.job.capture_evidence == updated_evidence


def test_capture_evidence_defaults_to_empty_for_old_job_objects() -> None:
    assert _job().capture_evidence == {}

    storage = _storage()
    with storage.write_transaction() as session:
        upsert_job_snapshot(session, _job())

    with storage.session() as session:
        stored = session.get(JobSnapshot, "job-1")

    assert stored is not None
    assert stored.capture_evidence == {}
