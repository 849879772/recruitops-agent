"""Bounded screenshot throughput without duplicate charges or stale evidence."""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Event, Lock

import pytest

from packages.browser_bridge import BrowserBridgeStore, OperationName, OperationStatus
from packages.matching.client import DeepSeekClientError
from packages.storage import Storage
from packages.vision import VisionError, VisionService
from packages.vision import observation


IMAGE = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()
OTHER_IMAGE = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"1" * 20).decode()


def response():
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
        "text": "Software Engineer Applied", "confidence": 0.9})}}]}


def prepare(tmp_path, count=1):
    store = BrowserBridgeStore(Storage.from_url(f"sqlite:///{tmp_path / 'vision.db'}", initialize=True))
    requests = []
    for index in range(count):
        operation = store.create(OperationName.OBSERVE_APPLICATION_STATUS_PAGE,
            device_id="fixture", idempotency_key=f"image-{index}", command={
                "application_id": str(index), "page_url": f"https://example.test/records/{index}",
                "params": {"include_vision": True}})
        store.append_event(operation.operation_id, f"extracting-{index}", OperationStatus.EXTRACTING)
        requests.append({"operation_id": operation.operation_id,
            "page_url": f"https://example.test/records/{index}", "image_data_url": IMAGE})
    return store, requests


class ObservedSlots:
    """Expose queue arrival without relying on scheduling sleeps in tests."""
    def __init__(self, expected):
        self.slots = BoundedSemaphore(2)
        self.lock = Lock()
        self.calls = 0
        self.expected = expected
        self.arrived = Event()

    def acquire(self, **kwargs):
        with self.lock:
            self.calls += 1
            if self.calls == self.expected:
                self.arrived.set()
        return self.slots.acquire(**kwargs)

    def release(self):
        self.slots.release()


def test_independent_observations_overlap_but_never_exceed_two(tmp_path, monkeypatch):
    store, requests = prepare(tmp_path, 6)
    slots = ObservedSlots(6)
    monkeypatch.setattr(observation, "_PROVIDER_SLOTS", slots)
    lock, both_started, release = Lock(), Event(), Event()
    active = peak = calls = 0

    def transport(*_):
        nonlocal active, peak, calls
        with lock:
            calls += 1
            active += 1
            peak = max(active, peak)
            if active == 2:
                both_started.set()
        try:
            assert release.wait(5)
            return response()
        finally:
            with lock:
                active -= 1

    service = VisionService(api_key="fixture", transport=transport)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(observation.analyze_observation, store, service, **request) for request in requests]
        try:
            assert both_started.wait(5), "independent operations were serialized"
            assert slots.arrived.wait(5)
            assert calls == 2
        finally:
            release.set()
        assert len([future.result(timeout=5) for future in futures]) == 6
    assert peak == 2 and calls == 6
    assert not observation._READINGS


def test_simultaneous_duplicate_gets_same_reading_with_one_charge(tmp_path, monkeypatch):
    store, [request] = prepare(tmp_path)
    started, release, duplicate_entered = Event(), Event(), Event()
    calls = []
    original = observation._operation_reading
    entrants = []

    def reading(*args):
        entrants.append(args)
        if len(entrants) == 2:
            duplicate_entered.set()
        return original(*args)

    monkeypatch.setattr(observation, "_operation_reading", reading)

    def transport(*args):
        calls.append(args)
        started.set()
        assert release.wait(5)
        return response()

    service = VisionService(api_key="fixture", transport=transport)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(observation.analyze_observation, store, service, **request)
        try:
            assert started.wait(5)
            second = pool.submit(observation.analyze_observation, store, service, **request)
            assert duplicate_entered.wait(5)
            assert len(calls) == 1
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5)
    assert len(calls) == 1
    assert not observation._READINGS


def test_changed_images_rejected_during_inflight_request_and_after_cache(tmp_path):
    store, [request] = prepare(tmp_path)
    started, release = Event(), Event()
    calls = []

    def transport(*args):
        calls.append(args)
        started.set()
        assert release.wait(5)
        return response()

    service = VisionService(api_key="fixture", transport=transport)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(observation.analyze_observation, store, service, **request)
        try:
            assert started.wait(5)
            with pytest.raises(VisionError, match="observation_image_changed"):
                observation.analyze_observation(store, service, **{**request, "image_data_url": OTHER_IMAGE})
        finally:
            release.set()
        first.result(timeout=5)
    with pytest.raises(VisionError, match="observation_image_changed"):
        observation.analyze_observation(store, service, **{**request, "image_data_url": OTHER_IMAGE})
    assert len(calls) == 1
    assert not observation._READINGS


def test_cancelled_queued_operation_never_reaches_provider(tmp_path, monkeypatch):
    store, requests = prepare(tmp_path, 3)
    slots = ObservedSlots(3)
    monkeypatch.setattr(observation, "_PROVIDER_SLOTS", slots)
    both_started, release, lock = Event(), Event(), Lock()
    calls = []

    def transport(*args):
        with lock:
            calls.append(args)
            if len(calls) == 2:
                both_started.set()
        assert release.wait(5)
        return response()

    service = VisionService(api_key="fixture", transport=transport)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = [pool.submit(observation.analyze_observation, store, service, **request) for request in requests[:2]]
        try:
            assert both_started.wait(5)
            queued = pool.submit(observation.analyze_observation, store, service, **requests[2])
            assert slots.arrived.wait(5)
            store.cancel(requests[2]["operation_id"], reason="user_cancelled")
        finally:
            release.set()
        for future in first:
            future.result(timeout=5)
        with pytest.raises(VisionError, match="observation_not_extracting"):
            queued.result(timeout=5)
    assert len(calls) == 2
    assert not any(event.event_type in {"vision_analysis", "vision_request"}
        for event in store.get_events(requests[2]["operation_id"]))
    assert not observation._READINGS


