"""Anonymous contracts derived from the fresh 13-target browser retest.

Only the failure shapes are retained: synthetic titles, dates, IDs and URLs.
The real resolver and final verifier use disposable SQLite, never a provider or
the official browser profile/database. OCR metadata is deliberately unreliable.
"""
from copy import deepcopy
import json

import pytest

from packages.storage.models import ApplicationSnapshot
from packages.tools import application_status_model as model
from packages.tools.application_page_evidence import page_sources
from tests.test_application_page_model_fallback import URL, candidate, case


TITLE = "应用研发工程师(J12345)"
DAY = "2026-09-30"


def _dom(title, text, *, label="", raw_title=None, volunteer=None):
    record = {"title": title, "raw_title": raw_title or title, "context": text,
              "evidence": text, "label": label, "current_step_label": label,
              "raw_status_labels": [label] if label else [], "stage_labels": [],
              "applied_at": DAY, "signals": {"has_date": True,
                  "has_explicit_status": bool(label), "current_step_identified": bool(label)}}
    if volunteer:
        record["volunteer_index"] = volunteer
    return record


def _reading(title, text, *, label="", current=False, extra_cards=()):
    cards = [{"title": title, "text": text, "current_label": label, "current": current},
             *extra_cards]
    return {"reading_version": "literal-cards-v1", "text": "\n".join(card["text"] for card in cards),
            "confidence": .97, "model": "fixture", "image_sha256": "a" * 64,
            "cards": cards, "diagnostics": []}


def _setup(tmp_path, monkeypatch, *, text, visual_title=TITLE, app_title=TITLE,
           label="", current=False, dom_records=(), saved="applied", proposal=None,
           extra_cards=()):
    reading = _reading(visual_title, text, label=label, current=current, extra_cards=extra_cards)
    original = deepcopy(reading)
    repository, store, operation, client, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": app_title, "record_url": URL, "stage": saved}],
        observation={"page": {"text": "导航"}, "application_records": list(dom_records), "vision": reading},
        candidates=[proposal or candidate(app_title, observed_status="applied", label="官网投递",
            ref=None, quote="", evidence_ref="source:0", current=False)])
    calls = []
    original_verify = model.verify_application_status_evidence

    def verify(request, bridge):
        calls.append(request)
        return original_verify(request, bridge)

    monkeypatch.setattr(model, "verify_application_status_evidence", verify)
    return repository, store, operation, client, run, calls, original


def _unchanged(repository, result, *, stage="applied"):
    assert result["state"] == "unchanged" and not result.get("wrote"), json.dumps(result, ensure_ascii=False)
    with repository.storage.session() as session:
        saved = session.get(ApplicationSnapshot, "24")
        assert saved.stage == stage and saved.stage_history == []


@pytest.mark.parametrize("current", [False, True])
def test_explicit_hr_current_outranks_completed_assessment_and_wrong_ai_stage(
        tmp_path, monkeypatch, current):
    text = (f"第 1 志愿 {TITLE}\n当前进度：HR筛选-HR筛选中\n"
            f"校园招聘 {DAY} 18:17 投递\n测评已完成\n编辑 查看/打印")
    dom = _dom(TITLE, text, label="HR筛选-HR筛选中", volunteer="1")
    repository, store, operation, client, run, requests, reading = _setup(tmp_path, monkeypatch,
        text=text, label="测评已完成", current=current, dom_records=[dom],
        proposal=candidate(TITLE, observed_status="interview", label="HR筛选-HR筛选中",
            ref=None, quote="", evidence_ref="source:0",
            uncertainties=["未显示后续面试或录用结果"]))
    result = run(visual=True)["24"]
    _unchanged(repository, result)
    assert result["observed_status"] == "applied"
    assert result["observed_label"] == "HR筛选-HR筛选中"
    assert requests[-1].evidence == text
    assert store.get_operation(operation.operation_id).result["vision"] == reading
    assert len(client.calls) <= 1


@pytest.mark.parametrize("label,current", [("", False), ("官网投递", False), ("第3志愿", True)])
@pytest.mark.parametrize("layout", ["adjacent_lines", "inline"])
def test_personal_dated_submission_survives_bad_channel_or_preference_metadata(
        tmp_path, monkeypatch, label, current, layout):
    title = "软件测试工程师 - 深圳"
    submission = f"投递简历\n{DAY}" if layout == "adjacent_lines" else f"投递简历 {DAY}"
    text = f"{title} 第3志愿\n官网投递\n深圳 校招-正式\n{str(submission)}"
    dom = _dom(title, text, raw_title=f"{title}第 3 志愿", volunteer="3")
    repository, _, _, _, run, requests, _ = _setup(tmp_path, monkeypatch,
        text=text, visual_title=title, app_title=title, label=label, current=current, dom_records=[dom])
    result = run(visual=True)["24"]
    _unchanged(repository, result)
    assert result["observed_status"] == "applied"
    assert requests[-1].evidence == text


