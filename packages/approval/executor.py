from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from hashlib import sha256
from threading import RLock
from typing import Any, Protocol
from uuid import uuid4

from pydantic import Field

from packages.security import redact_sensitive

from .models import ApprovalModel, ApprovalStatus, ApprovalToken, OperationName
from .service import ApprovalRegistry


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _audit_attempt_key(idempotency_key: str, execution_id: str) -> str:
    material = f"{idempotency_key}\x00{execution_id}"
    return f"write-attempt:{sha256(material.encode('utf-8')).hexdigest()}"


class WriteEffect(ApprovalModel):
    before: dict[str, Any] | None = None
    after: dict[str, Any]
    rollback_payload: dict[str, Any] | None = None


class WriteAuditRecord(ApprovalModel):
    execution_id: str
    token_id: str
    task_id: str
    operation: OperationName
    idempotency_key: str
    operator: str = Field(min_length=1, max_length=200)
    evidence_digest: str
    started_at: datetime
    completed_at: datetime
    success: bool
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    rollback_payload: dict[str, Any] | None = None
    error_code: str | None = None
    approval_idempotency_key: str | None = None


class ApprovedWriteAdapter(Protocol):
    """Explicit atomic operation surface; arbitrary commands are intentionally absent.

    An adapter must either return a ``WriteEffect`` after committing its write or
    raise before committing it. This lets a failed execution claim be released
    without replaying a partially applied write.
    """

    def update_company_config(self, payload: dict[str, Any]) -> WriteEffect: ...

    def update_crawler_recipe(self, payload: dict[str, Any]) -> WriteEffect: ...

    def create_application(self, payload: dict[str, Any]) -> WriteEffect: ...

    def update_application_stage(self, payload: dict[str, Any]) -> WriteEffect: ...

    def create_schedule(self, payload: dict[str, Any]) -> WriteEffect: ...

    def bind_recruitment_mail(self, payload: dict[str, Any]) -> WriteEffect: ...


