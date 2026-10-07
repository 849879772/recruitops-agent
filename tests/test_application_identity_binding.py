from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from packages.approval import ApprovalRegistry, SqlAlchemyApprovalPersistence
from packages.approval.adapters import AgentApplicationWriteAdapter
from packages.approval.executor import ApprovedWriteExecutor
from packages.domain.application_identity import matching_records
from packages.storage import Storage, ApplicationSnapshot, BrowserOperation
from packages.storage.models import ApplicationIdentityBinding
from packages.tools.application_identity_binding import (
    ApplicationIdentityProposeInput, identity_candidates, application_identity_propose,
    hydrate_verified_identity_bindings,
)

URL = "https://careers.example.test/my/applications"


@pytest.fixture
def case(tmp_path):
    storage = Storage.from_url(f"sqlite+pysqlite:///{tmp_path / 'bindings.db'}", initialize=True)
    now = datetime.now(timezone.utc)
    with storage.write_transaction() as session:
        session.add(ApplicationSnapshot(id="a", company_name="示例企业", job_title="AI应用开发工程师",
            record_url=URL, stage="applied", source="fixture", source_ref="a", idempotency_key="a"))
        session.add(BrowserOperation(operation_id="op", idempotency_key="op", device_id="desktop-fixture",
            operation="observe_application_status_page", status="SUCCEEDED", completed_at=now,
            command={"application_ids": ["a"], "page_url": URL}, result={"page_url": URL,
                "application_records": [{"title": "AI应用开发工程师（AI-Coding方向）", "job_id": "J100",
                    "context": "AI应用开发工程师（AI-Coding方向） 当前状态：初筛", "label": "初筛"}]}))
    registry = ApprovalRegistry(SqlAlchemyApprovalPersistence(storage))
    executor = ApprovedWriteExecutor(registry, AgentApplicationWriteAdapter(storage))
    yield storage, registry, executor
    storage.engine.dispose()


def propose(case, *, action="bind", index=0, retry_of=None):
    storage, registry, _ = case
    candidates = identity_candidates(storage, "a")
    request = ApplicationIdentityProposeInput(application_id="a", action=action,
        identity_digest=candidates["identity_digest"], binding_revision=candidates["binding_revision"],
        operation_id=candidates["operation_id"] if action == "bind" else None,
        candidate_id=candidates["candidates"][index]["candidate_id"] if action == "bind" else None, retry_of=retry_of)
    return application_identity_propose(request, storage, registry)


def confirm(case, **kwargs):
    _, registry, executor = case
    response = propose(case, **kwargs)
    assert response.success
    token = response.data["approval_id"]
    assert registry.approve(token).allowed
    return executor.execute(token, operator="local-ui-user")


def app(storage):
    with storage.session() as session:
        return session.get(ApplicationSnapshot, "a")


def test_proposal_never_self_approves_or_changes_stage(case):
    storage, _, executor = case
    response = propose(case)
    assert response.data["approval_status"] == "pending"
    with pytest.raises(PermissionError):
        executor.execute(response.data["approval_id"], operator="model")
    with storage.session() as session:
        assert session.get(ApplicationIdentityBinding, "a") is None
        assert session.get(ApplicationSnapshot, "a").stage == "applied"
    with pytest.raises(ValidationError):
        ApplicationIdentityProposeInput(application_id="a", action="unbind", identity_digest="a" * 64,
                                         binding_revision=0, confirmed=True)


@pytest.mark.parametrize("evidence_source,requires_confirmation", [
    ("dom", False), ("site_api", False), ("vision", True), ("page_text", True),
])
def test_unique_dom_match_not_forced_to_confirm_by_evidence_source(case, evidence_source, requires_confirmation):
    storage, _, _ = case
    with storage.write_transaction() as session:
        operation = session.get(BrowserOperation, "op")
        operation.result = {**operation.result, "application_records": [{
            "title": "AI应用开发工程师", "evidence_source": evidence_source,
        }]}
    candidates = identity_candidates(storage, "a")
    assert candidates["requires_user_confirmation"] is requires_confirmation


def test_unrelated_fallback_card_does_not_force_unique_dom_match_to_confirm(case):
    storage, _, _ = case
    with storage.write_transaction() as session:
        operation = session.get(BrowserOperation, "op")
        operation.result = {**operation.result, "application_records": [
            {"title": "AI应用开发工程师", "evidence_source": "dom"},
            {"title": "硬件工程师", "evidence_source": "vision"},
        ]}
    assert identity_candidates(storage, "a")["requires_user_confirmation"] is False


