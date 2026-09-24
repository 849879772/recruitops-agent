"""Real adapter normalization + official consumers, isolated persisted fixtures.

Server messages are in-process, not a real desktop/WebSocket acceptance test.
"""

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from packages.browser_bridge import BrowserBridgeServer, BrowserBridgeStore, OperationStatus
from packages.mcp import server as mcp
from packages.storage import ApplicationSnapshot, Storage, WriteAudit
from packages.tools import browser_bridge as workflow
from packages.tools import batch_browser_operations as batch
from packages.tools import application_review_run as review
from tests.test_batch_browser_operations import _repository
from tests.test_desktop_browser import node_call


URL = "https://ats.example/applications"
DEVICE = "desktop-consumer-fixture"


class Socket:
    def __init__(self):
        self.messages = []

    async def send_json(self, value):
        self.messages.append(value)

    async def close(self, **_):
        pass


def environment(tmp_path, monkeypatch, stage="applied"):
    repo = _repository(tmp_path, [
        {"id": "target", "title": "Platform Engineer", "stage": stage, "record_url": URL},
        {"id": "other", "title": "Data Engineer", "stage": "applied", "record_url": URL},
    ])
    store = BrowserBridgeStore(repo.storage)
    monkeypatch.setattr(mcp, "get_settings", lambda: SimpleNamespace(write_enabled=True))
    return repo, store, BrowserBridgeServer(store, "isolated-fixture-token")


def handler(name, repo, store):
    if name == "review_and_update_application_status":
        # This compatibility consumer is called directly by API/shell; the current
        # MCP catalog exposes observe + batch/verify instead of this combined tool.
        async def consume(request):
            assert mcp.get_settings().write_enabled is True
            return await workflow.review_and_update_application_status_workflow(
                workflow.ReviewAndUpdateApplicationStatusInput(**request), store, repo,
            )
        return consume
    definition = next(d for d in mcp.TOOL_DEFINITIONS if d.name == name)
    return mcp._build_handler(definition, SimpleNamespace(repository=repo, browser_bridge=store))


def evidence(operation_id, status="written", *, other_status="applied"):
    entries = [{"status": value, "label": value,
                "context": f"{title} {value}", "evidence": f"{title} {value}",
                "confidence": 0.99}
               for title, value in [("Platform Engineer", status), ("Data Engineer", other_status)]]
    return node_call("a.normalizeObservation(input.raw,input.context)", {
        "context": {"operation_id": operation_id, "page_url": URL,
                    "application_ids": ["target", "other"]},
        "raw": {"protocolVersion": 3, "type": "extension.controlled_action_result",
                "requestId": operation_id, "ok": True, "data": {
                    "action": "observe_application_page", "selectorKey": "application_page",
                    "page": {"page_url": URL, "origin": "https://ats.example",
                             "path": "/applications", "text": "Platform Engineer; Data Engineer"},
                    "applicationRecords": [dict(item, title=title) for item, title in
                                           zip(entries, ["Platform Engineer", "Data Engineer"])],
                    "entries": entries, "semanticNodes": [],
                    "capturedAt": "2026-09-18T01:00:00Z",
                    "diagnostics": {"frameScope": "top_only", "iframeCount": 0},
                }},
    })


async def wait_operation(store, key):
    for _ in range(100):
        operation = store.get_by_idempotency_key(key)
        if operation is not None:
            return operation
        await asyncio.sleep(0.005)
    raise AssertionError("fixture operation was not created")


async def acknowledge(server, connection, operation):
    await server.handle_message(connection, {
        "type": "ack", "operation_id": operation.operation_id,
        "sequence": operation.last_outbox_sequence,
    })


