"""Offline boundaries for explicit task control and mail identity confirmation."""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.mcp import register_tools, register_read_only_tools
from packages.mcp import task_tools
from packages.mcp.server import MCPToolDependencies, TOOL_DEFINITIONS
from packages.recruitment_mail import RecruitmentMailStore
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.storage import Storage
from packages.storage.models import ApplicationSnapshot, Approval, TaskRun, ToolCall
from packages.tools.task_runtime_control import register_daily_task
from tests.test_mcp import FakeMCPServer, _parsed_mail


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'tasks.db'}", initialize=True)
    store = RecruitmentMailStore(storage)
    repository = PostgresRecruitmentRepository(storage)
    settings = SimpleNamespace(write_enabled=True)
    monkeypatch.setattr("packages.mcp.server.get_settings", lambda: settings)
    monkeypatch.setattr(task_tools, "get_settings", lambda: settings)
    server = FakeMCPServer()
    register_tools(server, repository, store)
    yield server, repository, store, settings
    service = getattr(store, "_mcp_mail_run_service", None)
    if service:
        service.close()
    storage.engine.dispose()


def call(server, name, values):
    return server.tools[name][0](values)


def test_new_tools_safety_surface_and_no_approval_tool(boundary):
    server, repository, store, _ = boundary
    readonly = FakeMCPServer()
    register_read_only_tools(readonly, repository, store)
    safe = {"background_task_status", "application_review_status", "recruitment_mail_run_status",
            "recruitment_mail_binding_candidates"}
    actions = {"daily_recruitment_sync_control", "application_review_control", "recruitment_mail_run_start",
               "recruitment_mail_run_control", "recruitment_mail_binding_propose"}
    assert safe <= readonly.tools.keys()
    assert actions.isdisjoint(readonly.tools)
    assert actions | safe <= server.tools.keys()
    assert not any("approve" in name for name in server.tools)
    definitions = {item.name: item for item in TOOL_DEFINITIONS}
    assert all(definitions[name].read_only for name in safe)
    assert all(not definitions[name].read_only for name in actions)


def test_readonly_mail_status_does_not_construct_worker_or_refresh(boundary, monkeypatch):
    server, repository, store, settings = boundary
    settings.write_enabled = False
    monkeypatch.setattr(task_tools, "mail_run_service", lambda *_: pytest.fail("read constructed worker"))
    monkeypatch.setattr("packages.recruitment_mail.freshness.ensure_mail_fresh", lambda *_a, **_k: pytest.fail("read synced mail"))
    result = call(server, "recruitment_mail_run_status", {})
    assert result.success and result.read_only
    assert result.data == {"runs": [], "run": None, "execution_mode": "foreground", "continuation_required": False}
    assert not hasattr(store, "_mcp_mail_run_service")
    with repository.storage.session() as session:
        assert session.scalars(select(TaskRun)).all() == []


def test_mail_start_and_control_forward_current_thread_and_frozen_run(boundary, monkeypatch):
    server, _, _, _ = boundary
    calls = []
    service = SimpleNamespace(
        start=lambda **kwargs: calls.append(("start", kwargs)) or {"run_id": "mail-1", "status": "accepted"},
        control=lambda run_id, action, **kwargs: calls.append((action, run_id, kwargs)) or {"run_id": run_id, "status": "accepted"},
        wait=lambda run_id, **kwargs: calls.append(("wait", run_id, kwargs)) or {"run_id": run_id, "status": "completed"},
    )
    monkeypatch.setattr(task_tools, "mail_run_service", lambda _deps: service)
    result = call(server, "recruitment_mail_run_start", {"record_ids": ["one"], "thread_id": "thread-a", "turn_id": "turn-a", "refresh": False})
    assert not result.read_only and result.data["run"]["status"] == "completed"
    assert not result.data["continuation_required"]
    call(server, "recruitment_mail_run_control", {"run_id": "mail-1", "action": "resume", "thread_id": "thread-b", "turn_id": "turn-b"})
    assert calls == [("start", {"record_ids": ["one"], "thread_id": "thread-a", "turn_id": "turn-a", "refresh": False}),
                     ("wait", "mail-1", {"timeout_seconds": 20}),
                     ("resume", "mail-1", {"thread_id": "thread-b", "turn_id": "turn-b"}),
                     ("wait", "mail-1", {"timeout_seconds": 20})]


