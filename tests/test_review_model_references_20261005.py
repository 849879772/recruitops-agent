"""Scoped reference, visual-input isolation and narrow OCR regressions."""

import json

import pytest

from packages.tools import application_status_model as model
from tests.test_application_page_model_fallback import URL, candidate, case


TITLE = "Agent工程师(J11671)"
TEXT = f"{TITLE}\n投递简历 2026-09-30\n初筛中"


def selection(*, title=TITLE, text=TEXT, source_ref="vision:card:0", **metadata):
    source = {"ref": source_ref, "text": text, "title": title,
              "current_label": "初筛中", "current": True, "scoped": True, **metadata}
    target = {"application_id": "24", "card_title": metadata.get("identity_title", title),
              "evidence_mode": "vision", "evidence_options": [
                  {"ref": "source:0", "source_ref": source_ref, "text": text,
                   "title": title, **metadata}]}
    return source, target


def proposal(**updates):
    values = dict(application_id="24", card_title=TITLE, observed_status="applied",
                  observed_label="初筛中", evidence_ref="source:0", quotation="",
                  current=True, uncertainties=[])
    values.update(updates)
    return model.StatusCandidate(**values)


@pytest.mark.parametrize("ref", ["source:0", "vision:card:0"])
def test_exact_option_and_unique_source_alias_restore_same_literal(ref):
    source, target = selection()
    restored, error = model._restore_candidate(proposal(evidence_ref=ref,
        current_node_ref="vision:card:0"), target, [source])
    assert error is None
    assert restored.evidence_ref == "source:0"
    assert restored.source_ref == "vision:card:0" and restored.quotation == TEXT
    assert restored.current_node_ref == "vision:card:0"


@pytest.mark.parametrize("fault", ["other_ref", "duplicate_alias", "other_source", "other_current", "forged_option"])
def test_source_alias_is_never_guessed_or_borrowed(fault):
    source, target = selection()
    sources, value = [source], proposal(evidence_ref="vision:card:0")
    other = {**source, "ref": "vision:card:1", "text": "AI开发工程师(J11707)\n初筛中"}
    if fault == "other_ref":
        value = value.model_copy(update={"evidence_ref": "vision:card:1"})
        sources.append(other)
    elif fault == "duplicate_alias":
        target["evidence_options"].append({**target["evidence_options"][0], "ref": "source:1"})
    elif fault == "other_source":
        value = value.model_copy(update={"source_ref": "vision:card:1"})
        sources.append(other)
    elif fault == "other_current":
        value = value.model_copy(update={"current_node_ref": "vision:card:1"})
        sources.append(other)
    elif fault == "forged_option":
        target["evidence_options"][0]["text"] += " 已录用"
    _, error = model._restore_candidate(value, target, sources)
    assert error == "model_evidence_ref_invalid"


def vision(title, text, *, label="笔试中", current=True):
    return {"text": text, "reading_version": "literal-cards-v1", "confidence": .98,
            "model": "fixture", "image_sha256": "a" * 64,
            "cards": [{"title": title, "text": text, "current_label": label, "current": current}]}


def test_visual_prompt_never_exposes_dom_context_or_labels(tmp_path, monkeypatch):
    text = f"{TITLE}\n初筛中"
    hidden = f"{TITLE}\nDOM_ONLY_HIDDEN_DETAIL\n当前状态: 面试中"
    record = {"title": TITLE, "context": hidden, "label": "面试中", "raw_status_labels": ["面试中"]}
    _, _, _, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE, "record_url": URL}],
        observation={"application_records": [record], "page": {"text": hidden},
                     "vision": vision(TITLE, text, label="初筛中")},
        candidates=[candidate(TITLE, label="初筛中", ref="vision:card:0", quote=text,
                              observed_status="applied")])
    run(visual=True)
    payload = json.loads(client.calls[0]["user_prompt"])
    assert "DOM_ONLY_HIDDEN_DETAIL" not in client.calls[0]["user_prompt"]
    assert all(row["ref"].startswith("vision:") for row in payload["sources"])
    assert payload["targets"][0]["context"] == ""
    assert payload["targets"][0]["label"] == "初筛中"
    assert payload["targets"][0]["raw_status_labels"] == ["初筛中"]


def test_visual_title_only_capture_reports_missing_coverage_without_model(tmp_path, monkeypatch):
    record = {"title": TITLE, "context": f"{TITLE}\n当前状态: 笔试中", "label": "笔试中"}
    _, _, _, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE, "record_url": URL}],
        observation={"application_records": [record], "vision": vision(TITLE, TITLE, label="", current=False)})
    row = run(visual=True)["24"]
    assert row["reason"] == "visual_target_evidence_incomplete" and not row.get("wrote")
    assert client.calls == []


