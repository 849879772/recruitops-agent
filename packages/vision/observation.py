"""Bind a paid screenshot reading to a single authorized browser observation."""

from contextlib import contextmanager
from copy import copy
from dataclasses import dataclass, field
from hashlib import sha256
from threading import BoundedSemaphore, Lock
from time import monotonic

from packages.browser_bridge import BrowserBridgeStore, OperationName, OperationStatus
from packages.domain.urls import normalize_http_page_url

from .service import VisionError, VisionResult, VisionService, images_digest


_REGISTRY_LOCK = Lock()
_PROVIDER_SLOTS = BoundedSemaphore(2)
_PROVIDER_QUEUE_TIMEOUT_SECONDS = 15.0
# The desktop gives image analysis 70 seconds; leave room for HTTP/DB overhead.
_OBSERVATION_BUDGET_SECONDS = 65.0
_ACTIVE_STATUSES = {OperationStatus.EXTRACTING.value, OperationStatus.VALIDATING.value}
_CACHED_STATUSES = _ACTIVE_STATUSES | {OperationStatus.SUCCEEDED.value, OperationStatus.STATE_UNCLEAR.value}


@dataclass
class _OperationReading:
    digest: str
    lock: Lock = field(default_factory=Lock)
    users: int = 0


_READINGS: dict[str, _OperationReading] = {}


@contextmanager
def _operation_reading(operation_id: str, digest: str, deadline: float):
    # Only the registry is global. Unrelated pages do not hold each other up.
    with _REGISTRY_LOCK:
        entry = _READINGS.get(operation_id)
        if entry is None:
            entry = _READINGS[operation_id] = _OperationReading(digest)
        elif entry.digest != digest:
            raise VisionError("observation_image_changed")
        entry.users += 1
    acquired = False
    try:
        acquired = entry.lock.acquire(timeout=max(0.0, deadline - monotonic()))
        if not acquired:
            raise VisionError("vision_duplicate_wait_timeout")
        yield
    finally:
        if acquired:
            entry.lock.release()
        with _REGISTRY_LOCK:
            entry.users -= 1
            if not entry.users:
                del _READINGS[operation_id]


def _bound_operation(store: BrowserBridgeStore, operation_id: str, page_url: str):
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
    return operation


def _record_failure(store, operation_id, event_key, exc, request_count):
    current = store.get_operation(operation_id)
    if current and current.status in _ACTIVE_STATUSES:
        payload = {"code": exc.code, "provider_request_attempted": request_count > 0}
        if exc.diagnostics:
            payload["diagnostics"] = exc.diagnostics
        # A queue/configuration failure was never billed and may be retried.
        # Preserve a later provider failure instead of colliding with that event.
        failure_key = sha256(f"{exc.code}:{request_count}".encode()).hexdigest()[:12]
        try:
            store.append_event(operation_id, f"vision-failed-{event_key}-{failure_key}", current.status,
                payload, event_type="vision_failure")
        except ValueError:
            # Cancel/navigation may win after the check. Never revive the operation.
            pass


def analyze_observation(
    store: BrowserBridgeStore, service: VisionService, *,
    operation_id: str, page_url: str, image_data_url: str | None = None,
    image_data_urls: list[str] | None = None,
) -> VisionResult:
    deadline = monotonic() + _OBSERVATION_BUDGET_SECONDS
    _bound_operation(store, operation_id, page_url)
    if (image_data_url is None) == (image_data_urls is None):
        raise VisionError("image_input_invalid")
    images = image_data_urls if image_data_urls is not None else [image_data_url]
    digest = images_digest(images, service.max_bytes)
    # The API runs one worker. Same-operation requests share one persisted reading,
    # while at most two independent observations can use the provider concurrently.
    with _operation_reading(operation_id, digest, deadline):
        event_key = sha256(operation_id.encode()).hexdigest()[:32]
        operation = _bound_operation(store, operation_id, page_url)
        if operation.status not in _CACHED_STATUSES:
            raise VisionError("observation_not_extracting")
        events = store.get_events(operation_id)
        for event in events:
            if event.event_type == "vision_analysis":
                result = VisionResult.model_validate(event.payload)
                if result.image_sha256 != digest:
                    raise VisionError("observation_image_changed")
                if _bound_operation(store, operation_id, page_url).status not in _CACHED_STATUSES:
                    raise VisionError("observation_not_extracting")
                return result
        requests = [event for event in events if event.event_type == "vision_request"]
        if any(event.payload.get("image_sha256") != digest for event in requests):
            raise VisionError("observation_image_changed")
        if requests:
            raise VisionError("vision_attempt_already_recorded")
        if operation.status not in _ACTIVE_STATUSES:
            raise VisionError("observation_not_extracting")
        try:
            service.validate_configuration()
        except VisionError as exc:
            _record_failure(store, operation_id, event_key, exc, 0)
            raise
        request_count = 0

        def before_request(attempt: int):
            nonlocal request_count
            current = _bound_operation(store, operation_id, page_url)
            if current.status not in _ACTIVE_STATUSES:
                raise VisionError("observation_not_extracting")
            try:
                store.append_event(
                    operation_id, f"vision-request-{event_key}" + (f"-{attempt}" if attempt > 1 else ""), current.status,
                    {"image_sha256": digest, "image_count": len(images), "model": service.model,
                     "provider_request_attempted": True, "attempt": attempt, "format_repair": attempt > 1},
                    event_type="vision_request",
                )
            except ValueError:
                raise VisionError("observation_not_extracting") from None
            request_count += 1

        acquired = False
        try:
            acquired = _PROVIDER_SLOTS.acquire(timeout=max(0.0, min(
                _PROVIDER_QUEUE_TIMEOUT_SECONDS, deadline - monotonic())))
            if not acquired:
                raise VisionError("vision_queue_timeout", diagnostics=[{"code": "provider_queue_timeout"}])
            current = _bound_operation(store, operation_id, page_url)
            if current.status not in _ACTIVE_STATUSES:
                raise VisionError("observation_not_extracting")
            remaining = deadline - monotonic()
            if remaining < 1:
                raise VisionError("vision_queue_timeout", diagnostics=[{"code": "observation_budget_exhausted"}])
            # Do not mutate a service shared by concurrent API requests. Repairs
            # retain the service's existing shared deadline inside this remainder.
            bounded_service = copy(service)
            bounded_service.timeout = min(service.timeout, remaining)
            result = bounded_service.analyze_images(images, on_request=before_request)
        except VisionError as exc:
            _record_failure(store, operation_id, event_key, exc, request_count)
            raise
        finally:
            if acquired:
                _PROVIDER_SLOTS.release()
        current = _bound_operation(store, operation_id, page_url)
        if current.status not in _ACTIVE_STATUSES:
            raise VisionError("observation_not_extracting")
        try:
            store.append_event(
                operation_id, f"vision-analysis-{event_key}", current.status,
                result.model_dump(mode="json"), event_type="vision_analysis",
            )
        except ValueError:
            raise VisionError("observation_not_extracting") from None
        return result
