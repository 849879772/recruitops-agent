from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from packages.browser_bridge import BrowserBridgeStore, OperationStatus
from packages.domain.models import Application, ApplicationStage
from packages.mcp import register_tools
from packages.storage import ApplicationSnapshot, Storage
from packages.tools.browser_bridge import (
    BrowserErrorCode,
    BrowserOperationStatusInput,
    CancelBrowserOperationInput,
    EdgeConnectionStatusInput,
    ObserveApplicationStatusPageInput,
    ReviewAndUpdateApplicationStatusInput,
    browser_operation_status,
    cancel_browser_operation,
    edge_connection_status,
    observe_application_status_page,
    review_and_update_application_status,
    review_and_update_application_status_workflow,
)


class FakeMCPServer:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *, name: str, description: str):
        def register(handler):
            self.tools[name] = handler
            return handler

        return register


def _store() -> BrowserBridgeStore:
    return BrowserBridgeStore(Storage.from_url("sqlite+pysqlite:///:memory:"))


def _request(**updates: object) -> ReviewAndUpdateApplicationStatusInput:
    values: dict[str, object] = {
        "application_id": "application-1",
        "device_id": "edge-1",
        "idempotency_key": "mcp-browser-1",
    }
    values.update(updates)
    return ReviewAndUpdateApplicationStatusInput(**values)


def test_review_tool_creates_one_persistent_idempotent_operation_without_confirmation() -> None:
    store = _store()

    first = review_and_update_application_status(_request(), store)
    replay = review_and_update_application_status(_request(), store)

    assert first.success is True
    assert first.read_only is False
    assert first.audit.side_effect is True
    assert first.data is not None
    assert first.data.requires_confirmation is False
    assert replay.data is not None
    assert replay.data.idempotent_replay is True
    assert replay.data.operation_id == first.data.operation_id
    assert len(store.fetch_unacked_outbox("edge-1")) == 1


def test_operation_status_reads_events_and_terminal_result_as_a_read_only_tool() -> None:
    store = _store()
    created = review_and_update_application_status(_request(), store)
    assert created.data is not None
    operation_id = created.data.operation_id

    store.ack("edge-1", 1)
    store.append_event(operation_id, "event-1", OperationStatus.NAVIGATING)
    store.append_event(operation_id, "event-2", OperationStatus.EXTRACTING)
    store.append_event(operation_id, "event-3", OperationStatus.VALIDATING)
    store.append_event(operation_id, "event-4", OperationStatus.UPDATING)
    store.terminal_result(operation_id, {"status": "applied"})

    response = browser_operation_status(
        BrowserOperationStatusInput(operation_id=operation_id),
        store,
    )

    assert response.success is True
    assert response.read_only is True
    assert response.audit.side_effect is False
    assert response.data is not None
    assert response.data.status is OperationStatus.SUCCEEDED
    assert response.data.terminal is True
    assert response.data.terminal_result == {"status": "applied"}
    assert response.data.next_sequence == 5
    assert response.data.changed is True
    assert response.data.wait_timed_out is False
    assert [event.status for event in response.data.events][-1] is OperationStatus.SUCCEEDED


def test_operation_status_uses_incremental_cursor_and_bounded_wait() -> None:
    store = _store()
    created = review_and_update_application_status(_request(), store)
    assert created.data is not None
    operation_id = created.data.operation_id
    store.ack("edge-1", 1)
    store.append_event(operation_id, "event-1", OperationStatus.NAVIGATING)
    store.append_event(operation_id, "event-2", OperationStatus.EXTRACTING)

    changed = browser_operation_status(
        BrowserOperationStatusInput(
            operation_id=operation_id,
            after_sequence=1,
            event_limit=1,
            timeout_ms=1,
        ),
        store,
    )
    assert changed.data is not None
    assert [event.sequence for event in changed.data.events] == [2]
    assert changed.data.next_sequence == 2
    assert changed.data.changed is True

    unchanged = browser_operation_status(
        BrowserOperationStatusInput(
            operation_id=operation_id,
            after_sequence=2,
            timeout_ms=1,
        ),
        store,
    )
    assert unchanged.data is not None
    assert unchanged.data.events == []
    assert unchanged.data.next_sequence == 2
    assert unchanged.data.changed is False
    assert unchanged.data.wait_timed_out is True


