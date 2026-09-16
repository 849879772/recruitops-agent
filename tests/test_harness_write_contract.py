from __future__ import annotations

from pathlib import Path

from packages.automation import AutomationStore
from packages.codex_runtime import JsonRpcServerRequest
from packages.codex_runtime.supervisor import _server_request_result
from packages.mcp.server import MCPToolDependencies, TOOL_DEFINITIONS
from packages.scheduler import LocalTaskScheduler, TaskType
from packages.storage import Storage


def _definition(name: str):
    return next(item for item in TOOL_DEFINITIONS if item.name == name)


def _dependencies(tmp_path: Path) -> MCPToolDependencies:
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'harness-write.db'}", initialize=True)
    return MCPToolDependencies(
        repository=object(),  # type: ignore[arg-type]
        mail_store=object(),  # type: ignore[arg-type]
        automation_scheduler=LocalTaskScheduler(lock_path=tmp_path / "automation.lock"),
        automation_store=AutomationStore(storage),
    )


def test_trusted_mcp_write_is_reversible_idempotent_and_isolated(tmp_path: Path) -> None:
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
    created = schedule_tool.operation(schedule_input, dependencies)
    replayed = schedule_tool.operation(schedule_input, dependencies)

    assert created.success is replayed.success is True
    assert created.read_only is replayed.read_only is False
    assert created.data.schedule_id == replayed.data.schedule_id

    listed = list_tool.operation(list_tool.input_model(), dependencies)
    assert listed.success is True
    assert listed.data.total == 1
    assert listed.data.schedules[0].active is True

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
    assert active.data.total == 0
