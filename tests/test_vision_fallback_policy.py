from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from apps.api.automation import CodexAutomationExecutor
from packages.mcp.server import TOOL_DEFINITIONS
from packages.tools.batch_browser_operations import BatchObserveApplicationStatusInput
from packages.tools.browser_bridge import ObserveApplicationStatusPageInput


def _single_observation(**overrides: object) -> ObserveApplicationStatusPageInput:
    values: dict[str, object] = {
        "application_id": "24",
        "application_url": "https://ats.example/applications/24",
        "idempotency_key": "vision-policy-test-24",
    }
    values.update(overrides)
    return ObserveApplicationStatusPageInput(**values)


def test_batch_status_review_is_dom_only() -> None:
    request = BatchObserveApplicationStatusInput(application_ids=["24"])

    assert request.include_vision is False
    with pytest.raises(ValidationError):
        BatchObserveApplicationStatusInput(application_ids=["24"], include_vision=True)


def test_individual_vision_requires_an_explicit_fallback_judgment() -> None:
    with pytest.raises(ValidationError):
        _single_observation(include_vision=True)

    request = _single_observation(
        include_vision=True,
        vision_fallback_reason="no_structured_evidence_visible_status_likely",
    )
    assert request.include_vision is True
    assert request.vision_fallback_reason == "no_structured_evidence_visible_status_likely"


def test_individual_vision_uses_a_cumulative_default_timeout() -> None:
    request = _single_observation(
        include_vision=True,
        vision_fallback_reason="no_structured_evidence_visible_status_likely",
    )
    assert request.timeout_ms == 120_000

    explicit = _single_observation(
        include_vision=True,
        vision_fallback_reason="no_structured_evidence_visible_status_likely",
        timeout_ms=45_000,
    )
    assert explicit.timeout_ms == 45_000
    assert _single_observation().timeout_ms == 45_000


def test_scheduled_application_review_explicitly_prohibits_vision() -> None:
    prompt = CodexAutomationExecutor._prompt(SimpleNamespace(
        task_id="application_progress",
        target_id="24",
        target_label="示例公司 / 软件开发工程师",
    ))

    assert "include_vision=false" in prompt
    assert "禁止调用视觉分析" in prompt
    assert "include_vision=true" not in prompt


def test_mcp_descriptions_expose_the_two_step_vision_policy() -> None:
    definitions = {definition.name: definition for definition in TOOL_DEFINITIONS}

    assert "Start with include_vision=false" in definitions["observe_application_status_page"].description
    assert "vision_fallback_reason" in definitions["observe_application_status_page"].description
    assert "never starts vision analysis" in definitions["batch_observe_application_status"].description
