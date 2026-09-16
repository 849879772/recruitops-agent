from contextlib import contextmanager
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select

from apps.api import local_ui, main
from packages.storage import ApplicationSnapshot, ScheduleEventSnapshot, Storage
from packages.tools.schedule_manage import (
    ScheduleEventCreateFields,
    ScheduleManageInput,
    schedule_manage,
)


@pytest.fixture
def storage(tmp_path):
    return Storage.from_url(
        f"sqlite+pysqlite:///{tmp_path / 'schedule.db'}",
        initialize=True,
    )


def _seed_application(storage, *, application_id="app-1", company_name="Acme", job_title="Backend"):
    with storage.write_transaction() as session:
        session.add(
            ApplicationSnapshot(
                id=application_id,
                company_name=company_name,
                job_title=job_title,
                job_id=None,
                record_url=None,
                stage="applied",
                idempotency_key=f"fixture:{application_id}",
                note=None,
                stage_history=[],
                source="fixture",
                source_ref=application_id,
            )
        )


def _create_request(**overrides):
    values = {
        "action": "create",
        "request_key": "schedule-request-1",
        "title": "Complete assessment",
        "event_type": "assessment",
        "company_name": "Acme",
    }
    values.update(overrides)
    return ScheduleManageInput(**values)


def test_job_title_is_optional_or_empty_and_company_remains_required():
    created = ScheduleEventCreateFields(
        title="Company task",
        event_type="assessment",
        company_name="Acme",
    )
    assert created.job_title == ""
    assert ScheduleEventCreateFields(
        title="Company task",
        event_type="assessment",
        company_name="Acme",
        job_title="",
    ).job_title == ""
    assert ScheduleManageInput(
        action="update",
        event_id="event-1",
        job_title="",
    ).job_title == ""

    with pytest.raises(ValidationError):
        ScheduleEventCreateFields(title="Task", event_type="assessment")


def test_unbound_no_date_item_is_canonical_and_idempotency_conflicts(storage):
    request = _create_request(time_kind="deadline")
    first = schedule_manage(request, storage, write_enabled=True)

    assert first.success is True
    assert first.data is not None
    event = first.data.event
    assert first.data.created is True
    assert event.application_id is None
    assert event.job_title == ""
    assert event.event_date is None
    assert event.event_time is None
    assert event.time_kind == "unspecified"
    assert event.starts_at is None and event.ends_at is None

    repeated = schedule_manage(
        _create_request(time_kind="appointment"),
        storage,
        write_enabled=True,
    )
    assert repeated.success is True
    assert repeated.data is not None
    assert repeated.data.created is False
    assert repeated.data.event.id == event.id

    conflict = schedule_manage(
        _create_request(note="private_note_marker"),
        storage,
        write_enabled=True,
    )
    assert conflict.success is False
    assert conflict.error_code.value == "invalid_input"
    assert "request_key" in (conflict.error_message or "")
    assert "payload" in (conflict.error_message or "")
    assert "private_note_marker" not in (conflict.error_message or "")
    with storage.session() as session:
        assert len(session.scalars(select(ScheduleEventSnapshot)).all()) == 1


def test_application_binding_rejects_mismatch_and_fills_empty_job(storage):
    _seed_application(storage)
    bound = schedule_manage(
        _create_request(application_id="app-1", job_title=""),
        storage,
        write_enabled=True,
    )
    assert bound.success is True
    assert bound.data is not None
    assert bound.data.event.application_id == "app-1"
    assert bound.data.event.company_name == "Acme"
    assert bound.data.event.job_title == "Backend"

    bad_company = schedule_manage(
        _create_request(
            request_key="bad-company",
            application_id="app-1",
            company_name="Other",
        ),
        storage,
        write_enabled=True,
    )
    bad_job = schedule_manage(
        _create_request(
            request_key="bad-job",
            application_id="app-1",
            job_title="Different role",
        ),
        storage,
        write_enabled=True,
    )
    assert bad_company.success is False
    assert bad_job.success is False
    assert bad_company.error_code.value == "invalid_input"
    assert "reselect the application" in (bad_company.error_message or "")
    assert "reselect the application" in (bad_job.error_message or "")


