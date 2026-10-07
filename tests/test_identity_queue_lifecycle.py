"""Synthetic closed-application and reread failures; never use live accounts."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from packages.storage.models import ApplicationIdentityBinding, ApplicationSnapshot, BrowserOperation
from packages.tools import application_identity_binding as binding, browser_bridge
from test_application_identity_binding import case, confirm, propose
from test_application_identity_queue import checkpoint, mismatch


@pytest.mark.parametrize("stage", ["rejected", "withdrawn"])
@pytest.mark.parametrize("receipt", ["latest", "checkpoint"])
def test_closed_application_leaves_queue_without_deleting_history(case, stage, receipt):
    storage, _, _ = case
    checkpoint(storage, "historical-review", {"a": mismatch()})
    history = [{"stage": "applied"}, {"stage": stage, "source": "recruitment_mail"}]
    with storage.write_transaction() as session:
        app = session.get(ApplicationSnapshot, "a")
        app.stage, app.stage_history = stage, history
        if receipt == "latest":
            app.last_review = mismatch()
        session.get(BrowserOperation, "op").completed_at = datetime.now(timezone.utc) - timedelta(days=3)
    assert binding.application_identity_queue(storage)["items"] == []
    result = binding.identity_candidates(storage, "a")
    assert not result["requires_user_confirmation"] and result["candidates"] == []
    assert result["unavailable_reason"] == "application_closed"
    with storage.session() as session:
        app = session.get(ApplicationSnapshot, "a")
        assert app.stage == stage and app.stage_history == history
        assert session.get(BrowserOperation, "op") is not None
    # A deliberate reopening makes the existing unresolved receipt actionable again.
    with storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "a").stage = "applied"
    assert binding.application_identity_queue(storage)["total"] == 1


@pytest.mark.parametrize("stage", ["applied", "written", "interview1", "offer"])
def test_open_applications_still_need_identity_confirmation(case, stage):
    storage, _, _ = case
    with storage.write_transaction() as session:
        app = session.get(ApplicationSnapshot, "a")
        app.stage, app.last_review = stage, mismatch()
    assert binding.application_identity_queue(storage)["total"] == 1


@pytest.mark.parametrize("stage", ["rejected", "withdrawn"])
def test_closed_reread_never_calls_browser(case, monkeypatch, stage):
    storage, _, _ = case
    with storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "a").stage = stage

    async def forbidden(*args):
        pytest.fail("Closed application must not open a browser")

    monkeypatch.setattr(browser_bridge, "observe_application_status_page_workflow", forbidden)
    with pytest.raises(ValueError, match="^application_closed$"):
        asyncio.run(binding.refresh_identity_candidates(storage, None, None, "a", request_id="fixture"))


@pytest.mark.parametrize("after_approval", [False, True])
def test_closing_after_display_or_approval_blocks_new_binding(case, after_approval):
    storage, registry, executor = case
    item = binding.identity_candidates(storage, "a")
    request = binding.ApplicationIdentityProposeInput(
        **{key: item[key] for key in ("application_id", "identity_digest", "binding_revision", "operation_id")},
        candidate_id=item["candidates"][0]["candidate_id"])
    if after_approval:
        token = propose(case).data["approval_id"]
        registry.approve(token)
    with storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "a").stage = "rejected"
    if after_approval:
        with pytest.raises(RuntimeError) as failure:
            executor.execute(token, operator="local-ui-user")
        assert str(failure.value.__cause__) == "application_closed"
    else:
        with pytest.raises(ValueError, match="^application_closed$"):
            binding.application_identity_propose(request, storage, registry)
        assert registry.list() == []
    with storage.session() as session:
        assert session.get(ApplicationIdentityBinding, "a") is None
        assert session.get(ApplicationSnapshot, "a").stage == "rejected"


def test_closing_does_not_prevent_explicit_unbinding(case):
    storage, _, _ = case
    confirm(case)
    with storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "a").stage = "rejected"
    confirm(case, action="unbind")
    with storage.session() as session:
        assert session.get(ApplicationIdentityBinding, "a").state == "unbound"
        assert session.get(ApplicationSnapshot, "a").stage == "rejected"


def test_closed_proposal_api_returns_machine_readable_reason(case, monkeypatch):
    from apps.api import main as api
    storage, registry, _ = case
    item = binding.identity_candidates(storage, "a")
    payload = {key: item[key] for key in ("application_id", "identity_digest", "binding_revision", "operation_id")}
    payload["candidate_id"] = item["candidates"][0]["candidate_id"]
    with storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "a").stage = "rejected"
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(write_enabled=True, api_token="fixture"))
    monkeypatch.setattr(api, "approval_registry", registry)
    api.app.dependency_overrides[api.recruitment_mail_store] = lambda: SimpleNamespace(storage=storage)
    try:
        response = TestClient(api.app).post("/api/applications/a/identity-proposals", json=payload,
            headers={"Authorization": "Bearer fixture"})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "application_closed"
        assert registry.list() == []
    finally:
        api.app.dependency_overrides.pop(api.recruitment_mail_store, None)


@pytest.mark.parametrize("specific,outer,timed_out,expected", [
    ("LOGIN_REQUIRED", "operation_failed", False, "LOGIN_REQUIRED"),
    ("CAPTCHA_REQUIRED", "operation_failed", False, "CAPTCHA_REQUIRED"),
    ("READINESS_TIMEOUT", "operation_failed", False, "READINESS_TIMEOUT"),
    (None, "connection_status_unavailable", False, "connection_status_unavailable"),
    ("CANCELLED", "timeout", True, "timeout"),
])
def test_reread_preserves_actual_browser_error(case, monkeypatch, specific, outer, timed_out, expected):
    storage, _, _ = case
    calls = []

    async def observe(request, *_):
        calls.append(request)
        return SimpleNamespace(success=False, error_code=outer, timed_out=timed_out,
            data=SimpleNamespace(status="FAILED", error_code=specific) if specific else None)

    monkeypatch.setattr(browser_bridge, "observe_application_status_page_workflow", observe)
    with pytest.raises(ValueError, match=f"^{expected}$"):
        asyncio.run(binding.refresh_identity_candidates(storage, None, None, "a", request_id="fixture"))
    assert len(calls) == 1


@pytest.mark.parametrize("succeeded", [False, True])
def test_application_closed_during_reread_does_not_report_browser_failure(case, monkeypatch, succeeded):
    storage, _, _ = case

    async def observe(*_):
        with storage.write_transaction() as session:
            session.get(ApplicationSnapshot, "a").stage = "rejected"
        return SimpleNamespace(success=succeeded, error_code="invalid_input",
            data=SimpleNamespace(status="SUCCEEDED", operation_id="op") if succeeded else None)

    monkeypatch.setattr(browser_bridge, "observe_application_status_page_workflow", observe)
    with pytest.raises(ValueError, match="^application_closed$"):
        asyncio.run(binding.refresh_identity_candidates(storage, None, None, "a", request_id="fixture"))


@pytest.mark.parametrize("code,message", [
    ("application_closed", "已淘汰或已撤回"),
    ("mail_only_application", "仅通过邮件更新"),
    ("connection_status_unavailable", "浏览器连接"),
    ("bridge_dependency_missing", "连接服务不可用"),
    ("LOGIN_REQUIRED", "官网要求登录"),
    ("CAPTCHA_REQUIRED", "官网要求验证码"),
    ("timeout", "读取官网页面超时"),
    ("READINESS_TIMEOUT", "限定时间内准备完成"),
    ("NAVIGATION_RESTRICTED", "官网跳转受限"),
    ("UNPARSED_APPLICATION_PAGE", "未能解析出投递记录"),
    ("COMMAND_INVALID", "通信协议不匹配"),
    ("ACTION_NOT_ALLOWED", "不支持本次读取操作"),
    ("private exception with token=secret", "暂时无法确认具体原因"),
])
def test_reread_api_localizes_known_codes_without_leaking_unknown_errors(case, monkeypatch, code, message):
    from apps.api import main as api
    storage, _, _ = case

    async def fail(*_, **__):
        raise ValueError(code)

    monkeypatch.setattr(binding, "refresh_identity_candidates", fail)
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(write_enabled=True, api_token="fixture"))
    api.app.dependency_overrides[api.recruitment_mail_store] = lambda: SimpleNamespace(storage=storage)
    api.app.dependency_overrides[api.repository] = lambda: object()
    try:
        response = TestClient(api.app).post("/api/applications/a/identity-reread", json={"request_id": "fixture"},
            headers={"Authorization": "Bearer fixture"})
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert message in detail["message"] and "投递阶段未修改" in detail["message"]
        assert detail["code"] == ("identity_observation_failed" if "secret" in code else code.lower())
        assert "secret" not in response.text
    finally:
        api.app.dependency_overrides.pop(api.recruitment_mail_store, None)
        api.app.dependency_overrides.pop(api.repository, None)
