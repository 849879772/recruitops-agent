"""Sanitized shapes from the 19-target review, using offline evidence only."""

import pytest

from packages.domain.application_status_semantics import (
    explicit_label_status, literal_current_status_conflict, literal_current_status_labels,
    literal_record_status, noncanonical_status_reason,
)
from packages.tools.application_page_evidence import validate_page_candidate
from tests.test_application_page_model_fallback import case, candidate, URL


TITLE = "示例开发工程师"
PENDING = "简历投递 成功 筛选 待评估"


def reading(text, *, label="", current=False):
    return {"reading_version": "literal-cards-v1", "text": text, "confidence": .95,
            "cards": [{"title": TITLE, "text": text, "current_label": label, "current": current}]}


@pytest.mark.parametrize("line,label", [
    ("状态: 简历等待筛选 项目: -", "简历等待筛选"),
    ("状态: 用人部门筛选 项目: -", "用人部门筛选"),
    ("状态: 初筛 项目: -", "初筛"),
    ("当前进度：简历复筛-简历复筛", "简历复筛-简历复筛"),
])
def test_explicit_screening_is_current_even_when_reader_flag_is_false(line, label):
    text = f"第1志愿 {TITLE}\n{line}\n修改申请\n2026-10-01 1:29"
    assert literal_current_status_labels(text) == [label]
    assert literal_record_status(text) == ("applied", label)
    app = {"id": "one", "job_title": TITLE}
    card, error = validate_page_candidate({"vision": reading(text)}, app, [app],
        card_title=TITLE, source_ref="vision:card:0", quotation=text, label=label,
        status="applied", current=False, visual=True)
    assert error is None and card["status"] == "applied"


@pytest.mark.parametrize("label", ["简历复筛", "简历复筛-简历复筛"])
def test_resume_rescreening_is_applied_not_interview(label):
    assert explicit_label_status(label) == "applied"
    assert explicit_label_status("尚未" + label) is None
    assert explicit_label_status("计划进入" + label) is None
    assert explicit_label_status(label + "-未通过") == "rejected"


@pytest.mark.parametrize("text", [
    "计划状态: 简历等待筛选", "如果状态: 用人部门筛选", "状态: 尚未简历等待筛选",
    "状态: 计划进入简历复筛", "状态: 简历复筛-未通过",
    "简历等待筛选", "用人部门筛选", "申请成功 用人部门筛选 笔试 初试 复试",
])
def test_unasserted_or_unselected_screening_does_not_gain_current_status(text):
    assert literal_record_status(TITLE + "\n" + text) is None


@pytest.mark.parametrize("line", [
    "状态: 简历等待筛选", "状态: 用人部门筛选", "当前进度：简历复筛-简历复筛",
])
@pytest.mark.parametrize("other", ["当前状态：笔试中", "当前状态：面试中", "当前状态：流程已结束"])
def test_new_screening_labels_keep_genuine_current_conflicts(line, other):
    text = f"{TITLE}\n{line}\n{other}"
    assert literal_current_status_conflict(text)
    assert literal_record_status(text) is None


@pytest.mark.parametrize("label", ["简历等待筛选", "用人部门筛选", "简历复筛-简历复筛"])
@pytest.mark.parametrize("suffix", ["未通过", "已撤回", "尚未开始", "取消", "计划进入面试"])
def test_new_label_cannot_hide_an_immediate_negative_or_future_suffix(label, suffix):
    text = f"{TITLE}\n状态: {label} {suffix}\n2026-10-01 投递"
    assert literal_record_status(text) is None


@pytest.mark.parametrize("tail", ["撤销申请 面试 Offer 入职", "面试 Offer", "", "\n面试\nOffer\n入职"])
def test_flat_pending_screening_card_proves_only_completed_submission(tail):
    text = f"{TITLE} 应届生 软件类 意向岗位：第二意向 {PENDING} {tail}"
    assert literal_record_status(text) == ("applied", "简历投递 成功")
    app = {"id": "one", "job_title": TITLE}
    card, error = validate_page_candidate({"vision": reading(text)}, app, [app],
        card_title=TITLE, source_ref="vision:card:0", quotation=text, label="简历投递 成功",
        status="applied", current=False, visual=True)
    assert error is None and card["label"] == "简历投递 成功"
    for label, status in [("面试", "interview"), ("Offer", "offer")]:
        if label in text:
            assert validate_page_candidate({"vision": reading(text)}, app, [app],
                card_title=TITLE, source_ref="vision:card:0", quotation=text, label=label,
                status=status, current=True, visual=True)[0] is None


@pytest.mark.parametrize("text", [
    "简历投递 筛选 待评估 面试 Offer 入职",
    "简历投递 失败 筛选 待评估 面试 Offer 入职",
    "如果 " + PENDING, "计划 " + PENDING, "尚未 " + PENDING,
    PENDING + " 未通过", PENDING + " 已获得Offer", PENDING + " 当前状态：流程中",
])
def test_flat_card_needs_completed_submission_without_negative_or_unknown_current(text):
    assert literal_record_status(TITLE + "\n" + text) is None


@pytest.mark.parametrize("line,status,label", [
    ("当前状态：笔试中", "written", "笔试中"),
    ("当前状态：面试中", "interview", "面试中"),
    ("当前状态：已获得offer", "offer", "已获得offer"),
    ("当前状态：已撤回", "withdrawn", "已撤回"),
    ("流程已结束", "rejected", "流程已结束"),
])
def test_real_later_or_terminal_assertion_takes_precedence_over_submission(line, status, label):
    assert literal_record_status(f"{TITLE}\n{PENDING}\n{line}") == (status, label)