@pytest.mark.parametrize("initial,observed,outcome,stage,reason", [
    ("applied", "written", "updated", "written", "updated"),
    ("applied", "applied", "unchanged", "applied", "unchanged"),
    ("written", "applied", "unchanged", "written", "historical_stage_retained"),
    ("applied", "not-mapped", "STATE_UNCLEAR", "applied", "status_evidence_unknown"),
])
def test_adapter_per_job_evidence_reaches_verified_terminal_and_replays(
    tmp_path, monkeypatch, initial, observed, outcome, stage, reason,
):
    repo, store, server = environment(tmp_path, monkeypatch, initial)
    consume = handler("review_and_update_application_status", repo, store)

    async def run():
        connection = await server.register(DEVICE, Socket())
        request = {"application_id": "target", "device_id": DEVICE,
                   "idempotency_key": "review-consumer", "timeout_ms": 2000}
        pending = asyncio.create_task(consume(request))
        operation = await wait_operation(store, request["idempotency_key"])
        assert operation.status == "CONNECTING"
        await acknowledge(server, connection, operation)
        assert store.get_operation(operation.operation_id).status == "DISPATCHED"
        payload = evidence(operation.operation_id, observed)
        assert payload["result"]["database_updated"] is False
        assert all("application_id" not in row for row in payload["result"]["entries"])
        await server.handle_message(connection, payload)
        assert store.get_operation(operation.operation_id).status == "VALIDATING"
        result = await pending
        assert result.data.verification["status"] == outcome
        assert result.data.verification["data"]["reason_code"] == reason
        assert result.data.verification["data"]["wrote"] is (outcome == "updated")
        terminal = "STATE_UNCLEAR" if outcome == "STATE_UNCLEAR" else "SUCCEEDED"
        assert result.data.status.value == terminal
        assert [e.status for e in store.get_events(operation.operation_id)] == [
            "VALIDATING", "UPDATING", terminal,
        ]
        with repo.storage.session() as session:
            assert session.get(ApplicationSnapshot, "target").stage == stage
            assert session.get(ApplicationSnapshot, "other").stage == "applied"
            audits = len(list(session.scalars(select(WriteAudit))))
            history = list(session.get(ApplicationSnapshot, "target").stage_history)
            assert audits == 1
            assert len(history) == (1 if outcome == "updated" else 0)
        replay = await consume(request)
        assert replay.success is (terminal == "SUCCEEDED")
        assert replay.data.idempotent_replay
        assert replay.data.verification == result.data.verification
        with repo.storage.session() as session:
            assert len(list(session.scalars(select(WriteAudit)))) == audits
            assert session.get(ApplicationSnapshot, "target").stage_history == history
        await server.unregister(connection)

    asyncio.run(run())


def test_retry_exhaustion_is_terminal_failed_not_a_hanging_run(tmp_path, monkeypatch):
    repo, store, server = environment(tmp_path, monkeypatch)
    original = workflow.observe_application_status_page_workflow
    attempts = []

    async def bounded(request, bridge, repository):
        attempts.append(request.idempotency_key)
        return await original(request.model_copy(update={"timeout_ms": 40}), bridge, repository)

    monkeypatch.setattr(batch, "observe_application_status_page_workflow", bounded)

    async def run():
        connection = await server.register(DEVICE, Socket())
        consume = handler("batch_observe_application_status", repo, store)
        result = await consume({"all_non_terminal": True})
        run_id = result.summary["run_id"]
        for wave in range(2):
            assert len(attempts) == 2 * (wave + 1)
            assert not result.summary["scope_complete"]
            assert result.summary["remaining_count"] == 2
            assert result.summary["write_count"] == 0
            result = await consume({"run_id": run_id})
            assert result.summary["run_id"] == run_id
        assert len(attempts) == 6
        assert result.summary["scope_complete"] and not result.success
        assert result.summary["failed"] == 2 and result.summary["write_count"] == 0
        assert {row.reason for row in result.failed} == {"timeout"}
        assert all(store.get_by_idempotency_key(key).status == "CANCELLED" for key in attempts)
        with repo.storage.session() as session:
            from packages.storage.models import TaskRun
            assert session.get(TaskRun, result.summary["run_id"]).status == "completed"
            assert list(session.scalars(select(WriteAudit))) == []
        await consume({"run_id": run_id})
        assert len(attempts) == 6, "an exhausted checkpoint must not navigate again"
        await server.unregister(connection)

    asyncio.run(run())


