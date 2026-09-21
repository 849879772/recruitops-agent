import asyncio
import json
from pathlib import Path
import tomllib

import pytest
from pydantic import ValidationError

from packages.codex_runtime import (
    CodexEventType,
    CodexHomeConfig,
    CodexRuntimeConfig,
    CodexSupervisor,
    CodexThreads,
    JsonRpcStdioClient,
    ProcessExitedError,
    RestartPolicy,
    SupervisorState,
    SupervisorStartupError,
    normalize_event,
)
from packages.codex_runtime.client import CODEX_STDIO_READER_LIMIT, create_process
from packages.codex_runtime.client import JsonRpcServerRequest
from packages.codex_runtime.supervisor import _server_request_result


class FakeReader:
    def __init__(self) -> None:
        self._items: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def readline(self) -> bytes:
        item = await self._items.get()
        return b"" if item is None else item

    async def push(self, message: dict) -> None:
        await self._items.put((json.dumps(message) + "\n").encode())

    async def close(self) -> None:
        await self._items.put(None)


class FakeWriter:
    def __init__(self, on_message) -> None:
        self.messages: list[dict] = []
        self.on_message = on_message
        self.closed = False

    def write(self, data: bytes) -> None:
        message = json.loads(data.decode())
        self.messages.append(message)
        result = self.on_message(message)
        if asyncio.iscoroutine(result):
            asyncio.create_task(result)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


