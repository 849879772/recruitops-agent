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


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'desktop-filler.db'}"
    storage = Storage.from_url(url, initialize=True)
    settings = SimpleNamespace(database_url=url, api_token="fixture", write_enabled=True,
                               env="desktop-isolated")
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setenv("RECRUITOPS_DESKTOP_INSTANCE_ID", "a" * 32)
    monkeypatch.setenv("RECRUITOPS_DESKTOP_WRITE_OPTIN", "a" * 32)
    app = FastAPI()
    app.include_router(module.router)
    client = TestClient(app)
    headers = {"Authorization": "Bearer fixture", "X-RecruitOps-Instance-Id": "a" * 32}
    return client, storage, headers, settings


BASE = "/api/integrations/resume-filler"
REGISTRATION = {"company": "Synthetic Robotics", "title": "Platform Engineer",
                "record_url": "https://ats.example/applications"}


@pytest.mark.parametrize("url,confirmed,accepted", [
    ("https://ats.example/#/job/1", True, False),
    ("https://ats.example/jobs/1", True, False),
    ("https://ats.example/job.html?id=1", True, False),
    ("https://ats.example/notapplications", False, False),
    ("https://ats.example/position/1", True, False),
    ("https://ats.example/%6aob/1", True, False),
    ("https://u:p@ats.example/applications", True, False),
    ("https://ats.example:bad/applications", True, False),
    ("https://ats.example/unknown", False, False),
    ("https://ats.example/unknown", True, True),
    ("https://ats.example/#/myApplications", False, True),
    ("https://tenant.jobs.feishu.cn/704852/position/application", False, True),
    ("https://tenant.jobs.feishu.cn/704852/position/application/", True, True),
    ("https://ats.example/#/position/application", False, True),
    ("https://tenant.jobs.feishu.cn/704852/position/application/123", True, False),
    ("https://ats.example/jobs/123#/position/application", True, False),
    ("https://ats.example/position/applications-extra", True, False),
])
def test_progress_url_confirmation_cannot_override_detail(isolated, url, confirmed, accepted):
    client, _, headers, _ = isolated
    response = client.post(BASE + "/application", headers=headers, json={
        **REGISTRATION, "record_url": url, "progress_url_confirmed": confirmed,
    })
    assert response.status_code == (200 if accepted else 422)


def test_instance_and_write_optin_are_required(isolated, monkeypatch):
    client, _, headers, settings = isolated
    for value in (None, "b" * 32):
        wrong = {"Authorization": "Bearer fixture"}
        if value:
            wrong["X-RecruitOps-Instance-Id"] = value
        assert client.get(BASE + "/applications", headers=wrong).status_code == 409
        assert client.post(BASE + "/application", headers=wrong, json=REGISTRATION).status_code == 409
    monkeypatch.delenv("RECRUITOPS_DESKTOP_WRITE_OPTIN")
    assert client.post(BASE + "/application", headers=headers, json=REGISTRATION).status_code == 403
    assert client.get(BASE + "/applications", headers=headers).status_code == 200
    monkeypatch.setenv("RECRUITOPS_DESKTOP_WRITE_OPTIN", "a" * 32)
    settings.write_enabled = False
    assert client.post(BASE + "/application", headers=headers, json=REGISTRATION).status_code == 403


def test_replay_preserves_written_history_and_timestamp(isolated):
    client, storage, headers, _ = isolated
    first = client.post(BASE + "/application", headers=headers, json=REGISTRATION).json()
    with storage.write_transaction() as session:
        row = session.get(ApplicationSnapshot, first["application_id"])
        row.stage = "written"
        row.stage_history = [{"stage": "written", "source": "fixture"}]
    with storage.session() as session:
        timestamp = session.get(ApplicationSnapshot, first["application_id"]).updated_at
    for _ in range(2):
        result = client.post(BASE + "/application", headers=headers,
                             json={**REGISTRATION, "application_id": first["application_id"]}).json()
        assert result["current_stage"] == "written" and result["total"] == 1
        assert result["created"] is False
    with storage.session() as session:
        row = session.get(ApplicationSnapshot, first["application_id"])
        assert row.stage_history == [{"stage": "written", "source": "fixture"}]
        assert row.updated_at == timestamp


