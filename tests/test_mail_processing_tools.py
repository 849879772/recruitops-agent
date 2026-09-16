from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from packages import mcp as mcp_package
from packages.mcp import server as mcp_server
from packages.tools import mail_processing
from packages.tools.mail_processing import (
    RecruitmentMailProcessInput,
    RecruitmentMailProcessResponse,
    RecruitmentMailProcessingStatusInput,
    RecruitmentMailProcessingStatusResponse,
    recruitment_mail_process,
    recruitment_mail_processing_status,
)
from packages.tools.typed import ToolErrorCode, ToolStatus


class _Store:
    pass


class _Repository:
    pass


class _MCPServer:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, *, name: str, description: str):
        def register(handler):
            self.tools[name] = handler
            return handler

        return register


def _settings(*, write_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(write_enabled=write_enabled)


def test_process_input_is_bounded_and_keeps_the_service_batch_contract() -> None:
    request = RecruitmentMailProcessInput(record_ids=[" mail-1 ", "mail-2"])

    assert request.limit == 20
    assert request.record_ids == ["mail-1", "mail-2"]
    with pytest.raises(ValidationError):
        RecruitmentMailProcessInput(limit=0)
    with pytest.raises(ValidationError):
        RecruitmentMailProcessInput(record_ids=[f"mail-{index}" for index in range(51)])
    with pytest.raises(ValidationError):
        RecruitmentMailProcessInput(record_ids=[" "])


def test_process_requires_write_enabled_before_loading_or_calling_service(monkeypatch) -> None:
    called = False

    def unexpected_loader():
        nonlocal called
        called = True
        raise AssertionError("the service must not load while writes are disabled")

    monkeypatch.setattr(mail_processing, "_load_processing_service", unexpected_loader)
    response = recruitment_mail_process(
        RecruitmentMailProcessInput(),
        _Store(),
        _Repository(),
        settings=_settings(write_enabled=False),
    )

    assert isinstance(response, RecruitmentMailProcessResponse)
    assert response.status is ToolStatus.FAILURE
    assert response.error_code is ToolErrorCode.READ_ONLY_VIOLATION
    assert response.read_only is False
    assert called is False


def test_process_delegates_once_with_bounded_arguments_and_returns_actual_partial_result(monkeypatch) -> None:
    seen = {}

    def process_pending_mail(store, repository, settings, *, limit, record_ids, client):
        seen.update(
            store=store,
            repository=repository,
            settings=settings,
            limit=limit,
            record_ids=record_ids,
            client=client,
        )
        return {
            "status": "partial",
            "processed": 1,
            "updated": 1,
            "results": [
                {
                    "record_id": "mail-1",
                    "state": "processed_updated",
                    "write_result": {"state": "updated", "wrote": True},
                }
            ],
        }

    monkeypatch.setattr(
        mail_processing,
        "_load_processing_service",
        lambda: (process_pending_mail, lambda _store, *, limit: {"items": []}),
    )
    store = _Store()
    repository = _Repository()
    settings = _settings(write_enabled=True)
    client = object()
    response = recruitment_mail_process(
        RecruitmentMailProcessInput(limit=20, record_ids=["mail-1"]),
        store,
        repository,
        settings=settings,
        client=client,
    )

    assert response.status is ToolStatus.SUCCESS
    assert response.data["status"] == "partial"
    assert response.data["results"][0]["state"] == "processed_updated"
    assert seen == {
        "store": store,
        "repository": repository,
        "settings": settings,
        "limit": 20,
        "record_ids": ["mail-1"],
        "client": client,
    }


def test_blocked_service_result_is_a_structured_failure_without_raw_details(monkeypatch) -> None:
    def process_pending_mail(*_args, **_kwargs):
        return {"status": "blocked", "reason": "write_disabled", "secret": "do-not-return"}

    monkeypatch.setattr(
        mail_processing,
        "_load_processing_service",
        lambda: (process_pending_mail, lambda _store, *, limit: {"items": []}),
    )
    response = recruitment_mail_process(
        RecruitmentMailProcessInput(),
        _Store(),
        _Repository(),
        settings=_settings(write_enabled=True),
    )

    assert response.status is ToolStatus.FAILURE
    assert response.error_code is ToolErrorCode.READ_ONLY_VIOLATION
    assert response.error_message == "Explicit mail processing requires Settings.write_enabled=true."
    assert "do-not-return" not in (response.error_message or "")


def test_service_exception_is_structured_and_does_not_expose_secret_text(monkeypatch) -> None:
    def process_pending_mail(*_args, **_kwargs):
        raise RuntimeError("api_key=do-not-return")

    monkeypatch.setattr(
        mail_processing,
        "_load_processing_service",
        lambda: (process_pending_mail, lambda _store, *, limit: {"items": []}),
    )
    response = recruitment_mail_process(
        RecruitmentMailProcessInput(),
        _Store(),
        _Repository(),
        settings=_settings(write_enabled=True),
    )

    assert response.status is ToolStatus.FAILURE
    assert response.error_code is ToolErrorCode.SOURCE_UNAVAILABLE
    assert "do-not-return" not in (response.error_message or "")


def test_processing_status_returns_items_without_sync_or_write(monkeypatch) -> None:
    calls = []

    def processing_status(store, *, limit):
        calls.append((store, limit))
        return {
            "items": [
                {
                    "record_id": "mail-1",
                    "processing_status": "pending",
                    "analysis": {"outcome": "proposed"},
                    "verified": False,
                    "written": False,
                }
            ]
        }

    monkeypatch.setattr(
        mail_processing,
        "_load_processing_service",
        lambda: (lambda *_args, **_kwargs: {}, processing_status),
    )
    store = _Store()
    response = recruitment_mail_processing_status(
        RecruitmentMailProcessingStatusInput(limit=50),
        store,
    )

    assert isinstance(response, RecruitmentMailProcessingStatusResponse)
    assert response.status is ToolStatus.SUCCESS
    assert response.data["items"][0]["analysis"]["outcome"] == "proposed"
    assert calls == [(store, 50)]


def test_mcp_process_refreshes_once_but_status_does_not_sync(monkeypatch) -> None:
    refresh_limits = []

    def refresh(_dependencies, *, limit):
        refresh_limits.append(limit)
        return {"status": "cached"}

    def process_pending_mail(*_args, **kwargs):
        assert kwargs["limit"] == 20
        assert kwargs["record_ids"] == ["mail-1"]
        return {"status": "completed", "processed": 0, "results": []}

    def processing_status(_store, *, limit):
        return {"items": [{"record_id": "mail-1", "processing_status": "pending"}]}

    monkeypatch.setattr(mcp_server, "_sync_mail_before_read", refresh)
    monkeypatch.setattr(
        mcp_server,
        "get_settings",
        lambda: _settings(write_enabled=True),
    )
    monkeypatch.setattr(
        mail_processing,
        "_load_processing_service",
        lambda: (process_pending_mail, processing_status),
    )

    server = _MCPServer()
    registered = mcp_package.register_tools(server, _Repository(), _Store())
    assert "recruitment_mail_process" in registered
    assert "recruitment_mail_processing_status" in registered

    processed = server.tools["recruitment_mail_process"](
        {"limit": 20, "record_ids": ["mail-1"]}
    )
    status = server.tools["recruitment_mail_processing_status"]({"limit": 50})

    assert processed.status is ToolStatus.SUCCESS
    assert processed.freshness == {"status": "cached"}
    assert status.status is ToolStatus.SUCCESS
    assert status.data["items"][0]["processing_status"] == "pending"
    assert refresh_limits == [20]
