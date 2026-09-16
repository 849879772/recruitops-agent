from __future__ import annotations

from datetime import datetime, timedelta, timezone

from packages.domain.models import ApplicationStage
from packages.pipeline.offline import (
    CompanyRunObservation,
    JobVisibility,
    ReconciliationAction,
    decode_offline_state,
    reconcile_offline_jobs,
)
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import ApplicationSnapshot, JobSnapshot, Storage, create_storage_engine, initialize_schema


UTC = timezone.utc


def _storage() -> Storage:
    engine = create_storage_engine("sqlite:///:memory:")
    initialize_schema(engine)
    return Storage(engine)


def _job(
    job_id: str,
    company_id: str,
    *,
    last_seen_at: datetime,
    source_ref: str | None = None,
) -> JobSnapshot:
    return JobSnapshot(
        id=job_id,
        company_id=company_id,
        title=f"Job {job_id}",
        city="Shanghai",
        detail_url=f"https://example.test/jobs/{job_id}",
        cohort=2027,
        cohort_status="confirmed",
        batch="formal",
        first_seen_at=last_seen_at,
        last_seen_at=last_seen_at,
        source="fixture",
        source_ref=source_ref or f"fixture:job:{job_id}:fp:{'a' * 64}",
        created_at=last_seen_at,
        updated_at=last_seen_at,
    )


def _save_jobs(storage: Storage, *jobs: JobSnapshot) -> None:
    with storage.write_transaction() as session:
        session.add_all(jobs)


def _status(storage: Storage, job_id: str) -> tuple[str, int, str | None, datetime | None]:
    with storage.session() as session:
        job = session.get(JobSnapshot, job_id)
        assert job is not None
        state = decode_offline_state(job.source_ref)
        return state.status, state.missing_runs, job.source_ref, job.last_seen_at


def test_first_missing_is_recorded_but_not_inactive() -> None:
    storage = _storage()
    seen = datetime(2026, 8, 20, 8, tzinfo=UTC)
    _save_jobs(storage, _job("job-1", "good", last_seen_at=seen))

    result = reconcile_offline_jobs(
        storage,
        observed_at=seen + timedelta(days=1),
        company_runs=[CompanyRunObservation("good", frozenset())],
        grace_runs=2,
        grace_days=0,
    )

    assert result.missing_count == 1
    assert result.inactive_count == 0
    assert result.plans[0].action == ReconciliationAction.MISSING
    status, missing_runs, source_ref, _last_seen = _status(storage, "job-1")
    assert status == JobVisibility.MISSING
    assert missing_runs == 1
    assert source_ref is not None and source_ref.endswith(f"fixture:job:job-1:fp:{'a' * 64}")


def test_grace_requires_both_missing_runs_and_last_seen_age() -> None:
    storage = _storage()
    seen = datetime(2026, 8, 20, 8, tzinfo=UTC)
    _save_jobs(storage, _job("job-1", "good", last_seen_at=seen))
    run = CompanyRunObservation("good", frozenset())

    first = reconcile_offline_jobs(
        storage,
        observed_at=seen + timedelta(days=1),
        company_runs=[run],
        grace_runs=2,
        grace_days=3,
    )
    second = reconcile_offline_jobs(
        storage,
        observed_at=seen + timedelta(days=2),
        company_runs=[run],
        grace_runs=2,
        grace_days=3,
    )
    third = reconcile_offline_jobs(
        storage,
        observed_at=seen + timedelta(days=3),
        company_runs=[run],
        grace_runs=2,
        grace_days=3,
    )

    assert first.plans[0].reason == "grace_runs_not_reached"
    assert second.inactive_count == 0
    assert second.plans[0].action == ReconciliationAction.MISSING
    assert third.inactive_count == 1
    assert third.plans[0].action == ReconciliationAction.INACTIVE
    assert _status(storage, "job-1")[0] == JobVisibility.INACTIVE


def test_failed_and_incomplete_companies_never_create_missing_observations() -> None:
    storage = _storage()
    seen = datetime(2026, 8, 20, 8, tzinfo=UTC)
    _save_jobs(
        storage,
        _job("good-job", "good", last_seen_at=seen),
        _job("failed-job", "failed", last_seen_at=seen),
        _job("partial-job", "partial", last_seen_at=seen),
    )
    runs = [
        CompanyRunObservation("good", frozenset()),
        CompanyRunObservation("failed", frozenset(), status="failed"),
        CompanyRunObservation("partial", frozenset(), pagination_complete=False),
    ]

    first = reconcile_offline_jobs(
        storage, observed_at=seen + timedelta(days=3), company_runs=runs, grace_runs=2, grace_days=0
    )
    second = reconcile_offline_jobs(
        storage,
        observed_at=seen + timedelta(days=4),
        company_runs=runs,
        grace_runs=2,
        grace_days=0,
    )

    assert first.skipped_company_ids == ("failed", "partial")
    assert first.missing_count == 1
    assert second.inactive_count == 1
    assert _status(storage, "failed-job")[0] == JobVisibility.ACTIVE
    assert _status(storage, "partial-job")[0] == JobVisibility.ACTIVE
    assert _status(storage, "good-job")[0] == JobVisibility.INACTIVE
    page = PostgresRecruitmentRepository(storage).search_jobs(limit=20, offset=0)
    assert {job.id for job in page.items} == {"failed-job", "partial-job"}