def test_real_process_factory_allows_large_app_server_messages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    process = object()

    async def fake_create_subprocess_exec(*command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    created = asyncio.run(create_process(("codex", "app-server"), tmp_path, {}))

    assert created is process
    assert captured["limit"] == CODEX_STDIO_READER_LIMIT
    assert CODEX_STDIO_READER_LIMIT >= 4 * 1024 * 1024


class FakeProcess:
    def __init__(self, on_message=lambda _message: None) -> None:
        self.stdout = FakeReader()
        self.stdin = FakeWriter(on_message)
        self.returncode: int | None = None
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.exit(-15)

    def kill(self) -> None:
        self.exit(-9)

    def exit(self, returncode: int) -> None:
        self.returncode = returncode
        self._exited.set()
        asyncio.get_running_loop().create_task(self.stdout.close())


async def _allow_tasks() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("failure", [None, "timeout", "error"])
def test_bundled_skills_registered_before_ready_and_on_restart(tmp_path, failure):
    async def scenario():
        processes = []

        async def factory(command, cwd, environment):
            process = FakeProcess()

            async def respond(message):
                if "id" not in message:
                    return
                if message["method"] == "skills/extraRoots/set":
                    if failure == "timeout":
                        return
                    if failure == "error":
                        await process.stdout.push({"id": message["id"], "error": {
                            "code": -32601, "message": "unsupported skills registration",
                        }})
                        return
                await process.stdout.push({"id": message["id"], "result": {}})

            process.stdin.on_message = respond
            processes.append(process)
            return process

        root = tmp_path / "installed app/.agents/skills"
        supervisor = CodexSupervisor(CodexRuntimeConfig(
            command=("fixture",), working_dir=tmp_path / "instance",
            skill_roots=(root,), startup_timeout_seconds=0.2,
        ), process_factory=factory)
        try:
            if failure:
                with pytest.raises(Exception, match="skills"):
                    await supervisor.start()
                assert supervisor.state is SupervisorState.FAILED
                assert processes[0].stdin.closed
            else:
                await supervisor.start()
                await supervisor.restart()
                assert supervisor.state is SupervisorState.RUNNING
                assert len(processes) == 2
            for process in processes:
                messages = process.stdin.messages
                assert [message["method"] for message in messages[:3]] == [
                    "initialize", "initialized", "skills/extraRoots/set",
                ]
                assert messages[2]["params"] == {"extraRoots": [str(root.resolve())]}
        finally:
            await supervisor.stop()

    asyncio.run(scenario())


def test_runtime_config_validates_launch_and_model_settings(tmp_path: Path) -> None:
    config = CodexRuntimeConfig(
        command=["codex", "app-server"],
        working_dir=tmp_path,
        startup_timeout_seconds=2,
        restart_policy="always",
        provider="deepseek",
        model="v4-pro",
    )

    assert config.command == ("codex", "app-server")
    assert config.working_dir == tmp_path
    assert config.restart_policy is RestartPolicy.ALWAYS
    assert config.provider == "deepseek"
    assert config.model == "v4-pro"

    with pytest.raises(ValidationError):
        CodexRuntimeConfig(command=["codex"], startup_timeout_seconds=0)


def test_codex_home_config_is_secret_free_and_responses_only(tmp_path: Path) -> None:
    config = CodexHomeConfig(
        model="deepseek-v4-pro",
        provider_id="deepseek",
        base_url="https://api.deepseek.com/",
        api_key_env="RECRUITOPS_LLM_API_KEY",
        mcp_command="D:/agent/.venv/Scripts/python.exe",
        mcp_args=("scripts/run_mcp_server.py",),
        mcp_env_vars=(
            "RECRUITOPS_DATABASE_URL",
            "RECRUITOPS_MAIL_IMAP_PASSWORD",
        ),
    )

    rendered = config.render_toml()
    assert 'wire_api = "responses"' in rendered
    assert 'base_url = "https://api.deepseek.com"' in rendered
    assert 'env_key = "RECRUITOPS_LLM_API_KEY"' in rendered
    assert "model_context_window = 1000000" in rendered
    assert "model_auto_compact_token_limit = 96000" in rendered
    assert "sk-" not in rendered
    parsed = tomllib.loads(rendered)
    assert parsed["approval_policy"] == "on-request"
    assert parsed["model_context_window"] == 1_000_000
    assert parsed["model_auto_compact_token_limit"] == 96_000
    assert parsed["model_providers"]["deepseek"]["wire_api"] == "responses"
    assert parsed["model_providers"]["deepseek"]["request_max_retries"] == 2
    assert parsed["model_providers"]["deepseek"]["stream_max_retries"] == 3
    assert parsed["model_providers"]["deepseek"]["stream_idle_timeout_ms"] == 120_000
    assert parsed["mcp_servers"]["recruitops"]["args"] == ["scripts/run_mcp_server.py"]
    assert parsed["mcp_servers"]["recruitops"]["env_vars"] == [
        "RECRUITOPS_DATABASE_URL",
        "RECRUITOPS_MAIL_IMAP_PASSWORD",
    ]
    assert (
        parsed["mcp_servers"]["recruitops"]["default_tools_approval_mode"]
        == "approve"
    )
    assert parsed["mcp_servers"]["recruitops"]["required"] is True
    assert "postgresql://user:password" not in rendered
    target = config.write(tmp_path / "codex-home")
    assert target.read_text(encoding="utf-8") == rendered


def test_codex_home_config_validates_retry_and_compaction_limits() -> None:
    base = {
        "model": "deepseek-v4-pro",
        "provider_id": "deepseek",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "RECRUITOPS_LLM_API_KEY",
        "mcp_command": "python",
    }

    with pytest.raises(ValidationError):
        CodexHomeConfig(**base, request_max_retries=11)
    with pytest.raises(ValidationError):
        CodexHomeConfig(**base, stream_max_retries=-1)
    with pytest.raises(ValidationError):
        CodexHomeConfig(**base, stream_idle_timeout_ms=999)
    with pytest.raises(ValueError, match="auto-compaction"):
        CodexHomeConfig(
            **base,
            model_context_window=32_768,
            model_auto_compact_token_limit=32_768,
        ).render_toml()


def test_harness_server_request_policy_only_auto_accepts_trusted_recruitops_mcp() -> None:
    trusted = JsonRpcServerRequest(
        request_id="mcp-1",
        method="mcpServer/elicitation/request",
        params={
            "serverName": "recruitops",
            "request": {"meta": {"codex_approval_kind": "mcp_tool_call"}},
        },
    )
    untrusted = JsonRpcServerRequest(
        request_id="mcp-2",
        method="mcpServer/elicitation/request",
        params={
            "serverName": "other",
            "request": {"meta": {"codex_approval_kind": "mcp_tool_call"}},
        },
    )
    command = JsonRpcServerRequest(
        request_id="command-1",
        method="item/commandExecution/requestApproval",
        params={"command": "echo forbidden"},
    )

    assert _server_request_result(trusted) == (
        {"action": "accept", "content": {"decision": "approve"}},
        "trusted_recruitops_mcp",
    )
    assert _server_request_result(untrusted) == (
        {"action": "decline", "content": None},
        "declined_untrusted_mcp",
    )
    assert _server_request_result(command) == (
        {"decision": "decline"},
        "declined_non_mcp_write",
    )


def test_client_assigns_ids_and_resolves_concurrent_pending_requests() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = JsonRpcStdioClient(process)
        await client.start()

        first = asyncio.create_task(client.request("first", {"value": 1}))
        second = asyncio.create_task(client.request("second", {"value": 2}))
        await _allow_tasks()

        assert [message["id"] for message in process.stdin.messages] == [1, 2]
        assert all("jsonrpc" not in message for message in process.stdin.messages)
        await process.stdout.push({"jsonrpc": "2.0", "id": 2, "result": "two"})
        await process.stdout.push({"jsonrpc": "2.0", "id": 1, "result": "one"})

        assert await first == "one"
        assert await second == "two"
        assert client.pending_count == 0
        await client.close()

    _run(scenario())


def test_client_dispatches_notifications_to_queue_and_handler() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        received = []
        client = JsonRpcStdioClient(process, notification_handler=received.append)
        await client.start()

        await process.stdout.push(
            {"jsonrpc": "2.0", "method": "item/agentMessage/delta", "params": {"delta": "hi"}}
        )
        notification = await client.next_notification()
        assert notification.method == "item/agentMessage/delta"
        assert received == [notification]
        await client.close()

    _run(scenario())


def test_client_queues_server_requests_and_can_respond() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = JsonRpcStdioClient(process)
        await client.start()

        await process.stdout.push(
            {"id": "approval-1", "method": "item/commandExecution/requestApproval", "params": {}}
        )
        request = await client.next_server_request()
        assert request.request_id == "approval-1"
        await client.respond(request.request_id, result={"decision": "decline"})
        assert process.stdin.messages[-1] == {
            "id": "approval-1",
            "result": {"decision": "decline"},
        }
        await client.close()

    _run(scenario())


def test_client_propagates_process_exit_to_all_pending_requests() -> None:
    async def scenario() -> None:
        process = FakeProcess()
        client = JsonRpcStdioClient(process)
        await client.start()
        first = asyncio.create_task(client.request("first"))
        second = asyncio.create_task(client.request("second"))
        await _allow_tasks()

        process.exit(23)
        with pytest.raises(ProcessExitedError) as first_error:
            await first
        with pytest.raises(ProcessExitedError) as second_error:
            await second
        assert first_error.value.returncode == 23
        assert second_error.value.returncode == 23
        assert client.pending_count == 0
        await client.close()

    _run(scenario())


def test_event_normalization_keeps_payload_and_extracts_common_ids() -> None:
    event = normalize_event(
        "item/agentMessage/delta",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "item-1",
            "delta": "hello",
        },
    )

    assert event.event_type is CodexEventType.TEXT_DELTA
    assert event.thread_id == "thread-1"
    assert event.turn_id == "turn-1"
    assert event.item_id == "item-1"
    assert event.text == "hello"
    assert event.data["delta"] == "hello"

    nested = normalize_event(
        "thread/started",
        {"thread": {"id": "thread-2"}, "turn": {"id": "turn-2"}},
    )
    assert nested.thread_id == "thread-2"
    assert nested.turn_id == "turn-2"

    reasoning = normalize_event(
        "item/reasoning/textDelta",
        {"threadId": "thread-2", "delta": "private reasoning"},
    )
    assert reasoning.event_type is CodexEventType.REASONING_DELTA
    assert reasoning.text == "private reasoning"

    provider_error = normalize_event(
        "error",
        {
            "threadId": "thread-2",
            "error": {
                "message": "unexpected status 402 Payment Required: Insufficient Balance"
            },
        },
    )
    assert provider_error.event_type is CodexEventType.ERROR
    assert provider_error.text == "unexpected status 402 Payment Required: Insufficient Balance"


