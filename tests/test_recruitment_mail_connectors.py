from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser

from packages.recruitment_mail import ImapConnectionConfig, ImapReadOnlyConnector, MailCursor
from packages.recruitment_mail.connectors import _source_metadata


RAW = b"""Message-ID: <mail-1@example.com>\r
Date: Thu, 20 Aug 2026 09:30:00 +0800\r
From: hr@example.com\r
To: candidate@example.com\r
Subject: Interview invitation\r
Content-Type: text/plain; charset=utf-8\r
\r
Interview at 10:00 tomorrow.
"""


class FakeImap:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.logged_out = False

    def login(self, username, password):
        self.calls.append(("login", username, password))
        return "OK", []

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"2"]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "search":
            return "OK", [b"11 12"]
        return "OK", [(b"12 (BODY[] {10})", RAW)]

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True


class FakeNetEaseImap(FakeImap):
    def _simple_command(self, command, arguments):
        self.calls.append(("simple_command", command, arguments))
        return "OK", []


class FakeLegacyImap(FakeImap):
    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "search":
            return "OK", [b"12"]
        return "OK", [(b"12 (BODY[] {10})", RAW)]


def test_imap_connector_is_incremental_and_does_not_mark_mail_read() -> None:
    fake = FakeImap()
    connector = ImapReadOnlyConnector(
        ImapConnectionConfig(
            host="imap.example.com",
            username="candidate@example.com",
            password="secret-value",
        ),
        client_factory=lambda _host, _port, _context: fake,
    )

    batch = connector.fetch_since(MailCursor(mailbox="INBOX", token="10"), limit=2)

    assert len(batch.messages) == 2
    assert batch.messages[0].message_id == "<mail-1@example.com>"
    assert batch.next_cursor.token == "12"
    assert ("select", "INBOX", True) in fake.calls
    assert ("uid", "fetch", b"11", "(BODY.PEEK[])") in fake.calls
    assert not any("FLAGS" in str(call) or "STORE" in str(call) for call in fake.calls)
    assert fake.closed is True
    assert fake.logged_out is True


def test_imap_connector_rejects_non_numeric_cursor_before_network() -> None:
    created = []
    connector = ImapReadOnlyConnector(
        ImapConnectionConfig(host="imap.example.com", username="u", password="p"),
        client_factory=lambda *_args: created.append(True),
    )

    try:
        connector.fetch_since(MailCursor(token="not-a-uid"))
    except ValueError as exc:
        assert "numeric UID" in str(exc)
    else:
        raise AssertionError("invalid cursor was accepted")
    assert created == []


def test_netease_connector_identifies_client_before_selecting_mailbox() -> None:
    fake = FakeNetEaseImap()
    connector = ImapReadOnlyConnector(
        ImapConnectionConfig(
            host="imap.163.com",
            username="candidate@163.com",
            password="client-authorization-code",
        ),
        client_factory=lambda _host, _port, _context: fake,
    )

    connector.fetch_since(limit=1)

    identity_index = next(
        index
        for index, call in enumerate(fake.calls)
        if call[0] == "simple_command" and call[1] == "ID"
    )
    select_index = fake.calls.index(("select", "INBOX", True))
    assert identity_index < select_index


def test_imap_connector_ignores_server_echo_of_last_uid_when_no_mail_is_new() -> None:
    fake = FakeImap()
    connector = ImapReadOnlyConnector(
        ImapConnectionConfig(
            host="imap.example.com",
            username="candidate@example.com",
            password="secret-value",
        ),
        client_factory=lambda _host, _port, _context: fake,
    )

    batch = connector.fetch_since(MailCursor(mailbox="INBOX", token="12"), limit=2)

    assert batch.messages == []
    assert batch.next_cursor.token == "12"
    assert not any(call[0] == "uid" and call[1] == "fetch" for call in fake.calls)


def _auth_metadata(
    authentication_results: str,
    *,
    sender: str = "jobs@shmail.ibeisen.com",
    trusted_authserv_ids: set[str] | None = None,
):
    raw = (
        f"From: Recruiter <{sender}>\r\n"
        f"Authentication-Results: {authentication_results}\r\n"
        "Message-ID: <one@shmail.ibeisen.com>\r\n\r\n"
    ).encode()
    message = BytesParser(policy=policy.default).parsebytes(raw)
    return _source_metadata(
        message,
        uid="1",
        uid_validity="1",
        trusted_authserv_ids=trusted_authserv_ids or {"163.com", "coremail", "coremail.net"},
    )


def test_gzmx_authserv_requires_netease_trust_and_aligned_dkim():
    from packages.recruitment_mail.authentication import has_aligned_authentication
    header = "gzmx14; spf=pass smtp.mail=jobs@shmail.ib\teisen.com; dkim=pass header.i=@shmail.ibeisen.com"
    assert has_aligned_authentication(_auth_metadata(header))
    assert _auth_metadata(header, trusted_authserv_ids={"gmail.com"})["authentication_results"] == []
    assert not has_aligned_authentication(_auth_metadata("gzmx14; dkim=pass header.i=@unrelated.example"))


