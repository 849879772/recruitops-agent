"""Offline user-clicked login rechecks stay scoped, fenced and replay-safe."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api import main
from packages.storage.models import ApplicationSnapshot, TaskRun, ToolCall
from packages.tools import application_review_run as review
from packages.tools.application_review_results import ApplicationReviewResultsInput, application_review_results
from packages.tools.batch_browser_operations import BatchObserveApplicationStatusInput as Input
from tests.test_application_review_run import _repository, _part

REQUEST = "10000000-0000-4000-8000-000000000001"


@pytest.fixture
def case(tmp_path, monkeypatch):
    repo = _repository(tmp_path, 15)
    with repo.storage.write_transaction() as db:
        for application in db.scalars(select(ApplicationSnapshot)):
            application.record_url = "https://careers.example.test/applications"
    visited = []

    async def observe(request, *_):
        visited.extend(request.application_ids)
        return _part(request, {item: ("unchanged", None) for item in request.application_ids})

    monkeypatch.setattr(review, "batch_observe_application_status", observe)
    monkeypatch.setattr(main, "browser_bridge_store", object())
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(api_token="fixture-only", write_enabled=True))
    main.app.dependency_overrides[main.repository] = lambda: repo
    yield repo, visited
    main.app.dependency_overrides.pop(main.repository, None)


def post(client, body, token="fixture-only"):
    return client.post("/api/applications/review-results/recheck", json=body,
                       headers={"Authorization": f"Bearer {token}"})


def test_click_rechecks_exact_subset_and_retry_reuses_completed_checkpoint(case):
    repo, visited = case
    client = TestClient(main.app)
    body = {"application_ids": ["0", "2"], "request_id": REQUEST}
    first = post(client, body)
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["scope_complete"] and not result["continuation_required"]
    assert result["summary"]["selection"] == "explicit_subset"
    assert result["summary"]["total"] == 2
    assert result["summary"]["excluded_terminal"] == 0
    assert set(visited) == {"0", "2"}
    retry = post(client, body)
    assert retry.status_code == 200 and retry.json()["run_id"] == result["run_id"]
    assert visited == ["0", "2"]
    continued = post(client, {"run_id": result["run_id"], "request_id": REQUEST})
    assert continued.status_code == 200 and continued.json()["run_id"] == result["run_id"]
    with repo.storage.session() as db:
        assert len(list(db.scalars(select(TaskRun)))) == 1
    assert application_review_results(ApplicationReviewResultsInput(run_id=result["run_id"], category="all"), repo).data["total"] == 2


def test_api_requires_authority_and_does_not_allow_scope_expansion(case, monkeypatch):
    repo, visited = case
    client = TestClient(main.app)
    body = {"application_ids": ["0"], "request_id": REQUEST}
    assert post(client, body, token="wrong").status_code == 401
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(api_token="fixture-only", write_enabled=False))
    assert post(client, body).status_code == 503
    assert visited == []
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(api_token="fixture-only", write_enabled=True))
    response = post(client, body).json()
    assert post(client, {"application_ids": ["0", "1"], "request_id": REQUEST}).status_code == 409
    assert post(client, {"run_id": response["run_id"], "request_id": "20000000-0000-4000-8000-000000000002"}).status_code == 409
    assert post(client, {**body, "run_id": response["run_id"]}).status_code == 422
    assert post(client, {"application_ids": ["0", "0"], "request_id": REQUEST}).status_code == 422
    assert post(client, {"application_ids": [], "request_id": REQUEST}).status_code == 422
    assert visited == ["0"]


def test_group_validation_rejects_unknown_mail_only_or_other_company_host(case):
    repo, visited = case
    client = TestClient(main.app)
    assert post(client, {"application_ids": ["unknown"], "request_id": REQUEST}).status_code == 404
    with repo.storage.write_transaction() as db:
        db.get(ApplicationSnapshot, "1").record_url = None
        db.get(ApplicationSnapshot, "2").company_name = "另一公司"
        db.get(ApplicationSnapshot, "3").record_url = "https://another.example.test/applications"
    for ids in (["1"], ["0", "2"], ["0", "3"]):
        assert post(client, {"application_ids": ids, "request_id": REQUEST}).status_code == 422
    assert visited == []


def test_recheck_freezes_subset_across_waves_and_excludes_unselected_records(case, monkeypatch):
    repo, visited = case
    monkeypatch.setattr(review, "_WAVE_PAGES", 1)
    with repo.storage.write_transaction() as db:
        db.get(ApplicationSnapshot, "1").record_url += "/different"
    first = asyncio.run(review.continue_application_review(
        Input(application_ids=["0", "1"], turn_id="login-recheck:"+REQUEST), object(), repo))
    assert first.summary["continuation_required"] and first.total == 2
    with repo.storage.write_transaction() as db:
        db.add(ApplicationSnapshot(id="new", company_name="新增", job_title="不应复核", stage="applied",
            record_url="https://careers.example.test/other", idempotency_key="new", source="fixture"))
    second = asyncio.run(review.continue_application_review(Input(run_id=first.summary["run_id"]), object(), repo))
    assert second.summary["scope_complete"] and second.total == 2
    assert set(visited) == {"0", "1"}


def test_ui_recheck_does_not_continue_another_active_review(case):
    repo, visited = case
    from time import time
    run = "status-review-"+"a"*32
    with repo.storage.write_transaction() as db:
        db.add(TaskRun(id=run, task_type="application_status_review", status="running", user_request="其他复核", source="fixture"))
        db.flush()
        db.add(ToolCall(id=run, task_id=run, tool_name="application_review_checkpoint", source="fixture",
            arguments={"ids":["0"], "results":{}, "attempts":{}, "database_total":15,
                       "excluded_terminal":0, "pages_total":0, "lease_until":time()+60}))
    client = TestClient(main.app)
    assert post(client, {"application_ids":["1"], "request_id":REQUEST}).status_code == 409
    assert visited == []


def test_safe_navigation_reason_never_exposes_diagnostic_payload(case):
    repo, _ = case
    from packages.tools.application_review_results import _receipt
    from packages.tools.batch_browser_operations import ApplicationStatusResult
    row = ApplicationStatusResult(application_id="0", state="unresolved", reason="unparsed_page", elapsed_ms=0,
        diagnostics={"navigation_diagnostics":{"reason":"returned_to_home_without_application_records", "token":"private"}})
    receipt = _receipt(row)
    assert receipt["navigation_reason"] == "returned_to_home_without_application_records"
    assert "private" not in str(receipt)
    row.diagnostics = {"navigation_diagnostics":{"reason":"private unknown text"}}
    assert _receipt(row)["navigation_reason"] is None


def test_ui_recheck_does_not_interrupt_full_review_waiting_for_continuation(case):
    repo, visited = case
    from time import time
    run = "status-review-"+"c"*32
    with repo.storage.write_transaction() as db:
        db.add(TaskRun(id=run, task_type="application_status_review", status="awaiting_continuation", user_request="全量复核", source="fixture"))
        db.flush()
        db.add(ToolCall(id=run, task_id=run, tool_name="application_review_checkpoint", source="fixture",
            arguments={"ids":["0"], "results":{}, "attempts":{}, "database_total":15,
                       "excluded_terminal":0, "pages_total":0, "lease_until":0,
                       "run_status":"awaiting_continuation", "continuation_until":time()+60}))
    assert post(TestClient(main.app), {"application_ids":["1"], "request_id":REQUEST}).status_code == 409
    assert visited == []


def test_closed_application_does_not_start_an_empty_successful_review(case):
    repo, visited = case
    with repo.storage.write_transaction() as db:
        db.get(ApplicationSnapshot,"0").stage = "rejected"
    assert post(TestClient(main.app), {"application_ids":["0"],"request_id":REQUEST}).status_code == 409
    assert visited == []


def test_lost_http_receipt_replay_still_reads_same_run_after_application_becomes_terminal(case):
    repo, visited = case
    client = TestClient(main.app)
    body = {"application_ids":["0"], "request_id":REQUEST}
    first = post(client,body).json()
    with repo.storage.write_transaction() as db:
        db.get(ApplicationSnapshot,"0").stage = "rejected"
    replay = post(client,body)
    assert replay.status_code == 200
    assert replay.json()["run_id"] == first["run_id"]
    assert visited == ["0"]


def test_sso_timeout_remains_a_failure_not_a_login_pause(tmp_path, monkeypatch):
    from packages.tools import batch_browser_operations as batch
    from packages.tools.batch_browser_operations import batch_observe_application_status
    from packages.browser_bridge.models import OperationStatus
    repo = _repository(tmp_path, 1)

    async def observe(*_):
        return SimpleNamespace(success=False, error_code=None, data=SimpleNamespace(
            operation_id="fixture-sso", error_code="AUTHENTICATION_RECOVERY_TIMEOUT", status=OperationStatus.FAILED,
            observation=None, result={"navigation_diagnostics":{"reason":"official_sso_return_pending"}}))

    monkeypatch.setattr(batch,"observe_application_status_page_workflow",observe)
    from packages import config
    monkeypatch.setattr(config,"get_settings",lambda:SimpleNamespace(write_enabled=False))
    result = asyncio.run(batch_observe_application_status(Input(application_ids=["0"]),object(),repo))
    assert not result.blocked
    assert result.failed[0].reason == "authentication_recovery_timeout"
    assert result.failed[0].wrote is False
