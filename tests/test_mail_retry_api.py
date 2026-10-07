"""User-click retry API contract; service/model/mailbox are mocked."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def test_retry_route_requires_local_auth_and_write_optin_and_binds_one_record(monkeypatch):
    from apps.api import main as api
    calls = []
    settings = SimpleNamespace(write_enabled=False, api_token="synthetic-token")
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    def start(**kwargs):
        calls.append(kwargs)
        return {"run_id": "synthetic-mail-run", "task_kind": "recruitment_mail", "status": "accepted"}
    monkeypatch.setattr(api, "mail_processing_run_service", lambda: SimpleNamespace(start=start))
    client = TestClient(api.app)
    endpoint = "/api/recruitment-mails/synthetic-failed/retry"
    assert client.post(endpoint).status_code in {401, 403}
    headers = {"Authorization": "Bearer synthetic-token"}
    assert client.post(endpoint, headers=headers).status_code == 503
    assert calls == []
    settings.write_enabled = True
    response = client.post(endpoint, headers=headers)
    assert response.status_code == 200 and response.json()["status"] == "accepted"
    assert calls == [{"record_ids": ["synthetic-failed"], "retry_failed": True, "refresh": False}]


@pytest.mark.parametrize("error,status", [(KeyError("missing"), 404), (ValueError("another_mail_run_is_active"), 409),
                                         (PermissionError("write_disabled"), 503)])
def test_retry_route_preserves_service_rejection_without_starting_other_work(monkeypatch, error, status):
    from apps.api import main as api
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(write_enabled=True, api_token="fixture"))
    def start(**kwargs):
        assert kwargs == {"record_ids": ["failed"], "retry_failed": True, "refresh": False}
        raise error
    monkeypatch.setattr(api, "mail_processing_run_service", lambda: SimpleNamespace(start=start))
    response = TestClient(api.app).post("/api/recruitment-mails/failed/retry", headers={"Authorization": "Bearer fixture"})
    assert response.status_code == status
