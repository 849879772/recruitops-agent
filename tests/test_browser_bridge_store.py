from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import inspect

from packages.browser_bridge import (
    BrowserBridgeStore,
    OperationName,
    OperationStatus,
    WebPageData,
    validate_web_page_data,
)
from packages.storage import Storage


def _storage(database_url: str = "sqlite+pysqlite:///:memory:") -> Storage:
    return Storage.from_url(database_url, initialize=True)


def _create(store: BrowserBridgeStore, *, operation_id: str = "operation-1"):
    return store.create(
        OperationName.REVIEW_AND_UPDATE_APPLICATION_STATUS,
        device_id="edge-1",
        idempotency_key=f"idem-{operation_id}",
        operation_id=operation_id,
        command={"page_url": "https://ats.example/applications/1", "application_ids": ["app-1"]},
    )


def test_schema_contains_operation_event_outbox_and_cursor_tables() -> None:
    storage = _storage()

    assert {
        "browser_operations",
        "browser_operation_events",
        "browser_outbox",
        "browser_outbox_cursors",
        "browser_bridge_devices",
    } <= set(inspect(storage.engine).get_table_names())


def test_device_connection_status_is_persisted_without_credentials(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'browser-bridge-devices.db'}"
    storage = _storage(database_url)
    store = BrowserBridgeStore(storage)
    connected_at = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)

    connected = store.mark_device_connected("edge-1", seen_at=connected_at)
    assert connected.device_id == "edge-1"
    assert connected.connected is True
    assert connected.status == "connected"
    assert connected.last_seen_at == connected_at
    assert dict(connected)["pending_outbox_count"] == 0

    columns = {column["name"] for column in inspect(storage.engine).get_columns("browser_bridge_devices")}
    assert {"device_id", "connected", "last_seen_at"} <= columns
    assert {"token", "cookie", "cookies"}.isdisjoint(columns)

    disconnected_at = datetime(2026, 8, 22, 12, 1, tzinfo=timezone.utc)
    disconnected = store.mark_device_disconnected("edge-1", seen_at=disconnected_at)
    assert disconnected.connected is False
    assert disconnected.status == "disconnected"
    assert disconnected.last_seen_at == disconnected_at

    second_store = BrowserBridgeStore(_storage(database_url))
    persisted = second_store.get_connection_status("edge-1")
    assert persisted is not None
    assert persisted.connected is False
    assert persisted.last_seen_at == disconnected_at


def test_create_is_idempotent_and_writes_one_dispatch_outbox_item() -> None:
    store = BrowserBridgeStore(_storage())

    first = _create(store)
    replay = store.create(
        OperationName.REVIEW_AND_UPDATE_APPLICATION_STATUS,
        device_id="edge-1",
        idempotency_key="idem-operation-1",
        operation_id="operation-1",
        command={"page_url": "https://ats.example/applications/1", "application_ids": ["app-1"]},
    )

    assert first.operation_id == replay.operation_id == "operation-1"
    assert first.status == replay.status == OperationStatus.CONNECTING.value
    pending = store.fetch_unacked_outbox("edge-1", 0)
    assert [(item.sequence, item.message_type) for item in pending] == [(1, "operation.dispatch")]

    with pytest.raises(ValueError, match="conflicts"):
        store.create(
            OperationName.REVIEW_AND_UPDATE_APPLICATION_STATUS,
            device_id="edge-1",
            idempotency_key="idem-operation-1",
            operation_id="operation-1",
            command={"page_url": "https://ats.example/applications/2"},
        )


def test_ack_and_event_replay_are_idempotent_and_keep_sequences_separate() -> None:
    store = BrowserBridgeStore(_storage())
    _create(store)

    acked = store.ack("edge-1", 1, ack_id="ack-1")
    replayed = store.ack("edge-1", 1, ack_id="ack-1")
    assert acked.status == replayed.status == OperationStatus.DISPATCHED.value
    assert store.fetch_unacked_outbox("edge-1", 0) == []

    event = store.append_event(
        "operation-1",
        "event-1",
        OperationStatus.NAVIGATING,
        {"message": "opening approved page"},
        sequence=1,
    )
    replayed_event = store.append_event(
        "operation-1",
        "event-1",
        OperationStatus.NAVIGATING,
        {"message": "opening approved page"},
        sequence=1,
    )
    assert event.event_id == replayed_event.event_id == "event-1"
    assert store.status_for("operation-1") is OperationStatus.NAVIGATING

    with pytest.raises(ValueError, match="transition"):
        store.append_event("operation-1", "event-2", OperationStatus.CONNECTING, sequence=2)


