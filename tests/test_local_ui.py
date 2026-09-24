import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api import main, local_ui
from packages.storage import Storage, ApplicationSnapshot, CompanySnapshot, JobSnapshot
from packages.storage.models import ScheduleEventSnapshot
from packages.recruitment_mail.storage import RecruitmentMailRecord


@pytest.fixture
def case(monkeypatch, tmp_path):
    settings = SimpleNamespace(database_url=f"sqlite:///{tmp_path / 'test.db'}",
                               write_enabled=True, api_token="secret")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(local_ui, "get_settings", lambda: settings)
    monkeypatch.chdir(tmp_path)
    storage = Storage.from_url(settings.database_url, initialize=True)
    monkeypatch.setattr(main, "get_repository", lambda: main.PostgresRecruitmentRepository(storage))
    now = datetime.now(timezone.utc)
    with storage.write_transaction() as session:
        session.add(ApplicationSnapshot(id="fixture", company_name="Test", job_title="Engineer",
            stage="applied", stage_history=[], source="fixture", source_ref="fixture",
            idempotency_key="fixture", updated_at=now))
    client = TestClient(main.app, base_url="http://127.0.0.1:18010")
    headers = {"Origin": "http://127.0.0.1:18010", "X-RecruitOps-Local-UI": "1"}
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


def test_local_edit_corrects_application_identity_and_linked_schedule(case):
    client, headers, stamp, storage, _ = case
    created = client.post("/api/local-ui/applications/fixture/events", headers=headers,
                          json={"event_type": "面试", "event_date": "2026-10-01"})
    assert created.status_code == 200, created.text
    response = client.patch("/api/local-ui/applications/fixture", headers=headers, json={
        "stage": "applied", "company_name": " 新公司 ", "job_title": " 新岗位 ",
        "expected_updated_at": stamp,
    })
    assert response.status_code == 200, response.text
    with storage.session() as session:
        row = session.get(ApplicationSnapshot, "fixture")
        event = session.scalar(select(ScheduleEventSnapshot))
        assert (row.company_name, row.job_title, row.id) == ("新公司", "新岗位", "fixture")
        assert row.stage_history[-1]["previous_company_name"] == "Test"
        assert row.stage_history[-1]["previous_job_title"] == "Engineer"
        assert (event.company_name, event.job_title, event.title) == ("新公司", "新岗位", "新公司 · 面试")
    assert client.patch("/api/local-ui/applications/fixture", headers=headers, json={
        "stage": "applied", "company_name": "过期修改", "job_title": "岗位",
        "expected_updated_at": stamp,
    }).status_code == 409


def test_local_edit_rejects_empty_or_duplicate_identity(case):
    client, headers, stamp, storage, _ = case
    with storage.write_transaction() as session:
        session.add(ApplicationSnapshot(id="other", company_name="Other", job_title="Designer",
            stage="applied", stage_history=[], source="fixture", source_ref="other",
            idempotency_key="other"))
    base = {"stage": "applied", "expected_updated_at": stamp}
    path = "/api/local-ui/applications/fixture"
    assert client.patch(path, headers=headers, json={**base,
        "company_name": "  ", "job_title": "Developer"}).status_code == 422
    assert client.patch(path, headers=headers, json={**base,
        "company_name": "Other", "job_title": "Designer"}).status_code == 409
    with storage.session() as session:
        row = session.get(ApplicationSnapshot, "fixture")
        assert (row.company_name, row.job_title) == ("Test", "Engineer")


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://localhost:18010", "http://127.0.0.1:9999"])
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
    with storage.write_transaction() as session:
        session.add(RecruitmentMailRecord(id="linked-mail", dedupe_key="linked-mail",
            dedupe_kind="message_id", mailbox="INBOX", message_id="fixture@example.test",
            content_digest="a" * 64, category="interview", confidence=1,
            requires_confirmation=False, application_id="fixture", body_text="Fixture mail"))
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
        mail = session.get(RecruitmentMailRecord, "linked-mail")
        assert mail.application_id is None
        assert mail.body_text == "Fixture mail"
    backups = list((tmp_path / '.data/backups').glob('manual-application-delete-*.json'))
    assert len(backups) == 1
    backup = json.loads(backups[0].read_text(encoding="utf-8"))
    assert backup["application"]["id"] == "fixture"
    assert len(backup["events"]) == 1
    assert backup["mail_links"] == [{"id": "linked-mail", "application_id": "fixture"}]


