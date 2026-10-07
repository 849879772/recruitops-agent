"""Central identity queue and proposal replay fixtures; no live business data."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from packages.approval import ApprovalRegistry, SqlAlchemyApprovalPersistence
from packages.storage.models import ApplicationSnapshot, BrowserOperation, TaskRun, ToolCall
from packages.tools.application_identity_binding import application_identity_queue
from test_application_identity_binding import case, confirm, propose


def checkpoint(storage, run_id, rows, *, offset=0):
    created = datetime.now(timezone.utc) + timedelta(seconds=offset)
    with storage.write_transaction() as session:
        session.add(TaskRun(id=run_id, task_type="application_review", user_request="synthetic review",
                            status="completed", source="fixture", created_at=created))
        session.flush()
        session.add(ToolCall(id=f"checkpoint-{run_id}", task_id=run_id,
                            tool_name="application_review_checkpoint", source="fixture",
                            arguments={"ids": list(rows), "results": rows}, created_at=created))


def mismatch():
    return {"state": "unresolved", "reason": "target_record_not_matched", "operation_id": "op"}


def test_queue_deduplicates_old_messages_waves_and_uses_latest_result(case):
    storage, _, _ = case
    checkpoint(storage, "old", {"a": mismatch()}, offset=-2)
    checkpoint(storage, "new", {"a": mismatch()}, offset=-1)
    first = application_identity_queue(storage)
    assert first["read_only"] and first["total"] == 1
    assert first["items"][0]["run_id"] == "new"
    assert first["items"][0]["job_title"] == "AI应用开发工程师"
    checkpoint(storage, "settled", {"a": {"state": "unchanged", "reason": "verified"}})
    assert application_identity_queue(storage)["total"] == 0


def test_valid_binding_resolves_all_old_history_without_changing_stage(case):
    storage, _, _ = case
    checkpoint(storage, "old", {"a": mismatch()})
    assert application_identity_queue(storage)["total"] == 1
    confirm(case)
    for _ in range(2):
        assert application_identity_queue(storage)["items"] == []
    with storage.write_transaction() as session:
        operation = session.get(BrowserOperation, "op")
        operation.completed_at = datetime.now(timezone.utc) - timedelta(days=2)
    # Evidence expiry does not undo an already confirmed identity.
    assert application_identity_queue(storage)["total"] == 0
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, "a").stage == "applied"
        assert len(list(session.query(ToolCall))) == 1


@pytest.mark.parametrize("change", ["ambiguous_card", "site_id", "local_title"])
def test_binding_is_only_resolved_when_current_identity_stays_unique(case, change):
    storage, _, _ = case
    checkpoint(storage, "review", {"a": mismatch()})
    confirm(case)
    with storage.write_transaction() as session:
        operation = session.get(BrowserOperation, "op")
        cards = operation.result["application_records"]
        if change == "ambiguous_card":
            operation.result = {**operation.result, "application_records": cards * 2}
        elif change == "site_id":
            operation.result = {**operation.result, "application_records": [{**cards[0], "job_id": "J200"}]}
        else:
            session.get(ApplicationSnapshot, "a").job_title = "另一类开发工程师"
    assert application_identity_queue(storage)["total"] == 1


def test_expired_evidence_is_a_single_nonselectable_queue_item(case):
    storage, _, _ = case
    checkpoint(storage, "review", {"a": mismatch()})
    with storage.write_transaction() as session:
        session.get(BrowserOperation, "op").completed_at = datetime.now(timezone.utc) - timedelta(days=2)
    queue = application_identity_queue(storage)
    assert queue["total"] == 1 and queue["items"][0]["candidates"] == []
    assert "expired" in queue["items"][0]["unavailable_reason"]


def test_queue_ignores_deleted_mail_only_and_nonidentity_results(case):
    storage, _, _ = case
    checkpoint(storage, "review", {"a": mismatch(), "deleted": mismatch()})
    with storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "a").record_url = ""
    assert application_identity_queue(storage)["total"] == 0


def test_repeated_proposal_posts_and_registry_restart_reuse_same_token(case):
    storage, registry, executor = case
    first = propose(case)
    assert propose(case).data == first.data
    other = ApprovalRegistry(SqlAlchemyApprovalPersistence(storage))
    assert propose((storage, other, executor)).data == first.data
    registry.approve(first.data["approval_id"])
    assert propose(case).data["approval_id"] == first.data["approval_id"]
    assert propose(case).data["approval_status"] == "approved"
    assert len(registry.list()) == 1


def test_rejected_proposal_requires_explicit_retry_and_retries_are_idempotent(case):
    _, registry, _ = case
    first = propose(case)
    token = first.data["approval_id"]
    registry.reject(token)
    denied = propose(case)
    assert not denied.success and denied.data["retry_of"] == token
    retried = propose(case, retry_of=token)
    assert retried.success and retried.data["approval_id"] != token
    assert propose(case, retry_of=token).data == retried.data
    assert propose(case).data == retried.data
    assert len(registry.list()) == 2


def test_retry_cannot_claim_an_unrelated_or_live_token(case):
    with pytest.raises(ValueError, match="retry_requires"):
        propose(case, retry_of="unrelated")


def test_queue_api_is_read_only_and_proposal_post_replays(case, monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api import main as api
    storage, registry, _ = case
    checkpoint(storage, "review", {"a": mismatch()})
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(write_enabled=True, api_token="fixture"))
    monkeypatch.setattr(api, "approval_registry", registry)
    api.app.dependency_overrides[api.recruitment_mail_store] = lambda: SimpleNamespace(storage=storage)
    try:
        client = TestClient(api.app)
        queue = client.get("/api/applications/identity-queue").json()
        assert queue["total"] == 1 and registry.list() == []
        item = queue["items"][0]
        payload = {key: item[key] for key in ("application_id", "identity_digest", "binding_revision", "operation_id")}
        payload["candidate_id"] = item["candidates"][0]["candidate_id"]
        first = client.post("/api/applications/a/identity-proposals", json=payload, headers={"Authorization": "Bearer fixture"})
        second = client.post("/api/applications/a/identity-proposals", json=payload, headers={"Authorization": "Bearer fixture"})
        assert first.status_code == second.status_code == 200
        assert first.json()["data"]["approval_id"] == second.json()["data"]["approval_id"]
        assert len(registry.list()) == 1
    finally:
        api.app.dependency_overrides.pop(api.recruitment_mail_store, None)