def test_dom_quote_cannot_be_relabelled_as_visual_when_status_is_cropped(tmp_path, monkeypatch):
    visible = f"{TITLE}\n官网投递"
    dom = f"{TITLE}\n当前状态: 笔试中"
    record = {"title": TITLE, "context": dom, "label": "笔试中"}
    repository, _, _, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE, "record_url": URL}],
        observation={"application_records": [record], "vision": vision(TITLE, visible, label="", current=False),
                     "vision_capture": {"truncated": True}},
        candidates=[candidate(TITLE, ref="vision:text", quote=dom)])
    row = run(visual=True)["24"]
    assert row["reason"] == "visual_target_evidence_incomplete" and not row.get("wrote")
    assert repository.list_applications()[0].stage == "applied"
    assert dom not in client.calls[0]["user_prompt"]


def test_dom_selector_cannot_be_used_in_visual_model():
    source, target = selection()
    _, error = model._restore_candidate(proposal(source_ref="node:0"), target,
                                        [source, {"ref": "node:0", "text": TEXT}])
    assert error == "model_evidence_ref_invalid"


def test_literal_reader_omitted_target_card_is_not_a_model_quote_fault(tmp_path, monkeypatch):
    other = "软件测试工程师"
    text = f"{other}\n初筛中\n{TITLE}\n官网投递"
    reading = vision(other, f"{other}\n初筛中", label="初筛中")
    reading["text"] = text
    dom = f"{TITLE}\n当前状态: 初筛中"
    _, _, _, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE, "record_url": URL}],
        observation={"application_records": [{"title": TITLE, "context": dom, "label": "初筛中"}],
                     "vision": reading}, candidates=[candidate(TITLE, quote=dom, ref="vision:text")])
    row = run(visual=True)["24"]
    assert row["reason"] == "visual_target_evidence_incomplete" and not row.get("wrote")
    assert client.calls == []


@pytest.mark.parametrize("record_mode", ["empty", "other", "bound_dom"])
def test_target_absent_from_visual_text_remains_identity_or_record_problem(tmp_path, monkeypatch, record_mode):
    other = "软件测试工程师"
    reading = vision(other, f"{other}\n初筛中", label="初筛中")
    records = [] if record_mode == "empty" else [{"title": other, "context": f"{other}\n初筛中", "label": "初筛中"}]
    if record_mode == "bound_dom":
        records.append({"title": TITLE, "context": f"{TITLE}\n当前状态: 初筛中", "label": "初筛中"})
    _, _, _, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE, "record_url": URL}],
        observation={"application_records": records, "vision": reading})
    row = run(visual=True)["24"]
    assert row["reason"] in {"application_records_missing", "target_record_not_matched", "record_present_status_unknown"}
    assert row["reason"] != "visual_target_evidence_incomplete"
    assert not row.get("wrote") and client.calls == []


@pytest.mark.parametrize("source_label", ["", "官网投递"])
def test_scoped_source_badge_is_unknown_state_not_missing_card_or_model_fault(tmp_path, monkeypatch, source_label):
    text = f"{TITLE}\n官网投递"
    _, _, _, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE, "record_url": URL}],
        observation={"application_records": [], "vision": vision(TITLE, text, label=source_label, current=False)},
        candidates=[candidate(TITLE, quote=text, ref="vision:card:0", label="官网投递", observed_status="applied")])
    row = run(visual=True)["24"]
    assert row["reason"] == "record_present_status_unknown" and not row.get("wrote")
    assert len(client.calls) == 1


@pytest.mark.parametrize("label", ["面试中", "笔试中"])
def test_false_current_real_stage_is_not_erased_as_source_metadata(tmp_path, monkeypatch, label):
    text = f"{TITLE}\n官网投递\n{label}"
    repository, _, _, _, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE, "record_url": URL}],
        observation={"application_records": [], "vision": vision(TITLE, text, label=label, current=False)},
        candidates=[candidate(TITLE, quote=text, ref="vision:card:0", label="官网投递", observed_status="applied")])
    row = run(visual=True)["24"]
    assert row["state"] == "updated" and row["observed_label"] == label and row["wrote"]
    assert repository.list_applications()[0].stage == ("interview1" if label == "面试中" else "written")


def test_ref_alias_does_not_manufacture_active_visual_marker(tmp_path, monkeypatch):
    text = f"{TITLE}\n筛选 笔试 面试"
    repository, _, _, _, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE, "record_url": URL}],
        observation={"application_records": [], "vision": vision(TITLE, text, label="", current=False)},
        candidates=[candidate(TITLE, label="笔试", evidence_ref="vision:card:0", ref=None,
                              quote="", current_node_ref="vision:card:0")])
    row = run(visual=True)["24"]
    assert row["state"] == "unresolved" and not row.get("wrote")
    assert repository.list_applications()[0].stage == "applied"