def test_terminal_result_and_cancel_follow_terminal_idempotency_rules() -> None:
    store = BrowserBridgeStore(_storage())
    _create(store)
    store.ack("edge-1", 1)
    store.append_event("operation-1", "event-1", OperationStatus.NAVIGATING)
    store.append_event("operation-1", "event-2", OperationStatus.EXTRACTING)
    store.append_event("operation-1", "event-3", OperationStatus.VALIDATING)
    store.append_event("operation-1", "event-4", OperationStatus.UPDATING)

    result = store.terminal_result(
        "operation-1",
        {"status": "applied", "confidence": 0.98},
        status=OperationStatus.SUCCEEDED,
    )
    replay = store.terminal_result(
        "operation-1",
        {"status": "applied", "confidence": 0.98},
        status=OperationStatus.SUCCEEDED,
    )
    assert result.status == replay.status == OperationStatus.SUCCEEDED.value
    assert result.result == {"status": "applied", "confidence": 0.98}

    with pytest.raises(ValueError, match="conflicting terminal"):
        store.terminal_result("operation-1", {"status": "rejected"})
    with pytest.raises(ValueError, match="cannot cancel"):
        store.cancel("operation-1")


def test_cancel_is_idempotent_and_enqueues_a_device_command() -> None:
    store = BrowserBridgeStore(_storage())
    _create(store)

    cancelled = store.cancel("operation-1", reason="user stopped the review")
    replay = store.cancel("operation-1", reason="user stopped the review")
    assert cancelled.status == replay.status == OperationStatus.CANCELLED.value
    assert cancelled.result == {"reason": "user stopped the review"}
    assert cancelled.last_outbox_sequence == 2

    pending = store.fetch_unacked_outbox("edge-1", 0)
    assert [(item.sequence, item.message_type) for item in pending] == [
        (1, "operation.dispatch"),
        (2, "operation.cancel"),
    ]
    store.ack("edge-1", 1)
    assert [item.sequence for item in store.fetch_unacked_outbox("edge-1", 0)] == [2]


def test_outbox_sequences_are_device_scoped_and_cursor_filter_is_recoverable() -> None:
    store = BrowserBridgeStore(_storage())
    _create(store, operation_id="operation-1")
    store.create(
        OperationName.REVIEW_AND_UPDATE_APPLICATION_STATUS,
        device_id="edge-1",
        idempotency_key="idem-operation-2",
        operation_id="operation-2",
    )
    store.create(
        OperationName.REVIEW_AND_UPDATE_APPLICATION_STATUS,
        device_id="edge-2",
        idempotency_key="idem-operation-3",
        operation_id="operation-3",
    )

    store.ack("edge-1", 1)
    assert [item.sequence for item in store.fetch_unacked_outbox("edge-1", 0)] == [2]
    assert [item.sequence for item in store.fetch_unacked_outbox("edge-1", 1)] == [2]
    assert [item.sequence for item in store.fetch_unacked_outbox("edge-2", 0)] == [1]


def test_store_recovers_operation_and_unacked_outbox_in_a_new_instance(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'browser-bridge.db'}"
    first_storage = _storage(database_url)
    _create(BrowserBridgeStore(first_storage))
    first_storage.engine.dispose()

    second_storage = Storage.from_url(database_url, initialize=True)
    second_store = BrowserBridgeStore(second_storage)
    operation = second_store.get_operation("operation-1")

    assert operation is not None
    assert operation.status == OperationStatus.CONNECTING.value
    assert [item.sequence for item in second_store.fetch_unacked_outbox("edge-1")] == [1]


def test_page_data_is_allowlisted_and_bounded_before_persistence() -> None:
    page = WebPageData(
        url="https://ats.example/applications/1",
        origin="https://ats.example",
        path="/applications/1",
        title="Application",
        page_text="current status: interview",
        links=["https://ats.example/applications/1"],
    )
    assert validate_web_page_data(page)["text"] == "current status: interview"

    store = BrowserBridgeStore(_storage())
    _create(store)
    store.append_event(
        "operation-1",
        "event-page",
        OperationStatus.EXTRACTING,
        {"page_data": page.model_dump(mode="json")},
    )

    with pytest.raises(ValueError):
        WebPageData(url="https://ats.example/applications/1", html="<script>alert(1)</script>")
    with pytest.raises(ValueError):
        WebPageData(
            url="https://ats.example/applications/1",
            page_text="x" * 20_001,
        )
    with pytest.raises(ValueError, match="cookies"):
        store.append_event(
            "operation-1",
            "event-forbidden",
            OperationStatus.VALIDATING,
            {"cookies": "session=secret"},
        )
