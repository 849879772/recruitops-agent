from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api import main, local_ui
from packages.storage import Storage, ApplicationSnapshot, CompanySnapshot, JobSnapshot
from packages.storage.models import ScheduleEventSnapshot


@pytest.fixture
def case(monkeypatch, tmp_path):
    settings = SimpleNamespace(database_url=f"sqlite:///{tmp_path / 'test.db'}",
                               write_enabled=True, api_token="secret")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(local_ui, "get_settings", lambda: settings)
    monkeypatch.chdir(tmp_path)
    storage = Storage.from_url(settings.database_url, initialize=True)
    now = datetime.now(timezone.utc)
    with storage.write_transaction() as session:
        session.add(ApplicationSnapshot(id="fixture", company_name="Test", job_title="Engineer",
            stage="applied", stage_history=[], source="fixture", source_ref="fixture",
            idempotency_key="fixture", updated_at=now))
    client = TestClient(main.app, base_url="http://127.0.0.1:8012")
    headers = {"Origin": "http://127.0.0.1:8012", "X-RecruitOps-Local-UI": "1"}
    return client, headers, now.isoformat(), storage, settings


def test_local_edit_without_token_and_stale_conflict(case):
    client, headers, stamp, storage, _ = case
    body = dict(stage="written", result="通过", note="manual", expected_updated_at=stamp)
    response = client.patch("/api/local-ui/applications/fixture", headers=headers, json=body)
    assert response.status_code == 200, response.text
    with storage.session() as session:
        row = session.get(ApplicationSnapshot, "fixture")
        assert row.stage == "written"
        assert row.stage_history[-1]["source"] == "manual"
        assert row.note == "manual"
    assert client.patch("/api/local-ui/applications/fixture", headers=headers, json=body).status_code == 409


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://localhost:8012", "http://127.0.0.1:9999"])
def test_external_origin_is_blocked(case, origin):
    client, headers, stamp, *_ = case
    headers["Origin"] = origin
    assert client.patch("/api/local-ui/applications/fixture", headers=headers,
        json=dict(stage="written", expected_updated_at=stamp)).status_code == 403


def test_unmarked_request_and_readonly_blocked(case):
    client, headers, stamp, _, settings = case
    body = dict(stage="written", expected_updated_at=stamp)
    assert client.patch("/api/local-ui/applications/fixture", json=body).status_code == 403
    settings.write_enabled = False
    assert client.patch("/api/local-ui/applications/fixture", headers=headers, json=body).status_code == 403


def test_add_event_and_backed_up_delete(case, tmp_path):
    client, headers, stamp, storage, _ = case
    response = client.post("/api/local-ui/applications/fixture/events", headers=headers,
        json=dict(event_type="interview", event_date="2026-10-01", event_time="09:00"))
    assert response.status_code == 200, response.text
    with storage.session() as session:
        assert len(session.scalars(select(ScheduleEventSnapshot)).all()) == 1
    response = client.request("DELETE", "/api/local-ui/applications/fixture", headers=headers,
                              json=dict(expected_updated_at=stamp))
    assert response.status_code == 200, response.text
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, "fixture") is None
        assert not session.scalars(select(ScheduleEventSnapshot)).all()
    assert len(list((tmp_path / '.data/backups').glob('manual-application-delete-*.json'))) == 1


def test_token_bypass_is_scoped_to_local_ui():
    token = local_ui.local_ui_request.set(True)
    try:
        main._require_local_api_token(None, require_configured=True)
    finally:
        local_ui.local_ui_request.reset(token)


def test_ui_uses_inline_editor_not_assistant_prompt():
    from pathlib import Path
    js = (Path(__file__).parents[1] / 'apps/web/app.js').read_text(encoding='utf-8')
    assert 'menu.dataset.assistantPrompt' not in js
    assert 'requestSessionToken' not in js
    assert 'editor.hidden = !editor.hidden' in js


def seed_record_job(storage, title="New Engineer"):
    with storage.write_transaction() as session:
        session.add(CompanySnapshot(id="company", name="Test", integration_status="connected",
            source="fixture", source_ref="company"))
        session.add(JobSnapshot(id="job", company_id="company", title=title,
            detail_url="https://example.com/job", cohort_status="confirmed", batch="campus",
            source="fixture", source_ref="job"))


def test_record_job_directly_and_idempotently(case):
    client, headers, _, storage, _ = case
    seed_record_job(storage)
    response = client.post("/api/local-ui/applications", headers=headers, json={"job_id": "job"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["created"] is True
    assert result["stage"] == "applied"
    repeat = client.post("/api/local-ui/applications", headers=headers, json={"job_id": "job"})
    assert repeat.json() == {**result, "created": False}
    with storage.session() as session:
        rows = session.scalars(select(ApplicationSnapshot).where(ApplicationSnapshot.job_id == "job")).all()
        assert len(rows) == 1
        assert len(rows[0].stage_history) == 1
        assert rows[0].record_url is None


def test_record_job_preserves_existing_terminal_stage(case):
    client, headers, _, storage, _ = case
    seed_record_job(storage, "Engineer")
    with storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "fixture").stage = "rejected"
    response = client.post("/api/local-ui/applications", headers=headers, json={"job_id": "job"})
    assert response.json() == {"application_id": "fixture", "created": False, "stage": "rejected"}
    with storage.session() as session:
        row = session.get(ApplicationSnapshot, "fixture")
        assert row.job_id == "job"
        assert row.stage_history == []


def test_record_job_access_and_missing_job(case):
    client, headers, _, _, settings = case
    path = "/api/local-ui/applications"
    body = {"job_id": "missing"}
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, headers={**headers, "Origin": "https://evil.example"}, json=body).status_code == 403
    assert client.post(path, headers=headers, json=body).status_code == 404
    settings.write_enabled = False
    assert client.post(path, headers=headers, json=body).status_code == 403


def test_bind_progress_url_without_stage_change(case):
    client, headers, stamp, storage, _ = case
    path = "/api/local-ui/applications/fixture/record-url"
    body = {"record_url": "https://example.com/personal/applications", "expected_updated_at": stamp}
    assert client.patch(path, json=body).status_code == 403
    assert client.patch(path, headers=headers, json={**body, "record_url": "javascript:alert(1)"}).status_code == 422
    assert client.patch(path, headers=headers, json=body).status_code == 200
    with storage.session() as session:
        row = session.get(ApplicationSnapshot, "fixture")
        assert row.record_url == body["record_url"]
        assert row.stage == "applied" and row.stage_history == []
    assert client.patch(path, headers=headers, json=body).status_code == 409


def test_record_button_does_not_use_model():
    from pathlib import Path
    js = (Path(__file__).parents[1] / "apps/web/app.js").read_text(encoding="utf-8")
    handler = js.split('if (action === "record")', 1)[1].split('if (action ===', 1)[0]
    assert '/api/local-ui/applications' in handler
    assert 'submitAssistantQuestion' not in handler


@pytest.mark.parametrize('path', ['/', '/index.html', '/app.js', '/styles.css'])
def test_mutable_web_assets_always_revalidate(path):
    client = TestClient(main.app)
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-cache, must-revalidate'
    cached = client.get(path, headers={'If-None-Match': response.headers['etag']})
    assert cached.status_code in (200, 304)
    assert cached.headers['cache-control'] == 'no-cache, must-revalidate'