def test_netease_internal_authserv_preserves_aligned_dkim_result() -> None:
    metadata = _auth_metadata(
        "gzchengxin8; spf=pass jobs@shmail.ib\teisen.com; "
        "dkim=pass jobs@shmail.ibeisen.com"
    )

    assert metadata["authentication_results"] == [
        {
            "method": "spf",
            "result": "pass",
            "authserv_id": "gzchengxin8",
            "aligned": True,
            "identity_domain": "shmail.ibeisen.com",
        },
        {
            "method": "dkim",
            "result": "pass",
            "authserv_id": "gzchengxin8",
            "aligned": True,
            "identity_domain": "shmail.ibeisen.com",
        },
    ]


def test_untrusted_authserv_and_misaligned_dkim_cannot_authenticate_sender() -> None:
    assert _auth_metadata(
        "attacker.example; dkim=pass jobs@shmail.ibeisen.com"
    )["authentication_results"] == []
    result = _auth_metadata(
        "gzchengxin8; dkim=pass jobs@unrelated.example"
    )["authentication_results"]
    assert result[0]["aligned"] is False


def test_netease_gzga_authserv_accepts_aligned_spf_from_smtp_mail() -> None:
    result = _auth_metadata(
        "gzga-mx-mtada-g4-5; spf=pass smtp.mail=dji.com; smtp.helo=untrusted.example",
        sender="jobs@dji.com",
    )["authentication_results"]

    assert result == [{
        "method": "spf",
        "result": "pass",
        "authserv_id": "gzga-mx-mtada-g4-5",
        "aligned": True,
        "identity_domain": "dji.com",
    }]
    helo_only = _auth_metadata(
        "gzga-mx-mtada-g4-5; spf=pass smtp.helo=untrusted.example",
        sender="jobs@dji.com",
    )["authentication_results"]
    assert helo_only == [{
        "method": "spf",
        "result": "pass",
        "authserv_id": "gzga-mx-mtada-g4-5",
        "aligned": False,
    }]


def test_netease_gzga_authserv_is_rejected_without_netease_trust() -> None:
    result = _auth_metadata(
        "gzga-mx-mtada-g4-5; spf=pass smtp.mail=dji.com",
        sender="jobs@dji.com",
        trusted_authserv_ids={"mail.example.net"},
    )["authentication_results"]

    assert result == []


def test_later_authentication_result_cannot_add_post_header_spoof() -> None:
    raw = (
        "From: Recruiter <jobs@dji.com>\r\n"
        "Authentication-Results: gzga-mx-mtada-g4-5; spf=pass smtp.mail=dji.com\r\n"
        "Authentication-Results: attacker.example; dkim=pass dji.com\r\n"
        "Message-ID: <one@dji.com>\r\n\r\n"
    ).encode()
    message = BytesParser(policy=policy.default).parsebytes(raw)
    metadata = _source_metadata(
        message,
        uid="1",
        uid_validity="1",
        trusted_authserv_ids={"163.com", "coremail", "coremail.net"},
    )

    assert metadata["authentication_results"] == [{
        "method": "spf",
        "result": "pass",
        "authserv_id": "gzga-mx-mtada-g4-5",
        "aligned": True,
        "identity_domain": "dji.com",
    }]


def test_only_receiving_servers_first_authentication_result_is_considered() -> None:
    raw = (
        "From: Recruiter <jobs@shmail.ibeisen.com>\r\n"
        "Authentication-Results: coremail; dkim=fail jobs@shmail.ibeisen.com\r\n"
        "Authentication-Results: gzchengxin8; dkim=pass jobs@shmail.ibeisen.com\r\n"
        "Message-ID: <one@shmail.ibeisen.com>\r\n\r\n"
    ).encode()
    message = BytesParser(policy=policy.default).parsebytes(raw)
    metadata = _source_metadata(
        message,
        uid="1",
        uid_validity="1",
        trusted_authserv_ids={"163.com", "coremail", "coremail.net"},
    )

    assert len(metadata["authentication_results"]) == 1
    assert metadata["authentication_results"][0]["result"] == "fail"


def test_legacy_exact_lookup_is_bounded_and_read_only() -> None:
    fake = FakeLegacyImap()
    connector = ImapReadOnlyConnector(
        ImapConnectionConfig(
            host="imap.example.com",
            username="candidate@example.com",
            password="secret-value",
        ),
        client_factory=lambda _host, _port, _context: fake,
    )

    metadata = connector.find_exact_source_metadata(
        subject="Interview invitation",
        received_at=datetime(
            2026, 8, 20, 9, 30,
            tzinfo=timezone(timedelta(hours=8)),
        ),
        body_text="Interview at 10:00 tomorrow.",
    )

    assert metadata["imap_uid"] == "12"
    assert not any("STORE" in str(call) for call in fake.calls)
