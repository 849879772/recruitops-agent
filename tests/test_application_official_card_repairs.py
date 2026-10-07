"""Synthetic personal-card, preference, and process-ladder regressions only."""

import pytest

from packages.domain.application_identity import matching_records
from packages.domain.application_status_semantics import (
    current_status_labels, explicit_label_status, literal_record_status,
    status_evidence_conflict, timeline_without_current,
)
from packages.tools.application_page_evidence import validate_page_candidate
from packages.tools.application_review_summary import review_result_presentation
from packages.tools.batch_browser_operations import ApplicationStatusResult
from tests.test_application_page_model_fallback import case, candidate, URL
from tests.test_review_whole_card_evidence import reading


TITLE = "AI测试开发工程师"


def ladder_record(*, dated=True, conflict=True):
    context = TITLE + " 变更职位 催促流程 结束流程 "
    if dated:
        context += "投递时间：2026-09-27 "
    context += "投递简历 简历筛选 面试 录用评估 offer 预入职 流程中"
    return {"title": TITLE, "raw_title": TITLE, "status": "", "label": "投递简历",
            "context": context, "stage_labels": ["简历筛选", "面试"],
            "raw_status_labels": ["投递简历", "简历筛选", "面试", "offer"],
            "signals": {"conflicting_statuses": conflict, "has_progress_timeline": True,
                        "has_active_step": False, "current_step_identified": False,
                        "has_explicit_status": False}}


@pytest.mark.parametrize("label", ["简历评估", "等待处理"])
def test_screening_labels_have_shared_applied_semantics(label):
    from packages.tools.application_status_model import _label_status
    assert explicit_label_status(label) == _label_status(label) == "applied"


@pytest.mark.parametrize("prefix", ["【2027校园招聘】", "【2027校招】", "2027校园招聘-"])
def test_closed_campus_prefix_matches_without_erasing_identity(prefix):
    record = {"raw_title": prefix + TITLE}
    assert matching_records({"job_title": TITLE}, [record]) == [record]
    assert matching_records({"job_title": "【2026校园招聘】" + TITLE}, [record]) == []


@pytest.mark.parametrize("different", ["AI测试开发工程师（接受调剂到算法岗）", "AI测试开发工程师-图像方向",
                                      "AI测试开发工程师(J12345)", "AI测试开发工程师-C++"])
def test_campus_prefix_does_not_remove_substantive_role_suffix(different):
    assert matching_records({"job_title": TITLE}, [{"raw_title": "【2027校园招聘】" + different}]) == []


def test_fake_conflict_is_repaired_but_undated_ladder_remains_unknown():
    record = ladder_record()
    assert status_evidence_conflict(record) is False
    assert timeline_without_current(record) is False
    assert current_status_labels(record) == ["投递时间：2026-09-27"]
    undated = ladder_record(dated=False)
    assert not status_evidence_conflict(undated)
    assert timeline_without_current(undated)
    assert current_status_labels(undated) == []


@pytest.mark.parametrize("labels", [["笔试中", "面试中"], ["面试", "流程已结束"], []])
def test_unknown_or_actual_current_conflicts_are_not_reclassified_as_ladder(labels):
    record = ladder_record(dated=False)
    record["raw_status_labels"] = labels
    assert status_evidence_conflict(record)


@pytest.mark.parametrize("stage", ["applied", "written", "interview1", "offer"])
def test_legacy_dated_ladder_resolves_without_model_and_never_regresses(tmp_path, monkeypatch, stage):
    from packages.tools import application_status_model as model
    repo, _, _, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE, "record_url": URL, "stage": stage}],
        observation={"application_records": [ladder_record()]}, candidates=[candidate(TITLE)])
    monkeypatch.setattr(model, "configured_model_client", lambda _: pytest.fail("No model for dated personal baseline"))
    row = run()["24"]
    assert row["state"] == "unchanged", row.get("reason")
    assert row["model_disposition"] == "rule_resolved" and not client.calls
    assert not row.get("wrote") and repo.list_applications()[0].stage == stage


