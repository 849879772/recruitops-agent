from types import SimpleNamespace

from packages.mcp import server
from packages.tools.recruitment_mail import (
    RecruitmentMailSearchInput, RecruitmentMailSearchResponse,
    RecruitmentMailSyncInput,
)
from packages.tools.typed import EvidenceSource, ToolStatus


def test_sync_success_is_valid_typed_output(monkeypatch):
    monkeypatch.setattr(server, "_sync_mail_before_read", lambda *a, **k: {
        "status": "synced", "sync": {"fetched": 0, "inserted": 0},
    })
    result = server._mail_sync_operation(RecruitmentMailSyncInput(), object())
    assert result.success
    assert result.data.sync["fetched"] == 0
    assert result.read_only is False


def test_successful_refresh_records_a_timestamp(monkeypatch):
    monkeypatch.setattr(server, "_sync_mail_before_read", lambda *a, **k: {
        "status": "synced", "sync": {"fetched": 0, "inserted": 0},
        "synced_at": "2026-09-07T08:00:00+00:00",
    })
    result = server._mail_sync_operation(RecruitmentMailSyncInput(), object())
    assert result.data.freshness["synced_at"] == "2026-09-07T08:00:00+00:00"


def test_read_exposes_sync_failure_without_claiming_freshness(monkeypatch):
    calls = []
    def refresh(*args, **kwargs):
        calls.append("sync")
        return {"status": "failed", "error_type": "TimeoutError"}
    def read(request, store):
        calls.append("read")
        return RecruitmentMailSearchResponse(
            tool_name="recruitment_mail_search", status=ToolStatus.SUCCESS,
            success=True, evidence=[EvidenceSource(source="agent_database")],
            timeout_ms=5000, elapsed_ms=0,
        )
    monkeypatch.setattr(server, "_sync_mail_before_read", refresh)
    result = server._mail_store_operation(read)(
        RecruitmentMailSearchInput(), SimpleNamespace(mail_store=object()),
    )
    assert calls == ["sync", "read"]
    assert result.freshness["status"] == "failed"


def test_sync_disabled_has_typed_failure(monkeypatch):
    monkeypatch.setattr(server, "_sync_mail_before_read", lambda *a, **k: {"status": "disabled"})
    result = server._mail_sync_operation(RecruitmentMailSyncInput(), object())
    assert not result.success
    assert result.data.freshness["status"] == "disabled"


def test_api_reads_expose_cache_failure_without_processing(monkeypatch):
    from apps.api import main as api
    from packages.recruitment_mail import freshness, RecruitmentMailStore
    from packages.storage import Storage

    store = RecruitmentMailStore(Storage.from_url("sqlite+pysqlite:///:memory:"))
    calls = []
    monkeypatch.setattr(freshness, "ensure_mail_fresh", lambda *a, **k: (
        calls.append("sync") or {"status": "failed", "error_type": "TimeoutError"}
    ))
    data = api.list_recruitment_mails(limit=50, offset=0, store=store)
    assert calls == ["sync"]
    assert data.total == 0
    assert data.freshness["status"] == "failed"


def test_startup_does_not_report_failed_sync_as_completed(monkeypatch):
    import asyncio
    from apps.api import main as api

    monkeypatch.setattr(api, "_run_recruitment_mail_sync", lambda: {"status": "failed"})
    state = SimpleNamespace()
    asyncio.run(api._run_startup_mail_sync(state))
    assert state.startup_mail_sync_status == "failed"


def test_freshness_uses_ttl_singleflight_cache(monkeypatch):
    from packages.recruitment_mail import freshness
    from packages.recruitment_mail.sync import RecruitmentMailSyncResult

    freshness.clear_freshness_cache()
    calls = []
    monkeypatch.setattr(freshness, "sync_configured_mail", lambda *a, **k: (
        calls.append("sync") or RecruitmentMailSyncResult(
            run_id="run-1", operation_id="op-1", account_key="account-1",
            mailbox="INBOX", fetched=0, inserted=0, reused=0, attempts=1,
        )
    ))
    settings = SimpleNamespace(
        mail_enabled=True, mail_imap_host="imap.example.com",
        mail_imap_username="user", mail_imap_mailbox="INBOX",
        mail_sync_ttl_seconds=300,
    )
    first = freshness.ensure_mail_fresh(settings, object())
    second = freshness.ensure_mail_fresh(settings, object())
    assert first["status"] == "synced"
    assert second["status"] == "cached"
    assert first["synced_at"]
    assert calls == ["sync"]


def test_failed_refresh_keeps_last_success_timestamp(monkeypatch):
    from packages.recruitment_mail import freshness
    from packages.recruitment_mail.sync import RecruitmentMailSyncResult

    freshness.clear_freshness_cache()
    responses = iter([
        RecruitmentMailSyncResult(
            run_id="run-1", operation_id="op-1", account_key="account-1",
            mailbox="INBOX", fetched=0, inserted=0, reused=0, attempts=1,
        ),
        RuntimeError("imap timeout"),
    ])
    def sync(*_args, **_kwargs):
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(freshness, "sync_configured_mail", sync)
    settings = SimpleNamespace(
        mail_enabled=True, mail_imap_host="imap.example.com",
        mail_imap_username="user", mail_imap_mailbox="INBOX",
        mail_sync_ttl_seconds=0,
    )
    first = freshness.ensure_mail_fresh(settings, object())
    failed = freshness.ensure_mail_fresh(settings, object())
    assert failed["status"] == "failed"
    assert failed["synced_at"] == first["synced_at"]
