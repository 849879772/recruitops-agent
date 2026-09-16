from __future__ import annotations

import pytest
from threading import Event, Thread
from time import monotonic, sleep
from types import SimpleNamespace

from packages.recruitment_mail import (
    ImapConnectionConfig,
    ImapReadOnlyConnector,
    MailConnectorError,
    RecruitmentMailSyncResult,
)
from packages.recruitment_mail import freshness


def _settings(**overrides):
    values = {
        "mail_enabled": True,
        "mail_imap_host": "imap.example.com",
        "mail_imap_username": "candidate@example.com",
        "mail_imap_mailbox": "INBOX",
        "mail_sync_ttl_seconds": 300,
        "mail_sync_timeout_seconds": 1.0,
        "mail_sync_lock_wait_seconds": 0.02,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _sync_result(run_id: str = "run-1") -> RecruitmentMailSyncResult:
    return RecruitmentMailSyncResult(
        run_id=run_id,
        operation_id=f"operation-{run_id}",
        account_key="account-1",
        mailbox="INBOX",
        fetched=0,
        inserted=0,
        reused=0,
        attempts=1,
    )


def test_failed_refresh_does_not_replace_a_successful_cache(monkeypatch) -> None:
    freshness.clear_freshness_cache()
    responses = [_sync_result(), RuntimeError("imap timeout")]
    calls = []

    def sync(*_args, **_kwargs):
        calls.append(1)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(freshness, "sync_configured_mail", sync)
    settings = _settings()
    store = object()

    first = freshness.ensure_mail_fresh(settings, store)
    failed = freshness.ensure_mail_fresh(settings, store, force=True)
    cached = freshness.ensure_mail_fresh(settings, store)

    assert first["status"] == "synced"
    assert failed["status"] == "failed"
    assert failed["error_type"] == "RuntimeError"
    assert cached["status"] == "cached"
    assert cached["synced_at"] == first["synced_at"]
    assert calls == [1, 1]


def test_competing_refresh_returns_bounded_explicit_failure(monkeypatch) -> None:
    freshness.clear_freshness_cache()
    started = Event()
    release = Event()
    calls = []

    def sync(*_args, **_kwargs):
        calls.append(1)
        started.set()
        assert release.wait(timeout=1)
        return _sync_result()

    monkeypatch.setattr(freshness, "sync_configured_mail", sync)
    settings = _settings(mail_sync_timeout_seconds=1.0, mail_sync_lock_wait_seconds=0.02)
    store = object()
    owner_result = []
    owner = Thread(
        target=lambda: owner_result.append(freshness.ensure_mail_fresh(settings, store)),
        daemon=True,
    )
    owner.start()
    assert started.wait(timeout=1)

    began = monotonic()
    competing = freshness.ensure_mail_fresh(settings, store)
    elapsed = monotonic() - began

    assert elapsed < 0.25
    assert competing == {
        "status": "failed",
        "sync": {},
        "synced_at": None,
        "error_type": "mail_sync_in_progress",
        "timed_out": True,
    }
    assert calls == [1]

    release.set()
    owner.join(timeout=1)
    assert not owner.is_alive()
    assert owner_result[0]["status"] == "synced"
    assert freshness._inflight == {}


def test_sync_timeout_is_forwarded_without_creating_background_workers(monkeypatch) -> None:
    freshness.clear_freshness_cache()
    seen = []

    def sync(*_args, **kwargs):
        seen.append(kwargs["timeout_seconds"])
        raise MailConnectorError("imap_sync_timeout")

    monkeypatch.setattr(freshness, "sync_configured_mail", sync)
    result = freshness.ensure_mail_fresh(
        _settings(mail_sync_timeout_seconds=0.05),
        object(),
    )

    assert result["status"] == "failed"
    assert result["error_type"] == "imap_sync_timeout"
    assert result["timed_out"] is True
    assert seen == [0.05]
    assert freshness._inflight == {}


class _FakeSocket:
    def __init__(self, events):
        self.events = events

    def settimeout(self, value):
        self.events.append(("settimeout", value))

    def shutdown(self, _how):
        self.events.append(("shutdown",))

    def close(self):
        self.events.append(("socket_close",))


class _FakeImap:
    def __init__(self, events, *, login_delay=0):
        self.events = events
        self.sock = _FakeSocket(events)
        self.login_delay = login_delay

    def login(self, _username, _password):
        self.events.append(("login",))
        if self.login_delay:
            sleep(self.login_delay)
        return "OK", []

    def select(self, _mailbox, readonly=False):
        self.events.append(("select", readonly))
        return "OK", []

    def uid(self, command, *_args):
        self.events.append(("uid", command))
        return "OK", [b""]

    def close(self):
        self.events.append(("close",))

    def logout(self):
        self.events.append(("logout",))


def _connector(fake, timeout_seconds=0.5):
    return ImapReadOnlyConnector(
        ImapConnectionConfig(
            host="imap.example.com",
            username="candidate@example.com",
            password="secret",
            timeout_seconds=timeout_seconds,
        ),
        client_factory=lambda *_args: fake,
    )


def test_connector_reapplies_remaining_deadline_before_normal_cleanup() -> None:
    events = []
    _connector(_FakeImap(events)).fetch_since(limit=1)

    assert [event[0] for event in events[-4:]] == [
        "settimeout",
        "close",
        "settimeout",
        "logout",
    ]
    assert all(event[1] > 0 for event in events if event[0] == "settimeout")


def test_connector_aborts_socket_after_deadline_without_imap_cleanup_commands() -> None:
    events = []
    connector = _connector(_FakeImap(events, login_delay=0.02), timeout_seconds=0.005)

    with pytest.raises(MailConnectorError, match="imap_sync_timeout"):
        connector.fetch_since(limit=1)

    kinds = [event[0] for event in events]
    assert "close" not in kinds
    assert "logout" not in kinds
    assert "shutdown" in kinds
    assert "socket_close" in kinds


def test_success_cache_isolated_by_store_engine(monkeypatch) -> None:
    freshness.clear_freshness_cache()
    calls = []

    def sync(settings, store, **_kwargs):
        calls.append(store)
        return _sync_result(run_id=f"run-{len(calls)}")

    monkeypatch.setattr(freshness, "sync_configured_mail", sync)
    settings = _settings()
    store_a = SimpleNamespace(storage=SimpleNamespace(engine=object()))
    store_b = SimpleNamespace(storage=SimpleNamespace(engine=object()))

    first_a = freshness.ensure_mail_fresh(settings, store_a)
    first_b = freshness.ensure_mail_fresh(settings, store_b)
    cached_a = freshness.ensure_mail_fresh(settings, store_a)

    assert first_a["status"] == "synced"
    assert first_b["status"] == "synced"
    assert cached_a["status"] == "cached"
    assert calls == [store_a, store_b]
