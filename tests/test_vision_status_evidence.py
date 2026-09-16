from datetime import datetime, timezone

import pytest

from packages.browser_bridge import BrowserBridgeStore, OperationName, OperationStatus
from packages.storage import ApplicationSnapshot, Storage
from packages.tools.application_status_evidence import VerifyApplicationStatusEvidenceInput, verify_application_status_evidence


def verify(tmp_path, *, persisted=True, confidence=0.97, quote="Software Engineer: Applied", conflict=False):
    storage = Storage.from_url(f"sqlite:///{tmp_path / 'evidence.db'}", initialize=True)
    store = BrowserBridgeStore(storage)
    with storage.write_transaction() as session:
        session.add(ApplicationSnapshot(id="24", company_name="Example", job_title="Software Engineer",
            record_url="https://example.com/applications", stage="applied", idempotency_key="application:24",
            stage_history=[], source="test", source_ref="24"))
    operation = store.create(OperationName.OBSERVE_APPLICATION_STATUS_PAGE, device_id="edge", idempotency_key="vision",
        command={"application_id": "24", "page_url": "https://example.com/applications", "params": {"include_vision": True}})
    store.append_event(operation.operation_id, "extracting", OperationStatus.EXTRACTING)
    vision = {"text": "Software Engineer: Applied", "confidence": confidence,
              "image_sha256": "0" * 64, "model": "deepseek-flash", "usage": {"total_tokens": 100}}
    if persisted:
        store.append_event(operation.operation_id, "vision-analysis", OperationStatus.EXTRACTING, vision, event_type="vision_analysis")
    result = {"page_url": "https://example.com/applications", "captured_at": "2026-09-13T10:00:00Z", "vision": vision}
    if conflict:
        result["entries"] = [{"application_id": "24", "status": "rejected", "label": "Rejected"}]
    store.append_event(operation.operation_id, "validating", OperationStatus.VALIDATING)
    store.terminal_result(operation.operation_id, result, status=OperationStatus.SUCCEEDED)
    return verify_application_status_evidence(VerifyApplicationStatusEvidenceInput(
        application_id="24", observation_operation_id=operation.operation_id,
        observed_status="applied", observed_label="Applied", evidence=quote, confidence=0.99,
        captured_at=datetime(2026, 9, 13, 10, tzinfo=timezone.utc)), store)


def test_server_reading_can_support_existing_verification_gate(tmp_path):
    result = verify(tmp_path)
    assert result.success and result.status == "unchanged"


def test_extension_cannot_fabricate_visual_model_output(tmp_path):
    result = verify(tmp_path, persisted=False)
    assert not result.success
    assert result.error_code == "evidence_not_in_observation"


def test_model_cannot_raise_low_image_confidence(tmp_path):
    result = verify(tmp_path, confidence=0.3)
    assert not result.success
    assert result.verification.data.reason_code == "confidence_below_threshold"


def test_image_quote_must_include_target_job(tmp_path):
    result = verify(tmp_path, quote="Applied")
    assert not result.success and result.error_code == "visual_evidence_unverified"


def test_image_does_not_override_contradictory_structured_entry(tmp_path):
    result = verify(tmp_path, conflict=True)
    assert not result.success and result.error_code == "status_evidence_conflict"
