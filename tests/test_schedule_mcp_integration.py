from types import SimpleNamespace

from packages.storage import ApplicationSnapshot, Storage
from packages.repositories.postgres import PostgresRecruitmentRepository
from packages.mcp import register_agent_tools
from packages.mcp import server as mcp_server
from tests.test_mcp import FakeMCPServer


def test_agent_registered_handler_can_create_read_and_complete_same_item(monkeypatch):
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    repo = PostgresRecruitmentRepository(storage)
    server = FakeMCPServer()
    monkeypatch.setattr(mcp_server, "get_settings", lambda: SimpleNamespace(write_enabled=True))
    register_agent_tools(server, repo, None)
    write = lambda request: server.tools["schedule_manage"][0](request).model_dump(mode="json")
    request = {"action":"create", "request_key":"conversation-turn-1",
               "title":"完成测评", "company_name":"示例公司", "event_type":"测评"}
    first = write(request)
    assert first["success"] is True and first["read_only"] is False
    event = first["data"]["event"]
    assert event["event_date"] is None
    repeated = write(request)
    assert repeated["data"]["created"] is False
    read = server.tools["schedule_window"][0]({"start_date":"2026-09-14","end_date":"2026-10-14"}).model_dump(mode="json")
    assert read["data"]["events"][0]["id"] == event["id"]
    completed = write({"action":"update","event_id":event["id"],"status":"completed",
                       "expected_updated_at":event["updated_at"]})
    assert completed["success"] is True
    assert repo.list_schedule()[0].status == "completed"
    assert repo.list_applications() == []


def test_agent_registered_handler_updates_bound_status_after_application_rename(monkeypatch):
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    repo = PostgresRecruitmentRepository(storage)
    with storage.write_transaction() as session:
        session.add(ApplicationSnapshot(
            id="app-1", company_name="Acme", job_title="Software(Shenzhen)",
            stage="applied", idempotency_key="fixture:app-1", stage_history=[],
            source="fixture", source_ref="app-1",
        ))
    server = FakeMCPServer()
    monkeypatch.setattr(mcp_server, "get_settings", lambda: SimpleNamespace(write_enabled=True))
    register_agent_tools(server, repo, None)
    handler = server.tools["schedule_manage"][0]
    created = handler({
        "action": "create", "request_key": "bound-reminder",
        "title": "Interview reminder", "event_type": "interview",
        "company_name": "Acme", "application_id": "app-1",
    })
    assert created.success is True
    event = created.data.event
    with storage.write_transaction() as session:
        application = session.get(ApplicationSnapshot, "app-1")
        application.job_title = "Software（Shenzhen）(J12262)"
    updated = handler({
        "action": "update", "event_id": event.id, "status": "completed",
        "expected_updated_at": event.updated_at.isoformat(),
    })
    assert updated.success is True, updated.error_message
    assert updated.data.event.application_id == "app-1"
    assert updated.data.event.job_title == "Software（Shenzhen）(J12262)"
    assert updated.data.event.status == "completed"
    assert updated.data.event.source_ref == event.source_ref
    assert repo.list_applications()[0].stage == "applied"