def test_confirmation_is_persistent_page_scoped_and_does_not_change_stage(case):
    storage, _, _ = case
    confirm(case)
    row = hydrate_verified_identity_bindings(storage, [app(storage)])[0]
    assert row.stage == "applied"
    assert row.verified_identity_bindings[0]["external_job_id"] == "J100"
    cards = [{"title": "AI应用开发工程师（AI-Coding方向）", "job_id": "J100"}]
    assert matching_records(row, cards) == cards
    assert matching_records(row, [{**cards[0], "job_id": "J101"}]) == []
    # A changed title or saved page requires reconfirmation, never reuse an alias.
    row.record_url = "https://careers.example.test/other"
    hydrate_verified_identity_bindings(storage, [row])
    assert row.verified_identity_bindings == []


def test_unbind_removes_alias_preserves_history_and_hydrator_rejects_forged_hints(case):
    storage, _, _ = case
    confirm(case)
    confirm(case, action="unbind")
    row = app(storage)
    row.verified_identity_bindings = [{"verified": True, "raw_title": "假的别名"}]
    hydrate_verified_identity_bindings(storage, [row])
    assert row.verified_identity_bindings == []
    with storage.session() as session:
        assert session.get(ApplicationIdentityBinding, "a").revision == 2
        assert session.get(ApplicationSnapshot, "a").stage == "applied"


@pytest.mark.parametrize("change", ["title", "url", "card", "scope"])
def test_approval_rechecks_application_and_observation(case, change):
    storage, registry, executor = case
    response = propose(case)
    token = response.data["approval_id"]
    registry.approve(token)
    with storage.write_transaction() as session:
        row = session.get(ApplicationSnapshot, "a")
        if change == "title":
            row.job_title = "测试开发"
        elif change == "url":
            row.record_url = URL + "/different"
        else:
            operation = session.get(BrowserOperation, "op")
            if change == "card":
                operation.result = {**operation.result, "application_records": []}
            else:
                operation.command = {**operation.command, "application_ids": ["other"]}
    with pytest.raises(RuntimeError) as failure:
        executor.execute(token, operator="local-ui-user")
    assert isinstance(failure.value.__cause__, ValueError)
    with storage.session() as session:
        assert session.get(ApplicationIdentityBinding, "a") is None


def test_revocation_or_correction_invalidates_other_pending_preview(case):
    storage, registry, executor = case
    response = propose(case)
    confirm(case, action="unbind")
    token = response.data["approval_id"]
    registry.approve(token)
    with pytest.raises(RuntimeError) as failure:
        executor.execute(token, operator="local-ui-user")
    assert "binding_changed" in str(failure.value.__cause__)


def test_expired_confirmation_can_be_proposed_again_without_new_observation(case):
    storage, registry, _ = case
    first = propose(case)
    token = first.data["approval_id"]
    decision = registry.approve(token, now=datetime.now(timezone.utc) + timedelta(minutes=16))
    assert not decision.allowed
    repeated = propose(case)
    assert not repeated.success and repeated.data["retry_of"] == token
    second = propose(case, retry_of=token)
    assert second.success and second.data["approval_status"] == "pending"
    assert second.data["approval_id"] != token
    assert identity_candidates(storage, "a")["binding_revision"] == 0


def test_sqlite_competing_confirmations_use_revision_cas(case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import packages.tools.application_identity_binding as binding
    storage, _, _ = case
    confirm(case)
    payloads = [propose(case).data["preview"]["payload"] for _ in range(2)]
    barrier = Barrier(2)
    original = binding._selected_card
    def read_together(*args):
        result = original(*args)
        barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(binding, "_selected_card", read_together)
    def apply(payload):
        try:
            binding.ApplicationIdentityBindingAdapter(storage).bind_application_identity(payload)
            return "success"
        except ValueError as exc:
            assert "binding_changed" in str(exc)
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(apply, payloads))
    assert sorted(outcomes) == ["conflict", "success"]
    assert identity_candidates(storage, "a")["binding_revision"] == 2


