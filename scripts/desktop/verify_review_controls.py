"""Exercise full review and legacy cancellation against fresh, migrated PostgreSQL.

All application rows are synthetic. Browser operations are mocked, and an
existing instance (including a desktop user's database) is never accepted.
"""

import argparse
import asyncio
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from apps.api.daily_progress import task_progress
from packages.config import Settings
from packages.desktop_runtime.resources import Bundle, Layout
from packages.desktop_runtime.supervisor import Events, Supervisor
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.scheduler.runtime import _load_frozen_resume
from packages.storage import AgentStateStore, ApplicationSnapshot, Storage, TaskRun
from packages.tools import application_review_run as review
from packages.tools.batch_browser_operations import (
    ApplicationStatusResult, BatchObserveApplicationStatusInput,
    BatchObserveApplicationStatusResponse,
)
from packages.tools.task_runtime_control import request_daily_control
from packages.tools.typed import EvidenceSource, ToolStatus


def verify(storage):
    with storage.session() as session:
        constraint = session.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='task_runs'::regclass AND conname='task_runs_max_steps_check'"
        )).scalar_one()
        assert "50" in constraint
    for invalid in (0, 86):
        try:
            with storage.write_transaction() as session:
                session.add(TaskRun(id=f"invalid-budget-{invalid}", task_type="application_status_review",
                                    user_request="synthetic constraint probe", max_steps=invalid, source="test"))
        except IntegrityError as error:
            assert error.orig.diag.constraint_name == "task_runs_max_steps_check"
        else:
            raise AssertionError("real migration did not enforce the expected task budget")

    repository = PostgresRecruitmentRepository(storage)
    tested = []
    previous_count = 0
    for count in (0, 51, 86, 120):
        with storage.write_transaction() as session:
            for index in range(previous_count, count):
                session.add(ApplicationSnapshot(
                    id=f"synthetic-{index}", company_name="Synthetic Review Fixture",
                    job_title=f"Synthetic Role {index}", stage="applied", stage_history=[],
                    record_url=f"https://fixture{index}.example.test/applications",
                    idempotency_key=f"application:synthetic-{index}", source="test",
                ))
        visited = []

        async def observe(request, *_):
            visited.extend(request.application_ids)
            return BatchObserveApplicationStatusResponse(
                tool_name="batch_observe_application_status", status=ToolStatus.SUCCESS, success=True,
                total=len(request.application_ids), pages_total=1,
                evidence=[EvidenceSource(source="synthetic.native.fixture")],
                timeout_ms=1000, elapsed_ms=0,
                unchanged=[ApplicationStatusResult(application_id=item, state="unchanged", elapsed_ms=0)
                           for item in request.application_ids],
            )

        async def run():
            result = await review.continue_application_review(
                BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repository)
            waves = 1
            while result.summary["continuation_required"]:
                assert waves < 20
                result = await review.continue_application_review(
                    BatchObserveApplicationStatusInput(run_id=result.summary["run_id"]), object(), repository)
                waves += 1
            assert result.success and result.summary["completed_count"] == count
            assert result.summary["scope_total"] == count and result.summary["remaining_count"] == 0
            with storage.session() as session:
                task = session.get(TaskRun, result.summary["run_id"])
                assert (task.step_count, task.max_steps, task.current_step) == (1, 1, f"{count}/{count}")
            return waves

        with patch.object(review, "batch_observe_application_status", observe):
            waves = asyncio.run(run())
        assert len(visited) == len(set(visited)) == count
        tested.append({"scope": count, "processed": count, "waves": waves})
        previous_count = count

    store = AgentStateStore(storage)
    for index in range(6):
        run_id = f"synthetic-legacy-{index}"
        saved = {"checkpoint_ref": "synthetic-checkpoint.json", "progress": {"attempted_unique": 16}}
        store.save_task_state(run_id, saved, ensure_task_run=True)
        with storage.write_transaction() as session:
            session.get(TaskRun, run_id).status = "stopped" if index % 2 else "failed"
        assert task_progress(storage, run_id=run_id)["run"]["can_cancel"]
        assert request_daily_control(storage, run_id, "cancel")["status"] == "cancelled"
        assert store.get_task_run(run_id)["state"] == saved
        assert not task_progress(storage, run_id=run_id)["run"]["can_resume"]
        try:
            _load_frozen_resume(Settings(), store.get_task_run(run_id))
        except ValueError as error:
            assert "cancelled daily task" in str(error)
        else:
            raise AssertionError("cancelled task incorrectly resumable")
    assert task_progress(storage, include_recoverable=True)["runs"] == []
    assert len(repository.list_applications()) == 120
    return {"constraint": constraint, "scopes": tested, "legacy_cancelled": 6,
            "application_records_preserved": 120, "real_browser_or_model_calls": 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--instance", required=True, type=Path)
    args = parser.parse_args()
    bundle = Bundle.load(args.bundle)
    layout = Layout(bundle.root, args.instance.resolve())
    layout.validate(ROOT)
    if layout.data.exists():
        raise ValueError("requires a fresh isolated synthetic instance")
    runtime = Supervisor(bundle, layout, ROOT, Events(io.StringIO()), timeout=120)
    runtime.shell_token = "7" * 64
    storage = None
    try:
        runtime.start()
        storage = Storage.from_url(URL.create("postgresql+psycopg", username="desktop",
            password=runtime.env["PGPASSWORD"], host="127.0.0.1", port=runtime.db_port,
            database="postgres"), hide_parameters=True)
        with storage.session() as session:
            actual = session.execute(text("SHOW data_directory")).scalar_one()
            assert Path(actual).resolve() == (layout.data / "pgdata").resolve()
        result = verify(storage)
    finally:
        if storage is not None:
            storage.engine.dispose()
        runtime.stop()
    assert json.loads((layout.data / "instance.json").read_text())["state"] == "stopped"
    result["clean_shutdown"] = True
    (layout.data / "review-controls-acceptance.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
