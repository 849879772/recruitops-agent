from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api import company_sources
from apps.api.local_ui import is_local_ui, local_ui_request
from packages.discovery.company_registry import CompanySourceRegistry
from packages.storage import Storage


def _app(monkeypatch, tmp_path, *, write_enabled=True):
    settings = SimpleNamespace(
        database_url=f"sqlite:///{tmp_path / 'api.db'}", write_enabled=write_enabled
    )
    monkeypatch.setattr(company_sources, "get_settings", lambda: settings)
    storage = Storage.from_url(settings.database_url, initialize=True)
    app = FastAPI()
    app.include_router(company_sources.router)

    @app.middleware("http")
    async def local_ui_boundary(request, call_next):
        trusted = is_local_ui(request)
        token = local_ui_request.set(trusted)
        try:
            return await call_next(request)
        finally:
            local_ui_request.reset(token)

    monkeypatch.setattr(company_sources, "start_retry", None)
    return TestClient(app, base_url="http://127.0.0.1:8123"), CompanySourceRegistry(storage), settings


def _headers():
    return {"Origin": "http://127.0.0.1:8123", "X-RecruitOps-Local-UI": "1"}


def test_list_patch_and_retry_use_local_ui_boundary(monkeypatch, tmp_path):
    client, registry, _ = _app(monkeypatch, tmp_path)
    row = registry.upsert_source(
        source="feed", source_record_id="1", company_name="Alpha",
        source_url="https://feed.example/rows", entry_url="https://jobs.example/old",
    )

    response = client.get("/api/company-sources?page=1&page_size=30&q=Alpha&status=pending")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert "attempts" not in response.json()["items"][0]

    body = {"entry_url": "https://jobs.example/manual", "expected_updated_at": row["updated_at"]}
    assert client.patch(f"/api/company-sources/{row['id']}/entry", json=body).status_code == 403
    response = client.patch(f"/api/company-sources/{row['id']}/entry", headers=_headers(), json=body)
    assert response.status_code == 200
    assert response.json()["original_entry_url"] == "https://jobs.example/old"

    called = []
    def callback(record_id):
        called.append(record_id)
        registry.record_attempt(record_id, status="running")

    monkeypatch.setattr(company_sources, "start_retry", callback)
    response = client.post(f"/api/company-sources/{row['id']}/retry", headers=_headers())
    assert response.status_code == 202
    assert response.json() == {"id": row["id"], "status": "running"}
    assert called == [row["id"]]
    detail = client.get(f"/api/company-sources/{row['id']}").json()
    assert detail["attempts"][0]["status"] == "running"
    assert detail["attempts_total"] == 1


def test_running_source_rejects_entry_patch(monkeypatch, tmp_path):
    client, registry, _ = _app(monkeypatch, tmp_path)
    row = registry.upsert_source(
        source="feed", source_record_id="running", company_name="Running",
        source_url="https://feed.example/rows", entry_url="https://jobs.example/old",
    )
    registry.record_attempt(row["id"], status="running", attempted_url=row["entry_url"])
    current = registry.get(row["id"])
    response = client.patch(
        f"/api/company-sources/{row['id']}/entry", headers=_headers(),
        json={"entry_url": "https://jobs.example/manual", "expected_updated_at": current["updated_at"]},
    )
    assert response.status_code == 409
    assert registry.get(row["id"])["entry_url"] == "https://jobs.example/old"


def test_retry_without_callback_is_not_a_false_success(monkeypatch, tmp_path):
    client, registry, _ = _app(monkeypatch, tmp_path)
    row = registry.upsert_source(
        source="feed", source_record_id="1", company_name="Alpha",
        source_url="https://feed.example/rows", entry_url="https://jobs.example/old",
    )
    response = client.post(f"/api/company-sources/{row['id']}/retry", headers=_headers())
    assert response.status_code == 503
    assert registry.get(row["id"])["attempts"] == []


def test_unusable_sources_are_hidden_from_business_list(monkeypatch, tmp_path):
    client, registry, _ = _app(monkeypatch, tmp_path)
    row = registry.upsert_source(
        source="feed", source_record_id="unusable", company_name="Invalid Entry",
        source_url="https://feed.example/rows", entry_url="https://x.wjx.com/vm/form",
    )
    registry.record_attempt(row["id"], status="unusable", failure_stage="entry")
    row = registry.get(row["id"])
    assert row["status"] == "unusable"
    response = client.get("/api/company-sources?page=1&page_size=30")
    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert client.get("/api/company-sources?status=unusable").status_code == 422


def test_callback_failure_does_not_leave_a_running_attempt(monkeypatch, tmp_path):
    client, registry, _ = _app(monkeypatch, tmp_path)
    row = registry.upsert_source(
        source="feed", source_record_id="1", company_name="Alpha",
        source_url="https://feed.example/rows", entry_url="https://jobs.example/old",
    )

    def callback(_record_id):
        raise RuntimeError("thread dispatch failed")

    monkeypatch.setattr(company_sources, "start_retry", callback)
    response = client.post(f"/api/company-sources/{row['id']}/retry", headers=_headers())
    assert response.status_code == 503
    assert registry.get(row["id"])["attempts"] == []
