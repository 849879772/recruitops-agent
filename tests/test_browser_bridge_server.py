from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from packages.browser_bridge import (
    BrowserBridgeServer,
    BrowserBridgeStore,
    OperationName,
    OperationStatus,
    create_auth_signature,
    install_browser_bridge,
)
from packages.storage import Storage


API_TOKEN = "local-api-token"


def _app_and_store() -> tuple[FastAPI, BrowserBridgeStore]:
    store = BrowserBridgeStore(
        Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    )
    app = FastAPI()
    install_browser_bridge(app, store, api_token=API_TOKEN)
    return app, store


def _create(store: BrowserBridgeStore, operation_id: str = "operation-1") -> None:
    store.create(
        OperationName.REVIEW_AND_UPDATE_APPLICATION_STATUS,
        device_id="edge-1",
        idempotency_key=f"idem-{operation_id}",
        operation_id=operation_id,
        command={
            "command": {
                "page_url": "https://ats.example/applications/1",
                "origin": "https://ats.example",
                "approval_token": "approval-token",
            }
        },
    )


def _authenticate(websocket, device_id: str = "edge-1") -> dict:
    challenge = websocket.receive_json()
    assert challenge["type"] == "challenge"
    websocket.send_json(
        {
            "type": "auth",
            "protocol_version": 1,
            "device_id": device_id,
            "challenge": challenge["challenge"],
            "signature": create_auth_signature(
                API_TOKEN,
                challenge["challenge"],
                device_id,
            ),
        }
    )
    return challenge


def test_challenge_auth_dispatches_unacked_outbox_and_ack_is_durable() -> None:
    app, store = _app_and_store()
    _create(store)

    with TestClient(app) as client:
        with client.websocket_connect("/browser-bridge") as websocket:
            challenge = _authenticate(websocket)
            dispatch = websocket.receive_json()

            assert dispatch["type"] == "operation.dispatch"
            assert dispatch["operation_id"] == "operation-1"
            assert API_TOKEN not in json.dumps(challenge)
            assert API_TOKEN not in json.dumps(dispatch)

            websocket.send_json(
                {
                    "type": "ack",
                    "sequence": dispatch["sequence"],
                    "operation_id": "operation-1",
                    "ack_id": "ack-1",
                }
            )

    assert store.fetch_unacked_outbox("edge-1") == []
    assert store.get_operation("operation-1").status == OperationStatus.DISPATCHED.value


def test_extension_reload_replaces_connection_and_redelivers_unacked_command() -> None:
    app, store = _app_and_store()
    _create(store)

    with TestClient(app) as client:
        with client.websocket_connect("/browser-bridge") as original:
            _authenticate(original)
            first_dispatch = original.receive_json()

            with client.websocket_connect("/browser-bridge") as reloaded:
                _authenticate(reloaded)
                replay = reloaded.receive_json()
                assert replay == first_dispatch
                status = store.get_connection_status("edge-1")
                assert status is not None and status.connected is True
                reloaded.send_json(
                    {
                        "type": "ack",
                        "sequence": replay["sequence"],
                        "operation_id": replay["operation_id"],
                        "ack_id": "ack-after-extension-reload",
                    }
                )
                reloaded.send_json({"type": "heartbeat"})
                assert reloaded.receive_json()["ack"] is True

    assert store.fetch_unacked_outbox("edge-1") == []


def test_background_dispatches_an_operation_from_an_independent_store_and_stop_cleans_state(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'browser-bridge-cross-process.db'}"
    server_store = BrowserBridgeStore(Storage.from_url(database_url, initialize=True))
    writer_store = BrowserBridgeStore(Storage.from_url(database_url, initialize=True))
    app = FastAPI()
    server = BrowserBridgeServer(
        server_store,
        API_TOKEN,
        dispatch_interval_seconds=0.01,
    )
    install_browser_bridge(app, server=server)

    with TestClient(app) as client:
        with client.websocket_connect("/browser-bridge") as websocket:
            _authenticate(websocket)
            assert server.dispatcher_task is not None
            _create(writer_store)

            dispatch = websocket.receive_json()
            assert dispatch["type"] == "operation.dispatch"
            assert dispatch["operation_id"] == "operation-1"
            status = writer_store.get_connection_status("edge-1")
            assert status is not None and status.connected is True

    assert server.dispatcher_task is None
    status = writer_store.get_connection_status("edge-1")
    assert status is not None and status.connected is False


