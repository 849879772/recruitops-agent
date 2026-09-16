from datetime import datetime, timezone
from types import SimpleNamespace

from packages.recruitment_mail import (
    EmailMessage,
    MailIdentity,
    RecruitmentMailStore,
)
from packages.recruitment_mail.backfill import backfill_mail_authentication
from packages.recruitment_mail.connectors import MailConnectorError
from packages.storage import Storage


def _record():
    store = RecruitmentMailStore(Storage.from_url("sqlite+pysqlite:///:memory:"))
    message = EmailMessage(
        identity=MailIdentity(message_id="mail-one"),
        subject="辞谢信",
        body_text="很遗憾，您没有通过岗位筛选。",
        received_at=datetime(2026, 9, 6, tzinfo=timezone.utc),
        source_metadata={"imap_uid": "9", "uid_validity": "1", "authentication_results": []},
    )
    return store, store.upsert(message, source="imap_readonly")


def _settings():
    return SimpleNamespace(
        mail_enabled=True,
        mail_imap_host="imap.163.com",
        mail_imap_port=993,
        mail_imap_username="candidate@163.com",
        mail_imap_password="secret",
    )


def test_backfill_persists_aligned_authentication_without_changing_application():
    store, record = _record()

    class Connector:
        def fetch_source_metadata(self, uid, *, expected_uid_validity=None):
            assert (uid, expected_uid_validity) == ("9", "1")
            return {
                "imap_uid": "9", "uid_validity": "1",
                "authentication_results": [{
                    "method": "dkim", "result": "pass", "authserv_id": "gzchengxin8",
                    "identity_domain": "shmail.ibeisen.com", "aligned": True,
                }],
            }

    result = backfill_mail_authentication(
        _settings(), store, record.id, connector_factory=lambda _config: Connector()
    )

    updated = store.get(record_id=record.id)
    assert result.authenticated and result.attempted
    assert updated.processing_status == "pending"
    assert updated.application_id is None
    assert updated.raw_metadata["authentication_backfill"]["outcome"] == "authenticated"


def test_failed_backfill_is_not_retried_until_persisted_evidence_changes():
    store, record = _record()
    calls = []

    class Connector:
        def fetch_source_metadata(self, *_args, **_kwargs):
            calls.append("fetch")
            raise MailConnectorError("imap_header_fetch_empty")

    first = backfill_mail_authentication(
        _settings(), store, record.id, connector_factory=lambda _config: Connector()
    )
    second = backfill_mail_authentication(
        _settings(), store, record.id, connector_factory=lambda _config: Connector()
    )

    assert first.attempted and not second.attempted
    assert calls == ["fetch"]
    updated = store.get(record_id=record.id)
    assert updated.processing_status == "needs_auth_metadata"
    assert updated.processing_error.endswith("retryable=false")