def test_supervisor_starts_probes_for_health_and_normalizes_events(tmp_path: Path) -> None:
    async def scenario() -> None:
        processes: list[FakeProcess] = []

        def respond(process: FakeProcess, message: dict) -> None:
            if message.get("method") == "initialize":
                asyncio.create_task(
                    process.stdout.push(
                        {
                            "id": message["id"],
                            "result": {"codexHome": "C:/codex", "platformFamily": "windows"},
                        }
                    )
                )

        async def factory(command, working_dir, environment):
            assert command == ("codex", "app-server")
            assert working_dir == tmp_path
            assert environment == {}
            process = FakeProcess()
            process.stdin.on_message = lambda message: respond(process, message)
            processes.append(process)
            return process

        events = []
        supervisor = CodexSupervisor(
            CodexRuntimeConfig(
                command=["codex", "app-server"],
                working_dir=tmp_path,
                provider="deepseek",
                model="v4-pro",
            ),
            process_factory=factory,
            event_handler=events.append,
        )

        status = await supervisor.start()
        assert status.state is SupervisorState.RUNNING
        assert processes[0].stdin.messages[0]["method"] == "initialize"
        assert processes[0].stdin.messages[1] == {"method": "initialized", "params": None}
        health = await supervisor.health()
        assert health.healthy is True
        assert health.response["codexHome"] == "C:/codex"
        assert supervisor.should_restart(failed=True) is True
        assert supervisor.should_restart(failed=False) is False

        await processes[0].stdout.push(
            {
                "jsonrpc": "2.0",
                "method": "turn/started",
                "params": {"threadId": "t1", "turnId": "turn1"},
            }
        )
        event = await supervisor.next_event()
        assert event.event_type is CodexEventType.TURN_STARTED
        assert events == [event]

        stopped = await supervisor.stop()
        assert stopped.state is SupervisorState.STOPPED

    _run(scenario())