@pytest.mark.parametrize("stage", ["applied", "written", "interview1", "offer", "rejected"])
def test_flat_submission_never_rolls_back_saved_stage(tmp_path, monkeypatch, stage):
    text = f"{TITLE} {PENDING} 撤销申请 面试 Offer 入职"
    repository, _, _, _, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE, "record_url": URL, "stage": stage}],
        observation={"vision": reading(text)},
        candidates=[candidate(TITLE, label="简历投递 成功", quote=text, ref="vision:card:0",
                              observed_status="applied", current=False)])
    assert not run(visual=True)["24"].get("wrote")
    assert repository.list_applications()[0].stage == stage


def test_routing_keeps_unknown_semantics_for_read_only_retention():
    label = "当前进度：分配简历-流程中"
    assert noncanonical_status_reason(label) == "record_present_status_unknown"
    assert literal_record_status(f"{TITLE}\n{label}\n2026-09-24 01:33 投递") is None


def test_channel_prefixed_dated_submission_preserves_exact_scoped_literal(tmp_path, monkeypatch):
    text = f"{TITLE}\n校园招聘 | 2026-09-27 22:13 投递\n查看/打印"
    label = "2026-09-27 22:13 投递"
    assert literal_record_status(text) == ("applied", label)
    vision = reading(text)
    vision["text"] = "投递记录\n已完成的投递 (1)\n" + text + "\n没有更多了"
    repository, _, _, _, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE, "record_url": URL}],
        observation={"vision": vision},
        candidates=[candidate(TITLE, label=label, quote=text, ref="vision:card:0",
                              observed_status="applied", current=False)])
    row = run(visual=True)["24"]
    assert row["state"] == "unchanged", row
    assert row["observed_label"] == label
    assert not row["wrote"]
    assert repository.list_applications()[0].stage == "applied"


def test_completed_applications_page_heading_is_not_the_card_submission_quote():
    text = f"{TITLE}\n校园招聘 | 2026-09-27 22:13 投递\n查看/打印"
    vision = reading(text)
    vision["text"] = "投递记录\n已完成的投递 (1)\n" + text
    app = {"id": "one", "job_title": TITLE}
    card, error = validate_page_candidate({"vision": vision}, app, [app],
        card_title=TITLE, source_ref="vision:text", quotation=vision["text"],
        label="已完成的投递", status="applied", current=True, visual=True)
    assert card is None and error == "model_quote_not_found"
    card, error = validate_page_candidate({"vision": vision}, app, [app],
        card_title=TITLE, source_ref="vision:card:0", quotation=text,
        label="已完成的投递", status="applied", current=True, visual=True)
    assert card is None and error == "model_quote_not_found"


@pytest.mark.parametrize("label", ["笔试/AI语言测试", "笔试／AI语言测试", "笔试 / AI语言测试"])
@pytest.mark.parametrize("prefix", ["状态：", "当前状态："])
def test_explicit_compound_written_field_is_literal_with_false_reader_flag(label, prefix):
    text = f"{TITLE}\n查看详情\n{prefix}{label} 项目：-\n2026-09-14 1:44"
    assert literal_current_status_labels(text) == [label]
    assert literal_record_status(text) == ("written", label)
    app = {"id": "one", "job_title": TITLE}
    card, error = validate_page_candidate({"vision": reading(text)}, app, [app],
        card_title=TITLE, source_ref="vision:card:0", quotation=text, label=label,
        status="written", current=False, visual=True)
    assert error is None and card["status"] == "written"


@pytest.mark.parametrize("line", [
    "AI语言测试", "状态：AI语言测试", "笔试/AI语言测试", "下一步：笔试/AI语言测试",
    "申请成功 筛选 笔试/AI语言测试 面试 Offer", "预计状态：笔试/AI语言测试",
    "计划 状态：笔试/AI语言测试", "后续 状态：笔试/AI语言测试", "尚未进入 状态：笔试/AI语言测试",
    "状态：计划进入笔试/AI语言测试", "状态：尚未笔试/AI语言测试",
    "状态：笔试/AI语言测试 未通过", "状态：笔试/AI语言测试 尚未安排",
    "状态：笔试/AI语言测试 已撤回", "状态：笔试/AI语言测试 计划进入面试",
    "状态：笔试/AI语言测试工程师", "状态：笔试/AI语言测试/面试", "状态：笔试/AI语言测试项目",
])
def test_compound_written_does_not_expand_to_bare_future_negative_or_longer_text(line):
    assert literal_record_status(f"{TITLE}\n{line}") is None


@pytest.mark.parametrize("other", ["当前状态：面试中", "状态：简历等待筛选", "当前状态：已获得offer",
                                   "当前状态：已撤回", "当前状态：流程已结束"])
def test_compound_written_does_not_hide_another_current_stage(other):
    text = f"{TITLE}\n状态：笔试/AI语言测试 项目：-\n{other}"
    assert literal_current_status_conflict(text)
    assert literal_record_status(text) is None


def test_compound_written_preserves_saved_written_stage(tmp_path, monkeypatch):
    text = f"{TITLE}\n查看详情\n状态：笔试/AI语言测试 项目：-\n2026-09-14 1:44"
    repository, _, _, _, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE, "record_url": URL, "stage": "written"}],
        observation={"vision": reading(text)},
        candidates=[candidate(TITLE, label="笔试/AI语言测试", quote=text, ref="vision:card:0", current=False)])
    row = run(visual=True)["24"]
    assert row["state"] == "unchanged" and not row["wrote"], row
    assert repository.list_applications()[0].stage == "written"
