"""Anonymous reproductions of the 13-row review's current/history boundary.

No provider, live browser, official profile or database is accessed here.
"""
from copy import deepcopy

import pytest

from packages.domain.application_status_semantics import (
    current_status_labels, dated_submission_baseline, literal_record_status, status_evidence_conflict,
)
from packages.tools.application_page_evidence import page_sources, validate_page_candidate


def reading(title, text, label, *, current=True):
    return {"reading_version": "literal-cards-v1", "text": text, "confidence": .98,
            "cards": [{"title": title, "text": text, "current_label": label, "current": current}]}


def validate(dom, label, *, title="软件研发工程师", status="applied", text=None, supplied_title=None,
             applications=None, current=True):
    text = text or f"{title}\n投递简历 2026-09-29\n{label}"
    observation = {"application_records": [dom], "vision": reading(title, text, label, current=current)}
    app = {"id": "a", "job_title": supplied_title or title}
    return validate_page_candidate(observation, app, applications or [app],
        card_title=supplied_title or title, source_ref="vision:card:0", quotation=text,
        label=label, status=status, current=current, visual=True)


@pytest.mark.parametrize("case,label,status,dom_label", [
    ("initial-screening-a", "初筛中", "applied", ""),
    ("initial-screening-b", "初筛中", "applied", ""),
    ("written-volunteer", "笔试中", "written", ""),
    ("written-campus", "笔试中", "written", ""),
    ("hr-screening-a", "HR筛选-进行中", "applied", "HR筛选-进行中"),
    ("hr-screening-b", "HR筛选-进行中", "applied", "HR筛选-进行中"),
    ("resume-routing", "分配简历-流程中", "applied", "分配简历-流程中"),
])
def test_seven_real_failure_shapes_do_not_compare_history_with_current(case, label, status, dom_label):
    title = "软件研发工程师"
    dom = {"title": title, "label": dom_label, "current_step_label": dom_label,
           "raw_status_labels": [dom_label] if dom_label else [],
           "context": f"{title}\n投递简历 2026-09-29\n{dom_label}",
           "applied_at": "2026-09-29", "signals": {"has_date": True,
               "has_explicit_status": bool(dom_label), "current_step_identified": bool(dom_label)}}
    card, reason = validate(dom, label, status=status)
    if case == "resume-routing":
        assert card is None and reason == "record_present_status_unknown"
        return
    assert reason is None, (case, reason)
    assert card["label"] == label and card["status"] == status
    assert "投递简历 2026-09-29" in card["context"]
    assert dom["applied_at"] == "2026-09-29"


@pytest.mark.parametrize("label", ["HR筛选-进行中", "HR筛选 · 进行中", "简历筛选 · 进行中", "分配简历-流程中"])
def test_current_label_is_not_replaced_by_a_same_stage_dated_submission(label):
    card = {"label": label, "context": f"软件研发工程师\n投递简历 2026-09-29\n{label}",
            "raw_status_labels": [label], "signals": {}}
    assert dated_submission_baseline(card) is None
    assert current_status_labels(card) == [label]


@pytest.mark.parametrize("dom_label,visual_label,status", [
    ("初筛中", "HR筛选-进行中", "applied"),
    ("笔试", "笔试中", "written"),
    ("简历筛选", "筛选 待评估", "applied"),
])
def test_same_canonical_current_stage_does_not_require_identical_wording(dom_label, visual_label, status):
    dom = {"title": "软件研发工程师", "label": dom_label,
           "context": f"软件研发工程师\n{dom_label}", "raw_status_labels": [dom_label]}
    card, reason = validate(dom, visual_label, status=status)
    assert card is not None and reason is None


@pytest.mark.parametrize("dom_label", ["面试中", "已投递", "投递时间：2026-02-30"])
def test_real_current_conflict_and_invalid_metadata_remain_rejected(dom_label):
    dom = {"title": "软件研发工程师", "label": dom_label,
           "context": f"软件研发工程师\n{dom_label}", "raw_status_labels": [dom_label]}
    card, reason = validate(dom, "笔试中", status="written")
    assert card is None and reason == "status_evidence_conflict"


def test_two_different_current_assertions_are_not_hidden_by_a_submission_date():
    card = {"label": "笔试中", "raw_status_labels": ["笔试中", "面试中", "投递简历 2026-09-29"],
            "context": "软件研发工程师\n投递简历 2026-09-29\n笔试中\n面试中",
            "signals": {"conflicting_statuses": True, "has_explicit_status": True}}
    assert status_evidence_conflict(card)
    found, reason = validate({**card, "title": "软件研发工程师"}, "笔试中", status="written")
    assert found is None and reason == "status_evidence_conflict"


def test_same_stage_current_aliases_with_submission_date_are_not_conflicting():
    card = {"label": "HR筛选中", "raw_status_labels": ["HR筛选中", "初筛中", "投递简历 2026-09-29"],
            "context": "HR筛选中\n初筛中\n投递简历 2026-09-29",
            "signals": {"conflicting_statuses": True, "has_explicit_status": True}}
    assert not status_evidence_conflict(card)
    assert current_status_labels(card) == ["HR筛选中"]


def ocr_observation(*, dom_date="2026-09-29", visual_date="2026-09-29", extra=None):
    original, ocr = "智能工具&AIOps平台开发第1志愿", "智能工具&AlOps平台开发第1志愿"
    text = f"{ocr}\n投递简历 {visual_date}\n初筛中"
    record = {"title": original, "context": f"{original}\n投递简历 {dom_date}\n初筛中"}
    return original, ocr, text, {"application_records": [record, *(extra or [])],
                               "vision": reading(ocr, text, "初筛中")}