def test_mail_status_waits_without_constructing_service_and_requests_current_turn_continuation(boundary, monkeypatch):
    server, _, store, _ = boundary
    captured = []
    monkeypatch.setattr(task_tools, "mail_run_service", lambda *_: pytest.fail("status constructed worker"))
    def wait(storage, **kwargs):
        assert storage is store.storage
        captured.append(kwargs)
        return {"run": {"run_id": "one", "status": "running"}, "runs": []}
    monkeypatch.setattr("packages.recruitment_mail.run_service.wait_mail_progress", wait)
    result = call(server, "recruitment_mail_run_status", {"run_id": "one"})
    assert result.read_only and result.data["continuation_required"]
    assert result.data["execution_mode"] == "foreground"
    assert captured == [{"run_id": "one", "thread_id": None, "timeout_seconds": 20}]
    call(server, "recruitment_mail_run_status", {"run_id": "one", "wait_ms": 0})
    assert captured[-1]["timeout_seconds"] == 0


@pytest.mark.parametrize("status", ["completed", "partial", "failed", "paused", "cancelled", "interrupted"])
def test_mail_terminal_receipts_stop_waiting_without_inventing_success(boundary, monkeypatch, status):
    server, _, _, _ = boundary
    monkeypatch.setattr("packages.recruitment_mail.run_service.wait_mail_progress", lambda *_a, **_k:
                        {"run": {"run_id": "one", "status": status}, "runs": []})
    result = call(server, "recruitment_mail_run_status", {"run_id": "one"})
    assert not result.data["continuation_required"]
    assert result.data["run"]["status"] == status


def test_mail_pause_is_prompt_and_does_not_wait_or_resume(boundary, monkeypatch):
    server, _, _, _ = boundary
    service = SimpleNamespace(control=lambda *_a, **_k: {"run_id": "one", "status": "pausing"},
                              wait=lambda *_a, **_k: pytest.fail("control blocked"))
    monkeypatch.setattr(task_tools, "mail_run_service", lambda _deps: service)
    result = call(server, "recruitment_mail_run_control", {"run_id": "one", "action": "pause"})
    assert result.data["run"]["status"] == "pausing"


def test_mail_wait_is_bounded_by_input_budget():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        task_tools.RecruitmentMailRunStatusInput(wait_ms=20_001)
    assert task_tools._mail_wait_seconds(task_tools.RecruitmentMailRunStatusInput(timeout_ms=5000)) == 4


@pytest.mark.parametrize("name,payload", [
    ("daily_recruitment_sync_control", {"run_id": "any", "action": "pause"}),
    ("recruitment_mail_run_start", {}),
    ("recruitment_mail_run_control", {"run_id": "any", "action": "resume"}),
    ("recruitment_mail_binding_propose", {"record_id": "any", "action": "unbind", "content_digest": "0" * 64, "binding_revision": 0}),
])
def test_new_actions_obey_write_opt_in(boundary, name, payload):
    server, _, _, settings = boundary
    settings.write_enabled = False
    with pytest.raises(PermissionError):
        call(server, name, payload)


def test_daily_control_is_cooperative_receipt_not_false_completion(boundary):
    server, repository, _, _ = boundary
    register_daily_task(repository.storage, "daily-a", "daily_recruitment_sync", thread_id="thread-a")
    result = call(server, "daily_recruitment_sync_control", {"run_id": "daily-a", "action": "pause"})
    assert result.success and not result.read_only
    assert result.data["status"] == "pausing"
    with repository.storage.session() as session:
        assert session.get(TaskRun, "daily-a").status == "pausing"
        assert not session.get(ToolCall, "control:daily-a").arguments["drained"]
    missing = call(server, "daily_recruitment_sync_control", {"run_id": "missing", "action": "cancel"})
    assert not missing.success and missing.data["reason"] == "control_not_available"


