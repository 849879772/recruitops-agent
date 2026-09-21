"""Real bridge server/store + runtime guard on disposable SQLite, not business acceptance."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from fastapi import FastAPI, HTTPException
import uvicorn
from packages.browser_bridge import BrowserBridgeStore, OperationName, install_browser_bridge
from packages.desktop_runtime.api_bootstrap import ReadOnlyGuard
from packages.storage import Storage, ApplicationSnapshot, WriteAudit
from packages.domain.models import Application, ApplicationStage
from packages.tools.browser_bridge import (ReviewAndUpdateApplicationStatusInput,
    review_and_update_application_status_workflow, BrowserOperationStatusInput,
    browser_operation_status)
from sqlalchemy import select

directory = Path(sys.argv[1]).resolve()
if not directory.is_relative_to(ROOT / ".desktop-runtime-tests"):
    raise SystemExit("isolated directory required")
directory.mkdir(parents=True, exist_ok=True)
store = BrowserBridgeStore(Storage.from_url(f"sqlite:///{directory / 'bridge.db'}", initialize=True))
token = os.environ["RECRUITOPS_DESKTOP_SHELL_TOKEN"]
app = FastAPI()
bridge = install_browser_bridge(app, store, api_token=token)
dispatches = {}
instance = "python-bridge-fixture"
run = secrets.token_hex(16)


class FixtureRepository:
    def list_applications(self):
        with store.storage.session() as session:
            rows = session.scalars(select(ApplicationSnapshot)).all()
            return [Application(id=row.id, company_name=row.company_name, job_title=row.job_title,
                                record_url=row.record_url, stage=ApplicationStage(row.stage),
                                idempotency_key=row.idempotency_key, source="test", source_ref=row.id)
                    for row in rows]


@app.post("/fixture/workflow")
async def workflow(value: dict):
    # Real tool entrypoint, verifier and write adapter; only repository data is synthetic.
    application_id = "consumer-app"
    with store.storage.write_transaction() as session:
        if session.get(ApplicationSnapshot, application_id) is None:
            session.add(ApplicationSnapshot(id=application_id, company_name="Fixture Company",
                        job_title="Fixture role", record_url="https://ats.example/applications/consumer-app",
                        stage="applied", idempotency_key="application:consumer-app", stage_history=[],
                        source="test", source_ref=application_id))
    result = await review_and_update_application_status_workflow(
        ReviewAndUpdateApplicationStatusInput(application_id=application_id,
            device_id=value["device_id"], operation_id=value["operation_id"],
            idempotency_key=value["operation_id"], timeout_ms=value.get("timeout_ms", 5000)),
        store, FixtureRepository())
    return result.model_dump(mode="json")


@app.get("/fixture/readable/{operation_id}")
def readable(operation_id: str):
    result = browser_operation_status(BrowserOperationStatusInput(operation_id=operation_id), store)
    with store.storage.session() as session:
        application = session.get(ApplicationSnapshot, "consumer-app")
        audits = session.scalars(select(WriteAudit)).all()
        return {"operation": result.model_dump(mode="json"), "stage": application.stage,
                "audits": len(audits), "history": application.stage_history}


@app.get("/desktop-runtime/ready")
def ready():
    return dict(instance_id=instance, run_id=run, status="ready", writes=True, websocket=True)


@app.post("/fixture/create")
def create(value: dict):
    review = value.get("review", False)
    operation = store.create(
        OperationName.REVIEW_AND_UPDATE_APPLICATION_STATUS if review else OperationName.OBSERVE_APPLICATION_STATUS_PAGE,
        device_id=value["device_id"], operation_id=value["operation_id"], idempotency_key=value["operation_id"],
        command=dict(action="read_application_status" if review else "observe_application_page",
                     selector_key="application_status" if review else "application_page", params={},
                     page_url="https://ats.example/applications", origin="https://ats.example",
                     application_id="fixture-app", application_ids=["fixture-app"]))
    for item in store.fetch_unacked_outbox(value["device_id"]):
        dispatches[item.operation_id] = bridge._outbox_message(value["device_id"], item)
    return {"operation_id": operation.operation_id}


@app.get("/fixture/state/{operation_id}")
def state(operation_id: str):
    operation = store.get_operation(operation_id)
    if operation is None:
        raise HTTPException(404)
    return {"status": operation.status, "result": operation.result,
            "events": [{"event_id": e.event_id, "event_type": e.event_type, "status": e.status, "payload": e.payload}
                       for e in store.get_events(operation_id)],
            "unacked": len(store.fetch_unacked_outbox(operation.device_id)),
            "connected": bridge.is_connected(operation.device_id)}


@app.post("/fixture/duplicate/{operation_id}")
async def duplicate(operation_id: str):
    message = dispatches[operation_id]
    connection = await bridge._connection_for(message["device_id"])
    await bridge._send(connection, message)
    return {"sent": True}


@app.post("/fixture/cancel/{operation_id}")
def cancel(operation_id: str):
    store.cancel(operation_id, reason="fixture_cancel")
    return {"cancelled": True}


@app.post("/fixture/disconnect/{device_id}")
async def disconnect(device_id: str):
    await bridge.close_device(device_id, code=1012)
    return {"closed": True}


def emit(sequence, event, **fields):
    print(json.dumps(dict(protocol=1, sequence=sequence, run_id=run, instance_id=instance,
                          event=event, stage="runtime", **fields)), flush=True)


async def main():
    sock = socket.socket()
    for _ in range(32):
        try:
            sock.bind(("127.0.0.1", 49152 + secrets.randbelow(16384)))
            break
        except OSError:
            continue
    else:
        raise RuntimeError("fixture port unavailable")
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    guarded = ReadOnlyGuard(app, token, writes=True, owned_origin=origin)
    server = uvicorn.Server(uvicorn.Config(guarded, log_level="critical", access_log=False, lifespan="on"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if task.done():
            await task
            return
        await asyncio.sleep(.01)
    emit(1, "ready", api_url=origin, writes=True, websocket=True)

    def stop_reader():
        for line in sys.stdin:
            if json.loads(line).get("command") == "stop":
                break
        server.should_exit = True

    threading.Thread(target=stop_reader, daemon=True).start()
    await task
    sock.close()
    store.storage.engine.dispose()
    emit(2, "stopped")


asyncio.run(main())