def test_unique_ocr_identity_restores_only_literal_source_title_never_quotation():
    original, ocr, text, observation = ocr_observation()
    app = {"id": "a", "job_title": original}
    source = next(item for item in page_sources(observation, visual=True) if item["ref"] == "vision:card:0")
    assert source["identity_title"] == original
    card, reason = validate_page_candidate(observation, app, [app], card_title=original,
        source_ref=source["ref"], quotation=text, label="初筛中", status="applied", current=True, visual=True)
    assert reason is None and card["title"] == ocr
    assert card["context"] == text and original not in card["context"]


def test_ocr_bridge_does_not_make_a_dom_rewritten_quotation_literal():
    original, _, text, observation = ocr_observation()
    app = {"id": "a", "job_title": original}
    card, reason = validate_page_candidate(observation, app, [app], card_title=original,
        source_ref="vision:card:0", quotation=text.replace("AlOps", "AIOps"), label="初筛中",
        status="applied", current=True, visual=True)
    assert card is None and reason == "model_quote_not_found"


@pytest.mark.parametrize("failure", ["duplicate", "date", "mixed-date", "id", "volunteer", "city", "role"])
def test_ocr_bridge_rejects_ambiguous_or_contradictory_anchors(failure):
    original, ocr, text, observation = ocr_observation()
    record = observation["application_records"][0]
    if failure == "duplicate":
        observation["application_records"].append(deepcopy(record))
    elif failure == "date":
        record["context"] = record["context"].replace("2026-09-29", "2026-09-28")
    elif failure == "mixed-date":
        record["context"] += "\n申请日期：2026-09-28"
    elif failure == "id":
        record["job_id"] = "J11671"
        observation["vision"]["text"] += "\n职位ID：J11707"
        observation["vision"]["cards"][0]["text"] += "\n职位ID：J11707"
    elif failure == "volunteer":
        record["title"] = original.replace("第1志愿", "第2志愿")
    elif failure == "city":
        record["title"] = original + "（深圳）"
        observation["vision"]["text"] = observation["vision"]["text"].replace(ocr, ocr + "（北京）")
        observation["vision"]["cards"][0]["title"] = ocr + "（北京）"
        observation["vision"]["cards"][0]["text"] = observation["vision"]["text"]
    elif failure == "role":
        record["title"] = original.replace("平台开发", "平台测试")
    source = next(item for item in page_sources(observation, visual=True) if item["ref"] == "vision:card:0")
    assert "identity_title" not in source


def test_wrong_literal_job_id_cannot_use_a_current_stage_from_another_card():
    title, wrong, label = "软件研发工程师(J11671)", "软件研发工程师(J11707)", "笔试中"
    app = {"id": "a", "job_title": title}
    text = f"{wrong}\n投递简历 2026-09-29\n{label}"
    found, reason = validate_page_candidate({"vision": reading(wrong, text, label)}, app, [app],
        card_title=wrong, source_ref="vision:card:0", quotation=text, label=label,
        status="written", current=True, visual=True)
    assert found is None and reason == "model_identity_mismatch"


def test_ocr_bridge_cannot_give_one_visual_card_to_two_stored_applications():
    original, _, text, observation = ocr_observation()
    app = {"id": "a", "job_title": original}
    found, reason = validate_page_candidate(observation, app, [app, {"id": "b", "job_title": original}],
        card_title=original, source_ref="vision:card:0", quotation=text, label="初筛中",
        status="applied", current=True, visual=True)
    assert found is None and reason == "target_record_ambiguous"


def test_other_job_status_cannot_be_borrowed_despite_shared_company_and_date():
    original, _, text, observation = ocr_observation()
    app = {"id": "a", "job_title": "硬件测试工程师"}
    found, reason = validate_page_candidate(observation, app, [app], card_title=original,
        source_ref="vision:card:0", quotation=text, label="初筛中", status="applied", current=True, visual=True)
    assert found is None and reason == "model_identity_mismatch"


def test_closed_current_hr_screening_with_wrapped_suffix_uses_original_literal_not_history():
    title, label = "软件研发工程师", "HR筛选-HR筛选\n中"
    text = f"{title}\n当前进度：{label}\n测评已完成\n2026-09-29 18:17 投递"
    assert literal_record_status(text) == ("applied", label)
    app = {"id": "a", "job_title": title}
    # The reader attached the old assessment badge rather than the separate
    # current-progress phrase. A closed literal recovery keeps that distinction.
    observation = {"vision": reading(title, text, "测评已完成", current=False)}
    found, reason = validate_page_candidate(observation, app, [app], card_title=title,
        source_ref="vision:card:0", quotation=text, label="HR筛选-HR筛选中",
        status="applied", current=True, visual=True)
    assert reason is None and found["label"] == label and found["context"] == text


@pytest.mark.parametrize("extra", ["笔试中", "流程结束"])
def test_closed_hr_recovery_never_hides_a_different_current_or_terminal_assertion(extra):
    text = f"软件研发工程师\n当前进度：HR筛选-HR筛选\n中\n{extra}\n2026-09-29 投递"
    assert literal_record_status(text) is None


def test_hr_phrase_without_current_prefix_does_not_override_a_visual_noncurrent_badge():
    title = "软件研发工程师"
    text = f"{title}\nHR筛选-HR筛选\n中\n测评已完成\n2026-09-29 投递"
    app = {"id": "a", "job_title": title}
    found, reason = validate_page_candidate({"vision": reading(title, text, "测评已完成", current=False)},
        app, [app], card_title=title, source_ref="vision:card:0", quotation=text,
        label="HR筛选-HR筛选中", status="applied", current=True, visual=True)
    assert found is None and reason == "record_present_status_unknown"
