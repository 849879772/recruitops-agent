from __future__ import annotations

from pathlib import Path

from packages.automation import AutomationStore
from packages.codex_runtime import JsonRpcServerRequest
from packages.codex_runtime.supervisor import _server_request_result
from packages.mcp.server import MCPToolDependencies, TOOL_DEFINITIONS, _build_handler
from packages.scheduler import LocalTaskScheduler, TaskType
from packages.storage import Storage


def _definition(name: str):
    return next(item for item in TOOL_DEFINITIONS if item.name == name)


def test_crawler_normalization_preserves_only_supplied_capture_evidence():
    from packages.tools.crawler_run import _normalized_jobs

    evidence = {"status": "complete", "identity_verified": True, "method": "fixture"}
    rows = _normalized_jobs("Example", [
        {"id": "1", "title": "Engineer", "capture_evidence": evidence},
        {"id": "2", "title": "Engineer", "jd_raw": "Unverified text"},
    ])
    assert rows[0]["capture_evidence"] == evidence
    assert rows[1]["capture_evidence"] == {}


def _dependencies(tmp_path: Path) -> MCPToolDependencies:
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'harness-write.db'}", initialize=True)
    return MCPToolDependencies(
        repository=object(),  # type: ignore[arg-type]
        mail_store=object(),  # type: ignore[arg-type]
        automation_scheduler=LocalTaskScheduler(lock_path=tmp_path / "automation.lock"),
        automation_store=AutomationStore(storage),
    )


def test_trusted_mcp_write_is_reversible_idempotent_and_isolated(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace
    ready_settings = SimpleNamespace(
        write_enabled=True,
        codex_runtime_enabled=True,
        automation_enabled=True,
        mail_enabled=True,
    )
    monkeypatch.setattr("packages.mcp.server.get_settings", lambda: ready_settings)
    monkeypatch.setattr(
        "packages.tools.operations._fresh_automation_settings",
        lambda: ready_settings,
    )
    request = JsonRpcServerRequest(
        request_id="mcp-write-1",
        method="mcpServer/elicitation/request",
        params={
            "serverName": "recruitops",
            "request": {"meta": {"codex_approval_kind": "mcp_tool_call"}},
        },
    )
    decision, reason = _server_request_result(request)
    assert decision == {"action": "accept", "content": {"decision": "approve"}}
    assert reason == "trusted_recruitops_mcp"

    dependencies = _dependencies(tmp_path)
    schedule_tool = _definition("automation_schedule")
    list_tool = _definition("automation_schedule_list")
    disable_tool = _definition("automation_schedule_disable")

    schedule_input = schedule_tool.input_model.model_validate(
        {"task_id": TaskType.CRAWLER_HEALTH.value, "start_time": "09:30"}
    )
    handler = _build_handler(schedule_tool, dependencies)
    created = handler(schedule_input)
    replayed = handler(schedule_input)
    second_time = handler(
        schedule_tool.input_model.model_validate(
            {"task_id": TaskType.CRAWLER_HEALTH.value, "start_time": "17:30"}
        ),
    )

    assert created.success is replayed.success is True
    assert created.read_only is replayed.read_only is False
    assert created.data.schedule_id == replayed.data.schedule_id
    assert second_time.success is True
    assert second_time.data.schedule_id != created.data.schedule_id

    listed = list_tool.operation(list_tool.input_model(), dependencies)
    assert listed.success is True
    assert listed.data.total == 2
    assert all(schedule.active for schedule in listed.data.schedules)

    disabled = disable_tool.operation(
        disable_tool.input_model(schedule_id=created.data.schedule_id),
        dependencies,
    )
    assert disabled.success is True
    assert disabled.read_only is False
    assert disabled.data.active is False

    active = list_tool.operation(
        list_tool.input_model(active_only=True),
        dependencies,
    )
    assert active.data.total == 1
