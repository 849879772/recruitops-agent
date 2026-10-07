"""HTTP regressions for status updates on an existing application binding."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api import local_ui, main
from packages.storage import ApplicationSnapshot, ScheduleEventSnapshot, Storage


@pytest.fixture
def bound_schedule_client(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'status-identity.db'}",
        write_enabled=True,
    )
    monkeypatch.setattr(local_ui, "get_settings", lambda: settings)
    storage = Storage.from_url(settings.database_url, initialize=True)
    with storage.write_transaction() as session:
        session.add(ApplicationSnapshot(
            id="app-1", company_name="诺瓦星云", job_title="软件工程师(深圳)",
            job_id=None, record_url=None, stage="applied",
            idempotency_key="fixture:app-1", note=None, stage_history=[],
            source="fixture", source_ref="app-1",
        ))
    client = TestClient(main.app, base_url="http://127.0.0.1:8012")
    headers = {"Origin": "http://127.0.0.1:8012", "X-RecruitOps-Local-UI": "1"}
    created = client.post("/api/local-ui/events", headers=headers, json={
        "title": "Complete assessment", "event_type": "assessment",
        "company_name": "诺瓦星云", "job_title": "软件工程师(深圳)",
        "application_id": "app-1",
    })
    assert created.status_code == 201, created.text
    return client, headers, storage, created.json()["event"]


@pytest.mark.parametrize("status", ["completed", "ignored", "pending"])
@pytest.mark.parametrize("source", ["local_ui", "recruitment_mail_schedule"])
def test_status_patch_refreshes_renamed_application_labels(bound_schedule_client, status, source):
    client, headers, storage, event = bound_schedule_client
    with storage.write_transaction() as session:
        application = session.get(ApplicationSnapshot, "app-1")
        application.company_name = "诺瓦星云科技"
        application.job_title = "27校招-软件工程师（深圳）(J12262)"
        row = session.get(ScheduleEventSnapshot, event["id"])
        row.source = source
        row.source_ref = "mail-fixture-1" if source == "recruitment_mail_schedule" else event["source_ref"]
        expected_source_ref = row.source_ref
    with storage.session() as session:
        expected_updated_at = session.get(ScheduleEventSnapshot, event["id"]).updated_at.isoformat()

    updated = client.patch(f"/api/local-ui/events/{event['id']}", headers=headers, json={
        "status": status, "expected_updated_at": expected_updated_at,
    })
    assert updated.status_code == 200, updated.text
    result = updated.json()["event"]
    assert result["status"] == status
    assert result["application_id"] == "app-1"
    assert result["company_name"] == "诺瓦星云科技"
    assert result["job_title"] == "27校招-软件工程师（深圳）(J12262)"
    assert (result["source"], result["source_ref"]) == (source, expected_source_ref)
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, "app-1").stage == "applied"


def test_stale_version_still_conflicts_when_labels_need_refresh(bound_schedule_client):
    client, headers, storage, event = bound_schedule_client
    with storage.write_transaction() as session:
        application = session.get(ApplicationSnapshot, "app-1")
        application.job_title = "软件工程师（深圳）(J12262)"
    stale_version = datetime.fromisoformat(event["updated_at"].replace("Z", "+00:00")) - timedelta(seconds=1)
    rejected = client.patch(f"/api/local-ui/events/{event['id']}", headers=headers, json={
        "status": "completed", "expected_updated_at": stale_version.isoformat(),
    })
    assert rejected.status_code == 409, rejected.text
    with storage.session() as session:
        row = session.get(ScheduleEventSnapshot, event["id"])
        assert row.status == "pending"
        assert row.job_title == event["job_title"]


def test_explicit_wrong_job_patch_still_rejected(bound_schedule_client):
    client, headers, storage, event = bound_schedule_client
    rejected = client.patch(f"/api/local-ui/events/{event['id']}", headers=headers, json={
        "status": "completed", "job_title": "软件工程师(西安)(J12263)",
        "expected_updated_at": event["updated_at"],
    })
    assert rejected.status_code == 422, rejected.text
    assert "job_title does not match" in rejected.json()["detail"]
    with storage.session() as session:
        row = session.get(ScheduleEventSnapshot, event["id"])
        assert row.status == "pending"
        assert row.job_title == event["job_title"]


def test_missing_bound_application_still_returns_not_found(bound_schedule_client):
    client, headers, storage, event = bound_schedule_client
    with storage.write_transaction() as session:
        session.delete(session.get(ApplicationSnapshot, "app-1"))
    rejected = client.patch(f"/api/local-ui/events/{event['id']}", headers=headers, json={
        "status": "completed", "expected_updated_at": event["updated_at"],
    })
    assert rejected.status_code == 404, rejected.text
    with storage.session() as session:
        row = session.get(ScheduleEventSnapshot, event["id"])
        assert row.status == "pending"
        assert row.application_id == "app-1"


def test_rebinding_with_old_labels_still_rejected(bound_schedule_client):
    client, headers, storage, event = bound_schedule_client
    with storage.write_transaction() as session:
        session.add(ApplicationSnapshot(
            id="app-2", company_name="Other company", job_title="Other role",
            job_id=None, record_url=None, stage="applied",
            idempotency_key="fixture:app-2", note=None, stage_history=[],
            source="fixture", source_ref="app-2",
        ))
    rejected = client.patch(f"/api/local-ui/events/{event['id']}", headers=headers, json={
        "application_id": "app-2", "status": "completed",
        "expected_updated_at": event["updated_at"],
    })
    assert rejected.status_code == 422, rejected.text
    with storage.session() as session:
        row = session.get(ScheduleEventSnapshot, event["id"])
        assert row.status == "pending"
        assert row.application_id == "app-1"