def test_parser_fake_conflict_does_not_block_a_real_visual_current_state(tmp_path, monkeypatch):
    label = "面试中"
    text = TITLE + "\n当前状态：" + label
    _, _, _, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE, "record_url": URL}],
        observation={"application_records": [ladder_record(dated=False)], "vision": reading(TITLE, text, label)},
        candidates=[candidate(TITLE, label=label, quote=text, ref="vision:card:0", observed_status="interview")])
    row = run(visual=True)["24"]
    assert row["state"] == "updated" and row["model_disposition"] == "called", row
    assert len(client.calls) == 1


@pytest.mark.parametrize("marker", ["has_active_step", "has_explicit_status"])
@pytest.mark.parametrize("visual", [False, True])
def test_true_same_card_current_conflict_remains_blocked(tmp_path, monkeypatch, marker, visual):
    record = ladder_record(dated=False)
    record["signals"][marker] = True
    text = TITLE + "\n面试中"
    repository, _, _, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": TITLE, "record_url": URL}],
        observation={"application_records": [record], "vision": reading(TITLE, text, "面试中")},
        candidates=[candidate(TITLE, label="面试中", quote=text, ref="vision:card:0", observed_status="interview")])
    row = run(visual=visual)["24"]
    assert (row["state"], row["reason"]) == ("unresolved", "status_evidence_conflict")
    assert len(client.calls) == (1 if visual else 0)
    assert not row.get("wrote") and repository.list_applications()[0].stage == "applied"


@pytest.mark.parametrize("title,label", [("AI Agent开发工程师", "简历评估"),
                                       ("应用软件开发工程师", "等待处理")])
def test_legacy_inner_preference_record_does_not_block_real_outer_visual_card(tmp_path, monkeypatch, title, label):
    visible = "【2027校园招聘】" + title
    text = visible + "\n杭州市\n第一意向\n最新状态：" + label + "\n简历投递 简历评估 面试 offer 入职"
    inner = {"title": "第一意向", "raw_title": "第一意向", "status": "applied", "label": "简历投递",
             "context": "第一意向 软件产品 最新状态：简历评估 简历投递 简历评估 面试 offer 入职"}
    _, _, _, _, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": title, "record_url": URL}],
        observation={"application_records": [inner], "vision": reading(visible, text, label)},
        candidates=[candidate(title, label=label, quote=text, ref="vision:card:0", observed_status="applied")])
    row = run(visual=True)["24"]
    assert row["state"] == "unchanged" and row["model_disposition"] == "called", row


def test_generic_in_progress_retains_written_without_false_verification_or_binding(tmp_path, monkeypatch):
    title, label = "Agent开发工程师（接受调剂）", "流程中"
    text = "2027届校园招聘\n人工智能集群\n" + title + "\n" + label
    repo, _, _, _, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": title, "record_url": URL, "stage": "written"}],
        observation={"vision": reading(title, text, label)},
        candidates=[candidate(title, label=label, quote=text, ref="vision:card:0", observed_status="unknown")])
    row = run(visual=True)["24"]
    assert row["state"] == "unresolved" and row["reason"] == "record_present_status_unknown", row
    assert not row.get("wrote") and repo.list_applications()[0].stage == "written"
    presented = review_result_presentation(ApplicationStatusResult(application_id="24", elapsed_ms=0,
        state=row["state"], reason=row["reason"], saved_stage="written"))
    assert presented.presentation_state == "retained"


@pytest.mark.parametrize("text", ["发布日期：2026-09-27\n投递简历", "2026-09-27\n面试 offer",
                                 "投递时间：2026-09-27\n未通过", "投递时间：2026-09-27\n流程结束",
                                 "投递时间：2026-09-27\n最新状态：面试"])
def test_a_date_or_true_terminal_cannot_become_applied_baseline(text):
    assert literal_record_status(text) != ("applied", "投递时间：2026-09-27")
    if "流程结束" not in text:
        assert literal_record_status(text) is None


def test_visual_same_title_duplicate_still_needs_identity_confirmation():
    title, label = "应用软件开发工程师", "等待处理"
    text = title + "\n" + label
    app = {"id": "one", "job_title": title}
    observation = {"application_records": [{"title": title, "application_id": "a"},
                                            {"title": title, "application_id": "b"}],
                   "vision": reading(title, text, label)}
    card, error = validate_page_candidate(observation, app, [app], card_title=title,
        source_ref="vision:card:0", quotation=text, label=label, status="applied", current=True, visual=True)
    assert card is None and error == "target_record_ambiguous"