def test_reappearing_job_is_restored_and_last_seen_uses_run_observed_at() -> None:
    storage = _storage()
    seen = datetime(2026, 8, 20, 8, tzinfo=UTC)
    original_ref = "fixture:job:job-1:fp:" + "b" * 64
    _save_jobs(storage, _job("job-1", "good", last_seen_at=seen, source_ref=original_ref))
    missing_run = CompanyRunObservation("good", frozenset())
    reconcile_offline_jobs(
        storage,
        observed_at=seen + timedelta(days=1),
        company_runs=[missing_run],
        grace_runs=2,
        grace_days=0,
    )
    reconcile_offline_jobs(
        storage,
        observed_at=seen + timedelta(days=2),
        company_runs=[missing_run],
        grace_runs=2,
        grace_days=0,
    )

    restored = reconcile_offline_jobs(
        storage,
        observed_at=seen + timedelta(days=3),
        company_runs=[CompanyRunObservation("good", frozenset({"job-1"}))],
        grace_runs=2,
        grace_days=0,
    )

    assert restored.restored_count == 1
    assert restored.plans[0].action == ReconciliationAction.RESTORED
    status, missing_runs, source_ref, last_seen_at = _status(storage, "job-1")
    assert status == JobVisibility.ACTIVE
    assert missing_runs == 0
    assert source_ref == original_ref
    assert last_seen_at == (seen + timedelta(days=3)).replace(tzinfo=None)


def test_dry_run_returns_plan_without_storage_write() -> None:
    writes: list[object] = []
    storage = _storage()
    storage.pre_write_hook = lambda engine: writes.append(engine)
    seen = datetime(2026, 8, 20, 8, tzinfo=UTC)
    original_ref = "fixture:job:job-1:fp:" + "c" * 64
    _save_jobs(storage, _job("job-1", "good", last_seen_at=seen, source_ref=original_ref))
    writes.clear()

    result = reconcile_offline_jobs(
        storage,
        observed_at=seen + timedelta(days=1),
        company_runs=[CompanyRunObservation("good", frozenset())],
        grace_runs=2,
        grace_days=0,
        dry_run=True,
    )

    assert result.dry_run is True
    assert result.written is False
    assert result.plans[0].action == ReconciliationAction.MISSING
    assert writes == []
    status, missing_runs, source_ref, last_seen_at = _status(storage, "job-1")
    assert status == JobVisibility.ACTIVE
    assert missing_runs == 0
    assert source_ref == original_ref
    assert last_seen_at == seen.replace(tzinfo=None)


def test_reconciliation_does_not_change_application_record() -> None:
    storage = _storage()
    seen = datetime(2026, 8, 20, 8, tzinfo=UTC)
    _save_jobs(storage, _job("job-1", "good", last_seen_at=seen))
    with storage.write_transaction() as session:
        session.add(
            ApplicationSnapshot(
                id="application-1",
                company_name="Good Co",
                job_title="Job job-1",
                job_id="job-1",
                stage=ApplicationStage.APPLIED.value,
                idempotency_key="application:job-1",
                source="fixture",
                source_ref="application-1",
            )
        )

    reconcile_offline_jobs(
        storage,
        observed_at=seen + timedelta(days=1),
        company_runs=[CompanyRunObservation("good", frozenset())],
        grace_runs=2,
        grace_days=0,
    )
    reconcile_offline_jobs(
        storage,
        observed_at=seen + timedelta(days=2),
        company_runs=[CompanyRunObservation("good", frozenset())],
        grace_runs=2,
        grace_days=0,
    )

    with storage.session() as session:
        application = session.get(ApplicationSnapshot, "application-1")
        assert application is not None
        assert application.stage == ApplicationStage.APPLIED.value
        assert application.job_id == "job-1"
        assert application.idempotency_key == "application:job-1"


def test_source_ref_overflow_is_returned_as_plan_without_clobbering_evidence() -> None:
    storage = _storage()
    seen = datetime(2026, 8, 20, 8, tzinfo=UTC)
    original_ref = "x" * 500
    _save_jobs(storage, _job("job-1", "good", last_seen_at=seen, source_ref=original_ref))

    result = reconcile_offline_jobs(
        storage,
        observed_at=seen + timedelta(days=1),
        company_runs=[CompanyRunObservation("good", frozenset())],
        grace_runs=2,
        grace_days=0,
    )

    assert result.planned_only_count == 1
    assert result.plans[0].persistable is False
    assert result.plans[0].will_write is False
    assert "source_ref_capacity_exceeded" in result.plans[0].reason
    assert _status(storage, "job-1")[2] == original_ref


def test_mapping_input_uses_run_observed_at_and_skips_missing_observation_evidence() -> None:
    storage = _storage()
    seen = datetime(2026, 8, 20, 8, tzinfo=UTC)
    _save_jobs(
        storage,
        _job("job-1", "good", last_seen_at=seen),
        _job("job-2", "unknown", last_seen_at=seen),
    )

    result = reconcile_offline_jobs(
        storage,
        {
            "observed_at": seen + timedelta(days=1),
            "company_results": [
                {"company_id": "good", "status": "completed"},
                {"company_id": "unknown", "status": "completed"},
            ],
        },
        observed_job_ids={"good": []},
        grace_runs=2,
        grace_days=0,
    )

    assert result.processed_company_ids == ("good",)
    assert result.skipped_company_ids == ("unknown",)
    assert result.missing_count == 1