@pytest.mark.parametrize("label,current", [("第1志愿", True), ("", False), ("官网主投", False)])
def test_literal_screening_is_sufficient_even_if_reader_selects_preference_label(
        tmp_path, monkeypatch, label, current):
    text = (f"第1志愿\n{DAY} 投递 官网主投 初筛中\n{TITLE}\n"
            "撤回投递 更新简历\n深圳 | 技术类 | 全职 | 2026-09-01 发布")
    dom = _dom(TITLE, text)
    repository, _, _, _, run, requests, _ = _setup(tmp_path, monkeypatch,
        text=text, label=label, current=current, dom_records=[dom],
        proposal=candidate(TITLE, observed_status="applied", label="初筛中", ref=None,
            quote="", evidence_ref="source:0", uncertainties=["第1志愿不是状态，无法判断后续进度"]))
    result = run(visual=True)["24"]
    _unchanged(repository, result)
    assert result["observed_label"] == "初筛中"
    assert requests[-1].evidence == text


def test_missing_display_suffix_with_one_ocr_glyph_uses_unique_same_card_anchor(
        tmp_path, monkeypatch):
    dom_title, visual_title = "智能工具&AIOps平台开发", "智能工具&AlOps平台开发"
    app_title = f"{dom_title}第 1 志愿"
    text = f"{visual_title} 第1志愿 官网投递 深圳 校招-正式 投递简历 {DAY}"
    dom_text = f"{dom_title}第 1 志愿 官网投递 深圳 校招-正式 投递简历 {DAY}"
    dom = _dom(dom_title, dom_text, raw_title=app_title, volunteer="1")
    repository, store, operation, client, run, requests, _ = _setup(tmp_path, monkeypatch,
        text=text, visual_title=visual_title, app_title=app_title, dom_records=[dom],
        proposal=candidate(app_title, observed_status="applied", label="投递简历", ref=None,
            quote="", evidence_ref="vision:card:0"))
    sources = page_sources(store.get_operation(operation.operation_id).result, visual=True)
    source = next(value for value in sources if value["ref"] == "vision:card:0")
    assert source["identity_title"] == app_title
    result = run(visual=True)["24"]
    _unchanged(repository, result)
    assert requests[-1].evidence == text
    assert "AIOps" not in requests[-1].evidence
    if client.calls:
        payload = json.loads(client.calls[0]["user_prompt"])
        assert all("AIOps" not in value["text"] for value in payload["sources"])


@pytest.mark.parametrize("fault", ["different_role", "different_volunteer", "different_date", "duplicate_owner",
                                 "different_city", "different_job_id"])
def test_ocr_bridge_does_not_join_different_or_nonunique_records(tmp_path, monkeypatch, fault):
    dom_title, visual_title = "智能工具&AIOps平台开发", "智能工具&AlOps平台开发"
    app_title = f"{dom_title}第 1 志愿"
    text = f"{visual_title} 第1志愿 官网投递 投递简历 {DAY}"
    dom = _dom(dom_title, f"{app_title} 官网投递 投递简历 {DAY}",
               raw_title=app_title, volunteer="1")
    records = [dom]
    if fault == "different_role":
        dom["title"] = "智能工具&Java平台开发"
        dom["raw_title"] = "智能工具&Java平台开发第 1 志愿"
        dom["context"] = dom["context"].replace("AIOps", "Java")
    elif fault == "different_volunteer":
        dom["raw_title"] = dom["raw_title"].replace("1", "2")
        dom["volunteer_index"] = "2"
        dom["context"] = dom["context"].replace("第 1 志愿", "第 2 志愿")
    elif fault == "different_date":
        dom["context"] = dom["context"].replace(DAY, "2026-09-29")
        dom["applied_at"] = "2026-09-29"
    elif fault == "duplicate_owner":
        records.append(deepcopy(dom))
    elif fault == "different_city":
        dom["title"] += "（深圳）"
        dom["raw_title"] = f"{dom_title}（深圳）第 1 志愿"
        dom["context"] = dom["context"].replace(dom_title, dom_title + "（深圳）")
        visual_title += "（北京）"
        text = text.replace("智能工具&AlOps平台开发", visual_title)
    else:
        dom["job_id"] = "J12345"
        dom["context"] += " 职位ID：J12345"
        text += " 职位ID：J54321"
    repository, _, _, _, run, _, _ = _setup(tmp_path, monkeypatch,
        text=text, visual_title=visual_title, app_title=app_title, dom_records=records)
    result = run(visual=True)["24"]
    assert result["state"] == "unresolved" and not result.get("wrote"), result
    assert result["reason"] != "status_evidence_conflict", result
    assert repository.list_applications()[0].stage == "applied"