def test_concurrent_registration_is_one_record(isolated):
    from concurrent.futures import ThreadPoolExecutor
    client, _, headers, _ = isolated
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post(
            BASE + "/application", headers=headers, json=REGISTRATION), range(4)))
    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["application_id"] for response in responses}) == 1
    assert all(response.json()["total"] == 1 for response in responses)


def test_same_progress_url_allows_distinct_jobs_and_exact_replay_is_idempotent(isolated):
    client, storage, headers, _ = isolated
    first_body = {**REGISTRATION, "title": "Platform Engineer"}
    second_body = {**REGISTRATION, "title": "Data Engineer"}
    first = client.post(BASE + "/application", headers=headers, json=first_body).json()
    second = client.post(BASE + "/application", headers=headers, json=second_body).json()

    assert first["created"] is True and second["created"] is True
    assert first["application_id"] != second["application_id"]
    assert first["total"] == 1 and second["total"] == 2
    with storage.write_transaction() as session:
        row = session.get(ApplicationSnapshot, first["application_id"])
        row.stage = "written"
        row.stage_history = [{"stage": "written", "source": "fixture"}]
    with storage.session() as session:
        timestamp = session.get(ApplicationSnapshot, first["application_id"]).updated_at

    replay = client.post(BASE + "/application", headers=headers, json=first_body).json()
    assert replay["created"] is False
    assert replay["application_id"] == first["application_id"]
    assert replay["current_stage"] == "written" and replay["total"] == 2
    with storage.session() as session:
        row = session.get(ApplicationSnapshot, first["application_id"])
        assert row.stage_history == [{"stage": "written", "source": "fixture"}]
        assert row.updated_at == timestamp


def test_job_id_distinguishes_same_company_and_title_on_same_progress_url(isolated):
    client, storage, headers, _ = isolated
    first_body = {**REGISTRATION, "job_id": "job-1001"}
    second_body = {**REGISTRATION, "job_id": "job-1002"}
    first = client.post(BASE + "/application", headers=headers, json=first_body).json()
    second = client.post(BASE + "/application", headers=headers, json=second_body).json()

    assert first["created"] is True and second["created"] is True
    assert first["application_id"] != second["application_id"]
    assert first["total"] == 1 and second["total"] == 2
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, first["application_id"]).job_id == "job-1001"
        assert session.get(ApplicationSnapshot, second["application_id"]).job_id == "job-1002"
    listed = client.get(BASE + "/applications", headers=headers).json()["items"]
    assert {row["job_id"] for row in listed} == {"job-1001", "job-1002"}
    replay = client.post(BASE + "/application", headers=headers, json=first_body).json()
    assert replay["created"] is False
    assert replay["application_id"] == first["application_id"] and replay["total"] == 2


def test_duplicate_existing_identity_requires_explicit_binding(isolated):
    client, storage, headers, _ = isolated
    for identity in ("one", "two"):
        with storage.write_transaction() as session:
            session.add(ApplicationSnapshot(id=identity, company_name=REGISTRATION["company"],
                job_title=REGISTRATION["title"], stage="written", record_url=REGISTRATION["record_url"],
                idempotency_key=identity, source="fixture", source_ref=identity, stage_history=[]))
    assert client.post(BASE + "/application", headers=headers, json=REGISTRATION).status_code == 409
    result = client.post(BASE + "/application", headers=headers,
                         json={**REGISTRATION, "application_id": "two"})
    assert result.json()["application_id"] == "two"
    assert result.json()["current_stage"] == "written"


