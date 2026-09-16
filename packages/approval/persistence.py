from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from packages.storage import Approval as ApprovalRecord
from packages.storage import Storage, TaskRun

from .models import ApprovalPreview, ApprovalStatus, ApprovalToken


_PREVIEW_KEY = "approval_preview"
_TOKEN_KEY = "approval_token"


class SqlAlchemyApprovalPersistence:
    """Persist approval capabilities in the Agent-owned database."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._initialized = False

    def _initialize(self) -> None:
        if not self._initialized:
            self.storage.initialize()
            self._initialized = True

    def load(self) -> list[tuple[ApprovalToken, ApprovalPreview]]:
        self._initialize()
        result: list[tuple[ApprovalToken, ApprovalPreview]] = []
        with self.storage.session() as session:
            rows = session.query(ApprovalRecord).order_by(ApprovalRecord.created_at).all()
            for row in rows:
                state = self._decode(row)
                if state is not None:
                    result.append(state)
        return result

    def get(self, token_id: str) -> tuple[ApprovalToken, ApprovalPreview] | None:
        """Read one approval capability from the Agent-owned database."""

        self._initialize()
        with self.storage.session() as session:
            return self._decode(session.get(ApprovalRecord, token_id))

    def create_or_get(
        self,
        token: ApprovalToken,
        preview: ApprovalPreview,
    ) -> tuple[bool, tuple[ApprovalToken, ApprovalPreview] | None]:
        """Insert one capability or return the row that won the idempotency race."""

        self._initialize()
        with self.storage.transaction(write=True) as session:
            self._insert_if_absent(
                session,
                TaskRun.__table__,
                {
                    "id": token.task_id,
                    "task_type": "approval",
                    "status": "awaiting_approval",
                    "user_request": f"Approval-gated operation: {token.operation.value}",
                    "current_step": "human_approval",
                    "step_count": 0,
                    "max_steps": 1,
                    "source": "approval_registry",
                    "source_ref": token.task_id,
                },
            )
            inserted = self._insert_if_absent(
                session,
                ApprovalRecord.__table__,
                {
                    "id": token.token_id,
                    "task_id": token.task_id,
                    "operation": token.operation.value,
                    "preview": self._envelope(token, preview),
                    "status": token.status.value,
                    "idempotency_key": token.idempotency_key,
                    "decided_at": self._decided_at(token),
                    "source": "approval_registry",
                    "source_ref": token.token_id,
                },
            )
            row = session.scalar(
                select(ApprovalRecord).where(
                    ApprovalRecord.idempotency_key == token.idempotency_key
                )
            )
            return inserted, self._decode(row)

    def compare_and_set(
        self,
        expected: ApprovalToken,
        replacement: ApprovalToken,
        preview: ApprovalPreview,
    ) -> bool:
        """Persist a transition only if the token still has the expected status.

        The status predicate is the durable version of the registry's state
        machine guard. A competing worker that changed the token first gets the
        row, and this update affects zero rows without overwriting it.
        """

        if expected.token_id != replacement.token_id:
            raise ValueError("compare-and-set tokens must have the same token_id")
        self._initialize()
        envelope = self._envelope(replacement, preview)
        decided_at = self._decided_at(replacement)
        with self.storage.transaction(write=True) as session:
            result = session.execute(
                update(ApprovalRecord)
                .where(
                    ApprovalRecord.id == expected.token_id,
                    ApprovalRecord.status == expected.status.value,
                )
                .values(
                    preview=envelope,
                    status=replacement.status.value,
                    decided_at=decided_at,
                )
            )
            return result.rowcount == 1

    def save(self, token: ApprovalToken, preview: ApprovalPreview) -> None:
        self._initialize()
        envelope = self._envelope(token, preview)
        decided_at = self._decided_at(token)
        with self.storage.transaction(write=True) as session:
            task = session.get(TaskRun, token.task_id)
            if task is None:
                session.add(
                    TaskRun(
                        id=token.task_id,
                        task_type="approval",
                        status="awaiting_approval",
                        user_request=f"Approval-gated operation: {token.operation.value}",
                        current_step="human_approval",
                        step_count=0,
                        max_steps=1,
                        source="approval_registry",
                        source_ref=token.task_id,
                    )
                )
            row = session.get(ApprovalRecord, token.token_id)
            if row is None:
                session.add(
                    ApprovalRecord(
                        id=token.token_id,
                        task_id=token.task_id,
                        operation=token.operation.value,
                        preview=envelope,
                        status=token.status.value,
                        idempotency_key=token.idempotency_key,
                        decided_at=decided_at,
                        source="approval_registry",
                        source_ref=token.token_id,
                    )
                )
            else:
                row.preview = envelope
                row.status = token.status.value
                row.decided_at = decided_at

    @staticmethod
    def _insert_if_absent(session, table, values: dict[str, object]) -> bool:
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            statement = sqlite_insert(table).values(values).on_conflict_do_nothing()
        elif dialect == "postgresql":
            statement = postgresql_insert(table).values(values).on_conflict_do_nothing()
        else:
            statement = insert(table).values(values)
        result = session.execute(statement)
        return result.rowcount == 1

    @staticmethod
    def _envelope(token: ApprovalToken, preview: ApprovalPreview) -> dict[str, object]:
        return {
            _PREVIEW_KEY: preview.model_dump(mode="json"),
            _TOKEN_KEY: token.model_dump(mode="json"),
        }

    @staticmethod
    def _decided_at(token: ApprovalToken) -> datetime | None:
        return (
            token.consumed_at
            if token.status is ApprovalStatus.CONSUMED
            else datetime.now(timezone.utc)
            if token.status is not ApprovalStatus.PENDING
            else None
        )

    @staticmethod
    def _decode(row: ApprovalRecord | None) -> tuple[ApprovalToken, ApprovalPreview] | None:
        if row is None:
            return None
        envelope = row.preview or {}
        preview_data = envelope.get(_PREVIEW_KEY)
        token_data = envelope.get(_TOKEN_KEY)
        if not isinstance(preview_data, dict) or not isinstance(token_data, dict):
            return None
        return (
            ApprovalToken.model_validate(token_data),
            ApprovalPreview.model_validate(preview_data),
        )


__all__ = ["SqlAlchemyApprovalPersistence"]
