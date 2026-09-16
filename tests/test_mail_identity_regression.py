from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from sqlalchemy import select

from packages.recruitment_mail import (
    EmailMessage,
    MailCursor,
    MailFetchBatch,
    MailIdentity,
    RecruitmentMailRecord,
    RecruitmentMailStore,
    RecruitmentMailSyncService,
)
from packages.storage import Storage


class _FakeConnector:
    def __init__(self, messages: list[EmailMessage], token: str) -> None:
        self.messages = messages
        self.token = token

    def fetch_since(self, cursor: MailCursor | None = None, *, limit: int = 100) -> MailFetchBatch:
        return MailFetchBatch(
            messages=self.messages[:limit],
            next_cursor=MailCursor(mailbox="INBOX", token=self.token),
        )


def _store(tmp_path: Path) -> RecruitmentMailStore:
    return RecruitmentMailStore(Storage.from_url(f"sqlite:///{tmp_path / 'mail.db'}"))


def _message(
    message_id: str,
    *,
    account_ref: str | None = "account-a",
    mailbox: str = "INBOX",
    source_metadata: dict[str, object] | None = None,
) -> EmailMessage:
    return EmailMessage(
        identity=MailIdentity(
            message_id=message_id,
            mailbox=mailbox,
            account_ref=account_ref,
        ),
        sender="hr@example.com",
        recipients=["candidate@example.com"],
        subject="Interview invitation",
        body_text="Please attend the interview tomorrow at 10:00.",
        source_metadata=source_metadata or {},
    )


def test_distinct_original_message_ids_do_not_collide_after_redaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_id = "<first-message@example.com>"
    second_id = "<second-message@example.com>"

    first = store.upsert(_message(first_id))
    second = store.upsert(_message(second_id))

    assert first.id != second.id
    assert first.message_id == second.message_id
    assert store.count() == 2
    persisted = json.dumps(
        [
            first.raw_metadata,
            first.parsed_result,
            second.raw_metadata,
            second.parsed_result,
            first.dedupe_key,
            second.dedupe_key,
        ],
        ensure_ascii=False,
    )
    assert first_id not in persisted
    assert second_id not in persisted


def test_message_identity_hash_isolated_by_account_and_mailbox(tmp_path: Path) -> None:
    store = _store(tmp_path)
    message_id = "<shared-message@example.com>"

    account_a_inbox = store.upsert(_message(message_id, account_ref="account-a"))
    account_b_inbox = store.upsert(_message(message_id, account_ref="account-b"))
    account_a_archive = store.upsert(
        _message(message_id, account_ref="account-a", mailbox="Archive")
    )

    assert len({account_a_inbox.id, account_b_inbox.id, account_a_archive.id}) == 3
    assert store.count() == 3
    assert store.get_by_mailbox_message_id(
        "INBOX", message_id, account_ref="account-a"
    ).id == account_a_inbox.id
    assert store.get_by_mailbox_message_id(
        "INBOX", message_id, account_ref="account-b"
    ).id == account_b_inbox.id


def test_known_legacy_identity_is_reused_without_rewriting_its_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    message_id = "<legacy-message@example.com>"
    message_hash = sha256(message_id.encode("utf-8")).hexdigest()
    legacy = store.upsert(
        _message(message_id, source_metadata={"message_id_hash": message_hash})
    )
    legacy_key = "message_id:" + sha256(
        f"INBOX\x1f{legacy.message_id}".encode("utf-8")
    ).hexdigest()

    with store.storage.write_transaction() as session:
        record = session.get(RecruitmentMailRecord, legacy.id)
        assert record is not None
        record.dedupe_key = legacy_key
        metadata = dict(record.raw_metadata)
        metadata.pop("message_id_hash")
        record.raw_metadata = metadata

    replay = store.upsert(
        _message(message_id, source_metadata={"message_id_hash": message_hash})
    )

    assert replay.id == legacy.id
    assert replay.dedupe_key == legacy_key
    assert store.count() == 1


def test_reupsert_preserves_omitted_transport_and_accepts_explicit_auth_failure(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    message_id = "<metadata-regression@example.com>"
    successful_transport = {
        "message_id": message_id,
        "message_id_hash": sha256(message_id.encode("utf-8")).hexdigest(),
        "imap_uid": "301",
        "uid_validity": "uid-validity-2",
        "authentication_results": [
            {"method": "spf", "result": "pass", "authserv_id": "mx.example"}
        ],
    }
    first = store.upsert(_message(message_id, source_metadata=successful_transport))

    without_transport = store.upsert(_message(message_id, source_metadata={}))

    assert without_transport.id == first.id
    assert without_transport.raw_metadata["transport"] == successful_transport | {
        "message_id": "<[REDACTED:email]>"
    }
    assert without_transport.imap_uid == "301"
    assert without_transport.uid_validity == "uid-validity-2"

    failed_transport = {
        "authentication_results": [
            {"method": "spf", "result": "fail", "authserv_id": "mx.example"}
        ]
    }
    failed = store.upsert(_message(message_id, source_metadata=failed_transport))

    assert failed.id == first.id
    assert failed.raw_metadata["transport"]["authentication_results"] == failed_transport[
        "authentication_results"
    ]
    assert failed.imap_uid == "301"
    assert failed.uid_validity == "uid-validity-2"


def test_sync_is_idempotent_and_preserves_source_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    messages = []
    for uid, message_id in (("101", "<sync-one@example.com>"), ("102", "<sync-two@example.com>")):
        messages.append(
            _message(
                message_id,
                source_metadata={
                    "message_id": message_id,
                    "message_id_hash": sha256(message_id.encode("utf-8")).hexdigest(),
                    "imap_uid": uid,
                    "uid_validity": "uid-validity-1",
                    "message_id_source": "header",
                    "provider_marker": "kept",
                },
            )
        )

    service = RecruitmentMailSyncService(store)
    first = service.sync(
        account_key="account-a",
        connector=_FakeConnector(messages, "102"),
        run_id="run-first",
        operation_id="operation-first",
    )
    second = service.sync(
        account_key="account-a",
        connector=_FakeConnector(messages, "102"),
        run_id="run-second",
        operation_id="operation-second",
    )

    assert (first.inserted, first.reused) == (2, 0)
    assert (second.inserted, second.reused) == (0, 2)
    assert store.count() == 2

    with store.storage.session() as session:
        records = list(session.scalars(select(RecruitmentMailRecord)))

    assert {record.raw_metadata["transport"]["imap_uid"] for record in records} == {
        "101",
        "102",
    }
    for record in records:
        transport = record.raw_metadata["transport"]
        assert transport["uid_validity"] == "uid-validity-1"
        assert transport["message_id_source"] == "header"
        assert transport["provider_marker"] == "kept"
        assert transport["message_id_hash"]
        assert record.raw_metadata["message_id_hash"] == transport["message_id_hash"]
        assert "@example.com>" not in json.dumps(record.raw_metadata, ensure_ascii=False)
