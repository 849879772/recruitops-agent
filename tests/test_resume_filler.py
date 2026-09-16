from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from packages.storage import Storage, ApplicationSnapshot
from apps.api import resume_filler as module


def test_registration_auth_dedup_and_existing_status(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'filler.db'}"
    storage = Storage.from_url(url, initialize=True)
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(database_url=url, api_token="test", write_enabled=True))
    app = FastAPI(); app.include_router(module.router)
    client = TestClient(app)
    endpoint = "/api/integrations/resume-filler/application"
    body = dict(company="测试公司", title="软件工程师", record_url="https://example.com/applications")
    assert client.post(endpoint, json=body).status_code == 401
    headers = {"Authorization": "Bearer test"}
    first = client.post(endpoint, json=body, headers=headers).json()
    assert first["created"] and first["total"] == 1
    with storage.write_transaction() as session:
        session.get(ApplicationSnapshot, first["application_id"]).stage = "rejected"
    second = client.post(endpoint, json={**body, "stage": "applied"}, headers=headers).json()
    assert not second["created"] and second["total"] == 1 and second["current_stage"] == "rejected"
    assert client.post(endpoint, json={**body, "record_url": "javascript:alert(1)"}, headers=headers).status_code == 422
    listing = endpoint + "s"
    assert client.get(listing).status_code == 401
    assert client.get(listing, headers=headers).json()["items"][0]["id"] == first["application_id"]
    with storage.write_transaction() as session:
        session.get(ApplicationSnapshot, first["application_id"]).record_url = None
    assert client.post(endpoint, json=body, headers=headers).status_code == 200
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, first["application_id"]).record_url == body["record_url"]
    new_url = "https://example.com/personal/applications"
    selected = {**body, "application_id": first["application_id"], "record_url": new_url}
    response = client.post(endpoint, json=selected, headers=headers)
    assert response.json()["current_stage"] == "rejected"
    assert response.json()["total"] == 1
    with storage.session() as session:
        row = session.get(ApplicationSnapshot, first["application_id"])
        assert row.record_url == new_url
        assert len(row.stage_history) == 1
    assert client.post(endpoint, json={**selected, "title": "wrong"}, headers=headers).status_code == 409
    assert client.post(endpoint, json={**selected, "application_id": "missing"}, headers=headers).status_code == 404
