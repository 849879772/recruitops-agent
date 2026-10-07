"""Navigation reuse is a hint; every screenshot keeps a fresh audited operation."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from packages.browser_bridge import BrowserBridgeStore, OperationStatus
from packages.storage import Storage
from packages.storage.models import BrowserOperation
from packages.tools.browser_bridge import (
    ObserveApplicationStatusPageInput, observe_application_status_page,
)


URL = "https://ats.example/applications"


def request(**updates):
    values = dict(application_id="a", application_ids=["a", "b"],
                  application_url=URL, device_id="desktop-1", task_id="run-1",
                  idempotency_key="dom")
    values.update(updates)
    return ObserveApplicationStatusPageInput(**values)


def ready_source(store, **updates):
    response = observe_application_status_page(request(**updates), store)
    assert response.success
    source_id = response.data.operation_id
    store.append_event(source_id, "extracting", OperationStatus.EXTRACTING)
    store.append_event(source_id, "validating", OperationStatus.VALIDATING)
    store.terminal_result(source_id, {"page": {"url": URL, "text": "投递记录"}})
    return source_id


def vision(source_id, **updates):
    values = dict(idempotency_key="vision", application_id="b", application_ids=["b"],
                  include_vision=True,
                  vision_fallback_reason="no_structured_evidence_visible_status_likely",
                  reuse_observation_operation_id=source_id)
    values.update(updates)
    return request(**values)


@pytest.fixture
def store():
    storage = Storage.from_url("sqlite+pysqlite:///:memory:")
    yield BrowserBridgeStore(storage)
    storage.engine.dispose()


def test_fresh_subset_reuses_navigation_but_not_operation_or_evidence(store):
    source_id = ready_source(store)
    response = observe_application_status_page(vision(source_id), store)
    assert response.success
    assert response.data.operation_id != source_id
    assert response.data.status == OperationStatus.CONNECTING
    assert response.data.result is None
    assert response.data.command["params"]["reuse_observation_operation_id"] == source_id
    assert response.data.command["params"]["review_task_id"] == "run-1"
    assert len(store.fetch_unacked_outbox("desktop-1")) == 2


@pytest.mark.parametrize("change", [
    "expired", "future", "no_timestamp", "device", "page", "target", "owner",
    "missing", "vision", "cancelled", "failed", "state_unclear", "error", "user_action",
])
def test_invalid_or_expired_hint_falls_back_to_normal_observation(store, change):
    source_id = ready_source(store)
    updates = {}
    if change == "missing":
        source_id = "missing-source"
    elif change == "device":
        updates["device_id"] = "desktop-2"
    elif change == "page":
        updates["application_url"] = "https://ats.example/other"
    elif change == "target":
        updates.update(application_id="c", application_ids=["c"])
    elif change == "owner":
        updates["task_id"] = "run-2"
    else:
        with store.storage.write_transaction() as session:
            row = session.get(BrowserOperation, source_id)
            if change in {"expired", "future"}:
                row.completed_at = datetime.now(timezone.utc) + timedelta(seconds=-31 if change == "expired" else 5)
            elif change == "no_timestamp":
                row.completed_at = None
            elif change == "vision":
                row.command = {**row.command, "params": {**row.command["params"], "include_vision": True}}
            elif change == "error":
                row.error_code = "LOGIN_REQUIRED"
            elif change == "user_action":
                row.result = {**row.result, "requires_user_action": True}
            else:
                row.status = change.upper()
    response = observe_application_status_page(vision(source_id, **updates), store)
    assert response.success
    assert "reuse_observation_operation_id" not in response.data.command["params"]
    assert response.data.result is None


def test_replay_remains_idempotent_after_source_expires(store):
    source_id = ready_source(store)
    req = vision(source_id)
    first = observe_application_status_page(req, store)
    with store.storage.write_transaction() as session:
        session.get(BrowserOperation, source_id).completed_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    replay = observe_application_status_page(req, store)
    assert replay.success and replay.data.idempotent_replay
    assert replay.data.operation_id == first.data.operation_id
    assert len(store.fetch_unacked_outbox("desktop-1")) == 2
    conflict = observe_application_status_page(vision(source_id, task_id="run-2"), store)
    assert not conflict.success


def test_dom_requests_cannot_supply_reuse_hint():
    with pytest.raises(ValidationError, match="only valid when include_vision"):
        request(reuse_observation_operation_id="some-source")
