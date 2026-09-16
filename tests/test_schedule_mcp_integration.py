from types import SimpleNamespace

from packages.storage import Storage
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
