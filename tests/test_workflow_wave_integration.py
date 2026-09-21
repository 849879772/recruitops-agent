"""Offline workflow boundaries; no desktop, mailbox, model or crawler acceptance."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from apps.api.automation import CodexAutomationExecutor
from packages.storage.models import TaskRun, ToolCall
from packages.tools import application_review_run as review
from packages.tools.batch_browser_operations import (
    ApplicationStatusResult, BatchObserveApplicationStatusInput,
    BatchObserveApplicationStatusResponse,
)
from packages.tools.typed import EvidenceSource, ToolStatus
from tests.test_automation_executor import _task
from tests.test_batch_browser_operations import _repository


@pytest.mark.parametrize("kind,company_ids,source_ids", [
    ("all", [], []), ("company", ["fixture"], []), ("source", [], ["fixture"]),
])
def test_scheduled_sync_preserves_exact_scope(tmp_path, kind, company_ids, source_ids):
    def handler(context):
        assert context.metadata["details"] == {
            "mode": "full", "company_ids": company_ids, "source_record_ids": source_ids,
        }
        assert context.run_id == "execution-1"
        return {"status": "completed", "daily_sync": {"pipeline": {}}}

    executor = CodexAutomationExecutor(None, None,
        settings=SimpleNamespace(write_enabled=True, agent_root=tmp_path),
        task_handlers={"daily_recruitment_intelligence": handler})
    task = replace(_task(), task_id="daily_recruitment_intelligence",
                   target_kind=kind, target_id="fixture" if kind != "all" else None)
    assert asyncio.run(executor(task)).status == "succeeded"


@pytest.mark.parametrize("enabled,kind,target,error", [
    (False, "all", None, "write_disabled"),
    (True, "source", None, "invalid_sync_scope"),
    (True, "company", None, "invalid_sync_scope"),
    (True, "application", "fixture", "invalid_sync_scope"),
])
def test_scheduled_sync_fails_closed_before_dependency_access(tmp_path, enabled, kind, target, error):
    executor = CodexAutomationExecutor(None, None,
        settings=SimpleNamespace(write_enabled=enabled, agent_root=tmp_path), task_handlers={})
    result = asyncio.run(executor(replace(_task(), task_id="daily_recruitment_intelligence",
                                         target_kind=kind, target_id=target)))
    assert result.status == "blocked" and result.error == error
    assert not list(tmp_path.iterdir())


def test_failed_review_drains_siblings_before_resume_and_keeps_scope(tmp_path, monkeypatch):
    repo = _repository(tmp_path, [
        {"id": str(i), "title": f"Engineer {i}",
         "record_url": f"https://fixture{i}.example/applications"}
        for i in range(10)
    ] + [{"id": "terminal", "title": "Old", "stage": "withdrawn", "record_url": ""}])

    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def broken(request, *_):
            if request.application_ids == ["0"]:
                await entered.wait()
                raise RuntimeError("must not leak diagnostic")
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        monkeypatch.setattr(review, "batch_observe_application_status", broken)
        first = await review.continue_application_review(
            BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repo)
        assert cancelled.is_set()
        assert first.summary["wave_error"] == "review_wave_failed"
        assert first.summary["run_status"] == "stopped"
        assert first.summary["remaining_count"] == 10
        assert first.summary["excluded_terminal"] == 1
        assert "diagnostic" not in first.model_dump_json()
        run_id = first.summary["run_id"]
        with repo.storage.session() as session:
            assert session.get(TaskRun, run_id).status == "stopped"
            assert session.get(ToolCall, run_id).arguments["lease_until"] == 0

        visited = []

        async def recovered(request, *_):
            visited.extend(request.application_ids)
            return BatchObserveApplicationStatusResponse(
                tool_name="batch_observe_application_status", status=ToolStatus.SUCCESS,
                success=True, total=1, pages_total=1,
                evidence=[EvidenceSource(source="fixture")], timeout_ms=1000, elapsed_ms=0,
                unchanged=[ApplicationStatusResult(application_id=request.application_ids[0],
                                                   state="unchanged", elapsed_ms=0)])

        monkeypatch.setattr(review, "batch_observe_application_status", recovered)
        resumed = await review.continue_application_review(
            BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repo)
        assert resumed.summary["run_id"] == run_id
        assert resumed.summary["processed_count"] == 10
        assert resumed.summary["scope_complete"] and resumed.success
        assert resumed.summary["wave_error"] is None
        assert set(visited) == {str(i) for i in range(10)}

    asyncio.run(run())


def test_cancelled_review_releases_lease_only_after_page_cleanup(tmp_path, monkeypatch):
    repo = _repository(tmp_path, [{"id": "1", "title": "Engineer",
                                 "record_url": "https://fixture.example/applications"}])

    async def run():
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def observe(*_):
            entered.set()
            try:
                await asyncio.sleep(60)
            finally:
                cleaned.set()

        monkeypatch.setattr(review, "batch_observe_application_status", observe)
        pending = asyncio.create_task(review.continue_application_review(
            BatchObserveApplicationStatusInput(all_non_terminal=True), object(), repo))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert cleaned.is_set()
        from sqlalchemy import select
        with repo.storage.session() as session:
                row = session.scalar(select(ToolCall))
                assert row.arguments["lease_until"] == 0
                assert row.arguments["results"]["1"]["reason"] == "review_wave_cancelled"
                assert row.arguments["attempts"]["1"] == 1
                assert session.get(TaskRun, row.task_id).error_code == "review_wave_cancelled"

    asyncio.run(run())


def test_fixture_mail_action_reaches_mcp_todo_and_stays_completed(monkeypatch):
    from packages.mcp import register_agent_tools
    from packages.mcp import server as mcp_server
    from packages.repositories.postgres import PostgresRecruitmentRepository
    from packages.recruitment_mail.processing import process_pending_mail
    from tests.test_mail_model_processing import setup_case, Client
    from tests.test_mcp import FakeMCPServer

    store, repo, record, settings, triage, proposal = setup_case()
    result = process_pending_mail(store, repo, settings, client=Client([triage, proposal]))
    assert result["schedule_items_created"] == 1
    assert store.get(record.id).application_id == "4"
    server = FakeMCPServer()
    persisted = PostgresRecruitmentRepository(store.storage)
    monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
    register_agent_tools(server, persisted, None)
    read = server.tools["schedule_window"][0]({
        "start_date": "2026-09-01", "end_date": "2026-10-01"}).model_dump(mode="json")
    event = read["data"]["events"][0]
    assert event["application_id"] == "4" and event["event_date"] is None
    response = server.tools["schedule_manage"][0]({
        "action": "update", "event_id": event["id"], "status": "completed",
        "expected_updated_at": event["updated_at"],
    })
    assert response.success
    repeated = process_pending_mail(store, repo, settings, client=Client([]))
    assert repeated["processed"] == 0
    assert PostgresRecruitmentRepository(store.storage).list_schedule()[0].status == "completed"