def test_delete_rejects_unmarked_readonly_and_stale_requests(case, tmp_path):
    client, headers, stamp, storage, settings = case
    path = "/api/local-ui/applications/fixture"
    body = dict(expected_updated_at=stamp)
    assert client.request("DELETE", path, json=body).status_code == 403
    settings.write_enabled = False
    assert client.request("DELETE", path, headers=headers, json=body).status_code == 403
    settings.write_enabled = True
    assert client.request("DELETE", path, headers=headers,
        json=dict(expected_updated_at="2000-01-01T00:00:00Z")).status_code == 409
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, "fixture") is not None
    assert not list((tmp_path / '.data/backups').glob('manual-application-delete-*.json'))


@pytest.mark.parametrize("stored,expected", [
    ("2026-09-20T08:00:00.123456+08:00", "2026-09-20T08:00:00.123456+08:00"),
    ("2026-09-20T08:00:00.123456+08:00", "2026-09-20T00:00:00.123456+00:00"),
    ("2026-09-19T19:00:00.123456-05:00", "2026-09-20T00:00:00.123456+00:00"),
    ("2026-09-20T00:00:00.123456", "2026-09-20T00:00:00.123456"),
    ("2026-09-20T00:00:00.123456", "2026-09-20T08:00:00.123456+08:00"),
])
def test_application_version_compares_instants_not_timezone_labels(stored, expected):
    row = SimpleNamespace(updated_at=datetime.fromisoformat(stored))
    session = SimpleNamespace(scalar=lambda statement: row)
    assert local_ui._row(session, "fixture", datetime.fromisoformat(expected)) is row
    stale = datetime.fromisoformat(expected).replace(microsecond=123455)
    with pytest.raises(HTTPException) as error:
        local_ui._row(session, "fixture", stale)
    assert error.value.status_code == 409


@pytest.mark.parametrize("action", ["delete", "edit", "link"])
def test_application_list_version_round_trips_into_manual_write(case, action):
    client, headers, *_ = case
    response = client.get("/api/applications/page", headers={**headers, "Authorization": "Bearer secret"})
    assert response.status_code == 200, response.text
    body = {"expected_updated_at": response.json()["items"][0]["updated_at"]}
    path = "/api/local-ui/applications/fixture"
    if action == "delete":
        response = client.request("DELETE", path, headers=headers, json=body)
    elif action == "edit":
        response = client.patch(path, headers=headers, json={**body, "stage": "written"})
    else:
        response = client.patch(path + "/record-url", headers=headers,
            json={**body, "record_url": "https://example.test/personal/applications"})
    assert response.status_code == 200, response.text


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


def test_manual_application_is_idempotent_and_preserves_terminal_history(case):
    client, headers, _, storage, _ = case
    path = "/api/local-ui/applications/manual"
    body = dict(company_name=" Example ", job_title=" Engineer ", stage="rejected", note="Fixture")
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["created"] is True
    repeat = client.post(path, headers=headers, json={**body, "stage": "applied", "note": "Changed"})
    assert repeat.json() == {**result, "created": False}
    with storage.session() as session:
        row = session.get(ApplicationSnapshot, result["application_id"])
        assert row.company_name == "Example"
        assert row.job_title == "Engineer"
        assert row.stage == "rejected" and row.note == "Fixture"
        assert row.job_id is None and row.record_url is None
        assert len(row.stage_history) == 1
        assert row.stage_history[0]["result"] == "淘汰"


@pytest.mark.parametrize("invalid", [
    {"company_name": " "}, {"job_title": " "}, {"stage": "invented"},
    {"record_url": "javascript:alert(1)"}, {"record_url": "https://user:secret@example.test/"},
    {"record_url": "https://example.test/#/job/1"}, {"unexpected": True},
])
def test_manual_application_validation(case, invalid):
    client, headers, *_ = case
    response = client.post("/api/local-ui/applications/manual", headers=headers,
        json={"company_name": "Example", "job_title": "Engineer", **invalid})
    assert response.status_code == 422


def test_manual_application_requires_same_origin_and_write_opt_in(case):
    client, headers, _, _, settings = case
    path = "/api/local-ui/applications/manual"
    body = {"company_name": "Example", "job_title": "Engineer"}
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, headers={**headers, "Origin": "https://evil.example"}, json=body).status_code == 403
    settings.write_enabled = False
    assert client.post(path, headers=headers, json=body).status_code == 403


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
