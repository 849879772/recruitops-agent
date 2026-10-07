"""Page/visual fallback and cache recovery against temporary SQLite only."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from packages.browser_bridge import BrowserBridgeStore, OperationName, OperationStatus
from packages.storage import ApplicationSnapshot
from packages.tools import application_status_model as model
from packages.tools.application_status_evidence import VerifyApplicationStatusEvidenceInput, verify_application_status_evidence
from packages.domain.application_status_semantics import status_is_unasserted
from tests.test_batch_browser_operations import _repository


URL = "https://ats.example/applications"
TITLE = "软件工程师"


class PageClient:
    model = "deepseek-flash"

    def __init__(self, candidates):
        self.candidates, self.calls = candidates, []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=json.dumps({"candidates": self.candidates}, ensure_ascii=False),
                               model=self.model, input_tokens=123, output_tokens=45, cache_read_input_tokens=12)


def candidate(title=TITLE, app_id="24", label="笔试中", ref="page:text", quote=None, **extra):
    return {"application_id": app_id, "card_title": title, "observed_status": "written",
            "observed_label": label, "quotation": quote or f"{title} 当前状态: {label}",
            "uncertainties": [], "source_ref": ref, "current": True, "current_node_ref": None, **extra}


def case(tmp_path, monkeypatch, *, observation=None, candidates=None, applications=None):
    repository = _repository(tmp_path, applications or [{"id": "24", "title": TITLE, "record_url": URL}])
    store = BrowserBridgeStore(repository.storage)
    operation = store.create(OperationName.OBSERVE_APPLICATION_STATUS_PAGE, device_id="edge-fixture",
        idempotency_key="page-case", command={"application_id": "24", "application_ids": [str(app.id) for app in repository.list_applications()],
                                             "page_url": URL, "params": {"include_vision": True}})
    store.append_event(operation.operation_id, "extracting", OperationStatus.EXTRACTING)
    observed = {"page_url": URL, "captured_at": "2026-09-29T01:00:00Z", "application_records": [],
                "entries": [], "page": {"text": f"{TITLE} 当前状态: 笔试中"}, **(observation or {})}
    if "vision" in observed:
        store.append_event(operation.operation_id, "vision", OperationStatus.EXTRACTING,
                           observed["vision"], event_type="vision_analysis")
    store.append_event(operation.operation_id, "validating", OperationStatus.VALIDATING)
    store.terminal_result(operation.operation_id, observed, status=OperationStatus.SUCCEEDED)
    client = PageClient(candidates or [candidate()])
    monkeypatch.setattr(model, "configured_model_client", lambda _: client)
    def run(*, visual=False):
        applications = list(repository.list_applications())
        resolver = model.resolve_visual_statuses if visual else model.resolve_page_statuses
        return asyncio.run(resolver(store, operation.operation_id, applications, {str(app.id) for app in applications}))
    return repository, store, operation, client, run


def test_missing_cards_uses_persisted_page_text_and_audits_usage(tmp_path, monkeypatch):
    repository, store, operation, client, run = case(tmp_path, monkeypatch)
    row = run()["24"]
    assert row["state"] == "updated" and row["model_disposition"] == "called", row
    payload = json.loads(client.calls[0]["user_prompt"])
    assert payload["sources"][0]["text"] == f"{TITLE} 当前状态: 笔试中"
    result = next(event for event in store.get_events(operation.operation_id) if event.event_type == "status_model_result")
    assert result.payload["usage"] == {"input_tokens": 123, "output_tokens": 45, "cache_read_input_tokens": 12}
    assert result.payload["targets"] == ["24"]
    assert run()["24"]["model_disposition"] == "cache_hit" and len(client.calls) == 1
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "24").stage == "written"


def test_control_request_blocks_new_text_model_call_after_observation(tmp_path, monkeypatch):
    from packages.tools import application_review_tasks
    repository, store, operation, client, run = case(tmp_path, monkeypatch)
    monkeypatch.setattr(application_review_tasks, "review_dispatch_allowed", lambda: False)
    row = run()["24"]
    assert row["reason"] == "review_control_requested"
    assert row["model_disposition"] == "skipped"
    assert client.calls == []
    assert repository.list_applications()[0].stage == "applied"


@pytest.mark.parametrize("fault", ["invented_quote", "invented_title", "no_current", "whole_ladder", "foreign_quote"])
def test_page_proposals_cannot_invent_or_borrow_evidence(tmp_path, monkeypatch, fault):
    proposal = candidate()
    text = proposal["quotation"]
    if fault == "invented_quote": proposal["quotation"] += " 已录用"
    if fault == "invented_title": proposal["card_title"] = "产品经理"
    if fault == "no_current": proposal["current"] = False
    if fault == "whole_ladder":
        text = proposal["quotation"] = f"{TITLE} 申请成功 → 筛选 → 笔试中 → 面试 → offer"
    if fault == "foreign_quote":
        text = f"{TITLE} 投递于昨日 产品经理 当前状态: 笔试中"
        proposal["quotation"] = "产品经理 当前状态: 笔试中"
    repository, _, _, _, run = case(tmp_path, monkeypatch, observation={"page": {"text": text}}, candidates=[proposal])
    row = run()["24"]
    assert row["state"] == "unresolved" and not row.get("wrote"), row
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "24").stage == "applied"


@pytest.mark.parametrize("conflicting", [False, True])
def test_unknown_ladder_recovers_only_from_persisted_active_node(tmp_path, monkeypatch, conflicting):
    quote = f"{TITLE} 申请成功 筛选 笔试 面试"
    record = {"title": TITLE, "context": quote, "stage_labels": ["申请成功", "筛选", "笔试", "面试"],
              "signals": {"has_progress_timeline": True}, "label": "", "status": ""}
    nodes = [{"text": quote, "rect": {"x": 0, "y": 0, "width": 500, "height": 200}, "frameId": 0},
             {"text": "笔试", "attributes": {"aria-current": "step"},
              "rect": {"x": 100, "y": 50, "width": 40, "height": 20}, "frameId": 0}]
    if conflicting:
        nodes.append({"text": "面试", "attributes": {"aria-current": "step"},
                      "rect": {"x": 160, "y": 50, "width": 40, "height": 20}, "frameId": 0})
    _, _, _, _, run = case(tmp_path, monkeypatch, observation={"page": {"text": quote},
        "application_records": [record], "semantic_nodes": nodes},
        candidates=[candidate(label="笔试", quote=quote, ref="node:0", current_node_ref="node:1")])
    row = run()["24"]
    assert row["state"] == ("unresolved" if conflicting else "updated")
    if conflicting:
        assert row["reason"] == "status_evidence_conflict"


def test_other_frame_active_node_cannot_authorize_ladder(tmp_path, monkeypatch):
    quote = f"{TITLE} 申请成功 筛选 笔试 面试"
    nodes = [{"text": quote, "rect": {"x": 0, "y": 0, "width": 500, "height": 200}, "frameId": 0},
             {"text": "笔试", "attributes": {"aria-current": "step"},
              "rect": {"x": 100, "y": 50, "width": 40, "height": 20}, "frameId": 1}]
    _, _, _, _, run = case(tmp_path, monkeypatch, observation={"semantic_nodes": nodes},
        candidates=[candidate(label="笔试", quote=quote, ref="node:0", current_node_ref="node:1")])
    assert run()["24"]["reason"] == "record_present_status_unknown"


def test_visual_page_groups_keep_independent_record_sources(tmp_path, monkeypatch):
    second = "算法工程师"
    text = f"{TITLE} 当前状态: 笔试中\n{second} 当前状态: 面试中"
    vision = {"text": text, "confidence": 0.98, "model": "deepseek-flash", "image_sha256": "a" * 64, "usage": {}}
    _, _, _, _, run = case(tmp_path, monkeypatch, observation={"vision": vision},
        applications=[{"id": "24", "title": TITLE, "record_url": URL}, {"id": "25", "title": second, "record_url": URL}],
        candidates=[candidate(ref="vision:line:0"), candidate(second, "25", "面试中", "vision:line:1", observed_status="interview")])
    rows = run(visual=True)
    assert {row["state"] for row in rows.values()} == {"updated"}, rows


def test_source_api_cannot_bypass_persisted_identity_and_current_marker(tmp_path, monkeypatch):
    _, store, operation, _, _ = case(tmp_path, monkeypatch)
    response = verify_application_status_evidence(VerifyApplicationStatusEvidenceInput(
        application_id="24", observation_operation_id=operation.operation_id, observed_status="written",
        observed_label="笔试中", evidence=f"{TITLE} 当前状态: 笔试中", confidence=1,
        captured_at="2026-09-29T01:00:00Z", source_ref="node:999", source_title=TITLE, current=True), store)
    assert not response.success


@pytest.mark.parametrize("text,title,label,status", [
    ("高级软件工程师 当前状态: 笔试中", "软件工程师", "笔试中", "written"),
    (f"{TITLE} 当前状态: 审批排队", TITLE, "审批排队", "offer"),
    (f"{TITLE} 产品经理 当前状态: 笔试中", TITLE, "笔试中", "written"),
    (f"{TITLE} 当前状态: 笔试中 {TITLE} 当前状态: 面试中", TITLE, "笔试中", "written"),
])
def test_direct_source_verifier_rejects_partial_titles_unsupported_stages_and_cross_card(tmp_path, monkeypatch, text, title, label, status):
    _, store, operation, _, _ = case(tmp_path, monkeypatch, observation={"page": {"text": text}})
    response = verify_application_status_evidence(VerifyApplicationStatusEvidenceInput(
        application_id="24", observation_operation_id=operation.operation_id, observed_status=status,
        observed_label=label, evidence=text, confidence=1, captured_at="2026-09-29T01:00:00Z",
        source_ref="page:text", source_title=title, current=True), store)
    assert not response.success and response.status == "STATE_UNCLEAR"


def test_iframe_page_source_remains_independently_addressable(tmp_path, monkeypatch):
    _, _, _, client, run = case(tmp_path, monkeypatch, observation={"page": {"text": "网站导航"},
        "page_segments": [{"frameId": 7, "frameUrl": "https://ats.example/frame", "text": f"{TITLE} 当前状态: 笔试中"}]},
        candidates=[candidate(ref="frame:7:text")])
    assert run()["24"]["state"] == "updated"
    assert any(source["ref"] == "frame:7:text" for source in json.loads(client.calls[0]["user_prompt"])["sources"])


def test_submission_success_does_not_inherit_future_boilerplate_uncertainty(tmp_path, monkeypatch):
    from tests.test_application_status_model_fallback import _case, _card
    label = "简历投递成功，我们将尽快处理，若通过将安排面试"
    _, _, client, run, _ = _case(tmp_path, monkeypatch, cards=[_card(TITLE, label)])
    result = run()
    assert result.unchanged and not result.unresolved and not client.calls
    assert not status_is_unasserted(label, "applied")
    assert status_is_unasserted("如果简历投递成功，将安排面试", "applied")


def test_temporary_cache_retries_after_ttl_and_stops_at_bound(tmp_path, monkeypatch):
    _, _, _, client, run = case(tmp_path, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(model, "time", lambda: clock[0])
    def unavailable(**kwargs):
        client.calls.append(kwargs)
        raise model.DeepSeekClientError("transport_failed")
    client.complete_structured = unavailable
    assert run()["24"]["reason"] == "model_unavailable"
    assert run()["24"]["model_disposition"] == "cache_hit"
    assert len(client.calls) == 1
    for _ in range(4):
        clock[0] += model.MODEL_RETRY_TTL_SECONDS + 1
        run()
    assert len(client.calls) == model.MODEL_MAX_ATTEMPTS


def test_cancelled_claim_is_recoverable_without_parallel_recall(tmp_path, monkeypatch):
    _, store, operation, client, _ = case(tmp_path, monkeypatch)
    target = {"application_id": "24"}
    clock = [1000.0]
    monkeypatch.setattr(model, "time", lambda: clock[0])
    entered = asyncio.Event()
    release = asyncio.Event()
    async def fake_thread(*args, **kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(content=json.dumps({"candidates": [candidate()]}))
    monkeypatch.setattr(model.asyncio, "to_thread", fake_thread)
    async def run_both():
        first = asyncio.create_task(model.propose_page_statuses(store, operation.operation_id, [target], client=client))
        await entered.wait()
        second = await model.propose_page_statuses(store, operation.operation_id, [target], client=client)
        assert second[1] == "model_interrupted"
        first.cancel()
        with pytest.raises(asyncio.CancelledError): await first
    asyncio.run(run_both())
    monkeypatch.undo()
    monkeypatch.setattr(model, "time", lambda: clock[0] + model.MODEL_RETRY_TTL_SECONDS + 1)
    proposal, error = asyncio.run(model.propose_page_statuses(store, operation.operation_id, [target], client=client))
    assert not error and proposal is not None and len(client.calls) == 1


def test_orphan_claim_recovers_after_lease_without_repeating_early(tmp_path, monkeypatch):
    _, store, operation, client, _ = case(tmp_path, monkeypatch)
    clock = [1000.0]
    monkeypatch.setattr(model, "time", lambda: clock[0])
    append = store.append_event
    crashed = [False]
    def crash_result(*args, **kwargs):
        if kwargs.get("event_type") == "status_model_result" and not crashed[0]:
            crashed[0] = True
            raise RuntimeError("fixture result persistence crash")
        return append(*args, **kwargs)
    monkeypatch.setattr(store, "append_event", crash_result)
    def propose():
        return asyncio.run(model.propose_page_statuses(store, operation.operation_id, [{"application_id": "24"}], client=client))
    assert propose()[1] == "model_cache_unavailable"
    assert propose()[1] == "model_interrupted" and len(client.calls) == 1
    clock[0] += model.MODEL_RETRY_TTL_SECONDS + 1
    assert propose()[1] is None and len(client.calls) == 2


def test_cache_key_changes_with_model_and_prompt_version(tmp_path, monkeypatch):
    _, store, operation, client, _ = case(tmp_path, monkeypatch)
    def propose():
        return asyncio.run(model.propose_page_statuses(store, operation.operation_id, [{"application_id": "24"}], client=client))
    assert propose()[1] is None
    assert propose()[1] is None and len(client.calls) == 1
    client.model = "deepseek-v4-pro"
    assert propose()[1] is None and len(client.calls) == 2
    monkeypatch.setattr(model, "_VERSION", "fixture-new-prompt-version")
    assert propose()[1] is None and len(client.calls) == 3