def test_provider_queue_wait_is_bounded_and_never_counted_as_paid_call(tmp_path, monkeypatch):
    store, [request] = prepare(tmp_path)
    slots = BoundedSemaphore(2)
    monkeypatch.setattr(observation, "_PROVIDER_SLOTS", slots)
    monkeypatch.setattr(observation, "_PROVIDER_QUEUE_TIMEOUT_SECONDS", 0.02)
    assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
    service = VisionService(api_key="fixture", transport=lambda *_: response())
    try:
        with pytest.raises(VisionError, match="vision_queue_timeout"):
            observation.analyze_observation(store, service, **request)
    finally:
        slots.release()
        slots.release()
    events = store.get_events(request["operation_id"])
    assert not any(event.event_type == "vision_request" for event in events)
    failure = next(event for event in events if event.event_type == "vision_failure")
    assert failure.payload == {"code": "vision_queue_timeout", "provider_request_attempted": False,
        "diagnostics": [{"code": "provider_queue_timeout"}]}
    assert not observation._READINGS
    # A never-billed attempt may retry after resources become available.
    assert observation.analyze_observation(store, service, **request).text


def test_failure_after_unpaid_queue_timeout_retains_provider_diagnostics(tmp_path, monkeypatch):
    store, [request] = prepare(tmp_path)
    slots = BoundedSemaphore(2)
    monkeypatch.setattr(observation, "_PROVIDER_SLOTS", slots)
    monkeypatch.setattr(observation, "_PROVIDER_QUEUE_TIMEOUT_SECONDS", 0.02)
    slots.acquire()
    slots.acquire()

    def fail(*_):
        raise DeepSeekClientError("http_429")

    service = VisionService(api_key="fixture", transport=fail)
    try:
        with pytest.raises(VisionError, match="vision_queue_timeout"):
            observation.analyze_observation(store, service, **request)
    finally:
        slots.release()
        slots.release()
    with pytest.raises(VisionError, match="http_429"):
        observation.analyze_observation(store, service, **request)
    failures = [event.payload for event in store.get_events(request["operation_id"])
        if event.event_type == "vision_failure"]
    assert [item["code"] for item in failures] == ["vision_queue_timeout", "http_429"]
    assert failures[1]["provider_request_attempted"] is True
    assert failures[1]["diagnostics"] == [{"code": "provider_error", "attempt": 1}]


def test_duplicate_wait_timeout_does_not_interrupt_or_rebill_owner(tmp_path, monkeypatch):
    store, [request] = prepare(tmp_path)
    started, release = Event(), Event()
    calls = []

    def transport(*args):
        calls.append(args)
        started.set()
        assert release.wait(5)
        return response()

    service = VisionService(api_key="fixture", transport=transport)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(observation.analyze_observation, store, service, **request)
        try:
            assert started.wait(5)
            monkeypatch.setattr(observation, "_OBSERVATION_BUDGET_SECONDS", 0.02)
            with pytest.raises(VisionError, match="vision_duplicate_wait_timeout"):
                observation.analyze_observation(store, service, **request)
        finally:
            release.set()
        result = first.result(timeout=5)
    assert observation.analyze_observation(store, service, **request) == result
    assert len(calls) == 1 and not observation._READINGS


def test_provider_failures_release_slots_preserve_attempt_guard_and_digest(tmp_path, monkeypatch):
    store, requests = prepare(tmp_path, 3)
    slots = BoundedSemaphore(2)
    monkeypatch.setattr(observation, "_PROVIDER_SLOTS", slots)
    calls = []

    def fail(*args):
        calls.append(args)
        raise DeepSeekClientError("http_429")

    service = VisionService(api_key="fixture", transport=fail)
    for request in requests:
        with pytest.raises(VisionError, match="http_429"):
            observation.analyze_observation(store, service, **request)
        with pytest.raises(VisionError, match="vision_attempt_already_recorded"):
            observation.analyze_observation(store, service, **request)
        with pytest.raises(VisionError, match="observation_image_changed"):
            observation.analyze_observation(store, service, **{**request, "image_data_url": OTHER_IMAGE})
        failure = next(event for event in store.get_events(request["operation_id"])
            if event.event_type == "vision_failure")
        assert failure.payload["provider_request_attempted"] is True
        assert failure.payload["diagnostics"] == [{"code": "provider_error", "attempt": 1}]
    assert len(calls) == 3 and not observation._READINGS
    assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
    slots.release()
    slots.release()


def test_cached_reading_cannot_revive_cancelled_operation(tmp_path):
    store, [request] = prepare(tmp_path)
    calls = []
    service = VisionService(api_key="fixture", transport=lambda *args: calls.append(args) or response())
    observation.analyze_observation(store, service, **request)
    store.cancel(request["operation_id"], reason="user_cancelled")
    with pytest.raises(VisionError, match="observation_not_extracting"):
        observation.analyze_observation(store, service, **request)
    assert len(calls) == 1 and not observation._READINGS


def test_provider_deadline_is_bounded_without_mutating_shared_service(tmp_path, monkeypatch):
    store, [request] = prepare(tmp_path)
    monkeypatch.setattr(observation, "_OBSERVATION_BUDGET_SECONDS", 1.5)
    timeouts = []
    service = VisionService(api_key="fixture", timeout=90,
        transport=lambda *args: timeouts.append(args[3]) or response())
    observation.analyze_observation(store, service, **request)
    assert 0 < timeouts[0] <= 1.5
    assert service.timeout == 90 and not observation._READINGS
