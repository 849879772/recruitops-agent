"""Transactional persistence for the Edge active browser bridge."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.storage.database import Storage
from packages.storage.models import (
    BrowserBridgeDevice,
    BrowserOperation,
    BrowserOperationEvent,
    BrowserOutbox,
    BrowserOutboxCursor,
)

from .models import (
    BrowserConnectionStatus,
    MAX_DEVICE_ID_LENGTH,
    MAX_ERROR_CODE_LENGTH,
    MAX_EVENT_ID_LENGTH,
    MAX_EVENT_TYPE_LENGTH,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    MAX_OPERATION_ID_LENGTH,
    MAX_PAYLOAD_STRING_LENGTH,
    OperationName,
    OperationStatus,
    TERMINAL_STATUSES,
    normalize_operation,
    normalize_status,
    validate_bridge_payload,
)


def _now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _text(value: str, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{name} is empty or too long")
    return value


def _sequence(value: int | None, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


_ALLOWED_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.CONNECTING: frozenset(
        {
            OperationStatus.CONNECTING,
            OperationStatus.DISPATCHED,
            OperationStatus.NAVIGATING,
            OperationStatus.WAITING_FOR_LOGIN,
            OperationStatus.EXTRACTING,
            OperationStatus.VALIDATING,
            OperationStatus.STATE_UNCLEAR,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
        }
    ),
    OperationStatus.DISPATCHED: frozenset(
        {
            OperationStatus.DISPATCHED,
            OperationStatus.NAVIGATING,
            OperationStatus.WAITING_FOR_LOGIN,
            OperationStatus.EXTRACTING,
            OperationStatus.VALIDATING,
            OperationStatus.STATE_UNCLEAR,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
        }
    ),
    OperationStatus.NAVIGATING: frozenset(
        {
            OperationStatus.NAVIGATING,
            OperationStatus.WAITING_FOR_LOGIN,
            OperationStatus.EXTRACTING,
            OperationStatus.VALIDATING,
            OperationStatus.STATE_UNCLEAR,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
        }
    ),
    OperationStatus.WAITING_FOR_LOGIN: frozenset(
        {
            OperationStatus.WAITING_FOR_LOGIN,
            OperationStatus.NAVIGATING,
            OperationStatus.EXTRACTING,
            OperationStatus.VALIDATING,
            OperationStatus.STATE_UNCLEAR,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
        }
    ),
    OperationStatus.EXTRACTING: frozenset(
        {
            OperationStatus.EXTRACTING,
            OperationStatus.WAITING_FOR_LOGIN,
            OperationStatus.VALIDATING,
            OperationStatus.STATE_UNCLEAR,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
        }
    ),
    OperationStatus.VALIDATING: frozenset(
        {
            OperationStatus.VALIDATING,
            OperationStatus.UPDATING,
            OperationStatus.SUCCEEDED,
            OperationStatus.STATE_UNCLEAR,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
        }
    ),
    OperationStatus.UPDATING: frozenset(
        {
            OperationStatus.UPDATING,
            OperationStatus.SUCCEEDED,
            OperationStatus.STATE_UNCLEAR,
            OperationStatus.FAILED,
            OperationStatus.CANCELLED,
        }
    ),
    OperationStatus.SUCCEEDED: frozenset({OperationStatus.SUCCEEDED}),
    OperationStatus.STATE_UNCLEAR: frozenset({OperationStatus.STATE_UNCLEAR}),
    OperationStatus.FAILED: frozenset({OperationStatus.FAILED}),
    OperationStatus.CANCELLED: frozenset({OperationStatus.CANCELLED}),
}


class BrowserBridgeStore:
    """SQLAlchemy-backed operation log and at-least-once device outbox."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._initialized = False

    def mark_device_connected(
        self,
        device_id: str,
        *,
        seen_at: datetime | None = None,
    ) -> BrowserConnectionStatus:
        """Persist an authenticated device as connected and refresh its presence."""

        return self._set_device_connection(device_id, connected=True, seen_at=seen_at)

    def mark_device_disconnected(
        self,
        device_id: str,
        *,
        seen_at: datetime | None = None,
    ) -> BrowserConnectionStatus:
        """Persist a device disconnect without retaining any bridge credentials."""

        return self._set_device_connection(device_id, connected=False, seen_at=seen_at)

    def get_connection_status(self, device_id: str) -> BrowserConnectionStatus | None:
        """Read durable connection evidence for MCP and other read-only callers."""

        self._initialize()
        device_value = _text(device_id, "device_id", MAX_DEVICE_ID_LENGTH)
        with self.storage.session() as session:
            device = session.get(BrowserBridgeDevice, device_value)
            return self._connection_status_in_session(session, device)

    get_device_status = get_connection_status
    connection_status = get_connection_status

    def list_connected_device_ids(self) -> list[str]:
        """Return the device ids currently marked connected in the shared database."""

        self._initialize()
        with self.storage.session() as session:
            return list(
                session.scalars(
                    select(BrowserBridgeDevice.device_id)
                    .where(BrowserBridgeDevice.connected.is_(True))
                    .order_by(BrowserBridgeDevice.device_id)
                )
            )

    def create(
        self,
        operation: OperationName | str,
        *,
        device_id: str,
        idempotency_key: str,
        operation_id: str | None = None,
        command: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        created_at: datetime | None = None,
    ) -> BrowserOperation:
        """Create one operation and its durable dispatch command exactly once."""

        self._initialize()
        operation_value = normalize_operation(operation)
        device_value = _text(device_id, "device_id", MAX_DEVICE_ID_LENGTH)
        idem_value = _text(idempotency_key, "idempotency_key", MAX_IDEMPOTENCY_KEY_LENGTH)
        operation_id_value = (
            _text(operation_id, "operation_id", MAX_OPERATION_ID_LENGTH)
            if operation_id is not None
            else f"operation-{uuid4().hex}"
        )
        if command is not None and payload is not None:
            raise TypeError("command and payload cannot both be provided")
        command_value = validate_bridge_payload(command if command is not None else payload)
        timestamp = _now(created_at)

        with self.storage.write_transaction() as session:
            existing = session.scalar(
                select(BrowserOperation)
                .where(BrowserOperation.idempotency_key == idem_value)
                .with_for_update()
            )
            if existing is not None:
                self._assert_create_matches(
                    existing,
                    operation_id=operation_id_value if operation_id is not None else None,
                    operation=operation_value,
                    device_id=device_value,
                    command=command_value,
                )
                return existing

            existing_by_id = session.get(BrowserOperation, operation_id_value)
            if existing_by_id is not None:
                raise ValueError("browser operation id conflicts with an existing operation")

            operation_row = BrowserOperation(
                operation_id=operation_id_value,
                idempotency_key=idem_value,
                operation=operation_value,
                device_id=device_value,
                status=OperationStatus.CONNECTING.value,
                command=deepcopy(command_value),
                last_event_sequence=0,
                created_at=timestamp,
                updated_at=timestamp,
            )
            session.add(operation_row)
            session.flush()

            outbox_payload = {
                "operation_id": operation_id_value,
                "operation": operation_value,
                "command": deepcopy(command_value),
            }
            outbox = self._enqueue_in_session(
                session,
                operation_row,
                message_type="operation.dispatch",
                payload=outbox_payload,
                created_at=timestamp,
            )
            operation_row.last_outbox_sequence = outbox.sequence
            session.flush()
            return operation_row

    def append_event(
        self,
        operation_id: str,
        event_id: str,
        status: OperationStatus | str,
        payload: Mapping[str, Any] | None = None,
        *,
        sequence: int | None = None,
        event_type: str = "state",
        occurred_at: datetime | None = None,
    ) -> BrowserOperationEvent:
        """Append one ordered bridge event, accepting an exact replay idempotently."""

        self._initialize()
        operation_id_value = _text(operation_id, "operation_id", MAX_OPERATION_ID_LENGTH)
        event_id_value = _text(event_id, "event_id", MAX_EVENT_ID_LENGTH)
        status_value = normalize_status(status)
        event_type_value = _text(event_type, "event_type", MAX_EVENT_TYPE_LENGTH)
        payload_value = validate_bridge_payload(payload)
        sequence_value = _sequence(sequence, "sequence")

        with self.storage.write_transaction() as session:
            operation_row = self._operation_in_session(session, operation_id_value)
            if operation_row is None:
                raise KeyError(f"browser operation not found: {operation_id_value}")
            event = self._append_event_in_session(
                session,
                operation_row,
                event_id=event_id_value,
                status=status_value,
                payload=payload_value,
                sequence=sequence_value,
                event_type=event_type_value,
                occurred_at=occurred_at,
            )
            session.flush()
            return event

    def ack(
        self,
        device_id: str,
        sequence: int,
        *,
        operation_id: str | None = None,
        ack_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        acked_at: datetime | None = None,
    ) -> BrowserOperation:
        """ACK an outbox item and durably mark dispatch as DISPATCHED."""

        operation, _outbox = self._ack(
            device_id,
            sequence,
            operation_id=operation_id,
            ack_id=ack_id,
            payload=payload,
            acked_at=acked_at,
        )
        return operation

    def acknowledge_outbox(
        self,
        device_id: str,
        sequence: int,
        *,
        operation_id: str | None = None,
        ack_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        acked_at: datetime | None = None,
    ) -> BrowserOutbox:
        """ACK variant returning the outbox row for transport-level callers."""

        _operation, outbox = self._ack(
            device_id,
            sequence,
            operation_id=operation_id,
            ack_id=ack_id,
            payload=payload,
            acked_at=acked_at,
        )
        return outbox

    def terminal_result(
        self,
        operation_id: str,
        result: Mapping[str, Any] | None = None,
        *,
        status: OperationStatus | str = OperationStatus.SUCCEEDED,
        event_id: str | None = None,
        sequence: int | None = None,
        error_code: str | None = None,
        occurred_at: datetime | None = None,
    ) -> BrowserOperation:
        """Persist a terminal result and its event atomically and idempotently."""

        self._initialize()
        operation_id_value = _text(operation_id, "operation_id", MAX_OPERATION_ID_LENGTH)
        status_value = normalize_status(status)
        if status_value not in TERMINAL_STATUSES - {OperationStatus.CANCELLED}:
            raise ValueError("terminal_result requires SUCCEEDED, STATE_UNCLEAR, or FAILED")
        result_value = validate_bridge_payload(result)
        error_value = (
            _text(error_code, "error_code", MAX_ERROR_CODE_LENGTH)
            if error_code is not None
            else None
        )
        event_id_value = event_id or f"terminal-{operation_id_value}"
        event_id_value = _text(event_id_value, "event_id", MAX_EVENT_ID_LENGTH)
        sequence_value = _sequence(sequence, "sequence")
        timestamp = _now(occurred_at)
        event_payload: dict[str, Any] = {"result": deepcopy(result_value)}
        if error_value is not None:
            event_payload["error_code"] = error_value

        with self.storage.write_transaction() as session:
            operation_row = self._operation_in_session(session, operation_id_value)
            if operation_row is None:
                raise KeyError(f"browser operation not found: {operation_id_value}")
            if operation_row.status in {item.value for item in TERMINAL_STATUSES}:
                if (
                    operation_row.status == status_value.value
                    and operation_row.result == result_value
                    and operation_row.error_code == error_value
                ):
                    return operation_row
                raise ValueError("browser operation already has a conflicting terminal result")

            self._append_event_in_session(
                session,
                operation_row,
                event_id=event_id_value,
                status=status_value,
                payload=event_payload,
                sequence=sequence_value,
                event_type="terminal",
                occurred_at=timestamp,
            )
            operation_row.result = deepcopy(result_value)
            operation_row.error_code = error_value
            operation_row.completed_at = timestamp
            operation_row.updated_at = timestamp
            session.flush()
            return operation_row

    def cancel(
        self,
        operation_id: str,
        *,
        reason: str | None = None,
        event_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> BrowserOperation:
        """Cancel an active operation and enqueue a durable cancel command."""

        self._initialize()
        operation_id_value = _text(operation_id, "operation_id", MAX_OPERATION_ID_LENGTH)
        reason_value = (
            _text(reason, "cancel reason", MAX_PAYLOAD_STRING_LENGTH)
            if reason is not None
            else None
        )
        event_id_value = event_id or f"cancel-{operation_id_value}"
        event_id_value = _text(event_id_value, "event_id", MAX_EVENT_ID_LENGTH)
        timestamp = _now(occurred_at)
        result_value = {"reason": reason_value} if reason_value is not None else {}

        with self.storage.write_transaction() as session:
            operation_row = self._operation_in_session(session, operation_id_value)
            if operation_row is None:
                raise KeyError(f"browser operation not found: {operation_id_value}")
            if operation_row.status == OperationStatus.CANCELLED.value:
                if operation_row.result == result_value:
                    return operation_row
                raise ValueError("browser operation cancellation conflicts with existing result")
            if operation_row.status in {item.value for item in TERMINAL_STATUSES}:
                raise ValueError("cannot cancel a terminal browser operation")

            self._append_event_in_session(
                session,
                operation_row,
                event_id=event_id_value,
                status=OperationStatus.CANCELLED,
                payload=deepcopy(result_value),
                sequence=None,
                event_type="cancel",
                occurred_at=timestamp,
            )
            operation_row.result = deepcopy(result_value)
            operation_row.completed_at = timestamp
            operation_row.updated_at = timestamp
            cancel_payload = {"operation_id": operation_id_value}
            if reason_value is not None:
                cancel_payload["reason"] = reason_value
            cancel_outbox = self._enqueue_in_session(
                session,
                operation_row,
                message_type="operation.cancel",
                payload=cancel_payload,
                created_at=timestamp,
            )
            operation_row.last_outbox_sequence = cancel_outbox.sequence
            session.flush()
            return operation_row

    def recover_interrupted_operations(self, *, now: datetime | None = None) -> int:
        """Cancel orphaned status reads at exclusive, write-opted desktop startup.

        The caller must hold the instance supervisor lock and call this before
        starting the bridge. This is not a live-server sweep or a retry mechanism.
        Other operation types and terminal operation history remain untouched.
        """
        self._initialize()
        timestamp = _now(now)
        reason = "interrupted_by_restart"
        with self.storage.write_transaction() as session:
            operations = list(session.scalars(
                select(BrowserOperation).where(
                    BrowserOperation.operation.in_([
                        OperationName.OBSERVE_APPLICATION_STATUS_PAGE.value,
                        OperationName.REVIEW_AND_UPDATE_APPLICATION_STATUS.value,
                    ]),
                    BrowserOperation.status.not_in([status.value for status in TERMINAL_STATUSES]),
                ).order_by(BrowserOperation.operation_id).with_for_update()
            ))
            for operation in operations:
                result = {"reason": reason, "previous_status": operation.status}
                event_id = f"recovery-{uuid4().hex}"
                # Retire only this operation's unsent/unacknowledged dispatches.
                # Existing transport ACK receipts and other commands are evidence.
                dispatches = session.scalars(select(BrowserOutbox).where(
                    BrowserOutbox.operation_id == operation.operation_id,
                    BrowserOutbox.message_type == "operation.dispatch",
                    BrowserOutbox.acked_at.is_(None),
                ).with_for_update())
                for dispatch in dispatches:
                    dispatch.acked_at = timestamp
                    dispatch.ack_id = event_id
                    dispatch.ack_payload = {"retired_by": "startup_recovery", "reason": reason}
                self._append_event_in_session(
                    session, operation, event_id=event_id, status=OperationStatus.CANCELLED,
                    payload=result, sequence=None, event_type="recovery", occurred_at=timestamp,
                )
                operation.result = result
                operation.error_code = reason
                cancel = self._enqueue_in_session(
                    session, operation, message_type="operation.cancel",
                    payload={"operation_id": operation.operation_id, "reason": reason},
                    created_at=timestamp,
                )
                operation.last_outbox_sequence = cancel.sequence
            # Under the exclusive startup contract, no previous socket is live.
            # Preserve last_seen_at: recovery is not an authenticated heartbeat.
            for device in session.scalars(select(BrowserBridgeDevice).where(
                BrowserBridgeDevice.connected.is_(True),
            ).with_for_update()):
                device.connected = False
            session.flush()
            return len(operations)

    def get_operation(self, operation_id: str) -> BrowserOperation | None:
        self._initialize()
        operation_id_value = _text(operation_id, "operation_id", MAX_OPERATION_ID_LENGTH)
        with self.storage.session() as session:
            return self._operation_in_session(session, operation_id_value)

    get = get_operation
    get_status = get_operation

    def status_for(self, operation_id: str) -> OperationStatus | None:
        operation = self.get_operation(operation_id)
        return normalize_status(operation.status) if operation is not None else None

    def get_events(
        self,
        operation_id: str,
        after_sequence: int = 0,
        *,
        limit: int | None = None,
    ) -> list[BrowserOperationEvent]:
        self._initialize()
        operation_id_value = _text(operation_id, "operation_id", MAX_OPERATION_ID_LENGTH)
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
        ):
            raise ValueError("after_sequence must be a non-negative integer")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
        ):
            raise ValueError("limit must be between 1 and 100")
        with self.storage.session() as session:
            statement = (
                select(BrowserOperationEvent)
                .where(
                    BrowserOperationEvent.operation_id == operation_id_value,
                    BrowserOperationEvent.sequence > after_sequence,
                )
                .order_by(BrowserOperationEvent.sequence)
            )
            if limit is not None:
                statement = statement.limit(limit)
            return list(session.scalars(statement))

    list_events = get_events

    def fetch_unacked_outbox(
        self,
        device_id: str,
        after_sequence: int = 0,
        *,
        limit: int = 100,
    ) -> list[BrowserOutbox]:
        """Return pending messages after a device cursor, retaining ACK gaps."""

        self._initialize()
        device_value = _text(device_id, "device_id", MAX_DEVICE_ID_LENGTH)
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
        ):
            raise ValueError("after_sequence must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self.storage.session() as session:
            return list(
                session.scalars(
                    select(BrowserOutbox)
                    .where(
                        BrowserOutbox.device_id == device_value,
                        BrowserOutbox.sequence > after_sequence,
                        BrowserOutbox.acked_at.is_(None),
                    )
                    .order_by(BrowserOutbox.sequence)
                    .limit(limit)
                )
            )

    list_unacked_outbox = fetch_unacked_outbox
    get_unacked_outbox = fetch_unacked_outbox
    pending_outbox = fetch_unacked_outbox

    def get_by_idempotency_key(self, idempotency_key: str) -> BrowserOperation | None:
        self._initialize()
        idem_value = _text(idempotency_key, "idempotency_key", MAX_IDEMPOTENCY_KEY_LENGTH)
        with self.storage.session() as session:
            return session.scalar(
                select(BrowserOperation).where(BrowserOperation.idempotency_key == idem_value)
            )

    def _set_device_connection(
        self,
        device_id: str,
        *,
        connected: bool,
        seen_at: datetime | None,
    ) -> BrowserConnectionStatus:
        self._initialize()
        device_value = _text(device_id, "device_id", MAX_DEVICE_ID_LENGTH)
        timestamp = _now(seen_at)
        with self.storage.write_transaction() as session:
            device = session.get(BrowserBridgeDevice, device_value, with_for_update=True)
            if device is None:
                device = BrowserBridgeDevice(device_id=device_value)
                session.add(device)
                session.flush()
            device.connected = connected
            device.last_seen_at = timestamp
            session.flush()
            return self._connection_status_in_session(session, device)

    @staticmethod
    def _connection_status_in_session(
        session: Session,
        device: BrowserBridgeDevice | None,
    ) -> BrowserConnectionStatus | None:
        if device is None:
            return None
        pending_count = int(
            session.scalar(
                select(func.count(BrowserOutbox.outbox_id)).where(
                    BrowserOutbox.device_id == device.device_id,
                    BrowserOutbox.acked_at.is_(None),
                )
            )
            or 0
        )
        last_seen_at = _now(device.last_seen_at) if device.last_seen_at is not None else None
        return BrowserConnectionStatus(
            device_id=device.device_id,
            connected=bool(device.connected),
            last_seen_at=last_seen_at,
            pending_outbox_count=pending_count,
        )

    def _ack(
        self,
        device_id: str,
        sequence: int,
        *,
        operation_id: str | None,
        ack_id: str | None,
        payload: Mapping[str, Any] | None,
        acked_at: datetime | None,
    ) -> tuple[BrowserOperation, BrowserOutbox]:
        self._initialize()
        device_value = _text(device_id, "device_id", MAX_DEVICE_ID_LENGTH)
        sequence_value = _sequence(sequence, "sequence")
        assert sequence_value is not None
        operation_id_value = (
            _text(operation_id, "operation_id", MAX_OPERATION_ID_LENGTH)
            if operation_id is not None
            else None
        )
        ack_id_value = (
            _text(ack_id, "ack_id", MAX_EVENT_ID_LENGTH) if ack_id is not None else None
        )
        payload_value = validate_bridge_payload(payload)
        timestamp = _now(acked_at)

        with self.storage.write_transaction() as session:
            outbox = session.scalar(
                select(BrowserOutbox)
                .where(
                    BrowserOutbox.device_id == device_value,
                    BrowserOutbox.sequence == sequence_value,
                )
                .with_for_update()
            )
            if outbox is None:
                raise KeyError(f"browser outbox item not found: {device_value}/{sequence_value}")
            if operation_id_value is not None and outbox.operation_id != operation_id_value:
                raise ValueError("browser outbox ACK operation does not match the item")
            operation_row = self._operation_in_session(session, outbox.operation_id)
            if operation_row is None:
                raise KeyError(f"browser operation not found: {outbox.operation_id}")

            if outbox.acked_at is not None:
                if ack_id_value is not None and outbox.ack_id not in {None, ack_id_value}:
                    raise ValueError("browser outbox ACK conflicts with existing ACK")
                if payload is not None and outbox.ack_payload != payload_value:
                    raise ValueError("browser outbox ACK payload conflicts with existing ACK")
                return operation_row, outbox

            outbox.acked_at = timestamp
            outbox.ack_id = ack_id_value
            outbox.ack_payload = deepcopy(payload_value) if payload is not None else None
            if (
                outbox.message_type == "operation.dispatch"
                and operation_row.status == OperationStatus.CONNECTING.value
            ):
                # ACK transport state is separate from the Edge event sequence.
                # This keeps the first device event free to use sequence 1.
                operation_row.status = OperationStatus.DISPATCHED.value
            operation_row.updated_at = timestamp
            session.flush()
            return operation_row, outbox

    @staticmethod
    def _operation_in_session(
        session: Session,
        operation_id: str,
    ) -> BrowserOperation | None:
        return session.get(BrowserOperation, operation_id)

    @staticmethod
    def _assert_create_matches(
        existing: BrowserOperation,
        *,
        operation_id: str | None,
        operation: str,
        device_id: str,
        command: dict[str, Any],
    ) -> None:
        if operation_id is not None and existing.operation_id != operation_id:
            raise ValueError("browser operation idempotency key conflicts with operation id")
        if (
            existing.operation != operation
            or existing.device_id != device_id
            or existing.command != command
        ):
            raise ValueError("browser operation idempotency key conflicts with request")

    @classmethod
    def _append_event_in_session(
        cls,
        session: Session,
        operation: BrowserOperation,
        *,
        event_id: str,
        status: OperationStatus,
        payload: dict[str, Any],
        sequence: int | None,
        event_type: str,
        occurred_at: datetime | None,
    ) -> BrowserOperationEvent:
        existing = session.get(BrowserOperationEvent, event_id)
        if existing is not None:
            if (
                existing.operation_id != operation.operation_id
                or (sequence is not None and existing.sequence != sequence)
                or existing.status != status.value
                or existing.event_type != event_type
                or existing.payload != payload
                or (
                    occurred_at is not None
                    and not cls._datetimes_equal(existing.occurred_at, occurred_at)
                )
            ):
                raise ValueError("browser operation event conflicts with existing event")
            return existing

        if sequence is None:
            latest_event_sequence = int(
                session.scalar(
                    select(func.max(BrowserOperationEvent.sequence)).where(
                        BrowserOperationEvent.operation_id == operation.operation_id
                    )
                )
                or 0
            )
            sequence = max(operation.last_event_sequence or 0, latest_event_sequence) + 1
        if sequence <= (operation.last_event_sequence or 0):
            raise ValueError("browser operation event sequence must increase")
        occupied = session.scalar(
            select(BrowserOperationEvent).where(
                BrowserOperationEvent.operation_id == operation.operation_id,
                BrowserOperationEvent.sequence == sequence,
            )
        )
        if occupied is not None:
            raise ValueError("browser operation event sequence conflicts with existing event")

        current_status = normalize_status(operation.status)
        if status not in _ALLOWED_TRANSITIONS[current_status]:
            raise ValueError(
                f"invalid browser operation transition: {current_status.value} -> {status.value}"
            )
        timestamp = _now(occurred_at)
        event = BrowserOperationEvent(
            event_id=event_id,
            operation_id=operation.operation_id,
            sequence=sequence,
            status=status.value,
            event_type=event_type,
            payload=deepcopy(payload),
            occurred_at=timestamp,
            created_at=timestamp,
        )
        session.add(event)
        operation.status = status.value
        operation.last_event_sequence = sequence
        operation.updated_at = timestamp
        if status in TERMINAL_STATUSES and operation.completed_at is None:
            operation.completed_at = timestamp
        session.flush()
        return event

    def _enqueue_in_session(
        self,
        session: Session,
        operation: BrowserOperation,
        *,
        message_type: str,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> BrowserOutbox:
        message_type_value = _text(message_type, "message_type", 64)
        payload_value = validate_bridge_payload(payload)
        sequence = self._next_outbox_sequence(session, operation.device_id)
        outbox = BrowserOutbox(
            outbox_id=f"outbox-{uuid4().hex}",
            device_id=operation.device_id,
            sequence=sequence,
            operation_id=operation.operation_id,
            message_type=message_type_value,
            payload=deepcopy(payload_value),
            created_at=created_at,
        )
        session.add(outbox)
        session.flush()
        return outbox

    @staticmethod
    def _next_outbox_sequence(session: Session, device_id: str) -> int:
        cursor = session.get(BrowserOutboxCursor, device_id, with_for_update=True)
        if cursor is None:
            cursor = BrowserOutboxCursor(device_id=device_id, next_sequence=2)
            session.add(cursor)
            session.flush()
            return 1
        sequence = cursor.next_sequence
        cursor.next_sequence += 1
        session.flush()
        return sequence

    def _initialize(self) -> None:
        if not self._initialized:
            self.storage.initialize()
            self._initialized = True

    @staticmethod
    def _datetimes_equal(left: datetime | None, right: datetime | None) -> bool:
        if left is None or right is None:
            return left is right
        left_utc = _now(left)
        right_utc = _now(right)
        return left_utc == right_utc


BrowserBridgeStore.create_operation = BrowserBridgeStore.create
BrowserBridgeStore.append_operation_event = BrowserBridgeStore.append_event
BrowserBridgeStore.record_ack = BrowserBridgeStore.ack
BrowserBridgeStore.ack_outbox = BrowserBridgeStore.acknowledge_outbox
BrowserBridgeStore.record_terminal_result = BrowserBridgeStore.terminal_result
BrowserBridgeStore.save_terminal_result = BrowserBridgeStore.terminal_result
BrowserBridgeStore.cancel_operation = BrowserBridgeStore.cancel
BrowserBridgeStore.get_operation_status = BrowserBridgeStore.get_operation
BrowserBridgeStore.fetch_outbox = BrowserBridgeStore.fetch_unacked_outbox


BrowserOperationStore = BrowserBridgeStore
BrowserBridgeOperationStore = BrowserBridgeStore


__all__ = [
    "BrowserBridgeOperationStore",
    "BrowserBridgeStore",
    "BrowserOperationStore",
]