def observed(isolated, *, title="Platform Engineer", age=0, bound=True, label="written"):
    from datetime import datetime, timezone, timedelta
    from packages.browser_bridge import BrowserBridgeStore, OperationStatus
    from packages.tools.browser_bridge import ObserveApplicationStatusPageInput, observe_application_status_page
    client, storage, headers, _ = isolated
    application_id = client.post(BASE + "/application", headers=headers, json=REGISTRATION).json()["application_id"]
    store = BrowserBridgeStore(storage)
    created = observe_application_status_page(ObserveApplicationStatusPageInput(
        application_id=application_id if bound else "other", application_url=REGISTRATION["record_url"],
        device_id="fixture", idempotency_key="fixture-observation",
    ), store)
    operation_id = created.data.operation_id
    store.append_event(operation_id, "extract", OperationStatus.EXTRACTING)
    store.append_event(operation_id, "validate", OperationStatus.VALIDATING)
    quote = f"{title} {label}"
    store.terminal_result(operation_id, {
        "page_url": REGISTRATION["record_url"],
        "captured_at": (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat(),
        "application_records": [{"title": title, "status": label, "label": label,
                                 "evidence": quote, "context": quote, "confidence": 0.99}],
    }, status=OperationStatus.SUCCEEDED)
    return {"application_id": application_id, "observation_operation_id": operation_id,
            "page_url": REGISTRATION["record_url"]}


def test_sync_uses_persisted_evidence_updates_once_and_never_accepts_stage(isolated):
    client, storage, headers, _ = isolated
    body = observed(isolated)
    assert client.post(BASE + "/sync", headers=headers, json={**body, "stage": "offer"}).status_code == 422
    first = client.post(BASE + "/sync", headers=headers, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["success"] is True, first.text
    with storage.session() as session:
        row = session.get(ApplicationSnapshot, body["application_id"])
        assert row.stage == "written"
        history = row.stage_history
    again = client.post(BASE + "/sync", headers=headers, json=body)
    assert again.json()["success"] is True
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, body["application_id"]).stage_history == history


def test_desktop_local_observation_is_persisted_then_verified_without_stage_input(isolated):
    from datetime import datetime, timezone
    client, storage, headers, _ = isolated
    application_id = client.post(BASE + "/application", headers=headers, json=REGISTRATION).json()["application_id"]
    result = {
        "evidence_only": True, "database_updated": False,
        "application_id": application_id, "application_ids": [application_id],
        "page_url": REGISTRATION["record_url"], "captured_at": datetime.now(timezone.utc).isoformat(),
        "application_records": [{"title": REGISTRATION["title"], "status": "written", "label": "笔试中",
                                  "evidence": "Platform Engineer 笔试中", "context": "笔试中", "confidence": 0.99}],
    }
    body = {"application_id": application_id, "page_url": REGISTRATION["record_url"],
            "observation": {"protocol_version": 1, "type": "result", "operation_id": "desktop-local-one",
                            "event_id": "desktop-local-event-one", "status": "SUCCEEDED", "result": result}}
    assert client.post(BASE + "/sync-local-observation", headers=headers, json={**body, "stage": "offer"}).status_code == 422
    response = client.post(BASE + "/sync-local-observation", headers=headers, json=body)
    assert response.status_code == 200, response.text
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, application_id).stage == "written"


