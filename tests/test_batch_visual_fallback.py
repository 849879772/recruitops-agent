from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from packages.tools import application_status_model as model
from packages.tools import batch_browser_operations as batch
from packages.tools.application_review_summary import review_result_presentation
from test_batch_browser_operations import _repository, _observed


@pytest.mark.parametrize("mode", [
    "success", "opt_out", "blank", "image_only", "restricted", "model_failed", "vision_failed", "cancel",
    "invalid_output", "semantics", "identity_ambiguous", "unsupported", "not_configured", "settings_disabled", "budget",
])
def test_batch_only_captures_unresolved_readable_page_once(tmp_path, monkeypatch, mode):
    from packages import config
    url = "https://ats.example/applications"
    repository = _repository(tmp_path, [
        {"id": "1", "title": "软件工程师", "record_url": url},
        {"id": "2", "title": "算法工程师", "record_url": url},
    ])
    calls = []
    observation = {"page": {"url": url, "text": "" if mode in {"blank", "image_only"} else "软件工程师 算法工程师"},
                   "application_records": [], "semantic_nodes": [], "entries": [],
                   "diagnostics": {"scopeDeniedFrameCount": 1 if mode == "restricted" else 0,
                                   "visibleMediaCount": 1 if mode == "image_only" else 0}}
    if mode == "identity_ambiguous":
        observation["application_records"] = [{"title": "软件工程师", "context": "软件工程师 流程中"}] * 2
    async def observe(request, *_args):
        calls.append(request)
        if request.include_vision:
            return _observed({**observation, **({"vision_error": "http_503"} if mode == "vision_failed" else
                {"vision": {"text": "软件工程师 当前状态: 已投递\n算法工程师 当前状态: 已投递"}})}, "visual-op")
        return _observed(observation, "dom-op")

    async def text_review(_store, _operation, _applications, targets):
        pytest.fail("readable rule doubts must request screenshots, not a text-only provider first")

    async def visual_review(_store, operation, applications, targets):
        assert operation == "visual-op" and len(applications) == 2
        reason = {"model_failed": "model_unavailable", "invalid_output": "model_invalid_output",
                  "semantics": "status_semantics_unsupported", "identity_ambiguous": "target_record_ambiguous"}.get(mode)
        if reason:
            return {key: {"state": "unresolved", "reason": reason, "wrote": False,
                          "model_disposition": "called"} for key in targets}
        return {key: {"state": "unchanged", "reason": "same_stage", "observed_status": "applied", "wrote": False,
                      "model_disposition": "model_called"} for key in targets}

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(write_enabled=True, llm_enabled=True,
        llm_api_key="" if mode == "not_configured" else "fixture", vision_enabled=mode != "settings_disabled",
        vision_model="text-only-unsupported" if mode == "unsupported" else "deepseek-flash"))
    monkeypatch.setattr(batch, "observe_application_status_page_workflow", observe)
    monkeypatch.setattr(model, "resolve_page_statuses", text_review)
    monkeypatch.setattr(model, "resolve_visual_statuses", visual_review)
    if mode == "cancel":
        from packages.tools import application_review_tasks
        # The task is cancelled after the first DOM read, before its image fallback.
        monkeypatch.setattr(application_review_tasks, "review_dispatch_allowed", lambda: not calls)
    def events(_operation):
        result = [SimpleNamespace(event_type="vision_request", payload={
            "provider_request_attempted": True, "image_count": 2})]
        if mode != "vision_failed":
            result.append(SimpleNamespace(event_type="vision_analysis", payload={}))
        return result
    store = SimpleNamespace(get_events=events)
    token = model.MODEL_WAVE_DEADLINE.set(batch.perf_counter() + 5 if mode == "budget" else None)
    try:
        result = asyncio.run(batch.batch_observe_application_status(batch.BatchObserveApplicationStatusInput(
            application_ids=["1", "2"], include_vision=mode != "opt_out"), store, repository))
    finally:
        model.MODEL_WAVE_DEADLINE.reset(token)
    expected = 2 if mode in {"success", "image_only", "vision_failed", "model_failed", "invalid_output", "semantics", "identity_ambiguous"} else 1
    assert len(calls) == expected
    assert calls[0].include_vision is False
    if expected == 2:
        assert calls[1].include_vision is True
        assert calls[1].application_ids == ["1", "2"]
        assert calls[1].timeout_ms <= 70000
        assert calls[1].reuse_observation_operation_id == "dom-op"
    if mode in {"success", "image_only"}:
        assert len(result.unchanged) == 2
        assert result.summary["vision_record_dispositions"] == {"analyzed": 2}
        assert result.summary["vision_provider_request_count"] == 1
        assert result.summary["vision_analysis_count"] == 1
        assert result.summary["vision_image_count"] == 2
    if mode in {"model_failed", "invalid_output", "semantics", "identity_ambiguous"}:
        assert len(result.unresolved) == 2 and not result.updated
        assert all(not row.wrote for row in result.unresolved)
        assert result.summary["vision_provider_request_count"] == 1
        assert result.summary["vision_analysis_count"] == 1
    if mode == "vision_failed":
        assert len(result.unresolved) == 2
        assert all(row.vision_disposition == "http_503" for row in result.unresolved)
        assert result.summary["retained_count"] == 0
        assert result.summary["attention_required_count"] == 2
        assert result.summary["vision_provider_request_count"] == 1
        assert result.summary["vision_analysis_count"] == 0
    dispositions = {"opt_out": "disabled_by_request", "blank": "skipped_blank_page",
                    "restricted": "skipped_frame_restricted", "cancel": "skipped_task_stopped",
                    "unsupported": "vision_model_unsupported", "not_configured": "not_configured",
                    "settings_disabled": "disabled_in_settings",
                    "budget": "budget_deferred"}
    if mode in dispositions:
        assert result.summary["vision_record_dispositions"] == {dispositions[mode]: 2}
        assert result.summary["vision_provider_request_count"] == 0
        assert result.summary["vision_analysis_count"] == 0
        assert result.summary["vision_image_count"] == 0


@pytest.mark.parametrize("reason", ["CAPTCHA_REQUIRED", "LOGIN_REQUIRED", "DESKTOP_NAVIGATION_CHANGED", "APPLICATION_PAGE_UNAVAILABLE"])
def test_unavailable_or_auth_page_never_captures_images(tmp_path, monkeypatch, reason):
    url = "https://ats.example/applications"
    repository = _repository(tmp_path, [{"id": "1", "title": "软件工程师", "record_url": url}])
    calls = []

    async def observe(request, *_args):
        calls.append(request)
        result = _observed({}, "failed-op")
        result.data.observation = None
        result.data.error_code = reason
        return result

    async def model_forbidden(*_args):
        pytest.fail("unavailable/auth pages must not reach text or screenshot models")

    monkeypatch.setattr(batch, "observe_application_status_page_workflow", observe)
    monkeypatch.setattr(model, "resolve_page_statuses", model_forbidden)
    monkeypatch.setattr(model, "resolve_visual_statuses", model_forbidden)
    result = asyncio.run(batch.batch_observe_application_status(batch.BatchObserveApplicationStatusInput(
        application_ids=["1"]), object(), repository))
    assert len(calls) == 1 and calls[0].include_vision is False
    assert result.summary["vision_provider_request_count"] == 0


def test_vision_fault_is_not_silently_presented_as_retained():
    row = batch.ApplicationStatusResult(application_id="1", state="unresolved", reason="model_uncertain",
        vision_disposition="vision_timeout", elapsed_ms=1)
    assert review_result_presentation(row).presentation_state is None
