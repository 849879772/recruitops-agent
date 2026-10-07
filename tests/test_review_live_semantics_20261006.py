"""Sanitized literal shapes from the 13-target fresh browser test.

These fixtures never access a provider, real browser profile or formal database.
"""
from copy import deepcopy

import pytest

from packages.domain.application_status_semantics import (
    current_status_labels, literal_current_status_conflict, literal_current_status_labels,
    literal_record_status, noncanonical_status_reason, status_evidence_conflict,
)
from packages.tools.application_page_evidence import page_sources, validate_page_candidate


def observation(title, text, label, *, current=True, records=None):
    return {"application_records": records or [], "vision": {
        "reading_version": "literal-cards-v1", "text": text, "confidence": .95,
        "cards": [{"title": title, "text": text, "current_label": label, "current": current}],
    }}


def validate(obs, title, label, status="applied"):
    app = {"id": "target", "job_title": title}
    text = obs["vision"]["cards"][0]["text"]
    return validate_page_candidate(obs, app, [app], card_title=title,
        source_ref="vision:card:0", quotation=text, label=label, status=status,
        current=True, visual=True)


def test_real_routing_current_anchor_does_not_become_a_dated_submission_or_conflict():
    title = "AI应用工程师"
    label = "当前进度：分配简历-流程中"
    text = f"第 1 志愿 {title}\n{label}\n校园招聘 2026-09-24 01:33 投递"
    assert literal_record_status(text) is None
    assert literal_current_status_labels(text) == ["分配简历-流程中"]
    assert not literal_current_status_conflict(text)
    assert noncanonical_status_reason(label) == "record_present_status_unknown"
    obs = observation(title, text, label)
    assert validate(obs, title, label) == (None, "record_present_status_unknown")
    assert validate(obs, title, "2026-09-24 01:33 投递") == (None, "record_present_status_unknown")
    legacy = {"title": title, "label": "分配简历-流程中", "context": text,
              "raw_status_labels": ["分配简历-流程中", "2026-09-24 01:33 投递"],
              "signals": {"has_explicit_status": True, "conflicting_statuses": True}}
    assert not status_evidence_conflict(legacy)
    assert current_status_labels(legacy) == ["分配简历-流程中"]


@pytest.mark.parametrize("label", ["HR筛选-HR筛选中", "HR筛选-进行中"])
def test_real_explicit_screening_survives_completed_assessment_and_submission_history(label):
    title = "SH项目组-AI应用研发工程师（可实习）【2027届校招】(J20624)"
    text = f"第 1 志愿 {title}\n当前进度：{label}\n校园招聘 2026-10-03 18:17 投递\n测评已完成\n编辑 查看/打印"
    assert literal_record_status(text) == ("applied", label)
    obs = observation(title, text, "测评已完成")
    card, reason = validate(obs, title, label)
    assert reason is None and card["label"] == label
    dom = {"title": title, "label": label, "current_step_label": label,
           "context": text.replace("\n", " "), "signals": {"has_explicit_status": True,
               "current_step_identified": True}, "raw_status_labels": [label]}
    assert current_status_labels(dom) == [label]


@pytest.mark.parametrize("channel", ["官网主投", "官网投递", "内推"])
def test_live_screening_metadata_is_an_assertion_even_with_reader_false(channel):
    title = "测试开发工程师（直播）【2027届】"
    text = f"第1志愿\n2026-09-24 投递 {channel} 初筛中\n{title}\n撤回投递 更新简历"
    assert literal_record_status(text) == ("applied", "初筛中")
    card, reason = validate(observation(title, text, "", current=False), title, "初筛中")
    assert reason is None and card["label"] == "初筛中"


@pytest.mark.parametrize("prefix", ["2026-02-30 投递 官网主投", "2026-09-24 发布 官网主投", "计划投递 官网主投"])
def test_non_submission_or_invalid_date_metadata_never_synthesizes_screening(prefix):
    text = "软件开发工程师\n" + prefix + " 初筛中"
    assert literal_record_status(text) is None


@pytest.mark.parametrize("text", [
    "当前进度：HR筛选-进行中\n当前状态：笔试中\n2026-09-24 投递",
    "当前进度：HR筛选-HR筛选中\n笔试中\n2026-09-24 投递",
    "面试中\n笔试中",
    "当前进度：分配简历-流程中\n当前状态：笔试中",
    "当前进度：HR筛选中\n当前状态：流程已结束\n笔试中",
])
def test_genuine_two_current_stages_cannot_use_reader_current_to_bypass_veto(text):
    title = "软件开发工程师"
    text = title + "\n" + text
    assert literal_current_status_conflict(text)
    assert literal_record_status(text) is None
    obs = observation(title, text, "笔试中")
    assert validate(obs, title, "笔试中", "written") == (None, "status_evidence_conflict")


