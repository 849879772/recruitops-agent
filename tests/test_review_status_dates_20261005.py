"""Offline regression for dated submissions, current steps and inactive ladders."""

import pytest

from packages.domain.application_status_semantics import (
    current_status_labels, dated_submission_label, explicit_label_status, literal_record_status,
    noncanonical_status_reason, status_evidence_conflict,
)
from packages.tools.application_status_model import _label_status
from tests.test_application_page_model_fallback import case, candidate, URL


@pytest.mark.parametrize("label", [
    "申请时间：2026-10-01", "投递时间：2026/09/30 21:58:46",
    "申请日期: 2026.10.01", "投递于2026年10月1日",
    "投递简历\n2026-09-30", "2026-09-30 21:58 投递", "已提交简历 2024-02-29",
])
def test_valid_dated_submission_has_one_shared_baseline(label):
    assert dated_submission_label(label) == label
    assert literal_record_status(label) == ("applied", label)
    assert explicit_label_status(label) == _label_status(label) == "applied"


@pytest.mark.parametrize("text", [
    "申请时间：2026-02-30", "投递时间：2026-13-01", "投递时间：2026-10-01 24:00",
    "申请时间：2026-10-01 12:60", "申请时间：2026-10-01 12:00:60",
    "申请时间：2026-10-011", "发布日期：2026-10-01\n投递简历",
    "2026-10-01\n投递简历", "有效期：2026.09.08-2027.09.08", "点击申请", "申请",
    "预计申请时间：2026-10-01", "尚未申请 2026-10-01",
])
def test_invalid_dates_publication_dates_and_buttons_are_not_submission_evidence(text):
    assert dated_submission_label(text) is None
    assert literal_record_status(text) is None


@pytest.mark.parametrize("label", ["申请时间：2026-02-30", "投递时间：2026-13-01",
                                   "投递时间：2026-10-01 24:00", "2026-02-30 投递"])
def test_invalid_submission_metadata_cannot_fall_through_to_generic_submission_word(label):
    assert explicit_label_status(label) is None
    assert _label_status(label) is None


@pytest.mark.parametrize("stage", ["applied", "written", "interview1", "offer"])
def test_cvte_style_not_started_visual_card_confirms_submission_without_regression(tmp_path, monkeypatch, stage):
    title, label = "应用软件开发工程师", "申请时间：2026-10-01"
    text = (f"{title}\n{label}\n软件专业笔试\n应用软件专业面试\n综合面试\n未开始\n"
            "2026.09.08-2027.09.08\n重新选择\n点击这里开始")
    vision = {"reading_version": "literal-cards-v1", "text": text, "confidence": .99,
              "cards": [{"title": title, "text": text, "current_label": "未开始", "current": False}]}
    proposal = candidate(title, label="未开始", ref="vision:card:0", quote=text,
                         observed_status="unknown", current=False,
                         uncertainties=["页面步骤尚未开始，不能确认当前笔试或面试阶段"])
    repository, _, _, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": title, "record_url": URL, "stage": stage}],
        observation={"vision": vision}, candidates=[proposal])
    result = run(visual=True)["24"]
    assert result["state"] == "unchanged", result
    assert result["observed_label"] == label
    assert result["observed_status"] == "applied"
    assert repository.list_applications()[0].stage == stage
    assert len(client.calls) == (0 if stage == "applied" else 1)


@pytest.mark.parametrize("tail,expected", [("当前状态：面试", None),
                                           ("面试中", ("interview", "面试中")),
                                           ("流程结束", ("rejected", "流程结束"))])
def test_submission_baseline_never_overrides_newer_asserted_stage(tail, expected):
    assert literal_record_status("申请时间：2026-10-01\n" + tail) == expected


def legacy_record(labels, **changes):
    return {"title": "AI Agent开发工程师", "label": labels[0], "status": "",
            "raw_status_labels": labels, "context": "AI Agent开发工程师\n" + "\n".join(labels),
            "evidence_source": "conflicting-statuses",
            "signals": {"conflicting_statuses": True, "has_explicit_status": True}, **changes}