def test_desktop_batch_observation_syncs_distinct_confirmed_records_once(isolated):
    from datetime import datetime, timezone
    client, storage, headers, _ = isolated
    titles = ["Platform Engineer", "Data Engineer"]
    ids = [client.post(BASE + "/application", headers=headers,
                       json={**REGISTRATION, "title": title}).json()["application_id"] for title in titles]
    result = {
        "evidence_only": True, "database_updated": False,
        "application_ids": ids, "page_url": REGISTRATION["record_url"],
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "application_records": [
            {"title": title, "status": "written", "label": "笔试中",
             "evidence": f"{title} 笔试中", "context": f"{title} 笔试中", "confidence": 0.99}
            for title in titles],
    }
    body = {"application_ids": ids, "page_url": REGISTRATION["record_url"],
            "observation": {"protocol_version": 1, "type": "result", "operation_id": "desktop-local-batch",
                            "event_id": "desktop-local-batch-event", "status": "SUCCEEDED", "result": result}}
    assert client.post(BASE + "/sync-local-observations", headers=headers,
                       json={**body, "stage": "offer"}).status_code == 422
    response = client.post(BASE + "/sync-local-observations", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["results"] == [{"application_id": item, "success": True} for item in ids]
    with storage.session() as session:
        assert [session.get(ApplicationSnapshot, item).stage for item in ids] == ["written", "written"]
    repeated = client.post(BASE + "/sync-local-observations", headers=headers, json=body)
    assert repeated.status_code == 200
    with storage.session() as session:
        assert all(len(session.get(ApplicationSnapshot, item).stage_history) == 2 for item in ids)


def test_desktop_batch_observation_rejects_unbound_application_before_any_write(isolated):
    from datetime import datetime, timezone
    client, storage, headers, _ = isolated
    ids = [client.post(BASE + "/application", headers=headers,
                       json={**REGISTRATION, "title": title}).json()["application_id"]
           for title in ["Platform Engineer", "Data Engineer"]]
    body = {"application_ids": ids, "page_url": REGISTRATION["record_url"],
            "observation": {"protocol_version": 1, "type": "result", "operation_id": "desktop-batch-unbound",
                            "event_id": "desktop-batch-unbound-event", "status": "SUCCEEDED", "result": {
                                "evidence_only": True, "database_updated": False,
                                "application_ids": [ids[0]], "page_url": REGISTRATION["record_url"],
                                "captured_at": datetime.now(timezone.utc).isoformat(),
                                "application_records": []}}}
    assert client.post(BASE + "/sync-local-observations", headers=headers, json=body).status_code == 409
    with storage.session() as session:
        assert [session.get(ApplicationSnapshot, item).stage for item in ids] == ["applied", "applied"]


def test_desktop_local_observation_rejects_unbound_or_non_normalized_payload(isolated):
    from datetime import datetime, timezone
    client, storage, headers, _ = isolated
    application_id = client.post(BASE + "/application", headers=headers, json=REGISTRATION).json()["application_id"]
    base = {"protocol_version": 1, "type": "result", "operation_id": "desktop-local-reject",
            "event_id": "desktop-local-event-reject", "status": "SUCCEEDED", "result": {
                "evidence_only": True, "database_updated": False, "application_ids": ["other"],
                "page_url": REGISTRATION["record_url"], "captured_at": datetime.now(timezone.utc).isoformat(),
                "application_records": []}}
    response = client.post(BASE + "/sync-local-observation", headers=headers, json={
        "application_id": application_id, "page_url": REGISTRATION["record_url"], "observation": base})
    assert response.status_code == 409
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, application_id).stage == "applied"


@pytest.mark.parametrize("options", [
    {"title": "Other Engineer"}, {"age": 301}, {"age": -60},
    {"bound": False}, {"label": "login required"},
])
def test_sync_rejects_other_job_expiry_unbound_and_login(isolated, options):
    client, storage, headers, _ = isolated
    body = observed(isolated, **options)
    result = client.post(BASE + "/sync", headers=headers, json=body)
    assert result.status_code == 409 or result.json()["success"] is False
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, body["application_id"]).stage == "applied"


def test_sync_never_downgrades_written(isolated):
    client, storage, headers, _ = isolated
    body = observed(isolated, label="applied")
    with storage.write_transaction() as session:
        row = session.get(ApplicationSnapshot, body["application_id"])
        row.stage = "written"
        history = row.stage_history
    response = client.post(BASE + "/sync", headers=headers, json=body)
    assert response.status_code == 200
    with storage.session() as session:
        row = session.get(ApplicationSnapshot, body["application_id"])
        assert row.stage == "written" and row.stage_history == history


@pytest.mark.parametrize("mutation", ["page", "login", "captcha", "empty", "duplicate"])
def test_sync_wrong_page_auth_pause_and_ambiguous_cards_do_not_write(isolated, mutation):
    from packages.storage.models import BrowserOperation
    client, storage, headers, _ = isolated
    body = observed(isolated)
    if mutation == "page":
        body["page_url"] = "https://other.example/applications"
    else:
        with storage.write_transaction() as session:
            operation = session.get(BrowserOperation, body["observation_operation_id"])
            if mutation in {"login", "captcha"}:
                operation.status = "STATE_UNCLEAR"
                operation.error_code = mutation.upper() + "_REQUIRED"
            else:
                result = dict(operation.result)
                result["application_records"] = [] if mutation == "empty" else result["application_records"] * 2
                operation.result = result
    response = client.post(BASE + "/sync", headers=headers, json=body)
    assert response.status_code == 409
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, body["application_id"]).stage == "applied"
