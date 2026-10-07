"""Synthetic reproductions of missing identity choices; never use live accounts."""
from datetime import datetime, timezone

import pytest

from packages.storage.models import ApplicationSnapshot, BrowserOperation, BrowserOperationEvent
from packages.tools.application_identity_binding import application_identity_queue, identity_candidates
from packages.tools.application_page_evidence import validate_page_candidate
from test_application_identity_binding import case, confirm
from test_application_identity_queue import checkpoint


TITLE = "AI Agent 开发工程师"
TEXT = f"{TITLE} 第1志愿 官网投递 投递简历 2026-09-29"


def visual_case(storage, *, audited=True, duplicate=False):
    reading = {"reading_version": "literal-cards-v1", "text": TEXT, "confidence": .97,
               "cards": [{"title": TITLE, "text": TEXT, "current": True, "current_label": "第1志愿"}]}
    if duplicate:
        reading["cards"] *= 2
    with storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "a").job_title = TITLE + "第"
        operation = session.get(BrowserOperation, "op")
        operation.result = {**operation.result, "application_records": [], "vision": reading}
        if audited:
            session.add(BrowserOperationEvent(event_id="vision", operation_id="op", sequence=1,
                status="EXTRACTING", event_type="vision_analysis", payload=reading,
                occurred_at=datetime.now(timezone.utc)))
    checkpoint(storage, "review", {"a": {"state": "unresolved", "reason": "model_identity_mismatch"}})


def test_visual_name_mismatch_enters_queue_and_confirmed_alias_resolves_it(case):
    storage, _, _ = case
    visual_case(storage)
    queue = application_identity_queue(storage)
    assert queue["total"] == 1
    choice = queue["items"][0]["candidates"][0]
    assert choice["raw_title"] == TITLE and choice["selectable"]
    assert choice["evidence_source"] == "vision"
    confirm(case)
    assert application_identity_queue(storage)["total"] == 0
    with storage.session() as session:
        app = session.get(ApplicationSnapshot, "a")
        assert app.job_title == TITLE + "第" and app.stage == "applied"


def test_unaudited_visual_reading_is_not_a_binding_candidate(case):
    storage, _, _ = case
    visual_case(storage, audited=False)
    result = identity_candidates(storage, "a")
    assert result["candidates"] == []
    assert result["unavailable_reason"] == "application_records_missing"


def test_duplicate_visual_cards_cannot_be_collapsed_or_confirmed(case):
    storage, _, _ = case
    visual_case(storage, duplicate=True)
    result = identity_candidates(storage, "a")
    assert len(result["candidates"]) == 2
    assert all(not item["selectable"] for item in result["candidates"])
    with pytest.raises(ValueError, match="candidate_changed_or_not_unique|candidate_identity_not_unique"):
        confirm(case)


def test_confirm_rechecks_visual_source_after_preview(case):
    from test_application_identity_binding import propose
    storage, registry, executor = case
    visual_case(storage)
    proposal = propose(case)
    registry.approve(proposal.data["approval_id"])
    with storage.write_transaction() as session:
        event = session.get(BrowserOperationEvent, "vision")
        event.payload = {"error": "reading_replaced"}
    with pytest.raises(RuntimeError):
        executor.execute(proposal.data["approval_id"], operator="local-ui-user")


def submission(title="AI应用开发工程师"):
    return f"您已成功投递【{title}】岗位，您所选择的工作地点为【深圳】，简历评估中…"


def test_page_text_can_offer_a_named_submission_confirmation(case):
    storage, _, _ = case
    with storage.write_transaction() as session:
        operation = session.get(BrowserOperation, "op")
        operation.result = {**operation.result, "application_records": [],
                            "page": {"text": "AI应用开发工程师 投递 测评 面试 Offer 投递成功 " + submission()}}
    checkpoint(storage, "review", {"a": {"state": "unresolved", "reason": "target_record_ambiguous"}})
    queue = application_identity_queue(storage)
    assert queue["total"] == 1
    assert queue["items"][0]["candidates"][0]["evidence_source"] == "page_text"
    confirm(case)
    assert application_identity_queue(storage)["total"] == 0


