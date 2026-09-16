import json
from types import SimpleNamespace

import pytest

from packages.recruitment_mail import EmailMessage, MailIdentity, RecruitmentMailStore
from packages.recruitment_mail.processing import process_pending_mail
from packages.storage import Storage


@pytest.mark.parametrize("limit", [10, 20])
def test_fifteen_messages_require_two_batches_and_replay_skips(limit):
    store = RecruitmentMailStore(Storage.from_url("sqlite+pysqlite:///:memory:"))
    for i in range(15):
        store.upsert(EmailMessage(identity=MailIdentity(message_id=f"batch-{i}"),
                                  subject=f"Security notification {i}", body_text="New login"))
    repo = SimpleNamespace(list_applications=lambda: [])
    settings = SimpleNamespace(write_enabled=True)

    class Client:
        model = "fixture"
        calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            rows = [r for r in store.query(limit=200)
                    if (r.raw_metadata.get("model_processing") or {}).get("state") == "running"]
            return SimpleNamespace(content=json.dumps([
                dict(record_id=r.id, content_digest=r.content_digest,
                     relevance="irrelevant", reason="Security message") for r in rows
            ]))

    client = Client()
    first = process_pending_mail(store, repo, settings, limit=limit, client=client)
    assert first["processed"] == 10
    assert first["status"] == "partial"
    assert first["has_more"] and first["remaining_count"] == 5
    assert not first["scope_complete"]
    second = process_pending_mail(store, repo, settings, limit=limit, client=client)
    assert second["processed"] == 5
    assert second["remaining_count"] == 0 and not second["has_more"]
    assert second["scope_complete"]
    replay = process_pending_mail(store, repo, settings, client=client)
    assert replay["processed"] == 0 and client.calls == 2


def test_explicit_scope_does_not_include_other_pending_mail():
    store = RecruitmentMailStore(Storage.from_url("sqlite+pysqlite:///:memory:"))
    store.upsert(EmailMessage(identity=MailIdentity(message_id="outside"), body_text="Outside"))
    result = process_pending_mail(store, SimpleNamespace(list_applications=lambda: []),
                                  SimpleNamespace(write_enabled=True), record_ids=[], client=object())
    assert result["remaining_count"] == 0 and result["scope_complete"]


def test_terminal_failure_is_not_reported_as_all_complete_on_replay():
    store = RecruitmentMailStore(Storage.from_url("sqlite+pysqlite:///:memory:"))
    store.upsert(EmailMessage(identity=MailIdentity(message_id="bad"), body_text="Bad output"))
    repo = SimpleNamespace(list_applications=lambda: [])
    settings = SimpleNamespace(write_enabled=True)
    client = SimpleNamespace(complete=lambda **kwargs: SimpleNamespace(content="invalid json"))
    first = process_pending_mail(store, repo, settings, client=client)
    assert first["failed"] == 1 and not first["scope_complete"]
    replay = process_pending_mail(store, repo, settings, client=object())
    assert replay["processed"] == 0 and not replay["has_more"]
    assert replay["unfinished_count"] == 1
    assert not replay["scope_complete"] and replay["status"] == "partial"
