"""Startup store transaction fixtures; API opt-in/lock gating belongs to parent."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from packages.browser_bridge import BrowserBridgeStore, OperationName, OperationStatus, TERMINAL_STATUSES
from packages.storage import ApplicationSnapshot, Storage
from packages.storage.models import BrowserOperation, BrowserOperationEvent, BrowserOutbox, BrowserOutboxCursor


NOW = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
READS = [OperationName.OBSERVE_APPLICATION_STATUS_PAGE, OperationName.REVIEW_AND_UPDATE_APPLICATION_STATUS]
ACTIVE = [status for status in OperationStatus if status not in TERMINAL_STATUSES]


def create(store, name=READS[0], status=OperationStatus.CONNECTING, operation_id="read", ack=False):
    operation = store.create(name, device_id="fixture-device", idempotency_key=operation_id,
                             operation_id=operation_id, command={"application_ids": ["fixture-app"]})
    if ack:
        store.ack("fixture-device", operation.last_outbox_sequence, ack_id=f"ack-{operation_id}",
                  payload={"received": True}, acked_at=NOW - timedelta(minutes=1))
    if status == OperationStatus.UPDATING:
        store.append_event(operation_id, f"validate-{operation_id}", OperationStatus.VALIDATING)
    if status != OperationStatus.CONNECTING and not (ack and status == OperationStatus.DISPATCHED):
        store.append_event(operation_id, f"state-{operation_id}", status)
    return store.get_operation(operation_id)


def snapshot(storage):
    with storage.session() as session:
        return {
            model.__tablename__: [
                {column.name: getattr(row, column.name) for column in model.__table__.columns}
                for row in session.scalars(select(model).order_by(*model.__table__.primary_key.columns))
            ]
            for model in [BrowserOperation, BrowserOperationEvent, BrowserOutbox, BrowserOutboxCursor]
        }


@pytest.mark.parametrize("name", READS)
@pytest.mark.parametrize("status", ACTIVE)
@pytest.mark.parametrize("ack", [False, True])
def test_recovery_cancels_status_read_and_atomically_retires_dispatch(tmp_path, name, status, ack):
    url = f"sqlite:///{tmp_path / 'recovery.db'}"
    store = BrowserBridgeStore(Storage.from_url(url, initialize=True))
    # CONNECTING + ACK naturally becomes DISPATCHED; both cases are covered.
    original = create(store, name, status, ack=ack)
    before_events = store.get_events(original.operation_id)
    store.mark_device_connected("fixture-device", seen_at=NOW - timedelta(minutes=2))
    with store.storage.write_transaction() as session:
        session.add(ApplicationSnapshot(id="fixture-app", company_name="Fixture", job_title="Role",
                                       stage="written", stage_history=[], source="fixture",
                                       source_ref="fixture-app", idempotency_key="fixture-app"))
    # Independent store instance represents the next owned startup.
    recovered = BrowserBridgeStore(Storage.from_url(url))
    assert recovered.recover_interrupted_operations(now=NOW) == 1
    operation = recovered.get_operation(original.operation_id)
    assert operation.status == "CANCELLED"
    assert operation.error_code == "interrupted_by_restart"
    assert operation.result == {"reason": "interrupted_by_restart", "previous_status": original.status}
    assert operation.completed_at.replace(tzinfo=timezone.utc) == NOW
    events = recovered.get_events(original.operation_id)
    assert [e.event_id for e in events[:-1]] == [e.event_id for e in before_events]
    assert events[-1].event_type == "recovery" and events[-1].status == "CANCELLED"
    pending = recovered.fetch_unacked_outbox("fixture-device")
    assert len(pending) == 1 and pending[0].message_type == "operation.cancel"
    assert pending[0].sequence == original.last_outbox_sequence + 1
    with recovered.storage.session() as session:
        dispatch = session.scalar(select(BrowserOutbox).where(BrowserOutbox.message_type == "operation.dispatch"))
        assert dispatch.acked_at is not None
        if ack:
            assert dispatch.ack_id == "ack-read" and dispatch.ack_payload == {"received": True}
        else:
            assert dispatch.ack_payload["retired_by"] == "startup_recovery"
        assert session.get(ApplicationSnapshot, "fixture-app").stage == "written"
        assert session.get(ApplicationSnapshot, "fixture-app").stage_history == []
    presence = recovered.get_connection_status("fixture-device")
    assert not presence.connected and presence.last_seen_at == NOW - timedelta(minutes=2)
    before = snapshot(recovered.storage)
    assert recovered.recover_interrupted_operations(now=NOW + timedelta(minutes=1)) == 0
    assert snapshot(recovered.storage) == before
    recovered.ack("fixture-device", pending[0].sequence, operation_id=original.operation_id)
    assert recovered.fetch_unacked_outbox("fixture-device") == []


def test_recovery_preserves_terminal_history_and_unsupported_operations(tmp_path):
    store = BrowserBridgeStore(Storage.from_url(f"sqlite:///{tmp_path / 'recovery.db'}", initialize=True))
    for status in TERMINAL_STATUSES:
        operation = create(store, operation_id=status.value, status=OperationStatus.VALIDATING)
        if status == OperationStatus.CANCELLED:
            store.cancel(operation.operation_id, reason="user_cancelled")
        else:
            store.terminal_result(operation.operation_id, {"fixture": True}, status=status)
    create(store, name=OperationName.CAPTURE_OC_SNAPSHOT, operation_id="excluded-capture")
    store.mark_device_connected("old-idle-device", seen_at=NOW)
    before = snapshot(store.storage)
    assert store.recover_interrupted_operations(now=NOW) == 0
    assert snapshot(store.storage) == before
    assert store.list_connected_device_ids() == []


def test_recovery_rollback_keeps_dispatch_events_cursor_and_presence(tmp_path, monkeypatch):
    store = BrowserBridgeStore(Storage.from_url(f"sqlite:///{tmp_path / 'recovery.db'}", initialize=True))
    create(store, operation_id="first")
    create(store, operation_id="second", status=OperationStatus.VALIDATING)
    store.mark_device_connected("fixture-device", seen_at=NOW)
    before = snapshot(store.storage)
    enqueue = store._enqueue_in_session
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(1)
        result = enqueue(*args, **kwargs)
        if len(calls) == 2:
            raise RuntimeError("fixture transaction failure")
        return result

    monkeypatch.setattr(store, "_enqueue_in_session", fail_second)
    with pytest.raises(RuntimeError, match="fixture transaction failure"):
        store.recover_interrupted_operations(now=NOW)
    assert snapshot(store.storage) == before
    assert store.get_connection_status("fixture-device").connected
    monkeypatch.setattr(store, "_enqueue_in_session", enqueue)
    assert store.recover_interrupted_operations(now=NOW) == 2


def test_recovery_retains_unrelated_dispatch_gap_on_same_device(tmp_path):
    store = BrowserBridgeStore(Storage.from_url(f"sqlite:///{tmp_path / 'recovery.db'}", initialize=True))
    create(store, operation_id="first-read")
    capture = create(store, name=OperationName.CAPTURE_OC_SNAPSHOT, operation_id="capture")
    create(store, operation_id="second-read")
    assert store.recover_interrupted_operations(now=NOW) == 2
    pending = store.fetch_unacked_outbox("fixture-device")
    assert [(row.sequence, row.message_type) for row in pending] == [
        (2, "operation.dispatch"), (4, "operation.cancel"), (5, "operation.cancel"),
    ]
    assert pending[0].operation_id == capture.operation_id
    assert store.get_operation(capture.operation_id).status == "CONNECTING"
    assert store.get_events(capture.operation_id) == []
