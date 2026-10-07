"""Deterministic evidence selection and one bounded structural-repair regression."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from packages.tools import application_status_model as model
from packages.tools.application_status_evidence import VerifyApplicationStatusEvidenceInput, verify_application_status_evidence
from tests.test_application_status_model_fallback import FakeClient, _card, _case


def test_model_selects_server_card_without_retyping_quotation(tmp_path, monkeypatch):
    class Selector(FakeClient):
        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            targets = json.loads(kwargs["user_prompt"])["targets"]
            return SimpleNamespace(content=json.dumps({"candidates": [{
                "application_id": item["application_id"], "card_title": item["card_title"],
                "observed_status": "interview", "observed_label": item["label"],
                "evidence_ref": item["evidence_options"][0]["ref"], "uncertainties": [],
            } for item in targets]}, ensure_ascii=False))
    _, _, client, run, _ = _case(tmp_path, monkeypatch, client=Selector())
    assert len(run().updated) == 2
    assert len(client.calls) == 1


def test_wrong_card_selector_cannot_borrow_evidence(tmp_path, monkeypatch):
    def alter(proposal):
        proposal["candidates"][0].update(evidence_ref="card:1", quotation="")
        return proposal
    _, _, _, run, _ = _case(tmp_path, monkeypatch, client=FakeClient(alter))
    result = run()
    assert [(row.application_id, row.reason) for row in result.unresolved] == [("24", "model_evidence_ref_invalid")]
    assert [row.application_id for row in result.updated] == ["25"]


def test_layout_normalized_quote_is_restored_before_model_and_direct_verification(tmp_path, monkeypatch):
    card = _card("AI应用工程师（深圳）")
    card["context"] = "AI应用工程师（深圳）\n 当前状态: 面试安排确认中"
    def alter(proposal):
        candidate = proposal["candidates"][0]
        candidate["quotation"] = "AI应用工程师(深圳)当前状态:面试安排确认中"
        return proposal
    _, store, _, run, operations = _case(tmp_path, monkeypatch, cards=[card], client=FakeClient(alter))
    assert len(run().updated) == 1
    result = verify_application_status_evidence(VerifyApplicationStatusEvidenceInput(
        application_id="24", observation_operation_id=operations[0], observed_status="interview",
        observed_label="面试安排确认中", evidence="AI应用工程师(深圳)当前状态:面试安排确认中", confidence=.95,
        captured_at="2026-09-28T01:00:00Z"), store)
    assert result.success, result


def test_future_caveat_does_not_veto_current_evidence(tmp_path, monkeypatch):
    def alter(proposal):
        proposal["candidates"][0]["uncertainties"] = ["未来是否进入下一轮面试尚不确定"]
        return proposal
    _, _, _, run, _ = _case(tmp_path, monkeypatch, cards=[_card("AI应用工程师")], client=FakeClient(alter))
    assert len(run().updated) == 1


@pytest.mark.parametrize("note", ["当前阶段不确定，后续可能安排面试", "未来岗位对应关系可能有歧义", "含义不确定"])
def test_decisive_uncertainty_is_still_blocked(note):
    candidate = model.StatusCandidate(application_id="24", card_title="AI应用工程师", observed_status="interview",
        observed_label="面试中", quotation="AI应用工程师 当前状态: 面试中", uncertainties=[note])
    assert model._candidate_uncertain(candidate)


def test_one_repair_recovers_malformed_target_without_reprocessing_good_sibling(tmp_path, monkeypatch):
    client = FakeClient()
    def alter(proposal):
        if len(client.calls) == 1:
            proposal["candidates"][0].pop("observed_label")
        return proposal
    client.transform = alter
    _, store, _, run, operations = _case(tmp_path, monkeypatch, client=client)
    assert len(run().updated) == 2
    assert len(client.calls) == 2
    assert [item["application_id"] for item in json.loads(client.calls[1]["user_prompt"])["targets"]] == ["24"]
    event = next(item for item in store.get_events(operations[0]) if item.event_type == "status_model_result")
    assert event.payload["candidate_errors"] == {}
    assert event.payload["validation_diagnostics"][0]["loc"] == ["observed_label"]


def test_repair_timeout_keeps_good_sibling_and_caches_target_failure(tmp_path, monkeypatch):
    client = FakeClient(lambda proposal: {"candidates": [{**proposal["candidates"][0], "unknown_field": True},
                                                        *proposal["candidates"][1:]]})
    original = model.asyncio.to_thread
    calls = 0
    async def limited_thread(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.TimeoutError
        return await original(*args, **kwargs)
    monkeypatch.setattr(model.asyncio, "to_thread", limited_thread)
    _, _, _, run, _ = _case(tmp_path, monkeypatch, client=client)
    result = run()
    assert [row.application_id for row in result.updated] == ["25"]
    assert [(row.application_id, row.reason) for row in result.unresolved] == [("24", "model_timeout")]


def test_parser_diagnostics_never_store_untrusted_value_or_context():
    raw = json.dumps({"candidates": [{"application_id": "24", "card_title": "private title",
        "observed_status": "not-a-stage SECRET", "observed_label": "private label", "quotation": "private mail",
        "uncertainties": []}]})
    valid, errors, diagnostics = model._parse_candidates(raw, [{"application_id": "24"}])
    assert not valid and errors == {"24": "candidate_schema_invalid"}
    assert diagnostics == [{"application_id": "24", "type": "literal_error", "loc": ["observed_status"]}]


def test_json_failure_is_repaired_once_only(tmp_path, monkeypatch):
    class Broken(FakeClient):
        def complete_structured(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(content="not JSON private content")
    _, store, client, run, operations = _case(tmp_path, monkeypatch, client=Broken())
    result = run()
    assert len(result.unresolved) == 2 and not result.updated
    assert len(client.calls) == 2
    run()
    assert len(client.calls) == 2
    event = next(item for item in store.get_events(operations[0]) if item.event_type == "status_model_result")
    assert event.payload["invocation_count"] == 2
    assert "private content" not in json.dumps(event.payload)


def _visual_selector(title="AI应用工程师", label="筛选中", **updates):
    values = dict(application_id="24", card_title=title, observed_status="applied", observed_label=label,
                  evidence_ref="source:0", quotation="", current=True, uncertainties=[])
    values.update(updates)
    return model.StatusCandidate(**values)


def _target_option(title, text, source_ref="vision:card:0", **metadata):
    return {"application_id": "24", "card_title": title, "evidence_mode": "vision",
            "evidence_options": [{"ref": "source:0", "source_ref": source_ref, "text": text, **metadata}]}


@pytest.mark.parametrize(("company", "title", "label"), [
    ("OPPO", "AI应用工程师", "筛选中"),
    ("沐瞳科技", "AIOps工程师", "初筛中"),
    ("汇川技术", "软件工程师", "待评估"),
])
def test_visual_card_selector_accepts_its_own_redundant_line(company, title, label):
    text = f"{title}\n{company}\n{label}"
    sources = [
        {"ref": "vision:card:0", "text": text, "title": title, "scoped": True},
        {"ref": "vision:line:2", "text": label, "parent_ref": "vision:card:0"},
    ]
    candidate, error = model._restore_candidate(_visual_selector(title, label, source_ref="vision:line:2"),
                                                _target_option(title, text), sources)
    assert error is None
    assert candidate.source_ref == "vision:card:0" and candidate.quotation == text


def test_dom_card_selector_accepts_active_child_but_not_another_card():
    title, text = "AIOps工程师", "AIOps工程师 当前状态: 初筛中"
    sources = [
        {"ref": "node:0", "text": text, "rect": {"x": 0, "y": 0, "width": 100, "height": 100}},
        {"ref": "node:1", "text": "初筛中", "rect": {"x": 10, "y": 10, "width": 40, "height": 10}},
        {"ref": "node:2", "text": "初筛中", "rect": {"x": 10, "y": 200, "width": 40, "height": 10}},
    ]
    target = _target_option(title, text, "node:0")
    target["evidence_mode"] = "page"
    candidate, error = model._restore_candidate(_visual_selector(title, "初筛中", source_ref="node:1"), target, sources)
    assert error is None and candidate.source_ref == "node:0" and candidate.current_node_ref == "node:1"
    _, error = model._restore_candidate(_visual_selector(title, "初筛中", source_ref="node:2"), target, sources)
    assert error == "model_evidence_ref_invalid"


def test_redundant_selector_from_other_visual_card_remains_rejected_even_same_badge():
    title, text = "AI应用工程师", "AI应用工程师\n筛选中"
    sources = [
        {"ref": "vision:card:0", "text": text, "scoped": True},
        {"ref": "vision:card:1", "text": "软件工程师\n筛选中", "scoped": True},
        {"ref": "vision:line:5", "text": "筛选中", "parent_ref": "vision:card:1"},
    ]
    _, error = model._restore_candidate(_visual_selector(source_ref="vision:line:5"), _target_option(title, text), sources)
    assert error == "model_evidence_ref_invalid"


def test_evidence_options_prefer_complete_visual_card_then_block_never_title_line():
    app = SimpleNamespace(id="24", job_title="AI应用工程师", job_id=None)
    sources = [
        {"ref": "vision:line:0", "text": "AI应用工程师"},
        {"ref": "vision:block:0", "text": "AI应用工程师\n初筛中", "scoped": "legacy_block"},
        {"ref": "node:0", "text": "AI应用工程师 当前状态: 初筛中"},
        {"ref": "vision:card:0", "text": "AI应用工程师\n初筛中", "title": "AI应用工程师", "current_label": "初筛中", "current": True, "scoped": True},
        {"ref": "vision:card:1", "text": "软件工程师\n面试中", "title": "软件工程师", "scoped": True},
        {"ref": "vision:card:2", "text": "AI应用工程师", "title": "AI应用工程师", "scoped": True},
    ]
    options = model._source_evidence_options(sources, [app.job_title], {"软件工程师"}, app)
    assert [item["source_ref"] for item in options] == ["vision:card:0", "vision:block:0", "node:0"]
    assert options[0]["current_label"] == "初筛中"


def test_missing_title_quote_restores_unique_bound_card_not_whole_page():
    title, text = "软件工程师", "软件工程师\n当前状态: 待评估"
    target = _target_option(title, text)
    sources = [
        {"ref": "vision:card:0", "text": text, "scoped": True},
        {"ref": "vision:line:1", "text": "当前状态: 待评估", "parent_ref": "vision:card:0"},
        {"ref": "vision:text", "text": text + "\n产品经理\n面试中"},
    ]
    candidate = _visual_selector(title, "待评估", evidence_ref=None, source_ref="vision:line:1", quotation="当前状态: 待评估")
    restored, error = model._restore_candidate(candidate, target, sources)
    assert error is None and restored.quotation == text and restored.source_ref == "vision:card:0"
    candidate = candidate.model_copy(update={"source_ref": "vision:text"})
    _, error = model._restore_candidate(candidate, target, sources)
    assert error == "model_quote_not_found"


def test_missing_title_is_quote_error_not_identity_confirmation_request():
    candidate = _visual_selector(evidence_ref=None, source_ref="vision:line:0", quotation="筛选中")
    _, error = model._restore_candidate(candidate, {"evidence_mode": "vision", "evidence_options": []},
                                       [{"ref": "vision:line:0", "text": "筛选中"}])
    assert error == "model_quote_not_found"


def test_only_independently_corroborated_ocr_identity_can_select_literal_card():
    app = SimpleNamespace(id="24", job_title="AIOps工程师", job_id=None)
    source = {"ref": "vision:card:0", "text": "AlOps工程师\n初筛中", "title": "AlOps工程师",
              "scoped": True, "identity_title": "AIOps工程师"}
    assert model._source_matches_target(source, [app.job_title], app)
    options = model._source_evidence_options([source], [app.job_title], set(), app)
    assert options and options[0]["text"] == source["text"]
    candidate, error = model._restore_candidate(_visual_selector("AIOps工程师", "初筛中"),
        {"evidence_mode": "vision", "evidence_options": options}, [source])
    assert error is None and candidate.card_title == "AlOps工程师" and candidate.quotation == source["text"]
    uncorroborated = {key: value for key, value in source.items() if key != "identity_title"}
    assert not model._source_matches_target(uncorroborated, [app.job_title], app)


def test_prompt_accepts_submission_baseline_without_regressing_later_stages():
    assert "筛选、初筛、简历筛选、待评估 mean applied" in model._SYSTEM_PROMPT
    assert "dated submission action also supports applied/current=true" in model._SYSTEM_PROMPT
    assert "the server retains stronger stored stages" in model._SYSTEM_PROMPT
    assert "Talent pool alone or recommendation to another position does not" in model._SYSTEM_PROMPT
    assert "OMIT source_ref" in model._SYSTEM_PROMPT
