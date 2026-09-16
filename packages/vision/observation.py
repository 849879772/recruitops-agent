"""Bind a paid screenshot reading to a single authorized browser observation."""

from hashlib import sha256
from threading import RLock

from packages.browser_bridge import BrowserBridgeStore, OperationName, OperationStatus
from packages.domain.urls import normalize_http_page_url

from .service import VisionError, VisionResult, VisionService, image_digest


_LOCK = RLock()


def analyze_observation(
    store: BrowserBridgeStore, service: VisionService, *,
    operation_id: str, page_url: str, image_data_url: str,
) -> VisionResult:
    # The API runs one worker. Serializing here also bounds accidental duplicate image charges.
    with _LOCK:
        event_key = sha256(operation_id.encode()).hexdigest()[:32]
        operation = store.get_operation(operation_id)
        if operation is None or operation.operation != OperationName.OBSERVE_APPLICATION_STATUS_PAGE.value:
            raise VisionError("observation_not_found")
        command = operation.command or {}
        requested_url = normalize_http_page_url(page_url)
        if (
            not requested_url
            or requested_url != normalize_http_page_url(command.get("page_url") or "")
            or not command.get("application_id")
            or command.get("params", {}).get("include_vision") is not True
        ):
            raise VisionError("observation_binding_mismatch")
        digest = image_digest(image_data_url, service.max_bytes)
        events = store.get_events(operation_id)
        for event in events:
            if event.event_type == "vision_analysis":
                result = VisionResult.model_validate(event.payload)
                if result.image_sha256 != digest:
                    raise VisionError("observation_image_changed")
                return result
        if any(event.event_type == "vision_request" for event in events):
            raise VisionError("vision_attempt_already_recorded")
        if operation.status not in {OperationStatus.EXTRACTING.value, OperationStatus.VALIDATING.value}:
            raise VisionError("observation_not_extracting")
        store.append_event(
            operation_id, f"vision-request-{event_key}", operation.status,
            {"image_sha256": digest}, event_type="vision_request",
        )
        try:
            result = service.analyze(image_data_url)
        except VisionError as exc:
            store.append_event(
                operation_id, f"vision-failed-{event_key}", operation.status,
                {"code": exc.code}, event_type="vision_failure",
            )
            raise
        store.append_event(
            operation_id, f"vision-analysis-{event_key}", operation.status,
            result.model_dump(mode="json"), event_type="vision_analysis",
        )
        return result