def test_review_control_reaches_async_boundary_without_bridge(boundary, monkeypatch):
    from packages.tools.application_review_tasks import ApplicationReviewControlResponse

    server, repository, _, _ = boundary
    calls = []
    async def operation(request, bridge, repo):
        calls.append((request.action, bridge, repo))
        return ApplicationReviewControlResponse(tool_name="application_review_control", status="success", success=True,
                                                data={"status": "pausing"})
    monkeypatch.setattr("packages.mcp.server.control_application_review", operation)
    result = asyncio.run(call(server, "application_review_control", {"action": "pause"}))
    assert result.data["status"] == "pausing"
    assert calls == [("pause", None, repository)]


def test_binding_preview_persists_pending_but_does_not_change_mail(boundary, monkeypatch):
    server, repository, store, _ = boundary
    record = store.upsert(_parsed_mail())
    with repository.storage.write_transaction() as session:
        session.add(ApplicationSnapshot(id="target", company_name="示例公司", job_title="C++开发工程师",
                                        stage="applied", source="fixture", idempotency_key="fixture:target"))
    monkeypatch.setattr("packages.recruitment_mail.freshness.ensure_mail_fresh", lambda *_a, **_k: pytest.fail("candidates synced mail"))
    candidates = call(server, "recruitment_mail_binding_candidates", {"record_id": record.id, "query": "示例"})
    assert candidates.read_only and candidates.data["total"] == 1
    result = call(server, "recruitment_mail_binding_propose", {"record_id": record.id, "application_id": "target",
        "content_digest": candidates.data["content_digest"], "binding_revision": candidates.data["binding_revision"]})
    assert result.success and not result.read_only
    assert result.data["approval_status"] == "pending"
    assert result.data["requires_user_confirmation"]
    assert not result.data["business_write_performed"]
    assert store.get(record_id=record.id).application_id is None
    with repository.storage.session() as session:
        assert session.get(Approval, result.data["approval_id"]).status == "pending"
        assert session.get(ApplicationSnapshot, "target").stage == "applied"


def test_background_status_passes_explicit_discovery_scope(boundary, monkeypatch):
    server, _, _, _ = boundary
    captured = []
    def projection(storage, **kwargs):
        captured.append(kwargs)
        return {"runs": [{"run_id": "one"}, {"run_id": "two"}], "run": None}
    monkeypatch.setattr("apps.api.daily_progress.task_progress", projection)
    result = call(server, "background_task_status", {"thread_id": "thread-a"})
    assert result.success and result.data["selection"] == "ambiguous" and result.data["run"] is None
    assert captured == [{"run_id": None, "thread_id": "thread-a", "include_recoverable": True}]


def test_background_status_reads_real_receipts_and_thread_scope(boundary):
    server, repository, _, _ = boundary
    register_daily_task(repository.storage, "daily-a", "daily_recruitment_sync", thread_id="thread-a")
    register_daily_task(repository.storage, "daily-b", "daily_recruitment_sync", thread_id="thread-b")
    all_runs = call(server, "background_task_status", {})
    assert all_runs.data["selection"] == "ambiguous" and all_runs.data["run"] is None
    assert {item["run_id"] for item in all_runs.data["runs"]} == {"daily-a", "daily-b"}
    own = call(server, "background_task_status", {"thread_id": "thread-b"})
    assert own.data["run"]["run_id"] == "daily-b"
    assert own.read_only


def test_mail_service_cache_uses_one_pool_without_starting_it(boundary):
    _, repository, store, _ = boundary
    dependencies = MCPToolDependencies(repository, store)
    first = task_tools.mail_run_service(dependencies)
    assert first is task_tools.mail_run_service(dependencies)
    assert first._futures == {}