class ApprovedWriteExecutor:
    """Claim, execute, and consume one approved token with an audited write."""

    def __init__(
        self,
        registry: ApprovalRegistry,
        adapter: ApprovedWriteAdapter,
        *,
        before_write: Callable[[], None] | None = None,
        audit_sink: Callable[[WriteAuditRecord], None] | None = None,
    ) -> None:
        self.registry = registry
        self.adapter = adapter
        self.before_write = before_write
        self.audit_sink = audit_sink
        self._used_keys: set[str] = set()
        self._records: list[WriteAuditRecord] = []
        self._lock = RLock()

    def execute(
        self,
        token_id: str,
        *,
        operator: str,
        now: datetime | None = None,
    ) -> WriteAuditRecord:
        started = now or utc_now()
        if not isinstance(operator, str) or not operator.strip():
            raise ValueError("operator is required before an approved write can execute")
        with self._lock:
            token = self.registry.token(token_id)
            preview = self.registry.preview(token_id)
            if token.operation is OperationName.BROWSER_ACTION:
                raise PermissionError("browser actions require the dedicated browser endpoint")
            execution_id = uuid4().hex
            decision = self.registry.begin(
                token_id,
                now=started,
                existing_idempotency_keys=self._used_keys,
            )
            if not decision.allowed or decision.status is not ApprovalStatus.EXECUTING:
                code = decision.error_code.value if decision.error_code else "write_not_authorized"
                raise PermissionError(code)
            claimed_token = decision.token or token

            try:
                if self.before_write is not None:
                    self.before_write()
            except Exception as exc:
                self._release_claim(token_id, now=started)
                record = self._failure_record(
                    execution_id,
                    claimed_token,
                    operator=operator,
                    started=started,
                    error=exc,
                )
                self._store_record(record)
                raise RuntimeError(
                    "approved write backup failed; inspect the audit record"
                ) from exc

            try:
                effect = self._dispatch(preview.operation, dict(preview.payload))
            except Exception as exc:
                self._release_claim(token_id, now=started)
                record = self._failure_record(
                    execution_id,
                    claimed_token,
                    operator=operator,
                    started=started,
                    error=exc,
                )
                self._store_record(record)
                raise RuntimeError(
                    "approved write adapter failed; inspect the audit record"
                ) from exc

            completion = self.registry.complete(token_id, now=started)
            if not completion.allowed or completion.status is not ApprovalStatus.CONSUMED:
                code = (
                    completion.error_code.value
                    if completion.error_code
                    else "write_not_committed"
                )
                record = self._failure_record(
                    execution_id,
                    claimed_token,
                    operator=operator,
                    started=started,
                    error_code=code,
                    effect=effect,
                )
                self._store_record(record)
                raise RuntimeError("approved write completion failed; inspect the audit record")

            self._used_keys.add(claimed_token.idempotency_key)
            record = WriteAuditRecord(
                execution_id=execution_id,
                token_id=claimed_token.token_id,
                task_id=claimed_token.task_id,
                operation=claimed_token.operation,
                idempotency_key=claimed_token.idempotency_key,
                operator=operator,
                evidence_digest=claimed_token.evidence_digest,
                approval_idempotency_key=claimed_token.idempotency_key,
                started_at=started,
                completed_at=utc_now(),
                success=True,
                before=redact_sensitive(effect.before),
                after=redact_sensitive(effect.after),
                rollback_payload=redact_sensitive(effect.rollback_payload),
            )
            self._store_record(record)
            return record

    def _release_claim(self, token_id: str, *, now: datetime) -> None:
        """Best-effort release; a failed release must keep the claim blocking replay."""

        try:
            self.registry.release(token_id, now=now)
        except Exception:
            return

    @staticmethod
    def _failure_record(
        execution_id: str,
        token: ApprovalToken,
        *,
        operator: str,
        started: datetime,
        error: Exception | None = None,
        error_code: str | None = None,
        effect: WriteEffect | None = None,
    ) -> WriteAuditRecord:
        return WriteAuditRecord(
            execution_id=execution_id,
            token_id=token.token_id,
            task_id=token.task_id,
            operation=token.operation,
            idempotency_key=_audit_attempt_key(token.idempotency_key, execution_id),
            operator=operator,
            evidence_digest=token.evidence_digest,
            started_at=started,
            completed_at=utc_now(),
            success=False,
            before=redact_sensitive(effect.before) if effect is not None else None,
            after=redact_sensitive(effect.after) if effect is not None else None,
            rollback_payload=(
                redact_sensitive(effect.rollback_payload) if effect is not None else None
            ),
            error_code=error_code or (type(error).__name__ if error is not None else None),
            approval_idempotency_key=token.idempotency_key,
        )

    def _store_record(self, record: WriteAuditRecord) -> None:
        """Keep a local copy and forward the same immutable-shaped record to storage."""

        self._records.append(record)
        if self.audit_sink is not None:
            self.audit_sink(record)

    def records(self) -> list[WriteAuditRecord]:
        return list(self._records)

    def rollback_preview(self, execution_id: str) -> dict[str, Any] | None:
        for record in self._records:
            if record.execution_id == execution_id:
                return record.rollback_payload
        raise KeyError(f"write execution {execution_id!r} was not found")

    def _dispatch(self, operation: OperationName, payload: dict[str, Any]) -> WriteEffect:
        handler_names = {
            OperationName.COMPANY_CONFIG_UPDATE: "update_company_config",
            OperationName.CRAWLER_RECIPE_UPDATE: "update_crawler_recipe",
            OperationName.APPLICATION_CREATE: "create_application",
            OperationName.APPLICATION_STAGE_UPDATE: "update_application_stage",
            OperationName.SCHEDULE_CREATE: "create_schedule",
            OperationName.RECRUITMENT_MAIL_BINDING: "bind_recruitment_mail",
        }
        handler = getattr(self.adapter, handler_names[operation])
        return handler(payload)


__all__ = [
    "ApprovedWriteAdapter",
    "ApprovedWriteExecutor",
    "WriteAuditRecord",
    "WriteEffect",
]