def test_wave_deadline_cancels_operation_and_resumes_frozen_checkpoint(tmp_path, monkeypatch):
    repo, store, server = environment(tmp_path, monkeypatch)
    original = workflow.observe_application_status_page_workflow
    attempts = []

    async def track(request, bridge, repository):
        attempts.append(request.idempotency_key)
        return await original(request, bridge, repository)

    monkeypatch.setattr(batch, "observe_application_status_page_workflow", track)
    monkeypatch.setattr(review, "_WAVE_TIMEOUT_SECONDS", 0.1)

    async def run():
        connection = await server.register(DEVICE, Socket())
        first = await handler("batch_observe_application_status", repo, store)({"all_non_terminal": True})
        assert first.summary["run_status"] == "awaiting_continuation"
        assert first.summary["continuation_required"] is True
        assert first.summary["wave_error"] == "review_wave_timeout"
        assert first.summary["remaining_count"] == 2
        assert store.get_by_idempotency_key(attempts[0]).status == "CANCELLED"
        monkeypatch.setattr(review, "_WAVE_TIMEOUT_SECONDS", 3)
        pending = asyncio.create_task(handler("batch_observe_application_status", repo, store)({
            "run_id": first.summary["run_id"],
        }))
        while len(attempts) < 2:
            await asyncio.sleep(0.005)
        second = await wait_operation(store, attempts[1])
        await acknowledge(server, connection, second)
        await server.handle_message(connection, evidence(second.operation_id))
        resumed = await pending
        assert resumed.summary["run_id"] == first.summary["run_id"]
        assert resumed.summary["scope_complete"] and resumed.success
        assert resumed.summary["write_count"] == 1
        assert store.get_operation(second.operation_id).status == "SUCCEEDED"
        await server.unregister(connection)

    asyncio.run(run())


def test_observation_terminal_replay_returns_evidence_without_dispatch(tmp_path, monkeypatch):
    repo, store, server = environment(tmp_path, monkeypatch)

    async def run():
        connection = await server.register(DEVICE, Socket())
        consume = handler("observe_application_status_page", repo, store)
        request = {"application_id": "target", "device_id": DEVICE,
                   "idempotency_key": "observation-replay", "timeout_ms": 2000}
        pending = asyncio.create_task(consume(request))
        operation = await wait_operation(store, request["idempotency_key"])
        await acknowledge(server, connection, operation)
        await server.handle_message(connection, evidence(operation.operation_id))
        first = await pending
        assert first.success and first.data.status.value == "SUCCEEDED"
        replay = await consume(request)
        assert replay.success and replay.data.observation == first.data.observation
        assert replay.data.idempotent_replay
        assert store.fetch_unacked_outbox(DEVICE) == []
        with repo.storage.session() as session:
            assert list(session.scalars(select(WriteAudit))) == []
        await server.unregister(connection)

    asyncio.run(run())


@pytest.mark.parametrize("stale_status", ["DISPATCHED", "VALIDATING", "UPDATING"])
def test_resumed_persisted_review_without_evidence_is_bounded(tmp_path, monkeypatch, stale_status):
    repo, store, server = environment(tmp_path, monkeypatch)
    request = workflow.ReviewAndUpdateApplicationStatusInput(
        application_id="target", device_id=DEVICE, application_url=URL,
        idempotency_key="stale-review", timeout_ms=40,
    )
    created = workflow.review_and_update_application_status(request, store)
    operation = store.get_operation(created.data.operation_id)
    store.ack(DEVICE, operation.last_outbox_sequence, operation_id=operation.operation_id)
    if stale_status in {"VALIDATING", "UPDATING"}:
        store.append_event(operation.operation_id, "stalled-validation", "VALIDATING")
    if stale_status == "UPDATING":
        store.append_event(operation.operation_id, "stalled-update", "UPDATING")
    reloaded = BrowserBridgeStore(Storage.from_url(str(repo.storage.engine.url)))
    result = asyncio.run(handler("review_and_update_application_status", repo, reloaded)(
        request.model_dump(),
    ))
    assert not result.success and result.timed_out
    assert result.error_code.value == "timeout"
    assert reloaded.get_operation(operation.operation_id).status == "CANCELLED"
    with repo.storage.session() as session:
        assert list(session.scalars(select(WriteAudit))) == []