@pytest.mark.parametrize("duplicate", [False, True])
def test_named_submission_is_not_ambiguous_just_because_heading_repeats(duplicate):
    title, quote = "AI应用开发工程师", submission()
    text = title + " 投递 测评 面试 Offer 投递成功 " + quote
    if duplicate:
        text += "\n" + quote
    app = {"id": "one", "job_title": title}
    card, reason = validate_page_candidate({"page": {"text": text}}, app, [app],
        card_title=title, source_ref="page:text", quotation=quote, label="简历评估中",
        status="applied", current=True)
    assert reason == ("target_record_ambiguous" if duplicate else None)
    assert bool(card) is not duplicate


def test_named_submission_never_lends_a_later_stage():
    title = "AI应用开发工程师"
    text = title + " " + submission() + "\n另一个岗位 当前状态：面试中"
    app = {"id": "one", "job_title": title}
    card, reason = validate_page_candidate({"page": {"text": text}}, app, [app],
        card_title=title, source_ref="page:text", quotation=text, label="面试中",
        status="interview", current=True)
    assert card is None and reason


def test_named_submission_does_not_merge_two_same_name_headings():
    title, quote = "AI应用开发工程师", submission()
    text = title + " 深圳\n" + title + " 北京\n" + quote
    app = {"id": "one", "job_title": title}
    card, reason = validate_page_candidate({"page": {"text": text}}, app, [app],
        card_title=title, source_ref="page:text", quotation=quote, label="简历评估中",
        status="applied", current=True)
    assert card is None and reason == "target_record_ambiguous"


def test_pending_visual_identity_retains_only_minimal_choices_after_cleanup(case):
    from datetime import timedelta
    from packages.browser_bridge.retention import compact_browser_diagnostics
    storage, _, _ = case
    visual_case(storage)
    with storage.write_transaction() as session:
        app = session.get(ApplicationSnapshot, "a")
        app.last_review = {"state": "unresolved", "reason": "model_identity_mismatch", "operation_id": "op"}
    result = compact_browser_diagnostics(storage, enabled=True, dry_run=False,
        now=datetime.now(timezone.utc) + timedelta(hours=13))
    assert result["compacted"] == 1
    candidates = identity_candidates(storage, "a")["candidates"]
    assert len(candidates) == 1 and candidates[0]["raw_title"] == TITLE
    assert candidates[0]["context"] == ""
    with storage.session() as session:
        data = session.get(BrowserOperation, "op").result
        assert data["diagnostics_compacted_v1"] and data["retention_level"] == "identity"
        assert "vision" not in data and "投递简历" not in str(data)


def test_confirmed_visual_alias_is_sent_to_model_and_reverified(tmp_path, monkeypatch):
    import json
    from packages.approval import ApprovalRegistry, SqlAlchemyApprovalPersistence
    from packages.approval.adapters import AgentApplicationWriteAdapter
    from packages.approval.executor import ApprovedWriteExecutor
    from tests.test_application_page_model_fallback import case as model_case, candidate, URL

    reading = {"text": TEXT, "confidence": .98, "reading_version": "literal-cards-v1",
               "cards": [{"title": TITLE, "text": TEXT, "current": True, "current_label": "第1志愿"}]}
    repo, store, operation, client, run = model_case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE + "第", "record_url": URL}],
        observation={"vision": reading},
        candidates=[candidate(TITLE, label="投递简历 2026-09-29", ref="vision:card:0", quote=TEXT, observed_status="applied")])
    from packages.tools.application_identity_binding import ApplicationIdentityProposeInput, application_identity_propose
    queue = identity_candidates(repo.storage, "24")
    registry = ApprovalRegistry(SqlAlchemyApprovalPersistence(repo.storage))
    preview = application_identity_propose(ApplicationIdentityProposeInput(
        **{k: queue[k] for k in ("application_id", "identity_digest", "binding_revision", "operation_id")},
        candidate_id=queue["candidates"][0]["candidate_id"]), repo.storage, registry)
    registry.approve(preview.data["approval_id"])
    ApprovedWriteExecutor(registry, AgentApplicationWriteAdapter(repo.storage)).execute(
        preview.data["approval_id"], operator="local-ui-user")
    result = run(visual=True)["24"]
    assert result["state"] == "unchanged" and not result.get("wrote"), result
    payload = json.loads(client.calls[0]["user_prompt"])
    assert payload["targets"][0]["card_title"] == TITLE