def test_progress_result_and_heartbeat_are_routed_to_store() -> None:
    app, store = _app_and_store()
    _create(store)

    with TestClient(app) as client:
        with client.websocket_connect("/browser-bridge") as websocket:
            _authenticate(websocket)
            dispatch = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "ack",
                    "sequence": dispatch["sequence"],
                    "operation_id": "operation-1",
                }
            )
            for index, status in enumerate(
                ["NAVIGATING", "EXTRACTING"],
                start=1,
            ):
                websocket.send_json(
                    {
                        "type": "progress",
                        "operation_id": "operation-1",
                        "event_id": f"event-{index}",
                        "status": status,
                        "payload": {"stage": status},
                    }
                )
            websocket.send_json({"type": "heartbeat"})
            assert websocket.receive_json()["type"] == "heartbeat"
            websocket.send_json(
                {
                    "type": "result",
                    "operation_id": "operation-1",
                    "event_id": "result-1",
                    "status": "SUCCEEDED",
                    "result": {"status": "applied"},
                }
            )
            websocket.send_json({"type": "heartbeat"})
            assert websocket.receive_json()["type"] == "heartbeat"

    operation = store.get_operation("operation-1")
    assert operation.status == OperationStatus.VALIDATING.value
    assert operation.result is None
    events = store.get_events("operation-1")
    assert events[-1].status == OperationStatus.VALIDATING.value
    assert events[-1].payload == {"result": {"status": "applied"}}


def test_oc_capture_result_is_terminal_without_business_validation() -> None:
    app, store = _app_and_store()
    store.create(
        OperationName.CAPTURE_OC_SNAPSHOT,
        device_id="edge-1",
        idempotency_key="idem-oc-capture",
        operation_id="operation-oc-capture",
        command={"command": {"action": "capture_oc_page"}},
    )

    with TestClient(app) as client:
        with client.websocket_connect("/browser-bridge") as websocket:
            _authenticate(websocket)
            dispatch = websocket.receive_json()
            websocket.send_json(
                {
                    "type": "ack",
                    "sequence": dispatch["sequence"],
                    "operation_id": "operation-oc-capture",
                }
            )
            websocket.send_json(
                {
                    "type": "progress",
                    "operation_id": "operation-oc-capture",
                    "event_id": "event-oc-validating",
                    "status": "VALIDATING",
                    "payload": {"stage": "VALIDATING"},
                }
            )
            websocket.send_json(
                {
                    "type": "result",
                    "operation_id": "operation-oc-capture",
                    "event_id": "result-oc-capture",
                    "status": "SUCCEEDED",
                    "result": {
                        "snapshot_path": ".data/discovery/givemeoc_latest.json",
                        "record_count": 30,
                        "total_pages": 3,
                    },
                }
            )
            websocket.send_json({"type": "heartbeat"})
            assert websocket.receive_json()["type"] == "heartbeat"

    operation = store.get_operation("operation-oc-capture")
    assert operation.status == OperationStatus.SUCCEEDED.value
    assert operation.result == {
        "snapshot_path": ".data/discovery/givemeoc_latest.json",
        "record_count": 30,
        "total_pages": 3,
    }


def test_cancel_command_is_acknowledged_and_bad_signature_is_rejected() -> None:
    app, store = _app_and_store()
    _create(store)
    store.cancel("operation-1", reason="user stopped")

    with TestClient(app) as client:
        with client.websocket_connect("/browser-bridge") as websocket:
            _authenticate(websocket)
            dispatch = websocket.receive_json()
            websocket.send_json({"type": "ack", "sequence": dispatch["sequence"]})
            cancel = websocket.receive_json()
            assert cancel["type"] == "operation.cancel"
            websocket.send_json(
                {
                    "type": "cancel",
                    "sequence": cancel["sequence"],
                    "operation_id": "operation-1",
                }
            )

        assert store.fetch_unacked_outbox("edge-1") == []
        assert store.get_operation("operation-1").status == OperationStatus.CANCELLED.value

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/browser-bridge") as websocket:
                challenge = websocket.receive_json()
                websocket.send_json(
                    {
                        "type": "auth",
                        "device_id": "edge-1",
                        "challenge": challenge["challenge"],
                        "signature": "0" * 64,
                    }
                )
                websocket.receive_json()