def test_real_dated_aac_event_sequence_can_assert_written_without_promoting_a_ladder():
    title = "(27届秋招)软件开发工程师"
    text = (f"{title} 第1志愿 官网投递 意向城市：①深圳、②苏州、③南京；接受调剂到其他城市 "
            "投递简历 2026-10-01 评估中 2026-10-01 笔试中 2026-10-01")
    assert literal_record_status(text) == ("written", "笔试中")
    card, reason = validate(observation(title, text, "", current=False), title, "笔试中", "written")
    assert reason is None and card["status"] == "written"


def test_explicit_current_screening_outranks_older_dated_event_lines():
    text = ("软件开发工程师\n当前进度：HR筛选中\n投递简历\n2026-09-24\n"
            "评估中\n2026-09-24\n笔试中\n2026-09-25")
    assert not literal_current_status_conflict(text)
    assert literal_record_status(text) == ("applied", "HR筛选中")


@pytest.mark.parametrize("tail", [
    "投递简历 2026-10-02 评估中 2026-10-01 笔试中 2026-10-01",
    "投递简历 2026-10-01 评估中 2026-10-01 笔试中 2026-02-30",
    "投递简历 2026-10-01 评估中 2026-10-01 笔试中 2026-10-01 面试中",
    "投递简历 2026-10-01 评估中 2026-10-01 笔试中 2026-10-01 下一步进入面试",
    "当前进度：分配简历-流程中 投递简历 2026-10-01 评估中 2026-10-01 笔试中 2026-10-01",
])
def test_dated_sequence_is_not_a_highest_stage_or_invalid_history_rule(tail):
    assert literal_record_status("软件开发工程师\n" + tail) is None


def test_undated_unselected_ladder_keeps_only_the_independent_submission_baseline():
    text = "软件开发工程师\n投递简历 2026-10-01 评估中 笔试中 面试中"
    assert literal_record_status(text) == ("applied", "投递简历 2026-10-01")


def ocr_case():
    original, visual = "【27届校招】智能工具&AIOps平台开发", "【27届校招】智能工具&AlOps平台开发"
    raw_title = original + "第1志愿"
    text = f"{visual} 第1志愿\n官网投递\n投递简历 2026-09-14\n初筛中"
    record = {"title": original, "raw_title": raw_title, "volunteer_index": "1",
              "context": f"{raw_title}\n投递简历 2026-09-14\n初筛中"}
    return original, raw_title, visual, text, observation(visual, text, "初筛中", records=[record])


def test_real_ocr_title_can_omit_display_volunteer_only_when_same_card_corroborates_it():
    original, raw_title, visual, text, obs = ocr_case()
    source = next(item for item in page_sources(obs, visual=True) if item["ref"] == "vision:card:0")
    assert source["identity_title"] == raw_title
    card, reason = validate(obs, raw_title, "初筛中")
    assert reason is None and card["title"] == visual and card["context"] == text
    assert original not in card["context"]


@pytest.mark.parametrize("kind", ["missing-volunteer", "wrong-volunteer", "duplicate", "date", "city", "job-id", "role"])
def test_display_volunteer_ocr_bridge_keeps_other_identity_and_owner_guards(kind):
    _, _, _, _, obs = ocr_case()
    record = obs["application_records"][0]
    if kind == "duplicate":
        obs["application_records"].append(deepcopy(record))
    elif kind == "date":
        record["context"] = record["context"].replace("09-14", "09-15")
    elif kind == "city":
        record["raw_title"] += "（深圳）"
    elif kind == "job-id":
        record["job_id"] = "J11671"
        obs["vision"]["cards"][0]["text"] += "\n职位ID：J11707"
        obs["vision"]["text"] = obs["vision"]["cards"][0]["text"]
    elif kind == "role":
        record["raw_title"] = record["raw_title"].replace("平台开发", "平台测试")
    else:
        replacement = "" if kind == "missing-volunteer" else "第2志愿"
        obs["vision"]["cards"][0]["text"] = obs["vision"]["cards"][0]["text"].replace("第1志愿", replacement)
        obs["vision"]["text"] = obs["vision"]["cards"][0]["text"]
    source = next(item for item in page_sources(obs, visual=True) if item["ref"] == "vision:card:0")
    assert "identity_title" not in source
