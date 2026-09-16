from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .connectors import MailConnector
from .models import RecruitmentMailProcessingStatus
from .preparation import _message_content_digest, prepare_mail_for_model
from .storage import RecruitmentMailStore


class RecruitmentMailSyncResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    operation_id: str
    account_key: str
    mailbox: str
    fetched: int = Field(ge=0)
    inserted: int = Field(ge=0)
    reused: int = Field(ge=0)
    attempts: int = Field(ge=1)
    cursor: str | None = None
    next_cursor: str | None = None


class RecruitmentMailSyncService:
    """Incrementally read, parse and persist mail before advancing its cursor."""

    def __init__(self, store: RecruitmentMailStore) -> None:
        self.store = store

    def sync(
        self,
        *,
        account_key: str,
        connector: MailConnector,
        mailbox: str = "INBOX",
        limit: int = 100,
        run_id: str | None = None,
        operation_id: str | None = None,
        attempts: int = 1,
    ) -> RecruitmentMailSyncResult:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        run_id = run_id or f"mail-run-{uuid4().hex}"
        operation_id = operation_id or f"mail-sync-{uuid4().hex}"
        cursor = self.store.get_cursor(account_key, mailbox)
        self.store.start_sync_run(
            run_id=run_id,
            operation_id=operation_id,
            account_key=account_key,
            mailbox=mailbox,
            attempts=attempts,
            cursor_before=cursor.token,
        )
        fetched = 0
        inserted = 0
        reused = 0
        try:
            batch = connector.fetch_since(cursor, limit=limit)
            fetched = len(batch.messages)
            for message in batch.messages:
                existing = self.store.get_by_mailbox_message_id(
                    message.identity.mailbox,
                    message.identity.message_id,
                )
                same_content = (
                    existing is not None
                    and existing.content_digest == _message_content_digest(message)
                )
                if same_content:
                    record = existing
                else:
                    upsert_kwargs: dict[str, object] = {
                        "source": "imap_readonly",
                        "source_ref": message.identity.message_id,
                    }
                    if existing is not None:
                        upsert_kwargs["processing_status"] = (
                            RecruitmentMailProcessingStatus.PENDING
                        )
                    record = self.store.upsert(
                        message,
                        parsed=prepare_mail_for_model(message),
                        **upsert_kwargs,
                    )
                disposition = "inserted" if existing is None else "reused"
                self.store.record_sync_item(
                    operation_id=operation_id,
                    run_id=run_id,
                    mail_record_id=record.id,
                    disposition=disposition,
                )
                if existing is None:
                    inserted += 1
                else:
                    reused += 1

            # Advancing only after every upsert succeeds makes retries at-least-once and lossless.
            saved_cursor = self.store.save_cursor(account_key, batch.next_cursor)
            self.store.finish_sync_run(
                operation_id,
                status="succeeded",
                fetched=fetched,
                inserted=inserted,
                reused=reused,
                cursor=saved_cursor.token,
            )
        except Exception as exc:
            self.store.finish_sync_run(
                operation_id,
                status="failed",
                fetched=fetched,
                inserted=inserted,
                reused=reused,
                cursor=cursor.token,
                error=type(exc).__name__,
            )
            raise
        return RecruitmentMailSyncResult(
            run_id=run_id,
            operation_id=operation_id,
            account_key=account_key,
            mailbox=saved_cursor.mailbox,
            fetched=len(batch.messages),
            inserted=inserted,
            reused=reused,
            attempts=attempts,
            cursor=saved_cursor.token,
            next_cursor=saved_cursor.token,
        )
