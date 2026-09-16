from __future__ import annotations

from pathlib import Path

import pytest

from packages.recruitment_mail import (
    EmailMessage,
    MailCursor,
    MailFetchBatch,
    MailIdentity,
    RecruitmentMailStore,
    RecruitmentMailSyncService,
)
from packages.storage import Storage


class FakeConnector:
    def __init__(self, messages: list[EmailMessage], token: str = "12") -> None:
        self.messages = messages
        self.token = token
        self.seen_cursor: MailCursor | None = None

    def fetch_since(self, cursor: MailCursor | None = None, *, limit: int = 100) -> MailFetchBatch:
        self.seen_cursor = cursor
        return MailFetchBatch(
            messages=self.messages[:limit],
            next_cursor=MailCursor(mailbox="INBOX", token=self.token),
        )


def _message(message_id: str) -> EmailMessage:
    return EmailMessage(
        identity=MailIdentity(message_id=message_id, mailbox="INBOX"),
        sender="hr@example.com",
        subject="Interview invitation",
        body_text="Please attend the interview tomorrow at 10:00.",
    )


def _store(tmp_path: Path) -> RecruitmentMailStore:
    return RecruitmentMailStore(Storage.from_url(f"sqlite:///{tmp_path / 'agent.db'}"))


def test_sync_persists_messages_then_advances_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    connector = FakeConnector([_message("mail-1"), _message("mail-2")])

    result = RecruitmentMailSyncService(store).sync(
        account_key="account-hash",
        connector=connector,
    )

    assert connector.seen_cursor == MailCursor(mailbox="INBOX")
    assert (result.fetched, result.inserted, result.reused) == (2, 2, 0)
    assert result.run_id.startswith("mail-run-")
    assert result.operation_id.startswith("mail-sync-")
    assert result.attempts == 1
    assert result.cursor == result.next_cursor == "12"
    assert store.get_cursor("account-hash").token == "12"
    assert store.get_by_mailbox_message_id("INBOX", "mail-1") is not None
    receipt = store.get_sync_run(result.operation_id)
    assert receipt is not None
    assert (receipt.status, receipt.fetched, receipt.inserted, receipt.reused) == (
        "succeeded",
        2,
        2,
        0,
    )
    assert receipt.cursor_before is None and receipt.cursor == "12"
    assert {item.disposition for item in store.list_sync_items(result.operation_id)} == {
        "inserted"
    }


def test_sync_is_idempotent_and_resumes_from_saved_cursor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = RecruitmentMailSyncService(store)
    service.sync(account_key="account-hash", connector=FakeConnector([_message("mail-1")]))
    second = FakeConnector([_message("mail-1")], token="13")

    result = service.sync(account_key="account-hash", connector=second)

    assert second.seen_cursor is not None and second.seen_cursor.token == "12"
    assert (result.inserted, result.reused, result.next_cursor) == (0, 1, "13")
    assert len(store.list_sync_items(result.operation_id)) == 1
    assert store.list_sync_items(result.operation_id)[0].disposition == "reused"


def test_sync_does_not_advance_cursor_when_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    service = RecruitmentMailSyncService(store)
    connector = FakeConnector([_message("mail-1")])
    monkeypatch.setattr(store, "upsert", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        service.sync(
            account_key="account-hash",
            connector=connector,
            run_id="scheduler-run-1",
            operation_id="mail-operation-1",
            attempts=2,
        )

    assert store.get_cursor("account-hash").token is None
    receipt = store.get_sync_run("mail-operation-1")
    assert receipt is not None
    assert receipt.run_id == "scheduler-run-1"
    assert receipt.status == "failed"
    assert receipt.attempts == 2
    assert receipt.error == "RuntimeError"


def test_replaying_the_same_operation_does_not_duplicate_audit_items(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = RecruitmentMailSyncService(store)
    connector = FakeConnector([_message("mail-1")])

    first = service.sync(
        account_key="account-hash",
        connector=connector,
        run_id="run-1",
        operation_id="operation-1",
    )
    second = service.sync(
        account_key="account-hash",
        connector=connector,
        run_id="run-1",
        operation_id="operation-1",
        attempts=2,
    )

    assert first.operation_id == second.operation_id
    assert len(store.list_sync_items("operation-1")) == 1
    assert store.get_sync_run("operation-1").attempts == 2