@pytest.mark.parametrize("vision_enabled", [False, True])
def test_reread_is_single_application_evidence_only_and_respects_vision_optin(case, monkeypatch, vision_enabled):
    import asyncio
    from types import SimpleNamespace
    from packages.tools import browser_bridge
    from packages.tools.application_identity_binding import refresh_identity_candidates
    storage, registry, _ = case
    calls = []
    async def observe(request, bridge, repository):
        calls.append(request)
        with storage.write_transaction() as session:
            operation = session.get(BrowserOperation, "op")
            operation.result = {**operation.result, "application_records": [], "page": {"text": "岗位页面"}}
        return SimpleNamespace(success=True, data=SimpleNamespace(status="SUCCEEDED", operation_id="op"))
    def get_operation(_):
        with storage.session() as session:
            return session.get(BrowserOperation, "op")
    monkeypatch.setattr(browser_bridge, "observe_application_status_page_workflow", observe)
    result = asyncio.run(refresh_identity_candidates(storage, SimpleNamespace(get_operation=get_operation),
        object(), "a", request_id="test", vision_enabled=vision_enabled))
    assert len(calls) == (2 if vision_enabled else 1)
    assert all(request.application_id == "a" and not request.application_ids and request.timeout_ms == 45000 for request in calls)
    assert calls[0].include_vision is False
    if vision_enabled:
        assert calls[1].include_vision is True
        assert calls[1].idempotency_key != calls[0].idempotency_key
    assert result["stage_unchanged"] and result["unavailable_reason"] == "application_records_missing"
    assert registry.list() == []
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, "a").stage == "applied"


def test_reread_mail_only_never_opens_a_browser(case, monkeypatch):
    import asyncio
    from packages.tools import browser_bridge
    from packages.tools.application_identity_binding import refresh_identity_candidates
    storage, _, _ = case
    with storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "a").record_url = ""
    async def forbidden(*_):
        raise AssertionError("No browser call for mail-only application")
    monkeypatch.setattr(browser_bridge, "observe_application_status_page_workflow", forbidden)
    with pytest.raises(ValueError, match="mail_only"):
        asyncio.run(refresh_identity_candidates(storage, None, None, "a", request_id="test"))


@pytest.mark.parametrize("auth,writes,expected", [(None, True, 401), ("Bearer wrong", True, 401), ("Bearer fixture", False, 503), ("Bearer fixture", True, 200)])
def test_reread_route_keeps_auth_and_write_optin(case, monkeypatch, auth, writes, expected):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from apps.api import main as api
    from packages.tools import application_identity_binding as binding
    storage, _, _ = case
    calls = []
    async def reread(*args, **kwargs):
        calls.append((args, kwargs))
        return {"stage_unchanged": True, "candidates": []}
    monkeypatch.setattr(binding, "refresh_identity_candidates", reread)
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(write_enabled=writes, api_token="fixture"))
    api.app.dependency_overrides[api.recruitment_mail_store] = lambda: SimpleNamespace(storage=storage)
    api.app.dependency_overrides[api.repository] = lambda: object()
    try:
        response = TestClient(api.app).post("/api/applications/a/identity-reread", json={"request_id": "test"},
            headers={"Authorization": auth} if auth else {})
        assert response.status_code == expected, response.text
        assert len(calls) == (1 if expected == 200 else 0)
    finally:
        api.app.dependency_overrides.pop(api.recruitment_mail_store, None)
        api.app.dependency_overrides.pop(api.repository, None)