@pytest.mark.parametrize("ref", ["source:0", "vision:card:0"])
def test_audited_ocr_identity_selector_restores_literal_not_dom_text(ref):
    source, target = selection(title="AlOps工程师", text="AlOps工程师\n初筛中", identity_title="AIOps工程师")
    restored, error = model._restore_candidate(proposal(card_title="AIOps工程师", evidence_ref=ref), target, [source])
    assert error is None and restored.card_title == "AlOps工程师"
    assert restored.quotation == source["text"]


def test_single_corroborated_ocr_title_glyph_can_restore_copied_quote():
    source, target = selection(title="AlOps工程师", text="AlOps工程师\n初筛中", identity_title="AIOps工程师")
    restored, error = model._restore_candidate(proposal(card_title="AIOps工程师", evidence_ref=None,
        source_ref=source["ref"], quotation="AIOps工程师\n初筛中"), target, [source])
    assert error is None and restored.card_title == "AlOps工程师"
    assert restored.quotation == source["text"]


@pytest.mark.parametrize("selector", ["source:0", "vision:card:0"])
def test_audited_ocr_core_title_can_select_card_with_separate_volunteer_display(selector):
    title, identity = "【27届校招】智能工具&AlOps平台开发", "【27届校招】智能工具&AIOps平台开发第 1 志愿"
    source, target = selection(title=title, text=f"{title} 第1志愿\n投递简历 2026-09-14",
                               identity_title=identity)
    restored, error = model._restore_candidate(proposal(card_title="【27届校招】智能工具&AIOps平台开发",
        observed_label="投递简历", evidence_ref=selector), target, [source])
    assert error is None and restored.card_title == title
    assert restored.quotation == source["text"]


@pytest.mark.parametrize("wrong_title", ["AIOps工程师第2志愿", "AIOps工程师(北京)", "AI应用工程师"])
def test_audited_ocr_display_recovery_does_not_erase_real_identity(wrong_title):
    source, _ = selection(title="AlOps工程师", text="AlOps工程师 第1志愿\n投递简历 2026-09-14",
                           identity_title="AIOps工程师第1志愿")
    assert not model._audited_ocr_identity_matches(source, wrong_title)


def test_ocr_missing_volunteer_is_not_restored_unless_literal_card_contains_that_display():
    source, _ = selection(title="AlOps工程师", text="AlOps工程师\n投递简历 2026-09-14",
                           identity_title="AIOps工程师第1志愿")
    assert not model._audited_ocr_identity_matches(source, "AIOps工程师")


@pytest.mark.parametrize("fault", ["no_audit", "different_source", "invented_tail", "different_role"])
def test_ocr_recovery_never_globalizes_or_completes_missing_quote(fault):
    source, _ = selection(title="AlOps工程师", text="AlOps工程师\n初筛中", identity_title="AIOps工程师")
    value = proposal(card_title="AIOps工程师", evidence_ref=None, source_ref=source["ref"],
                     quotation="AIOps工程师\n初筛中")
    if fault == "no_audit":
        source.pop("identity_title")
    elif fault == "different_source":
        value = value.model_copy(update={"source_ref": "vision:card:1"})
    elif fault == "invented_tail":
        value = value.model_copy(update={"quotation": value.quotation + " 已录用"})
    elif fault == "different_role":
        source["identity_title"] = "Java工程师"
        value = value.model_copy(update={"card_title": "Java工程师", "quotation": "Java工程师\n初筛中"})
    assert model._restore_corroborated_ocr_quote(value, [source]) == value


@pytest.mark.parametrize(("label", "expected"), [("初筛中", "applied"), ("笔试中", "written"), ("面试中", "interview")])
def test_explicit_current_label_outranks_dated_submission(label, expected):
    source, _ = selection(text=f"{TITLE}\n投递简历 2026-09-30\n{label}")
    source["current_label"] = label
    assert model._source_literal_status(source) == (expected, label)


def test_unknown_active_label_does_not_fall_back_to_submission_date():
    source, _ = selection(text=f"{TITLE}\n投递简历 2026-09-30\n任意未知文案")
    source["current_label"] = "任意未知文案"
    assert model._source_literal_status(source) is None


def test_reader_metadata_cannot_override_two_real_current_source_assertions():
    source, _ = selection(text=f"{TITLE}\n当前状态：笔试中\n当前状态：面试中")
    source["current_label"] = "笔试中"
    assert model._source_literal_status(source) is None
