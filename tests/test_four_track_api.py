"""Synthetic local API contracts; no live mailbox, browser or business database."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api import main as api
from packages.approval import ApprovalRegistry, SqlAlchemyApprovalPersistence, ApprovedWriteExecutor, AgentApplicationWriteAdapter
from packages.domain.models import Application, ApplicationStage
from packages.recruitment_mail import EmailMessage, MailIdentity, RecruitmentMailStore
from packages.storage import Storage, ApplicationSnapshot


@pytest.fixture
def local_api(tmp_path, monkeypatch):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'four-tracks.db'}", initialize=True)
    store = RecruitmentMailStore(storage)
    settings = SimpleNamespace(write_enabled=True, api_token="fixture-token", mail_enabled=False)
    registry = ApprovalRegistry(SqlAlchemyApprovalPersistence(storage))
    executor = ApprovedWriteExecutor(registry, AgentApplicationWriteAdapter(storage))
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    monkeypatch.setattr(api, "get_storage_engine", lambda: storage.engine)
    monkeypatch.setattr(api, "approval_registry", registry)
    monkeypatch.setattr(api, "get_write_executor", lambda: executor)
    api.app.dependency_overrides[api.recruitment_mail_store] = lambda: store
    client = TestClient(api.app, base_url="http://localhost")
    yield client, store, settings
    api.app.dependency_overrides.clear()
    storage.engine.dispose()


def test_global_application_search_filters_before_paging_and_keeps_total(local_api):
    client, _, _ = local_api
    items = [Application(id=str(i), company_name="示例科技" if i >= 201 else "其他公司",
                         job_title="测试岗位", stage=ApplicationStage.APPLIED if i % 2 else ApplicationStage("interview1"),
                         source="fixture", source_ref=str(i), idempotency_key=str(i)) for i in range(205)]
    api.app.dependency_overrides[api.repository] = lambda: SimpleNamespace(list_applications=lambda: items)
    result = client.get("/api/applications/page", params={"query": "示例", "stages": "applied", "limit": 1, "offset": 1})
    assert result.status_code == 200
    page = result.json()
    assert page["total"] == 2 and page["unfiltered_total"] == 205
    assert page["stage_counts"] == {"applied": 2, "interview1": 2}
    assert page["items"][0]["id"] == "203"
    assert client.get("/api/applications/page?stage=applied&stages=offer").status_code == 422
    assert client.get("/api/applications/page?stage=not-a-stage").status_code == 422


def test_current_task_projection_is_guarded_read_only_and_empty_without_work(local_api):
    client, _, _ = local_api
    assert client.get("/api/local-ui/tasks/progress").status_code == 403
    response = client.get("/api/local-ui/tasks/progress", headers={"X-RecruitOps-Local-UI": "1", "Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 200
    assert response.json() == {"run": None, "runs": []}


def test_current_task_projection_can_read_one_terminal_run_without_history_fallback(local_api, monkeypatch):
    client, _, _ = local_api
    run_id = "a" * 32
    seen = []
    monkeypatch.setattr(api, "task_progress", lambda storage, run_id=None: seen.append(run_id) or {
        "runs": [{"run_id": run_id, "task_kind": "daily", "status": "failed"}],
        "run": {"run_id": run_id, "task_kind": "daily", "status": "failed"},
    })
    headers = {"X-RecruitOps-Local-UI": "1", "Sec-Fetch-Site": "same-origin"}
    response = client.get("/api/local-ui/tasks/progress", params={"run_id": run_id}, headers=headers)
    assert response.status_code == 200
    assert response.json()["run"]["status"] == "failed"
    assert seen == [run_id]
    assert client.get("/api/local-ui/tasks/progress", params={"run_id": "short"}, headers=headers).status_code == 422


def test_control_is_gated_and_persists_only_explicit_action(local_api, monkeypatch):
    client, store, settings = local_api
    calls = []
    monkeypatch.setattr("packages.tools.task_runtime_control.request_daily_control",
                        lambda storage, run_id, action: calls.append((run_id, action)) or {"success": True, "status": "pausing"})
    payload = {"task_kind": "daily", "action": "pause"}
    assert client.post("/api/local-ui/tasks/fixture/control", json=payload).status_code == 401
    settings.write_enabled = False
    headers = {"Authorization": "Bearer fixture-token"}
    assert client.post("/api/local-ui/tasks/fixture/control", json=payload, headers=headers).status_code == 503
    assert not calls
    settings.write_enabled = True
    response = client.post("/api/local-ui/tasks/fixture/control", json=payload, headers=headers)
    assert response.json()["status"] == "pausing" and calls == [("fixture", "pause")]


def test_mail_read_does_not_sync_and_binding_requires_real_approval(local_api, monkeypatch):
    client, store, _ = local_api
    monkeypatch.setattr("packages.recruitment_mail.freshness.ensure_mail_fresh",
                        lambda *_a, **_k: pytest.fail("read-only UI unexpectedly synchronized email"))
    record = store.upsert(EmailMessage(identity=MailIdentity(message_id="synthetic-four-track"),
                         sender="hr@example.test", subject="面试通知", body_text="请确认面试安排"), source="fixture")
    with store.storage.write_transaction() as session:
        session.add(ApplicationSnapshot(id="application-a", company_name="示例科技", job_title="软件测试工程师",
                    stage="applied", source="fixture", source_ref="a", idempotency_key="a"))
    assert client.get("/api/recruitment-mails?refresh=false").status_code == 200
    assert client.get(f"/api/recruitment-mails/{record.id}?refresh=false").status_code == 200
    candidates = client.get(f"/api/recruitment-mails/{record.id}/binding-candidates?query=示例").json()
    assert candidates["candidates"][0]["application_id"] == "application-a"
    body = {"record_id": record.id, "application_id": "application-a", "action": "bind",
            "content_digest": candidates["content_digest"], "binding_revision": 0}
    headers = {"Authorization": "Bearer fixture-token"}
    proposal = client.post(f"/api/recruitment-mails/{record.id}/binding-proposals", json=body, headers=headers)
    assert proposal.status_code == 200
    result = proposal.json()
    assert result["success"] and result["data"]["approval_status"] == "pending"
    assert store.get(record.id).application_id is None
    token = result["data"]["approval_id"]
    assert client.post(f"/api/approvals/{token}/execute", json={"operator": "synthetic-user"}, headers=headers).status_code == 409
    assert client.post(f"/api/approvals/{token}/approve", headers=headers).json()["allowed"]
    audit = client.post(f"/api/approvals/{token}/execute", json={"operator": "synthetic-user"}, headers=headers).json()
    assert audit["success"] and store.get(record.id).application_id == "application-a"
    with store.storage.session() as session:
        assert session.get(ApplicationSnapshot, "application-a").stage == "applied"
    # A stale or forged follow-up does not silently overwrite this binding.
    assert client.post(f"/api/recruitment-mails/{record.id}/binding-proposals", json=body, headers=headers).status_code == 409
    assert client.post(f"/api/recruitment-mails/{record.id}/binding-proposals", json={**body, "confirmed": True}, headers=headers).status_code == 422