@pytest.mark.parametrize("text", [
    f"{TITLE}\n当前进度：HR筛选中\n当前状态：面试中\n投递简历 {DAY}",
    f"{TITLE}\n当前状态：笔试中\n当前状态：面试中\n投递简历 {DAY}",
])
def test_true_current_conflicts_cannot_be_corrected_into_success(tmp_path, monkeypatch, text):
    repository, _, _, _, run, _, _ = _setup(tmp_path, monkeypatch,
        text=text, label="面试中", current=True,
        proposal=candidate(TITLE, label="面试中", observed_status="interview", ref=None,
                           quote="", evidence_ref="source:0"))
    result = run(visual=True)["24"]
    assert result["state"] == "unresolved" and result["reason"] == "status_evidence_conflict", result
    assert not result.get("wrote") and repository.list_applications()[0].stage == "applied"


def test_explicit_unknown_routing_current_does_not_turn_history_into_verified_applied(tmp_path, monkeypatch):
    text = f"{TITLE}\n当前进度：分配简历-流程中\n校园招聘 {DAY} 18:17 投递"
    repository, _, _, _, run, _, _ = _setup(tmp_path, monkeypatch,
        text=text, label="分配简历-流程中", current=True,
        dom_records=[_dom(TITLE, text, label="分配简历-流程中")])
    result = run(visual=True)["24"]
    assert result["state"] == "unresolved" and not result.get("wrote"), result
    assert result["reason"] != "status_evidence_conflict", result
    assert repository.list_applications()[0].stage == "applied"


@pytest.mark.parametrize("label,current", [("笔试中", True), ("官网投递", False), ("第1志愿", True)])
def test_unique_dated_assessment_sequence_reaches_written_even_if_model_says_applied(
        tmp_path, monkeypatch, label, current):
    text = f"{TITLE} 第1志愿\n官网投递\n投递简历\n{DAY}\n评估中\n{DAY}\n笔试中\n{DAY}"
    repository, _, _, client, run, requests, _ = _setup(tmp_path, monkeypatch,
        text=text, label=label, current=current, dom_records=[_dom(TITLE, text)])
    result = run(visual=True)["24"]
    assert result["state"] == "updated" and result.get("wrote"), json.dumps(result, ensure_ascii=False)
    assert result["observed_status"] == "written" and result["observed_label"] == "笔试中"
    assert requests[-1].evidence == text
    with repository.storage.session() as session:
        app = session.get(ApplicationSnapshot, "24")
        assert app.stage == "written" and len(app.stage_history) == 1
    again = run(visual=True)["24"]
    assert again["state"] == "unchanged" and not again.get("wrote")
    assert len(client.calls) <= 1


@pytest.mark.parametrize("saved", ["written", "interview1", "offer"])
def test_dated_baseline_with_bad_reader_metadata_never_regresses_later_saved_stage(
        tmp_path, monkeypatch, saved):
    text = f"{TITLE}\n官网投递\n投递简历\n{DAY}"
    repository, _, _, client, run, _, _ = _setup(tmp_path, monkeypatch,
        text=text, label="官网投递", current=False, saved=saved)
    result = run(visual=True)["24"]
    _unchanged(repository, result, stage=saved)
    assert result["observed_status"] == "applied"
    again = run(visual=True)["24"]
    _unchanged(repository, again, stage=saved)
    assert len(client.calls) <= 1


def test_readers_channel_badge_alone_is_not_a_dated_submission(tmp_path, monkeypatch):
    text = f"{TITLE}\n官网投递\n深圳 校招-正式"
    repository, _, _, _, run, _, _ = _setup(tmp_path, monkeypatch,
        text=text, label="官网投递", current=False)
    result = run(visual=True)["24"]
    assert result["state"] == "unresolved" and not result.get("wrote"), result
    assert repository.list_applications()[0].stage == "applied"
