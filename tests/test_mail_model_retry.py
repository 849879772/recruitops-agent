"""Offline mail transport recovery: bounded attempts and honest per-run receipts."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from packages.matching.client import DeepSeekClientError
from packages.recruitment_mail import processing
from packages.recruitment_mail.run_service import MailProcessingRunService
from packages.recruitment_mail.storage import RecruitmentMailRecord
from tests.test_mail_model_processing import setup_case
from tests.test_mail_schedule_items import items


class ScriptClient:
    model = "offline-fixture"
    timeout = 25

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def complete(self, **_kwargs):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(content=json.dumps(response), model=self.model)


@pytest.fixture(autouse=True)
def no_real_backoff(monkeypatch):
    monkeypatch.setattr(processing, "sleep", lambda _: None)


def expire_cooldown(store, identifier):
    with store.storage.write_transaction() as session:
        record = session.get(RecruitmentMailRecord, identifier)
        metadata = dict(record.raw_metadata)
        attempt = dict(metadata["model_processing"])
        attempt["next_retry_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        metadata["model_processing"] = attempt
        record.raw_metadata = metadata


@pytest.mark.parametrize("code,kind,retryable", [
    ("transport_failed", "model_transport", True),
    ("http_408", "model_request", True),
    ("http_429", "model_rate_limit", True),
    ("http_500", "model_service", True),
    ("http_503", "model_service", True),
    ("http_401", "model_auth", False),
    ("http_403", "model_auth", False),
    ("http_400", "model_request", False),
    ("structured_response_invalid", "model_output", False),
    ("response_truncated", "model_output", False),
    ("response_empty", "model_output", False),
    ("provider_error", "model_client", False),
])
def test_safe_provider_code_distinguishes_transport_auth_output(code, kind, retryable):
    diagnostic = processing._failure_diagnostic(DeepSeekClientError(code))
    assert diagnostic["code"] == code
    assert diagnostic["kind"] == kind
    assert diagnostic["retryable"] is retryable


def test_arbitrary_provider_error_text_is_never_persisted():
    diagnostic = processing._failure_diagnostic(DeepSeekClientError("private mail body api_key=secret"))
    assert diagnostic == {"kind": "model_client", "code": "unknown_client_error", "retryable": False}


@pytest.mark.parametrize("code", ["transport_failed", "http_429", "http_503"])
def test_transient_call_retries_once_before_guarded_single_write(code, monkeypatch):
    store, repo, record, settings, triage, proposal = setup_case()
    backoffs = []
    monkeypatch.setattr(processing, "sleep", backoffs.append)
    client = ScriptClient([DeepSeekClientError(code), triage, proposal])
    result = processing.process_pending_mail(store, repo, settings, client=client)
    assert result["failed"] == 0 and result["unchanged"] == 1
    assert result["model_attempted_count"] == 1 and result["model_call_count"] == 3
    assert result["results"][0]["analysis_source"] == "current_run"
    assert backoffs == [processing.MODEL_RETRY_BACKOFF_SECONDS]
    assert result["schedule_items_created"] == 1 and len(items(store)) == 1
    replay = processing.process_pending_mail(store, repo, settings, client=client)
    assert replay["model_attempted_count"] == replay["model_call_count"] == 0
    assert client.calls == 3 and len(items(store)) == 1


def test_analysis_failure_cooldown_then_explicit_processing_recovers_without_duplicate_schedule():
    store, repo, record, settings, triage, proposal = setup_case()
    client = ScriptClient([triage, DeepSeekClientError("http_503"), DeepSeekClientError("http_503"), triage, proposal])
    first = processing.process_pending_mail(store, repo, settings, client=client)
    assert first["failed"] == 1 and first["retry_pending_count"] == 1
    assert first["results"][0]["reason"] == "analysis_http_503"
    assert first["results"][0]["retryable"] is True
    assert items(store) == []
    replay = processing.process_pending_mail(store, repo, settings, client=client)
    assert replay["model_attempted_count"] == replay["model_call_count"] == 0
    assert replay["historical_failure_count"] == 1
    assert replay["historical_failures"][0]["analysis_source"] == "history"
    assert replay["historical_failures"][0]["diagnostic"]["code"] == "http_503"
    assert not replay["has_more"] and not replay["scope_complete"] and client.calls == 3
    expire_cooldown(store, record.id)
    recovered = processing.process_pending_mail(store, repo, settings, client=client)
    assert recovered["scope_complete"] and recovered["schedule_items_created"] == 1
    assert recovered["retry_pending_count"] == 0 and recovered["historical_failure_count"] == 0
    assert len(items(store)) == 1
    assert processing.process_pending_mail(store, repo, settings, client=client)["processed"] == 0
    assert client.calls == 5


def test_transient_failure_has_finite_cross_run_budget_even_after_cooldown():
    store, repo, record, settings, _, _ = setup_case()
    client = ScriptClient([DeepSeekClientError("http_429") for _ in range(6)])
    for count in range(1, 4):
        result = processing.process_pending_mail(store, repo, settings, client=client)
        assert result["processed"] == result["failed"] == 1
        assert result["results"][0]["processing_attempt_count"] == count
        expire_cooldown(store, record.id)
    replay = processing.process_pending_mail(store, repo, settings, client=client)
    assert replay["processed"] == replay["retry_pending_count"] == 0
    assert replay["historical_failure_count"] == 1
    assert not processing.retry_status(store.get(record.id))["retryable"]
    assert client.calls == 6


@pytest.mark.parametrize("code", ["http_401", "http_400", "structured_response_invalid", "response_truncated"])
def test_non_transient_provider_failures_do_not_retry_same_evidence(code):
    store, repo, record, settings, _, _ = setup_case()
    client = ScriptClient([DeepSeekClientError(code)])
    result = processing.process_pending_mail(store, repo, settings, client=client)
    assert result["failed"] == 1 and result["retry_pending_count"] == 0
    assert result["results"][0]["diagnostic"]["code"] == code
    assert processing.process_pending_mail(store, repo, settings, client=client)["model_call_count"] == 0
    assert client.calls == 1


def test_legacy_generic_client_failure_gets_exactly_one_compatibility_round():
    store, repo, record, settings, _, _ = setup_case()
    with store.storage.write_transaction() as session:
        saved = session.get(RecruitmentMailRecord, record.id)
        saved.processing_status = "failed_terminal"
        saved.processing_error = "analysis_DeepSeekClientError"
        saved.raw_metadata = dict(saved.raw_metadata, model_processing={
            "digest": record.content_digest, "version": processing.MAIL_ANALYSIS_VERSION,
            "state": "failed_terminal", "reason": "analysis_DeepSeekClientError",
            "diagnostic": {"kind": "DeepSeekClientError"},
            "inputs": processing._input_digest(record, repo.list_applications()),
        })
    assert processing.retry_status(store.get(record.id))["legacy_retry"]
    client = ScriptClient([DeepSeekClientError("transport_failed"), DeepSeekClientError("transport_failed")])
    first = processing.process_pending_mail(store, repo, settings, client=client)
    assert first["results"][0]["processing_attempt_count"] == 2
    assert first["results"][0]["max_processing_attempts"] == 2
    assert first["retry_pending_count"] == 0
    expire_cooldown(store, record.id)
    assert processing.process_pending_mail(store, repo, settings, client=client)["processed"] == 0
    assert client.calls == 2


def test_status_never_spends_retry_or_resets_budget():
    store, repo, record, settings, _, _ = setup_case()
    client = ScriptClient([DeepSeekClientError("http_503"), DeepSeekClientError("http_503")])
    processing.process_pending_mail(store, repo, settings, client=client)
    before = store.get(record.id).raw_metadata
    for _ in range(3):
        status = processing.processing_status(store)
        assert status["items"][0]["retryable"] and not status["items"][0]["eligible"]
    assert store.get(record.id).raw_metadata == before and client.calls == 2


def test_durable_run_separates_new_sync_from_historical_failure_and_retry():
    store, repo, record, settings, triage, proposal = setup_case()
    client = ScriptClient([triage, DeepSeekClientError("http_503"), DeepSeekClientError("http_503"), triage, proposal])
    service = MailProcessingRunService(store, repo, settings, client=client,
                                      sync_mail=lambda: {"status": "synced"})
    first = service.run(service.start(background=False)["run_id"])
    assert first["status"] == "partial" and first["failed"] == 1
    assert first["model_attempted_count"] == 1 and first["model_call_count"] == 3
    assert first["historical_failure_count"] == 0 and first["retry_pending_count"] == 1
    assert first["failure_results"][0]["diagnostic"]["code"] == "http_503"
    assert first["failure_results"][0]["analysis_source"] == "current_run"
    replay = service.run(service.start(background=False)["run_id"])
    assert replay["freshness"]["status"] == "synced"
    assert replay["model_attempted_count"] == replay["model_call_count"] == 0
    assert replay["history_reused_count"] == replay["historical_failure_count"] == 1
    assert replay["failure_results"][0]["model_attempted"] is False
    assert replay["failure_results"][0]["analysis_source"] == "history"
    assert replay["remaining"] == 0 and replay["status"] == "partial"
    assert client.calls == 3
    expire_cooldown(store, record.id)
    recovered = service.run(service.start(background=False)["run_id"])
    assert recovered["status"] == "completed" and recovered["model_attempted_count"] == 1
    assert recovered["model_call_count"] == 2 and len(items(store)) == 1


def test_cancellation_during_backoff_does_not_call_model_again_or_write(monkeypatch):
    store, repo, record, settings, _, _ = setup_case()
    stopped = []
    monkeypatch.setattr(processing, "sleep", lambda _: stopped.append(True))
    client = ScriptClient([DeepSeekClientError("http_503")])
    result = processing.process_pending_mail(store, repo, settings, client=client,
                                            should_stop=lambda: bool(stopped))
    assert result["reason"] == "stop_requested" and result["processed"] == 0
    assert client.calls == 1 and items(store) == []
    assert store.get(record.id).processing_status == "pending"
    assert processing._eligible(store.get(record.id))


def test_retry_will_not_exceed_remaining_model_time_budget(monkeypatch):
    now = [0]
    monkeypatch.setattr(processing, "monotonic", lambda: now[0])
    class SlowClient:
        timeout = 25
        calls = 0
        def complete(self, **_kwargs):
            self.calls += 1
            now[0] += 25
            raise DeepSeekClientError("transport_failed")
    client = SlowClient()
    execution = {"record_ids": set(), "calls": 0, "progress": None, "should_stop": None, "deadline": 45}
    with pytest.raises(DeepSeekClientError, match="transport_failed"):
        processing._model_call(client, SimpleNamespace(system_prompt="fixture", user_prompt="fixture"), {},
                               execution=execution, record_ids=["fixture"], phase="analysis")
    assert client.calls == 1


def test_explicit_retry_recovers_config_failure_without_syncing_or_expanding_scope():
    store, repo, record, settings, triage, proposal = setup_case()
    client = ScriptClient([DeepSeekClientError("http_401"), triage, proposal])
    processing.process_pending_mail(store, repo, settings, client=client)
    service = MailProcessingRunService(store, repo, settings, client=client,
        sync_mail=lambda: pytest.fail("targeted retry must not refresh mailbox"))
    accepted = service.start(record_ids=[record.id], retry_failed=True, background=False)
    assert accepted["total"] == accepted["remaining"] == 1
    result = service.run(accepted["run_id"])
    assert result["status"] == "completed" and result["retry_failed"]
    assert result["freshness"]["status"] == "not_requested"
    assert result["model_call_count"] == 2 and len(items(store)) == 1
    with pytest.raises(ValueError, match="mail_retry_requires_failed_records"):
        service.start(record_ids=[record.id], retry_failed=True, background=False)
    assert client.calls == 3


def test_explicit_retry_is_single_grant_and_another_requires_new_user_action():
    store, repo, record, settings, triage, proposal = setup_case()
    client = ScriptClient([DeepSeekClientError("http_401"), DeepSeekClientError("http_503"),
                           DeepSeekClientError("http_503"), triage, proposal])
    processing.process_pending_mail(store, repo, settings, client=client)
    service = MailProcessingRunService(store, repo, settings, client=client)
    explicit = service.run(service.start(record_ids=[record.id], retry_failed=True, background=False)["run_id"])
    assert explicit["status"] == "partial" and explicit["retry_pending_count"] == 0
    assert explicit["model_call_count"] == 2
    expire_cooldown(store, record.id)
    replay = service.run(service.start(background=False)["run_id"])
    assert replay["model_call_count"] == 0 and replay["historical_failure_count"] == 1
    consumed = processing.process_pending_mail(store, repo, settings, client=client,
        record_ids=[record.id], retry_request_id=explicit["run_id"])
    assert consumed["model_call_count"] == 0 and client.calls == 3
    fresh_explicit = service.run(service.start(record_ids=[record.id], retry_failed=True, background=False)["run_id"])
    assert fresh_explicit["status"] == "completed" and client.calls == 5


@pytest.mark.parametrize("record_ids", [None, [], ["missing"] * 51])
def test_explicit_retry_requires_bounded_nonempty_selection(record_ids):
    store, repo, _, settings, _, _ = setup_case()
    service = MailProcessingRunService(store, repo, settings)
    with pytest.raises(ValueError, match="mail_retry_requires_explicit_records"):
        service.start(record_ids=record_ids, retry_failed=True, background=False)


def test_explicit_retry_rejects_pending_missing_and_readonly():
    store, repo, record, settings, _, _ = setup_case()
    service = MailProcessingRunService(store, repo, settings)
    with pytest.raises(ValueError, match="mail_retry_requires_failed_records"):
        service.start(record_ids=[record.id], retry_failed=True, background=False)
    with pytest.raises(KeyError, match="requested_mail_missing"):
        service.start(record_ids=["missing"], retry_failed=True, background=False)
    settings.write_enabled = False
    with pytest.raises(PermissionError, match="write_disabled"):
        service.start(record_ids=[record.id], retry_failed=True, background=False)


def test_manual_retry_after_schedule_created_never_duplicates_schedule(monkeypatch):
    from packages.tools import application_status_update as writer
    store, repo, record, settings, triage, proposal = setup_case()
    original = writer.update_application_status
    def interrupt_write(*_args, **_kwargs):
        raise RuntimeError("offline write interruption")
    monkeypatch.setattr(writer, "update_application_status", interrupt_write)
    client = ScriptClient([triage, proposal, triage, proposal])
    first = processing.process_pending_mail(store, repo, settings, client=client)
    assert first["failed"] == 1 and len(items(store)) == 1
    monkeypatch.setattr(writer, "update_application_status", original)
    service = MailProcessingRunService(store, repo, settings, client=client)
    result = service.run(service.start(record_ids=[record.id], retry_failed=True, background=False)["run_id"])
    assert result["status"] == "completed" and len(items(store)) == 1


def test_explicit_retry_preserves_frozen_digest_and_does_not_start_overlapping_run():
    store, repo, record, settings, _, _ = setup_case()
    client = ScriptClient([DeepSeekClientError("http_401")])
    processing.process_pending_mail(store, repo, settings, client=client)
    service = MailProcessingRunService(store, repo, settings, client=client)
    accepted = service.start(record_ids=[record.id], retry_failed=True, background=False)
    with pytest.raises(ValueError, match="another_mail_run_is_active"):
        service.start(record_ids=[record.id], retry_failed=True, background=False)
    with store.storage.write_transaction() as session:
        session.get(RecruitmentMailRecord, record.id).content_digest = "f" * 64
    result = service.run(accepted["run_id"])
    assert result["status"] == "partial" and result["model_call_count"] == 0
    assert client.calls == 1
