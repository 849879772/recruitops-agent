from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from packages.browser_bridge import BrowserBridgeStore, OperationName, OperationStatus
from packages.matching.client import DeepSeekClientError
from packages.storage import Storage
from packages.vision import VisionError, VisionService, image_digest
from packages.vision.observation import analyze_observation


IMAGE = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()


def response(text="Example Software Engineer: Applied", confidence=0.96):
    return {"choices": [{"finish_reason": "stop", "message": {
        "content": json.dumps({"text": text, "confidence": confidence})
    }}], "usage": {"prompt_tokens": 180, "completion_tokens": 20, "total_tokens": 200}}


def test_image_is_sent_as_real_content_and_thinking_is_disabled():
    calls = []

    def transport(endpoint, headers, payload, timeout):
        calls.append(payload)
        return response()

    result = VisionService(api_key="test", transport=transport).analyze(IMAGE)
    assert len(calls) == 1
    assert calls[0]["model"] == "deepseek-flash"
    assert calls[0]["messages"][0]["content"][1] == {"type": "image_url", "image_url": {"url": IMAGE}}
    assert calls[0]["thinking"] == {"type": "disabled"}
    assert result.image_sha256 == image_digest(IMAGE)
    assert result.usage["total_tokens"] == 200
    assert IMAGE not in result.model_dump_json()


@pytest.mark.parametrize("image", ["https://example.com/private.png", "data:image/svg+xml;base64,AAAA", "data:image/png;base64,@@@@", "data:image/png;base64,aGVsbG8="])
def test_bad_images_never_reach_provider(image):
    with pytest.raises(VisionError):
        VisionService(api_key="test", transport=lambda *_: pytest.fail("network")).analyze(image)


def test_size_and_text_only_model_rejected_before_request():
    with pytest.raises(VisionError, match="image_too_large"):
        image_digest(IMAGE, 3)
    with pytest.raises(VisionError, match="vision_model_unsupported"):
        VisionService(api_key="test", model="text-only-unsupported", transport=lambda *_: pytest.fail("network")).analyze(IMAGE)


@pytest.mark.parametrize("raw", [{}, response(""), response(confidence=5), {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}])
def test_invalid_output_fails_closed_without_retries(raw):
    calls = []
    def transport(*args):
        calls.append(args)
        return raw
    with pytest.raises(VisionError):
        VisionService(api_key="test", transport=transport).analyze(IMAGE)
    assert len(calls) == 1


def prepare(tmp_path, include_vision=True):
    store = BrowserBridgeStore(Storage.from_url(f"sqlite:///{tmp_path / 'vision.db'}", initialize=True))
    operation = store.create(
        OperationName.OBSERVE_APPLICATION_STATUS_PAGE, device_id="edge-test", idempotency_key="vision-1",
        command={"application_id": "24", "page_url": "https://example.com/applications", "params": {"include_vision": include_vision}},
    )
    store.append_event(operation.operation_id, "extracting", OperationStatus.EXTRACTING)
    kwargs = {"operation_id": operation.operation_id, "page_url": "https://example.com/applications", "image_data_url": IMAGE}
    return store, kwargs


def test_repeat_operation_reuses_persisted_reading_and_rejects_another_image(tmp_path):
    store, kwargs = prepare(tmp_path)
    calls = []
    service = VisionService(api_key="test", transport=lambda *args: calls.append(args) or response())
    first = analyze_observation(store, service, **kwargs)
    assert analyze_observation(store, service, **kwargs) == first
    assert len(calls) == 1
    kwargs["image_data_url"] = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"1" * 20).decode()
    with pytest.raises(VisionError, match="observation_image_changed"):
        analyze_observation(store, service, **kwargs)
    assert len(calls) == 1


def test_failed_provider_attempt_is_not_automatically_rebilled(tmp_path):
    store, kwargs = prepare(tmp_path)
    calls = []
    def fail(*args):
        calls.append(args)
        raise DeepSeekClientError("http_429")
    service = VisionService(api_key="test", transport=fail)
    with pytest.raises(VisionError, match="http_429"):
        analyze_observation(store, service, **kwargs)
    with pytest.raises(VisionError, match="vision_attempt_already_recorded"):
        analyze_observation(store, service, **kwargs)
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["wrong_url", "disabled", "captcha"])
def test_unbound_or_blocked_observation_never_sends_image(tmp_path, change):
    store, kwargs = prepare(tmp_path, include_vision=change != "disabled")
    if change == "wrong_url":
        kwargs["page_url"] = "https://other.example.com/applications"
    if change == "captcha":
        store.append_event(kwargs["operation_id"], "login", OperationStatus.WAITING_FOR_LOGIN)
    with pytest.raises(VisionError):
        analyze_observation(store, VisionService(api_key="test", transport=lambda *_: pytest.fail("network")), **kwargs)


def test_api_route_removes_old_endpoint_and_requires_auth(monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api import main
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(api_token="local-secret", vision_enabled=True))
    client = TestClient(main.app)
    request = {"operation_id": "observation", "page_url": "https://example.com/applications", "image_data_url": IMAGE}
    assert "/api/browser/ocr" not in main.app.openapi()["paths"]
    assert client.post("/api/browser/ocr", json=request).status_code in {404, 405}
    assert client.post("/api/browser/vision", json=request).status_code == 401


def test_authenticated_api_binds_and_reuses_one_reading(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from apps.api import main
    store, request = prepare(tmp_path)
    calls = []
    service = VisionService(api_key="test", transport=lambda *args: calls.append(args) or response())
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(api_token="local-secret", vision_enabled=True))
    monkeypatch.setattr(main, "browser_bridge_store", store)
    monkeypatch.setattr(main, "browser_vision_service", lambda: service)
    client = TestClient(main.app)
    headers = {"Authorization": "Bearer local-secret"}
    first = client.post("/api/browser/vision", json=request, headers=headers)
    assert first.status_code == 200
    second = client.post("/api/browser/vision", json=request, headers=headers)
    assert second.status_code == 200 and second.json() == first.json()
    assert len(calls) == 1
    bad = client.post("/api/browser/vision", json={**request, "page_url": "https://wrong.example/"}, headers=headers)
    assert bad.status_code == 422 and len(calls) == 1