@pytest.mark.parametrize("name", ["observe_application_status_page", "review_and_update_application_status"])
def test_outer_cancellation_closes_acknowledged_status_operation(tmp_path, monkeypatch, name):
    repo, store, server = environment(tmp_path, monkeypatch)

    async def run():
        connection = await server.register(DEVICE, Socket())
        pending = asyncio.create_task(handler(name, repo, store)({
            "application_id": "target", "device_id": DEVICE,
            "idempotency_key": "cancel-consumer", "timeout_ms": 1000,
        }))
        operation = await wait_operation(store, "cancel-consumer")
        await acknowledge(server, connection, operation)
        await server.unregister(connection)
        assert store.fetch_unacked_outbox(DEVICE) == []
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        saved = store.get_operation(operation.operation_id)
        assert saved.status == "CANCELLED"
        assert saved.result["reason"] == "status_workflow_interrupted"
        assert [row.message_type for row in store.fetch_unacked_outbox(DEVICE)] == ["operation.cancel"]
        with repo.storage.session() as session:
            assert list(session.scalars(select(WriteAudit))) == []

    asyncio.run(run())


def test_disconnect_after_ack_retries_fresh_observation_and_checkpoints_once(tmp_path, monkeypatch):
    repo, store, server = environment(tmp_path, monkeypatch)
    original = workflow.observe_application_status_page_workflow
    attempts = []

    async def bounded(request, bridge, repository):
        attempts.append(request.idempotency_key)
        return await original(request.model_copy(update={
            "timeout_ms": 150 if len(attempts) == 1 else 2000,
        }), bridge, repository)

    monkeypatch.setattr(batch, "observe_application_status_page_workflow", bounded)

    async def run():
        connection = await server.register(DEVICE, Socket())
        pending = asyncio.create_task(handler("batch_observe_application_status", repo, store)({
            "all_non_terminal": True,
        }))
        while not attempts:
            await asyncio.sleep(0.005)
        first = await wait_operation(store, attempts[0])
        await acknowledge(server, connection, first)
        await server.unregister(connection)
        assert store.fetch_unacked_outbox(DEVICE) == []
        # A reconnect cannot replay an ACKed operation, even with a new store.
        reloaded = BrowserBridgeStore(Storage.from_url(str(repo.storage.engine.url)))
        assert reloaded.get_operation(first.operation_id).status == "DISPATCHED"
        socket = Socket()
        connection = await server.register(DEVICE, socket)
        assert await server.dispatch_pending(DEVICE) == 0
        for _ in range(200):
            if len(attempts) == 2:
                break
            await asyncio.sleep(0.005)
        assert len(attempts) == 2
        second = await wait_operation(store, attempts[1])
        assert second.operation_id != first.operation_id
        assert reloaded.get_operation(first.operation_id).status == "CANCELLED"
        await acknowledge(server, connection, second)
        await server.handle_message(connection, evidence(second.operation_id))
        result = await pending
        assert result.summary["scope_complete"] and result.success
        assert result.summary["updated"] == 1 and result.summary["unchanged"] == 1
        assert result.summary["write_count"] == 1
        assert store.get_operation(second.operation_id).status == "SUCCEEDED"
        with repo.storage.session() as session:
            before = len(list(session.scalars(select(WriteAudit))))
        replay = await handler("batch_observe_application_status", repo, store)({
            "run_id": result.summary["run_id"],
        })
        assert replay.summary["processed_count"] == 2 and len(attempts) == 2
        with repo.storage.session() as session:
            assert len(list(session.scalars(select(WriteAudit)))) == before
        # A delayed old result cannot reopen the cancelled attempt or write again.
        await server.handle_message(connection, evidence(first.operation_id))
        assert store.get_operation(first.operation_id).status == "CANCELLED"
        assert socket.messages[-1]["type"] == "error"
        await server.unregister(connection)

    asyncio.run(run())