def test_cancel_only_dispatches_for_active_operations_and_replays_cancel_idempotently() -> None:
    store = _store()
    created = review_and_update_application_status(_request(), store)
    assert created.data is not None
    operation_id = created.data.operation_id

    cancelled = cancel_browser_operation(
        CancelBrowserOperationInput(operation_id=operation_id, reason="stop"),
        store,
    )
    replay = cancel_browser_operation(
        CancelBrowserOperationInput(operation_id=operation_id, reason="stop"),
        store,
    )

    assert cancelled.success is True
    assert cancelled.read_only is False
    assert cancelled.data is not None
    assert cancelled.data.status is OperationStatus.CANCELLED
    assert replay.success is True
    assert replay.data is not None and replay.data.idempotent_replay is True
    assert len(store.fetch_unacked_outbox("edge-1")) == 2

    completed = review_and_update_application_status(
        _request(idempotency_key="mcp-browser-2"),
        store,
    )
    assert completed.data is not None
    dispatch = store.fetch_unacked_outbox("edge-1")[-1]
    store.ack("edge-1", dispatch.sequence, operation_id=completed.data.operation_id)
    store.append_event(
        completed.data.operation_id,
        "validating-mcp-browser-2",
        OperationStatus.VALIDATING,
    )
    store.terminal_result(completed.data.operation_id, {"status": "done"})
    rejected = cancel_browser_operation(
        CancelBrowserOperationInput(operation_id=completed.data.operation_id),
        store,
    )
    assert rejected.success is False
    assert rejected.error_code is BrowserErrorCode.OPERATION_NOT_ACTIVE


def test_edge_connection_status_uses_injected_provider_or_reports_unknown_persisted_state() -> None:
    store = _store()
    connected = edge_connection_status(
        EdgeConnectionStatusInput(device_id="edge-1"),
        store,
        lambda device_id: {
            "device_id": device_id,
            "status": "connected",
            "connected": True,
            "last_seen_at": datetime(2026, 8, 22, tzinfo=timezone.utc),
        },
    )
    unknown = edge_connection_status(EdgeConnectionStatusInput(device_id="edge-1"), store)

    assert connected.success is True
    assert connected.read_only is True
    assert connected.data is not None and connected.data.connected is True
    assert unknown.success is True
    assert unknown.data is not None
    assert unknown.data.status.value == "unknown"
    assert unknown.data.available is False


def test_observation_without_a_page_url_never_queues_an_invalid_edge_command() -> None:
    store = _store()
    response = observe_application_status_page(
        ObserveApplicationStatusPageInput(
            application_id="application-1",
            device_id="edge-1",
            idempotency_key="missing-observation-url",
        ),
        store,
    )

    assert response.success is False
    assert response.error_code is BrowserErrorCode.INVALID_INPUT
    assert store.fetch_unacked_outbox("edge-1") == []


def test_complete_mcp_registration_keeps_query_and_action_classifications_distinct() -> None:
    server = FakeMCPServer()
    register_tools(server, object(), object(), _store())  # type: ignore[arg-type]

    assert {
        "edge_connection_status",
        "observe_application_status_page",
        "verify_application_status_evidence",
        "browser_operation_status",
        "cancel_browser_operation",
    } <= set(server.tools)


def test_review_workflow_waits_for_edge_evidence_and_commits_verified_forward_stage(tmp_path) -> None:
    async def scenario() -> None:
        storage = Storage.from_url(f"sqlite:///{tmp_path / 'workflow.db'}", initialize=True)
        store = BrowserBridgeStore(storage)
        record_url = "https://ats.example/applications/24"
        with storage.write_transaction() as session:
            session.add(
                ApplicationSnapshot(
                    id="24",
                    company_name="示例公司",
                    job_title="软件开发工程师",
                    record_url=record_url,
                    stage="applied",
                    idempotency_key="application:24",
                    stage_history=[],
                    source="test",
                    source_ref="24",
                )
            )
        application = Application(
            id="24",
            company_name="示例公司",
            job_title="软件开发工程师",
            record_url=record_url,
            stage=ApplicationStage.APPLIED,
            idempotency_key="application:24",
            source="test",
            source_ref="24",
        )

        class Repository:
            def list_applications(self):
                return [application]

        request = ReviewAndUpdateApplicationStatusInput(
            application_id="24",
            device_id="edge-1",
            idempotency_key="workflow-24",
            timeout_ms=2_000,
        )
        pending = asyncio.create_task(
            review_and_update_application_status_workflow(request, store, Repository())
        )
        operation = None
        for _ in range(50):
            operation = store.get_by_idempotency_key("workflow-24")
            if operation is not None:
                break
            await asyncio.sleep(0.01)
        assert operation is not None
        dispatch = store.fetch_unacked_outbox("edge-1")[0]
        store.ack("edge-1", dispatch.sequence, operation_id=operation.operation_id)
        store.append_event(
            operation.operation_id,
            "browser-observation",
            OperationStatus.VALIDATING,
            {
                "result": {
                    "application_id": "24",
                    "application_ids": ["24"],
                    "page_url": record_url,
                    "captured_at": "2026-08-22T12:00:00Z",
                    "entries": [
                        {
                            "application_id": "24",
                            "status": "written",
                            "label": "笔试中",
                            "context": "示例公司 软件开发工程师 笔试中",
                        }
                    ],
                }
            },
            event_type="observation",
        )

        response = await pending
        assert response.success is True
        assert response.data is not None
        assert response.data.status is OperationStatus.SUCCEEDED
        assert response.data.verification is not None
        assert response.data.verification["status"] == "updated"
        with storage.session() as session:
            updated = session.get(ApplicationSnapshot, "24")
        assert updated is not None and updated.stage == "written"

    asyncio.run(scenario())
