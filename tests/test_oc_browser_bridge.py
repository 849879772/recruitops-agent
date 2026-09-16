from __future__ import annotations

import asyncio

from packages.browser_bridge import BrowserBridgeStore, OperationStatus
from packages.storage import Storage
from packages.tools.browser_bridge import (
    CaptureOcSnapshotInput,
    capture_oc_snapshot,
    capture_oc_snapshot_workflow,
)


def test_oc_capture_dispatch_is_fixed_and_workflow_returns_snapshot_metadata(tmp_path) -> None:
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'oc-bridge.db'}", initialize=True)
    store = BrowserBridgeStore(storage)
    store.mark_device_connected("edge-oc")
    request = CaptureOcSnapshotInput(
        device_id="edge-oc",
        idempotency_key="oc-2026-08-26",
        operation_id="oc-operation",
    )
    assert request.timeout_ms == 900_000

    created = capture_oc_snapshot(request, store)
    assert created.success and created.data is not None
    assert created.data.command == {
        "application_id": "oc-snapshot",
        "application_ids": ["oc-snapshot"],
        "action": "capture_oc_page",
        "selector_key": "oc_company_table",
        "params": {"page": 1, "apply_filters": True},
        "page_url": "https://www.givemeoc.com/",
        "origin": "https://www.givemeoc.com",
    }
    dispatch = store.fetch_unacked_outbox("edge-oc")[0]
    store.ack("edge-oc", dispatch.sequence, operation_id="oc-operation")
    store.append_event("oc-operation", "oc-nav", OperationStatus.NAVIGATING)
    store.append_event("oc-operation", "oc-extract", OperationStatus.EXTRACTING)
    store.append_event("oc-operation", "oc-validate", OperationStatus.VALIDATING)
    store.terminal_result(
        "oc-operation",
        {
            "snapshot_path": "D:/agent/.data/discovery/givemeoc_latest.json",
            "record_count": 404,
            "total_pages": 14,
            "sha256": "a" * 64,
        },
        status=OperationStatus.SUCCEEDED,
        event_id="oc-complete",
    )

    result = asyncio.run(capture_oc_snapshot_workflow(request, store))

    assert result.success and result.data is not None
    assert result.data.record_count == 404
    assert result.data.total_pages == 14
    assert result.data.sha256 == "a" * 64