def test_no_cards_from_other_scope_failed_operation_or_old_evidence(case):
    storage, _, _ = case
    for update in ({"status": "STATE_UNCLEAR"}, {"command": {"page_url": URL, "application_ids": ["other"]}},
                   {"completed_at": datetime.now(timezone.utc) - timedelta(days=2)}):
        with storage.write_transaction() as session:
            operation = session.get(BrowserOperation, "op")
            operation.status = "SUCCEEDED"
            operation.command = {"page_url": URL, "application_ids": ["a"]}
            for key, value in update.items():
                setattr(operation, key, value)
        assert identity_candidates(storage, "a")["candidates"] == []


def test_duplicate_titles_require_distinct_site_ids_and_confirmation(case):
    storage, _, _ = case
    with storage.write_transaction() as session:
        operation = session.get(BrowserOperation, "op")
        card = operation.result["application_records"][0]
        operation.result = {**operation.result, "application_records": [card, {**card, "job_id": "J101"}]}
    assert all(c["selectable"] for c in identity_candidates(storage, "a")["candidates"])
    confirm(case, index=1)
    row = hydrate_verified_identity_bindings(storage, [app(storage)])[0]
    cards = [{"title": "AI应用开发工程师（AI-Coding方向）", "job_id": "J100"},
             {"title": "AI应用开发工程师（AI-Coding方向）", "job_id": "J101"}]
    assert matching_records(row, cards) == [cards[1]]


def test_equivalent_raw_titles_without_distinct_ids_cannot_be_confirmed(case):
    storage, _, _ = case
    with storage.write_transaction() as session:
        operation = session.get(BrowserOperation, "op")
        operation.result = {**operation.result, "application_records": [
            {"title": "AI开发（方向A）"}, {"title": "AI 开发(方向A)"}]}
    candidates = identity_candidates(storage, "a")
    assert candidates["captured_at"].endswith("+00:00")
    assert not any(card["selectable"] for card in candidates["candidates"])
    with pytest.raises(ValueError, match="not_unique"):
        propose(case)


def test_api_enforces_local_auth_write_optin_and_scope(case, monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api import main as api
    storage, registry, _ = case
    settings = SimpleNamespace(write_enabled=False, api_token="binding-test")
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "approval_registry", registry)
    api.app.dependency_overrides[api.recruitment_mail_store] = lambda: SimpleNamespace(storage=storage)
    try:
        client = TestClient(api.app)
        data = client.get("/api/applications/a/identity-candidates").json()
        payload = {"application_id": "a", "identity_digest": data["identity_digest"], "binding_revision": 0,
                   "operation_id": data["operation_id"], "candidate_id": data["candidates"][0]["candidate_id"]}
        url = "/api/applications/a/identity-proposals"
        assert client.post(url, json=payload).status_code in {401, 403}
        headers = {"Authorization": "Bearer binding-test"}
        assert client.post(url, json=payload, headers=headers).status_code == 503
        settings.write_enabled = True
        assert client.post(url, json={**payload, "application_id": "b"}, headers=headers).status_code == 422
        response = client.post(url, json=payload, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["data"]["approval_status"] == "pending"
        # Generic approval registration cannot display one identity while
        # executing another payload. Only the canonical proposal API may issue.
        forged = response.json()["data"]["preview"]
        forged["before"]["company_name"] = "误导显示"
        generic = client.post("/api/approvals", json=forged, headers=headers)
        assert generic.status_code == 422
    finally:
        api.app.dependency_overrides.pop(api.recruitment_mail_store, None)


def test_transient_retry_and_summary_do_not_miscount_record_presence():
    from time import perf_counter
    from packages.tools.application_review_run import _is_transient_failure, _response
    assert _is_transient_failure({"state": "failed", "reason": "connection reset"})
    assert not _is_transient_failure({"state": "unresolved", "reason": "unparsed_page"})
    assert not _is_transient_failure({"state": "blocked", "reason": "login_required"})
    response = _response("status-review-test", {"ids": ["a"], "database_total": 1, "excluded_terminal": 0,
        "pages_total": 1, "results": {"a": {"state": "unresolved", "reason": "record_present_status_unknown", "elapsed_ms": 1}}}, perf_counter())
    assert response.summary["verification_success_count"] == 0
    assert response.summary["record_present_status_unknown_count"] == 1
    assert response.summary["remaining_count"] == 0
