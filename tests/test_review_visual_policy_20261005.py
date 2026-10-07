"""Isolated policy regressions: no live browser, formal data or remote model."""
import asyncio
from types import SimpleNamespace

import pytest

from packages import config
from packages.browser_bridge import BrowserBridgeStore
from packages.storage.models import ApplicationSnapshot, ToolCall
from packages.tools import application_review_run as review
from packages.tools import application_status_model as model
from packages.tools import batch_browser_operations as batch
from tests.test_application_review_run import _part, _repository as review_repository
from tests.test_batch_browser_operations import _observed, _repository


def _vision_settings(monkeypatch):
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(
        write_enabled=True, llm_enabled=True, llm_api_key="fixture", vision_enabled=True,
        vision_model="deepseek-flash"))


@pytest.mark.parametrize("mode", ["clear_unchanged", "stage_change", "truncated", "missing_volunteer",
                                 "enum_mismatch", "generic_in_progress", "card_truncated", "low_confidence",
                                 "low_confidence_applied"])
def test_visual_audit_is_required_for_changes_and_suspect_coverage(tmp_path, monkeypatch, mode):
    _vision_settings(monkeypatch)
    url = "https://ats.example/applications"
    repository = _repository(tmp_path, [{"id": "1", "title": "AI应用工程师", "record_url": url}])
    status = "written" if mode in {"stage_change", "low_confidence"} else "applied"
    if mode == "low_confidence":
        with repository.storage.write_transaction() as session:
            session.get(ApplicationSnapshot, "1").stage = "written"
    label = "笔试中" if status == "written" else "已投递"
    if mode == "enum_mismatch":
        label = "笔试中"
    if mode == "generic_in_progress":
        label = "流程中"
    card = {"title": "AI应用工程师", "status": status, "label": label,
            "context": f"AI应用工程师 当前状态: {label}", "confidence": .1 if mode.startswith("low_confidence") else .97,
            "signals": {"context_truncated": mode == "card_truncated"}}
    page_text = card["context"]
    if mode == "missing_volunteer":
        page_text += "\n志愿1：AI应用工程师\n志愿2：测试工程师"
    observation = {"page": {"url": url, "text": page_text}, "application_records": [card],
                   "entries": [card], "diagnostics": {"textCoverage": {"truncated": mode == "truncated"}}}
    calls = []

    async def observe(request, *_):
        calls.append(request.include_vision)
        return _observed({**observation, **({"vision": {"text": card["context"]}} if request.include_vision else {})},
                         "visual" if request.include_vision else "dom")

    async def visual(_store, op, _applications, targets):
        assert op == "visual" and targets == {"1"}
        # DOM proposals must not be written before the visual verifier runs.
        assert repository.list_applications()[0].stage == ("written" if mode == "low_confidence" else "applied")
        return {"1": {"state": "unresolved", "reason": "model_uncertain", "wrote": False}}

    async def no_text(*_):
        pytest.fail("visual policy must not call the text-only proposal path first")

    monkeypatch.setattr(batch, "observe_application_status_page_workflow", observe)
    monkeypatch.setattr(model, "resolve_visual_statuses", visual)
    monkeypatch.setattr(model, "resolve_page_statuses", no_text)
    result = asyncio.run(batch.batch_observe_application_status(batch.BatchObserveApplicationStatusInput(
        application_ids=["1"]), object(), repository))
    if mode == "clear_unchanged":
        assert calls == [False] and len(result.unchanged) == 1
    else:
        assert calls == [False, True] and len(result.unresolved) == 1 and not result.updated
    assert repository.list_applications()[0].stage == ("written" if mode == "low_confidence" else "applied")


@pytest.mark.parametrize("error", ["UNPARSED_APPLICATION_PAGE", "STATE_UNCLEAR"])
def test_stable_parser_failure_with_readable_diagnostics_enters_image_review(tmp_path, monkeypatch, error):
    _vision_settings(monkeypatch)
    url = "https://ats.example/applications"
    repository = _repository(tmp_path, [{"id": "1", "title": "软件工程师", "record_url": url}])
    calls = []

    async def observe(request, *_):
        calls.append(request.include_vision)
        if not request.include_vision:
            return SimpleNamespace(data=SimpleNamespace(observation=None, error_code=error,
                operation_id="unparsed", status="STATE_UNCLEAR",
                result={"page": {"url": url, "text": "软件工程师 投递记录"}, "semantic_nodes": []}))
        return _observed({"page": {"url": url}, "vision": {"text": "软件工程师 已投递"}}, "visual")

    async def visual(_store, op, _apps, targets):
        assert op == "visual" and targets == {"1"}
        return {"1": {"state": "unchanged", "reason": "same_stage", "wrote": False}}

    async def no_text(*_):
        pytest.fail("stable extraction failure must go directly to images")

    monkeypatch.setattr(batch, "observe_application_status_page_workflow", observe)
    monkeypatch.setattr(model, "resolve_visual_statuses", visual)
    monkeypatch.setattr(model, "resolve_page_statuses", no_text)
    result = asyncio.run(batch.batch_observe_application_status(batch.BatchObserveApplicationStatusInput(
        application_ids=["1"]), object(), repository))
    assert calls == [False, True] and len(result.unchanged) == 1 and not result.updated
    assert repository.list_applications()[0].stage == "applied"