@pytest.mark.parametrize("label", ["笔试", "面试中", "已投递"])
def test_legacy_current_label_and_submission_metadata_are_not_a_conflict(label):
    card = legacy_record([label, "投递时间：2026/09/30 21:58"])
    assert not status_evidence_conflict(card)
    assert current_status_labels(card) == [label]


@pytest.mark.parametrize("labels", [
    ["笔试", "面试", "投递时间：2026/09/30 21:58"],
    ["待开启", "投递时间：2026/09/30 21:58"],
    ["笔试", "状态待确认", "投递时间：2026/09/30 21:58"],
    ["笔试", "投递时间：2026-02-30"],
    ["笔试", "发布日期：2026-09-30"],
])
def test_legacy_date_compatibility_retains_real_unknown_and_invalid_conflicts(labels):
    assert status_evidence_conflict(legacy_record(labels))


def test_legacy_date_compatibility_cannot_hide_a_different_selected_label():
    assert status_evidence_conflict(legacy_record(["笔试", "投递时间：2026-09-30"], label="面试"))


def test_legacy_date_with_unselected_ladder_is_only_submission_baseline():
    card = legacy_record(["笔试", "面试", "申请时间：2026-10-01"], label="",
                         signals={"conflicting_statuses": True, "has_progress_timeline": True})
    assert not status_evidence_conflict(card)
    assert current_status_labels(card) == ["申请时间：2026-10-01"]


def test_legacy_meituan_false_conflict_reaches_verification(tmp_path, monkeypatch):
    title, label = "AI Agent开发工程师", "笔试"
    card = legacy_record([label, "投递时间：2026/09/30 21:58"])
    repository, _, _, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": title, "record_url": URL}],
        observation={"application_records": [card]},
        candidates=[candidate(title, label=label, quote=card["context"])])
    row = run()["24"]
    assert row["state"] == "updated", row
    assert row["observed_label"] == label
    assert repository.list_applications()[0].stage == "written"
    assert len(client.calls) == 1


@pytest.mark.parametrize("label", ["流程中", "进行中", "进行中。"])
def test_bare_generic_progress_is_unknown_not_an_applied_baseline(label):
    assert noncanonical_status_reason(label) == "record_present_status_unknown"
    assert explicit_label_status(label) is None
    assert _label_status(label) is None


@pytest.mark.parametrize("label,expected", [
    ("笔试进行中", "written"), ("初筛进行中", "applied"), ("面试进行中", "interview"),
])
def test_named_current_stage_is_not_mistaken_for_bare_generic_progress(label, expected):
    assert noncanonical_status_reason(label) is None
    assert explicit_label_status(label) == _label_status(label) == expected


def test_resume_routing_remains_a_closed_processing_baseline():
    from packages.tools.application_status_evidence import supports_no_newer_status
    label = "分配简历-流程中"
    assert noncanonical_status_reason(label) is None
    assert supports_no_newer_status({"label": label, "context": "软件工程师 当前状态：" + label})


@pytest.mark.parametrize("label", ["流程中", "进行中"])
def test_visual_generic_progress_is_read_then_retains_the_saved_stage(tmp_path, monkeypatch, label):
    title = "软件工程师"
    text = f"{title}\n{label}"
    vision = {"reading_version": "literal-cards-v1", "text": text, "confidence": .99,
              "cards": [{"title": title, "text": text, "current_label": label, "current": True}]}
    # A legacy parser enum cannot turn a generic phrase into a confirmed stage.
    record = {"title": title, "context": text, "label": label, "status": "applied"}
    repository, _, _, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": title, "record_url": URL, "stage": "written"}],
        observation={"application_records": [record], "vision": vision},
        candidates=[candidate(title, label=label, ref="vision:card:0", quote=text, observed_status="unknown")])
    row = run(visual=True)["24"]
    assert (row["state"], row["reason"]) == ("unresolved", "record_present_status_unknown"), row
    assert not row.get("wrote") and repository.list_applications()[0].stage == "written"
    assert len(client.calls) == 1