def test_date_only_update_becomes_appointment_without_duration(storage):
    created = schedule_manage(_create_request(), storage, write_enabled=True)
    assert created.data is not None
    event = created.data.event
    assert event.time_kind == "unspecified"

    updated = schedule_manage(
        ScheduleManageInput(
            action="update",
            event_id=event.id,
            event_date=date(2026, 10, 1),
        ),
        storage,
        write_enabled=True,
    )
    assert updated.success is True
    assert updated.data is not None
    assert updated.data.event.event_date == date(2026, 10, 1)
    assert updated.data.event.event_time is None
    assert updated.data.event.time_kind == "appointment"
    assert updated.data.event.starts_at is None
    assert updated.data.event.ends_at is None


def test_bound_update_keeps_identity_consistent_when_job_is_empty(storage):
    _seed_application(storage)
    created = schedule_manage(
        _create_request(application_id="app-1", job_title=""),
        storage,
        write_enabled=True,
    )
    assert created.data is not None
    event = created.data.event

    updated = schedule_manage(
        ScheduleManageInput(
            action="update",
            event_id=event.id,
            job_title="",
            expected_updated_at=event.updated_at,
        ),
        storage,
        write_enabled=True,
    )
    assert updated.success is True
    assert updated.data is not None
    assert updated.data.event.company_name == "Acme"
    assert updated.data.event.job_title == "Backend"

    stale = schedule_manage(
        ScheduleManageInput(
            action="update",
            event_id=event.id,
            status="completed",
            expected_updated_at=event.updated_at - timedelta(seconds=1),
        ),
        storage,
        write_enabled=True,
    )
    assert stale.success is False
    assert stale.error_code.value == "invalid_input"
    assert "changed" in (stale.error_message or "")


def test_storage_exception_response_is_generic_and_logged_without_secret(caplog):
    request = _create_request()

    class BrokenStorage:
        @contextmanager
        def write_transaction(self):
            raise RuntimeError("password=not-for-model")
            yield

    result = schedule_manage(request, BrokenStorage(), write_enabled=True)  # type: ignore[arg-type]

    assert result.success is False
    assert result.error_message == "Schedule storage was unavailable."
    assert "not-for-model" not in result.model_dump_json()
    assert "not-for-model" not in caplog.text
    assert "schedule_manage storage operation failed" in caplog.text


@pytest.fixture
def local_schedule_client(monkeypatch, tmp_path):
    settings = SimpleNamespace(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'local-ui.db'}",
        write_enabled=True,
    )
    monkeypatch.setattr(local_ui, "get_settings", lambda: settings)
    storage = Storage.from_url(settings.database_url, initialize=True)
    client = TestClient(main.app, base_url="http://127.0.0.1:8012")
    headers = {
        "Origin": "http://127.0.0.1:8012",
        "X-RecruitOps-Local-UI": "1",
    }
    return client, headers, storage


def test_local_ui_schedule_contract_requires_version_and_preserves_binding(
    local_schedule_client,
):
    client, headers, storage = local_schedule_client
    created = client.post(
        "/api/local-ui/events",
        headers=headers,
        json={
            "title": "Company assessment",
            "event_type": "assessment",
            "company_name": "Acme",
            "job_title": "",
        },
    )
    assert created.status_code == 201, created.text
    event = created.json()["event"]
    assert event["application_id"] is None
    assert event["job_title"] == ""
    assert event["event_date"] is None
    assert event["time_kind"] == "unspecified"
    assert event["starts_at"] is None and event["ends_at"] is None

    missing_version = client.patch(
        f"/api/local-ui/events/{event['id']}",
        headers=headers,
        json={"event_date": "2026-10-01"},
    )
    assert missing_version.status_code == 422

    updated = client.patch(
        f"/api/local-ui/events/{event['id']}",
        headers=headers,
        json={
            "event_date": "2026-10-01",
            "job_title": "",
            "expected_updated_at": event["updated_at"],
        },
    )
    assert updated.status_code == 200, updated.text
    updated_event = updated.json()["event"]
    assert updated_event["time_kind"] == "appointment"
    assert updated_event["event_time"] is None
    assert updated_event["starts_at"] is None and updated_event["ends_at"] is None
    assert updated_event["source"] == event["source"]
    assert updated_event["source_ref"] == event["source_ref"]

    stale = client.patch(
        f"/api/local-ui/events/{event['id']}",
        headers=headers,
        json={
            "status": "completed",
            "expected_updated_at": event["updated_at"],
        },
    )
    assert stale.status_code == 409, stale.text

    with storage.session() as session:
        row = session.get(ScheduleEventSnapshot, event["id"])
        assert row is not None
        assert row.application_id is None