def test_visual_current_step_is_not_skipped_by_generic_dom_label(tmp_path, monkeypatch):
    from tests.test_application_page_model_fallback import case, candidate, URL
    from tests.test_review_whole_card_evidence import reading
    title = "AI应用工程师"
    dom = {"title": title, "label": "流程中", "status": "applied",
           "context": f"{title}\n流程中", "raw_status_labels": ["流程中"]}
    image_text = f"{title}\n笔试中"
    repository, _, _, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": title, "record_url": URL}],
        observation={"application_records": [dom], "vision": reading(title, image_text, "笔试中")},
        candidates=[candidate(title, label="笔试中", ref="vision:card:0", quote=image_text)])
    result = run(visual=True)["24"]
    assert result["state"] == "updated" and result["wrote"], result
    assert len(client.calls) == 1 and repository.list_applications()[0].stage == "written"


def test_explicit_retry_routes_to_frozen_checkpoint_and_does_not_repeat_completed_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "_WAVE_PAGES", 10)
    _vision_settings(monkeypatch)
    repository = review_repository(tmp_path, 15)
    bridge = BrowserBridgeStore(repository.storage)
    visited = []

    async def page(request, *_):
        visited.extend(request.application_ids)
        return _part(request, {key: ("unchanged", None) for key in request.application_ids})

    monkeypatch.setattr(review, "batch_observe_application_status", page)

    async def run():
        selected = [str(i) for i in range(12)]
        first = await batch.batch_observe_application_status(batch.BatchObserveApplicationStatusInput(
            application_ids=selected), bridge, repository)
        assert first.summary["continuation_required"] and first.summary["processed_count"] == 10
        run_id = first.summary["run_id"]
        with repository.storage.session() as session:
            state = session.get(ToolCall, run_id).arguments
            assert state["ids"] == selected and state["selection"] == "explicit_subset"
            assert len(state["results"]) == 10
        second = await batch.batch_observe_application_status(batch.BatchObserveApplicationStatusInput(
            run_id=run_id), bridge, repository)
        assert second.summary["remaining_count"] == 0 and second.summary["scope_total"] == 12
        assert second.summary["run_id"] == run_id
    asyncio.run(run())
    assert len(visited) == len(set(visited)) == 12
    assert set(visited) == {str(i) for i in range(12)}


def test_explicit_checkpoint_does_not_silently_drop_unknown_or_terminal_ids(tmp_path, monkeypatch):
    _vision_settings(monkeypatch)
    repository = review_repository(tmp_path, 1)
    with repository.storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "0").stage = "rejected"
    result = asyncio.run(batch.batch_observe_application_status(batch.BatchObserveApplicationStatusInput(
        application_ids=["missing", "0"]), BrowserBridgeStore(repository.storage), repository))
    assert result.total == 2
    assert [(item.application_id, item.reason) for item in result.failed] == [("missing", "application_not_found")]
    assert [(item.application_id, item.reason) for item in result.excluded] == [("0", "terminal_stage_excluded")]
    assert result.pages_total == 0


def test_observation_wait_timeout_retains_operation_and_safe_navigation_identity(tmp_path, monkeypatch):
    repository = _repository(tmp_path, [{"id": "1", "title": "AI应用工程师", "record_url": "https://ats.example/applications"}])
    operation = SimpleNamespace(operation_id="timed-read", result={"navigation_diagnostics": {
        "reason": "will_redirect_official_sso", "phase": "navigation_recovery",
        "finalUrl": "https://uniportal.huawei.com/uniportal/?ticket=SECRET",
        "authNavigation": {"provider": "huawei", "hops": 2, "returnedToRecruitment": False},
        "pageText": "PRIVATE FORM"}})
    lookups = []

    def lookup(key):
        lookups.append(key)
        return operation

    async def timeout(*_):
        raise asyncio.TimeoutError

    monkeypatch.setattr(batch, "observe_application_status_page_workflow", timeout)
    result = asyncio.run(batch.batch_observe_application_status(batch.BatchObserveApplicationStatusInput(
        application_ids=["1"]), SimpleNamespace(get_by_idempotency_key=lookup), repository))
    assert len(lookups) == 1 and lookups[0].startswith("batch-status-")
    row = result.failed[0]
    assert row.operation_id == "timed-read" and row.reason == "observation_timeout"
    nav = row.diagnostics["navigation_diagnostics"]
    assert nav["authNavigation"]["hops"] == 2
    assert "SECRET" not in str(nav) and "PRIVATE FORM" not in str(nav)
