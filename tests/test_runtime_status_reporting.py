"""Offline regressions for task receipts and independent persistence counts."""

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from threading import Event
from types import SimpleNamespace

import pytest

from packages.discovery.company_registry import CompanySourceRecord
from packages.orchestration import DailyRecruitmentSync
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.scheduler import LocalTaskScheduler, TaskContext, TaskType
from packages.scheduler.models import RunStatus
from packages.scheduler.runtime import build_runtime_task_handlers
from packages.storage import Storage
from packages.storage.models import CompanySnapshot, JobSnapshot
from packages.storage.sync import AgentStateStore
from packages.tools.operations import OperationalTaskRunInput, OperationalTaskRunner
from packages.tools.typed import CompanyCoverageInput, company_coverage


TASK = TaskType.DAILY_RECRUITMENT_INTELLIGENCE.value


@pytest.mark.parametrize("modules", [
    ("packages.scheduler.runtime", "packages.tools.typed", "packages.tools.operations"),
    ("packages.tools.typed", "packages.tools.operations", "packages.scheduler.runtime"),
    ("packages.tools.operations", "packages.scheduler.runtime", "packages.tools.typed"),
])
def test_status_modules_import_in_fresh_process_without_cycles(modules):
    result = subprocess.run(
        [sys.executable, "-c", "import importlib, sys; [importlib.import_module(name) for name in sys.argv[1:]]", *modules],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("status", [[], {}, ["failed"], {"status": "failed"}, None, 1])
def test_generic_handler_non_string_status_is_not_a_business_failure(tmp_path, status):
    payload = {"status": status, "sync_status": status, "daily_sync": {"status": status}}
    result = LocalTaskScheduler(lock_path=tmp_path / "task.lock").run(
        TASK, lambda _: payload, max_retries=5,
    )
    assert result.status is RunStatus.SUCCESS
    assert result.attempts == 1
    assert result.value is payload


@pytest.mark.parametrize("payload", [
    {"status": "failed", "error": "crawl failed"},
    {"status": "completed", "daily_sync": {"status": "failed", "error": "crawl failed"}},
    {"sync_status": "failed", "error": "crawl failed"},
    {"status": "configuration_required", "message": "keywords required"},
])
def test_business_failure_is_terminal_and_keeps_value(tmp_path, payload):
    calls = []
    scheduler = LocalTaskScheduler(lock_path=tmp_path / "task.lock")
    result = scheduler.run(
        TASK, lambda context: calls.append(context.run_id) or payload,
        max_retries=5, run_id="original-run",
    )
    assert result.status is RunStatus.FAILED
    assert result.value is payload
    assert result.error
    assert result.attempts == 1
    assert calls == ["original-run"]
    assert scheduler.run(TASK, lambda _: {"status": "completed"}).success


def test_exception_retry_contract_is_unchanged(tmp_path):
    calls = []

    def handler(context):
        calls.append(context.attempt)
        if context.attempt == 1:
            raise RuntimeError("transient")
        return {"status": "completed"}

    result = LocalTaskScheduler(lock_path=tmp_path / "task.lock").run(
        TASK, handler, max_retries=1,
    )
    assert result.success and calls == [1, 2]


def test_operation_failure_envelope_keeps_daily_receipt(tmp_path):
    payload = {"status": "completed", "daily_sync": {"status": "failed", "error": "broken"}}
    calls = []
    runner = OperationalTaskRunner(
        LocalTaskScheduler(lock_path=tmp_path / "task.lock"),
        {TASK: lambda context: calls.append(context.run_id) or payload},
    )
    response = runner.run(OperationalTaskRunInput(task_id=TASK))
    assert not response.success
    assert response.status == "failure"
    assert response.data.run_status == "failed"
    assert response.data.result == payload
    assert response.data.error == "broken"
    assert calls == [response.data.run_id]


def test_background_failure_is_queryable_without_reexecution(tmp_path, monkeypatch):
    import packages.tools.operations as operations

    threads = []
    real_thread = operations.threading.Thread

    def track_thread(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    monkeypatch.setattr(operations.threading, "Thread", track_thread)
    calls = []
    release = Event()
    payload = {"status": "failed", "daily_sync": {"status": "failed", "error": "broken"}}

    def handler(context):
        assert release.wait(5)
        calls.append(context.run_id)
        return payload

    runner = OperationalTaskRunner(
        LocalTaskScheduler(lock_path=tmp_path / "task.lock"), {TASK: handler},
    )
    accepted = runner.start(OperationalTaskRunInput(task_id=TASK))
    release.set()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    status = runner.background_status(accepted.data.run_id)
    assert status["run_status"] == "failed"
    assert status["result"] == payload
    assert calls == [accepted.data.run_id]


def test_persisted_failure_receipt_survives_runner_restart(tmp_path):
    payload = {"status": "failed", "error": "broken", "daily_sync": {"status": "failed"}}
    state = {"metadata": {"checkpoint_path": "checkpoint.json"}, "result": payload}
    store = SimpleNamespace(get_task_run=lambda _: {
        "task_id": TASK, "run_id": "original-run", "run_status": "succeeded", "state": state,
    })
    runner = OperationalTaskRunner(
        LocalTaskScheduler(lock_path=tmp_path / "task.lock"), {}, state_store=store,
    )
    result = runner.background_status("original-run")
    assert result["run_status"] == "failed"
    assert result["result"] == payload
    assert state["metadata"] == {"checkpoint_path": "checkpoint.json"}


@pytest.fixture
def storage():
    value = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    yield value
    value.engine.dispose()


def seed_sources(storage):
    with storage.write_transaction() as db:
        for index in range(26):
            db.add(CompanySourceRecord(
                id=f"entry-{index}", source="offerbiu", source_record_id=str(index),
                company_name="Example", entry_url=f"https://example.com/{index}",
                status="unusable" if index == 25 else "pending",
            ))


def test_empty_company_coverage_does_not_hide_saved_entries(storage):
    seed_sources(storage)
    response = company_coverage(
        CompanyCoverageInput(company_name="Example", limit=1, offset=20),
        PostgresRecruitmentRepository(storage),
    )
    assert response.data.total == 0
    assert response.data.coverage_basis == "company_snapshots"
    counts = response.data.persistence
    assert counts.status == "available"
    assert counts.source_record_count == 26
    assert counts.company_snapshot_count == counts.job_snapshot_count == 0
    assert counts.offerbiu_unlinked_usable_entry_count == 25
    assert "not writes by this run or distinct companies" in counts.count_basis
    assert "does not establish absence" in response.error_message
    missing = company_coverage(
        CompanyCoverageInput(company_name="Other"), PostgresRecruitmentRepository(storage),
    )
    assert missing.data.persistence.source_record_count == 0


@pytest.mark.parametrize("with_storage", [False, True])
def test_unavailable_counts_are_unknown_not_zero(with_storage):
    storage = Storage.from_url("sqlite+pysqlite:///:memory:") if with_storage else None
    try:
        repository = SimpleNamespace(storage=storage, list_companies=lambda: [])
        response = company_coverage(CompanyCoverageInput(), repository)
        assert response.data.persistence.status == "unavailable"
        assert response.data.persistence.source_record_count is None
        assert response.data.persistence.job_snapshot_count is None
    finally:
        if storage:
            storage.engine.dispose()


def test_runtime_registration_survives_crawl_failure(storage):
    def discovery():
        seed_sources(storage)
        return {"applied": True, "registered_entries": 26, "pending_entries": [{}] * 20,
                "pending_entry_count": 25, "pending_entries_sample_count": 20,
                "pending_entries_limited": True}

    def crawl(_dry_run):
        raise RuntimeError("synthetic crawl failure")

    handlers = build_runtime_task_handlers(
        settings=SimpleNamespace(mail_enabled=False),
        repository=PostgresRecruitmentRepository(storage),
        daily_sync=DailyRecruitmentSync(discovery=discovery, crawl=crawl),
    )
    context = TaskContext(
        task_id=TASK, task_label="daily", scheduled_for=datetime.now(timezone.utc),
        run_id="original-run", attempt=1, write_enabled=True,
    )
    result = handlers[TASK](context)
    assert result["status"] == result["sync_status"] == "failed"
    assert "synthetic crawl failure" in result["error"]
    assert result["agent_write_performed"] is True
    assert result["source_write_attempted"] is False
    assert result["source_write_scope"] == "legacy_project_read_only"
    writes = result["write_statistics"]
    assert writes["source_registration_write_performed"] is True
    assert writes["pipeline_write_performed"] is False
    assert writes["job_snapshot_write_count"] is None
    assert result["persistence"]["source_record_count"] == 26
    assert result["persistence"]["job_snapshot_count"] == 0
    assert result["daily_sync"]["discovery"]["pending_entry_count"] == 25
    assert result["daily_sync"]["discovery"]["pending_entries_sample_count"] == 20


def test_runtime_does_not_overwrite_pipeline_failure_with_completed():
    handlers = build_runtime_task_handlers(
        settings=SimpleNamespace(mail_enabled=False), repository=SimpleNamespace(),
        daily_sync=DailyRecruitmentSync(crawl=lambda _: {"status": "failed", "error": "broken"}),
    )
    result = handlers[TASK](TaskContext(
        task_id=TASK, task_label="daily", scheduled_for=datetime.now(timezone.utc),
        run_id="original-run", attempt=1, write_enabled=True,
    ))
    assert result["status"] == "failed"
    assert result["error"] == "broken"


def test_coverage_counts_jobs_and_sources_independently_with_aliases(storage):
    seed_sources(storage)
    with storage.write_transaction() as db:
        db.add(CompanySnapshot(
            id="company-1", name="Example", aliases=["Alias"],
            integration_status="connected", source="fixture",
        ))
        for index in range(3):
            db.add(JobSnapshot(
                id=f"job-{index}", company_id="company-1", title="Engineer",
                detail_url=f"https://example.com/jobs/{index}", cohort_status="confirmed",
                batch="formal", source="fixture", source_ref=f"job-{index}",
            ))
    response = company_coverage(
        CompanyCoverageInput(company_name="Alias", integration_status="not_connected"),
        PostgresRecruitmentRepository(storage),
    )
    assert response.data.total == 0
    counts = response.data.persistence
    assert counts.scope == "company_name"
    assert counts.source_record_count == 26
    assert counts.company_snapshot_count == 1
    assert counts.job_snapshot_count == 3


def test_source_linked_jobs_are_counted_without_a_company_snapshot(storage):
    with storage.write_transaction() as db:
        db.add(CompanySourceRecord(
            id="linked-entry", source="offerbiu", source_record_id="linked-entry",
            company_name="Example", company_id="linked-company", status="complete",
        ))
        db.add(JobSnapshot(
            id="linked-job", company_id="linked-company", title="Engineer",
            detail_url="https://example.com/jobs/1", cohort_status="confirmed",
            batch="formal", source="fixture",
        ))
    response = company_coverage(
        CompanyCoverageInput(company_name="Example"), PostgresRecruitmentRepository(storage),
    )
    assert response.data.total == 0
    counts = response.data.persistence
    assert counts.source_record_count == counts.job_snapshot_count == 1
    assert counts.company_snapshot_count == counts.offerbiu_unlinked_usable_entry_count == 0


def test_runtime_persists_receipt_without_replacing_resume_state(storage, monkeypatch, tmp_path):
    import packages.scheduler.runtime as runtime

    monkeypatch.setattr(runtime.Storage, "from_url", lambda *_args, **_kwargs: storage)
    store = AgentStateStore(storage)
    resume_metadata = {"checkpoint_path": "synthetic-checkpoint.json", "company_ids": ["example"]}
    store.save_task_state(
        "original-run", {"metadata": resume_metadata, "steps": ["crawl"]}, ensure_task_run=True,
    )

    def crawl(_dry_run):
        raise RuntimeError("synthetic failure")

    handlers = build_runtime_task_handlers(
        settings=SimpleNamespace(mail_enabled=False, database_url="sqlite+pysqlite:///:memory:"),
        daily_sync=DailyRecruitmentSync(crawl=crawl, state_store=store),
    )
    result = handlers[TASK](TaskContext(
        task_id=TASK, task_label="daily", scheduled_for=datetime.now(timezone.utc),
        run_id="original-run", attempt=1, write_enabled=True,
    ))
    persisted = store.get_task_state("original-run")
    assert persisted["metadata"] == resume_metadata
    assert persisted["steps"] == ["crawl"]
    assert persisted["result"] == result
    restarted = OperationalTaskRunner(
        LocalTaskScheduler(lock_path=tmp_path / "task.lock"), {}, state_store=store,
    )
    status = restarted.background_status("original-run")
    assert status["run_status"] == "failed"
    assert status["result"] == result


def test_dry_run_does_not_claim_historical_inventory_as_new_writes(storage):
    seed_sources(storage)
    handlers = build_runtime_task_handlers(
        settings=SimpleNamespace(mail_enabled=False),
        repository=PostgresRecruitmentRepository(storage),
        daily_sync=DailyRecruitmentSync(
            discovery=lambda _dry_run: {"applied": False, "registered_entries": 0},
            crawl=lambda _dry_run: {"written": False},
        ),
    )
    result = handlers[TASK](TaskContext(
        task_id=TASK, task_label="daily", scheduled_for=datetime.now(timezone.utc),
        run_id="dry-run", attempt=1, write_enabled=True,
        metadata={"details": {"requested_dry_run": True}},
    ))
    assert result["agent_write_performed"] is False
    assert result["persistence"]["source_record_count"] == 26
    assert result["write_statistics"]["source_registered_entry_count"] == 0
    assert AgentStateStore(storage).get_task_state("dry-run") is None