def test_threads_wrap_start_resume_turn_and_interrupt() -> None:
    async def scenario() -> None:
        process = FakeProcess()

        def respond(message: dict) -> None:
            if "id" not in message:
                return
            method = message["method"]
            result = {
                "initialize": {"codexHome": "C:/codex"},
                "thread/start": {"thread": {"id": "thread-1"}},
                "thread/resume": {"thread": {"id": "thread-1"}},
                "thread/read": {"thread": {"id": "thread-1", "turns": []}},
                "thread/list": {
                    "data": [{"id": "thread-1"}],
                    "nextCursor": "cursor-2",
                },
                "turn/start": {"turn": {"id": "turn-1"}},
                "turn/interrupt": {},
                "thread/delete": {},
            }[method]
            asyncio.create_task(process.stdout.push({"id": message["id"], "result": result}))

        process.stdin.on_message = respond

        async def factory(_command, _working_dir, _environment):
            return process

        supervisor = CodexSupervisor(
            CodexRuntimeConfig(command=["codex", "app-server"]),
            process_factory=factory,
        )
        await supervisor.start()
        threads = CodexThreads(supervisor)
        thread = await threads.start(cwd="D:/work")
        assert thread.id == "thread-1"
        assert (await threads.resume(thread.id)).id == thread.id
        assert (await threads.read(thread.id)).id == thread.id
        page = await threads.list(limit=10)
        assert [item.id for item in page.data] == [thread.id]
        assert page.next_cursor == "cursor-2"
        turn = await threads.start_turn(thread.id, "hello")
        assert turn.id == "turn-1"
        await threads.interrupt(thread.id, turn.id)
        await threads.delete(thread.id)
        turn_request = next(
            message for message in process.stdin.messages if message.get("method") == "turn/start"
        )
        assert turn_request["params"]["input"] == [{"type": "text", "text": "hello"}]
        list_request = next(
            message for message in process.stdin.messages if message.get("method") == "thread/list"
        )
        assert list_request["params"] == {"limit": 10, "archived": False}
        delete_request = next(
            message for message in process.stdin.messages if message.get("method") == "thread/delete"
        )
        assert delete_request["params"] == {"threadId": thread.id}
        await supervisor.stop()

    _run(scenario())


def test_supervisor_startup_probe_timeout_marks_failure() -> None:
    async def scenario() -> None:
        process = FakeProcess()

        async def factory(_command, _working_dir, _environment):
            return process

        supervisor = CodexSupervisor(
            CodexRuntimeConfig(command=["codex"], startup_timeout_seconds=0.01),
            process_factory=factory,
        )

        with pytest.raises(SupervisorStartupError):
            await supervisor.start()
        assert supervisor.state is SupervisorState.FAILED
        assert process.returncode == -15

    _run(scenario())


def test_supervisor_restarts_after_unexpected_process_exit() -> None:
    async def scenario() -> None:
        processes: list[FakeProcess] = []
        second_started = asyncio.Event()

        async def factory(_command, _working_dir, _environment):
            process = FakeProcess()

            def respond(message: dict) -> None:
                if message.get("method") == "initialize" and "id" in message:
                    asyncio.create_task(
                        process.stdout.push(
                            {"id": message["id"], "result": {"codexHome": "C:/codex"}}
                        )
                    )

            process.stdin.on_message = respond
            processes.append(process)
            if len(processes) == 2:
                second_started.set()
            return process

        supervisor = CodexSupervisor(
            CodexRuntimeConfig(
                command=["codex", "app-server"],
                restart_policy=RestartPolicy.ON_FAILURE,
                max_restarts=1,
            ),
            process_factory=factory,
        )
        await supervisor.start()
        processes[0].exit(1)
        await asyncio.wait_for(second_started.wait(), timeout=1)
        for _ in range(20):
            if supervisor.state is SupervisorState.RUNNING:
                break
            await asyncio.sleep(0.01)
        assert supervisor.state is SupervisorState.RUNNING
        assert supervisor.status.restart_count == 1
        await supervisor.stop()
        assert len(processes) == 2

    _run(scenario())
