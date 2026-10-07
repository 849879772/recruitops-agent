"""Capacity pressure is a retryable service fault, not invalid user evidence."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.api import main
from packages.vision.service import VisionError
from test_vision_service import IMAGE


@pytest.mark.parametrize("code", ["vision_queue_timeout", "vision_duplicate_wait_timeout"])
def test_vision_capacity_errors_preserve_reason_and_service_status(monkeypatch, code):
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(
        api_token="local-fixture", vision_enabled=True, llm_enabled=True, write_enabled=True))
    monkeypatch.setattr(main, "browser_vision_service", lambda: object())

    def overloaded(*_args, **_kwargs):
        raise VisionError(code)

    monkeypatch.setattr(main, "analyze_observation", overloaded)
    response = TestClient(main.app).post("/api/browser/vision",
        headers={"Authorization": "Bearer local-fixture"},
        json={"operation_id": "fixture", "page_url": "https://example.test/applications",
              "image_data_url": IMAGE})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == code
