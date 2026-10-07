"""Anonymous identity regressions; all resolver writes use disposable SQLite."""
from copy import deepcopy
import json

import pytest

from packages.domain.application_identity import is_application_title, matching_records, unique_record
from packages.tools.application_page_evidence import page_sources, validate_page_candidate
from tests.test_application_page_model_fallback import URL, candidate, case


DAY = "2026-09-21"
DOM_TITLE = "Embodied Agent Developer (具身智能 Agent 开发工程师)"
OCR_TITLE = "Embedded Agent Developer (具身智能 Agent 开发工程师)"
NOTICE = "仅部分信息技术类岗位需进行笔试"


def _reading(title, text, *, current=False, label=""):
    return {"reading_version": "literal-cards-v1", "text": text,
            "cards": [{"title": title, "text": text, "current": current, "current_label": label}],
            "confidence": .95, "model": "fixture", "image_sha256": "a" * 64}


def _observation(dom_title=DOM_TITLE, visual_title=OCR_TITLE, *, dom_card=False):
    dom = f"{dom_title} 官网投递 北京、上海 意向城市：上海 投递简历 {DAY}"
    visual = f"{visual_title} 官网投递 北京、上海 2. 意向城市：上海 投递简历 {DAY}"
    return {"page": {"text": dom}, "page_segments": [{"frameId": 0, "text": dom}],
            "application_records": [{"title": dom_title, "raw_title": dom_title,
                                     "context": dom, "label": "", "raw_status_labels": []}] if dom_card else [],
            "vision": _reading(visual_title, visual)}


def _visual_source(observation):
    return next(source for source in page_sources(observation, visual=True) if source["ref"] == "vision:card:0")


@pytest.mark.parametrize("dom_title,visual_title,dom_card", [
    ("【27届】机器人系统工程师（控制）", "【27届】机器人系统工程【师】（控制）", True),
    (DOM_TITLE, OCR_TITLE, False),
    (DOM_TITLE, OCR_TITLE, True),
])
@pytest.mark.parametrize("selector", ["option", "literal", "canonical_copy"])
def test_same_capture_ocr_identity_preserves_visual_quote_and_updates_once(
        tmp_path, monkeypatch, dom_title, visual_title, dom_card, selector):
    observation = _observation(dom_title, visual_title, dom_card=dom_card)
    before = deepcopy(observation)
    text = observation["vision"]["text"]
    proposal = candidate(visual_title, observed_status="applied", label="投递简历", ref="vision:card:0", quote=text)
    if selector == "option":
        proposal.update(card_title=dom_title, evidence_ref="source:0", quotation="", source_ref=None)
    elif selector == "canonical_copy":
        proposal.update(card_title=dom_title, quotation=text.replace(visual_title, dom_title))
    repository, store, operation, client, run = case(tmp_path, monkeypatch, observation=observation,
        applications=[{"id": "24", "title": dom_title, "record_url": URL}], candidates=[proposal])
    source = _visual_source(observation)
    assert source["identity_title"] == dom_title and source["text"] == text
    # OCR repair remains evidence-local, never a global alias for these titles.
    assert not matching_records({"job_title": dom_title}, [{"title": visual_title}])
    result = run(visual=True)["24"]
    assert result["state"] == "unchanged" and result["observed_status"] == "applied", result
    assert not result["wrote"] and repository.list_applications()[0].stage == "applied"
    assert store.get_operation(operation.operation_id).result["vision"] == before["vision"]
    payload = json.loads(client.calls[0]["user_prompt"])
    assert all(dom_title not in source["text"] for source in payload["sources"])
    assert run(visual=True)["24"]["state"] == "unchanged" and len(client.calls) == 1


@pytest.mark.parametrize("fault", [
    "different_chinese_role", "different_english_role", "different_date", "no_visual_date",
    "different_city", "different_location_badge", "different_title_city", "different_job_id", "different_application_id",
    "duplicate_dom", "missing_dom", "separate_card_date",
])
def test_bilingual_bridge_rejects_unproved_or_conflicting_identity(tmp_path, monkeypatch, fault):
    observation = _observation()
    text = observation["page"]["text"]
    if fault == "different_chinese_role":
        text = text.replace("具身智能", "数据平台")
    elif fault == "different_english_role":
        text = text.replace("Embodied", "Backend")
    elif fault == "different_date":
        text = text.replace(DAY, "2026-09-20")
    elif fault == "different_city":
        text = text.replace("意向城市：上海", "意向城市：北京")
    elif fault == "different_location_badge":
        text = text.replace("北京、上海", "深圳、上海")
    elif fault == "different_title_city":
        text = text.replace(DOM_TITLE, DOM_TITLE + "（北京）")
    elif fault in {"different_job_id", "different_application_id"}:
        field = "职位ID" if fault == "different_job_id" else "申请ID"
        text = text.replace("投递简历", f"{field}：ID1111 投递简历")
        for card in observation["vision"]["cards"]:
            card["text"] = card["text"].replace("投递简历", f"{field}：ID2222 投递简历")
        observation["vision"]["text"] = observation["vision"]["cards"][0]["text"]
    elif fault == "duplicate_dom":
        text += "\n" + text
    elif fault == "missing_dom":
        text = "应聘记录"
    elif fault == "no_visual_date":
        observation["vision"]["text"] = observation["vision"]["text"].replace(DAY, "")
        observation["vision"]["cards"][0]["text"] = observation["vision"]["text"]
    else:
        text = text.replace("投递简历", "\n其他工程师 官网投递 投递简历")
    observation["page"]["text"] = text
    observation["page_segments"] = []
    assert "identity_title" not in _visual_source(observation)
    repository, _, _, client, run = case(tmp_path, monkeypatch, observation=observation,
        applications=[{"id": "24", "title": DOM_TITLE, "record_url": URL}])
    result = run(visual=True)["24"]
    assert result["state"] == "unresolved" and result["reason"] == "target_record_not_matched", result
    assert client.calls == [] and repository.list_applications()[0].stage == "applied"


@pytest.mark.parametrize("dom_card", [False, True])
def test_literal_dom_role_prevents_a_nearby_ocr_alias(dom_card):
    observation = _observation(dom_card=dom_card)
    other = observation["page"]["text"].replace(DOM_TITLE, OCR_TITLE)
    if dom_card:
        observation["application_records"].append({"title": OCR_TITLE, "context": other})
    else:
        observation["page"]["text"] += "\n" + other
        observation["page_segments"] = []
    assert "identity_title" not in _visual_source(observation)


def test_dom_state_cannot_fill_a_cropped_visual_card(tmp_path, monkeypatch):
    dom_title, visual_title = "机器人系统工程师（控制）", "机器人系统工程【师】（控制）"
    observation = _observation(dom_title, visual_title, dom_card=True)
    observation["application_records"][0]["context"] += " 当前状态：面试中"
    text = observation["vision"]["text"]
    repository, _, _, client, run = case(tmp_path, monkeypatch, observation=observation,
        applications=[{"id": "24", "title": dom_title, "record_url": URL}],
        candidates=[candidate(visual_title, observed_status="interview", label="面试中",
                              ref="vision:card:0", quote=text + " 当前状态：面试中")])
    result = run(visual=True)["24"]
    assert result["state"] == "unresolved" and not result.get("wrote"), result
    assert repository.list_applications()[0].stage == "applied"
    payload = json.loads(client.calls[0]["user_prompt"])
    assert all("面试中" not in source["text"] for source in payload["sources"])


@pytest.mark.parametrize("title,valid", [(NOTICE, False), ("仅部分算法岗位需要参加测评。", False),
                                       ("笔试系统工程师", True), ("信息技术岗位研发工程师", True)])
def test_conditional_process_notice_is_not_a_job_title(title, valid):
    assert is_application_title(title) is valid


@pytest.mark.parametrize("real_foreign_card", [False, True])
def test_notice_does_not_create_cross_job_ambiguity_but_real_foreign_card_does(real_foreign_card):
    title = "后端开发工程师-机器人"
    app = {"id": "24", "job_title": title}
    foreign = "算法工程师" if real_foreign_card else NOTICE
    text = f"{foreign} 正在进行的岗位：{title}/事业部 1 筛选 简历筛选 中 2 笔试 3 面试"
    observation = {"application_records": [{"title": foreign, "context": foreign}],
                   "vision": _reading(title, text, current=True, label="简历筛选 中")}
    verified, reason = validate_page_candidate(observation, app, [app], card_title=title,
        source_ref="vision:card:0", quotation=text, label="简历筛选 中", status="applied",
        current=True, visual=True)
    if real_foreign_card:
        assert verified is None and reason == "target_record_ambiguous"
    else:
        assert verified and reason is None


def test_present_visual_cards_report_unmatched_target_not_missing_records(tmp_path, monkeypatch):
    observation = {"page": {"text": "应聘记录"}, "application_records": [],
                   "vision": _reading("其他工程师", "其他工程师 当前状态：笔试中", current=True, label="笔试中")}
    _, _, _, client, run = case(tmp_path, monkeypatch, observation=observation,
        applications=[{"id": "24", "title": "目标工程师", "record_url": URL}])
    assert run(visual=True)["24"]["reason"] == "target_record_not_matched"
    assert client.calls == []


def _baseline_case(tmp_path, monkeypatch, *, fault=None):
    title = "应用研发工程师"
    text = f"{title}\n校园招聘 | {DAY} 22:13 投递\n查看/打印"
    observation = {"page": {"text": title}, "application_records": [],
                   "vision": _reading(title, text)}
    applications = [{"id": "24", "title": title, "record_url": URL}]
    if fault == "low_confidence":
        observation["vision"]["confidence"] = .79
    elif fault == "capture_truncated":
        observation["vision_capture"] = {"truncated": True}
    elif fault == "capture_failed":
        observation["vision_error"] = "VISION_CAPTURE_FAILED"
    elif fault == "missing_cards":
        observation["vision"]["cards"] = []
    elif fault == "duplicate_cards":
        observation["vision"]["cards"] *= 2
    elif fault == "duplicate_owner":
        applications.append({"id": "25", "title": title, "record_url": URL})
    elif fault == "visible_role_qualifier":
        visible = title + "（AI-Coding方向）(J12345)"
        text = text.replace(title, visible)
        observation["vision"] = _reading(visible, text)
    elif fault in {"role_qualifier", "department_qualifier", "changed_role"}:
        applications[0]["title"] = {"role_qualifier": title + "（AI-Coding方向）",
            "department_qualifier": title + "/库卡中国", "changed_role": "算法研发工程师"}[fault]
    elif fault == "later_saved_stage":
        applications[0]["stage"] = "interview1"
    elif fault == "dom_newer_state":
        observation["application_records"] = [{"title": title, "context": text + "\n当前状态：面试中",
            "label": "面试中", "raw_status_labels": ["面试中"], "status": "interview"}]
    elif fault in {"current_conflict", "unknown_current", "later_current"}:
        label = {"current_conflict": "笔试中", "unknown_current": "流程中",
                 "later_current": "面试中"}[fault]
        text += f"\n当前状态：{label}"
        if fault == "current_conflict":
            text += "\n当前状态：面试中"
        observation["vision"] = _reading(title, text, current=True, label=label)
    elif fault == "later_active_node":
        text = f"{title}\n申请成功 筛选 笔试 面试"
        observation["vision"] = _reading(title, text, current=True, label="笔试")
    return case(tmp_path, monkeypatch, observation=observation, applications=applications,
        candidates=[candidate(title, observed_status="applied", label="投递", ref=None,
                              quote="", evidence_ref="source:0")])


def test_literal_visual_baseline_does_not_call_status_model_and_preserves_quote(tmp_path, monkeypatch):
    from packages.tools import application_status_model as model

    repository, store, operation, client, run = _baseline_case(tmp_path, monkeypatch)
    reading = deepcopy(store.get_operation(operation.operation_id).result["vision"])
    requests = []
    original = model.verify_application_status_evidence

    def checked(request, bridge):
        requests.append(request)
        return original(request, bridge)

    monkeypatch.setattr(model, "verify_application_status_evidence", checked)
    for _ in range(2):
        result = run(visual=True)["24"]
        assert result["state"] == "unchanged" and result["reason"] == "no_newer_status_observed"
        assert result["model_disposition"] == "rule_resolved" and not result["wrote"]
    assert client.calls == []
    assert all(request.read_only and request.evidence == reading["cards"][0]["text"] for request in requests)
    assert store.get_operation(operation.operation_id).result["vision"] == reading
    assert repository.list_applications()[0].stage_history == []
    events = store.get_events(operation.operation_id)
    assert any(event.event_type == "vision_analysis" for event in events)
    assert not any(event.event_type == "status_model_request" for event in events)


@pytest.mark.parametrize("fault", ["low_confidence", "capture_truncated", "capture_failed", "missing_cards",
    "duplicate_cards", "duplicate_owner", "role_qualifier", "visible_role_qualifier", "department_qualifier", "changed_role",
    "later_saved_stage", "dom_newer_state", "current_conflict", "unknown_current", "later_current", "later_active_node"])
def test_visual_baseline_shortcut_cannot_resolve_unproved_identity_or_stage(tmp_path, monkeypatch, fault):
    repository, _, _, _, run = _baseline_case(tmp_path, monkeypatch, fault=fault)
    result = run(visual=True)["24"]
    assert result.get("model_disposition") != "rule_resolved", result
    if fault in {"duplicate_owner", "role_qualifier", "visible_role_qualifier", "department_qualifier", "changed_role", "current_conflict",
                 "unknown_current", "dom_newer_state"}:
        assert result["state"] == "unresolved" and not result.get("wrote"), result
    if fault == "later_saved_stage":
        assert repository.list_applications()[0].stage == "interview1"
    if fault == "later_current":
        assert result["model_disposition"] == "called" and result["state"] == "updated", result
        assert repository.list_applications()[0].stage == "interview1"


def test_visual_stage_change_keeps_real_status_model_service_failure(tmp_path, monkeypatch):
    from packages.tools import application_status_model as model

    repository, _, _, _, run = _baseline_case(tmp_path, monkeypatch, fault="later_current")
    monkeypatch.setattr(model, "configured_model_client", lambda _: None)
    result = run(visual=True)["24"]
    assert result["state"] == "unresolved" and result["reason"] == "model_unavailable", result
    assert result["model_disposition"] != "rule_resolved" and not result.get("wrote")
    assert repository.list_applications()[0].stage == "applied"


def test_read_only_baseline_cannot_write_if_saved_stage_changes_during_verification(tmp_path, monkeypatch):
    from packages.storage.models import ApplicationSnapshot
    from packages.tools import application_status_model as model

    repository, _, _, client, run = _baseline_case(tmp_path, monkeypatch)
    original = model.verify_application_status_evidence

    def changed(request, bridge):
        assert request.read_only
        with repository.storage.session() as session:
            session.get(ApplicationSnapshot, "24").stage = "interested"
            session.commit()
        return original(request, bridge)

    monkeypatch.setattr(model, "verify_application_status_evidence", changed)
    result = run(visual=True)["24"]
    assert result["state"] == "unresolved" and result["reason"] == "read_only_baseline_not_confirmed"
    assert result["model_disposition"] == "skipped" and not result["wrote"] and not client.calls
    saved = repository.list_applications()[0]
    assert saved.stage == "interested" and saved.stage_history == []


@pytest.mark.parametrize("suffix", ["-27届秋招", "-2027届秋招", "-27届春招", "-2027届校招"])
def test_closed_trailing_cohort_agrees_without_erasing_role_or_city(suffix):
    title = "服务端开发工程师-AI应用开发方向（南京）"
    assert matching_records({"job_title": title}, [{"title": title + suffix}])
    assert matching_records({"job_title": "27届-" + title}, [{"title": title + suffix}])


@pytest.mark.parametrize("target,visible", [
    ("26届-研发工程师", "研发工程师-27届秋招"),
    ("研发工程师", "26届-研发工程师-27届秋招"),
    ("27届-研发工程师-26届秋招", "研发工程师"),
    ("研发工程师（南京）", "研发工程师（上海）-27届秋招"),
    ("研发工程师", "研发工程师（AI-Coding方向）-27届秋招"),
    ("研发工程师", "研发工程师(J12345)-27届秋招"),
    ("研发工程师", "研发工程师--27届秋招"),
    ("研发工程师", "研发工程师-27届秋招-后端方向"),
])
def test_trailing_cohort_cannot_hide_conflicting_year_city_or_role(target, visible):
    assert not matching_records({"job_title": target}, [{"title": visible}])


def test_multiple_cohort_cards_still_require_unique_identity():
    cards = [{"title": "研发工程师（南京）-26届秋招"}, {"title": "研发工程师（南京）-27届秋招"}]
    assert unique_record({"job_title": "研发工程师（南京）"}, cards) is None
    assert matching_records({"job_title": "27届-研发工程师（南京）"}, cards) == [cards[1]]


@pytest.mark.parametrize("title,valid", [("笔试/AI语言测试", False), ("笔试／AI语言测试", False),
    ("AI语言测试工程师", True), ("笔试系统测试开发工程师", True), ("AI语言测试/研发工程师", True)])
def test_complete_process_component_is_not_a_job_title(title, valid):
    assert is_application_title(title) is valid


@pytest.mark.parametrize("shape", ["trailing_cohort", "process_component_title"])
def test_live_ats_parser_shapes_keep_a_unique_real_visual_role(tmp_path, monkeypatch, shape):
    title = "服务端开发工程师-AI应用开发方向（南京）" if shape == "trailing_cohort" else "27届秋招-自动驾驶-测试开发工程师"
    visible = title + "-27届秋招" if shape == "trailing_cohort" else title
    label, status = ("处理中", "applied") if shape == "trailing_cohort" else ("笔试/AI语言测试", "written")
    text = f"{visible}\n查看详情\n状态：{label} 项目：-\n{DAY} 1:44"
    dom_title = visible if shape == "trailing_cohort" else label
    observation = {"page": {"text": text}, "application_records": [
        {"title": dom_title, "context": text, "label": label, "status": status}],
        "vision": _reading(visible, text, current=shape == "trailing_cohort",
                           label=label if shape == "trailing_cohort" else "")}
    repository, _, _, client, run = case(tmp_path, monkeypatch, observation=observation,
        applications=[{"id": "24", "title": title, "record_url": URL, "stage": status}],
        candidates=[candidate(title, observed_status=status, label=label, ref=None,
                              quote="", evidence_ref="source:0")])
    result = run(visual=True)["24"]
    assert result["state"] == "unchanged" and result["observed_status"] == status, json.dumps(result, ensure_ascii=False)
    assert not result.get("wrote") and repository.list_applications()[0].stage == status


def test_unstarted_assessment_card_is_not_promoted_by_submission_date(tmp_path, monkeypatch):
    title = "应用软件开发工程师"
    text = (f"{title} 面试城市：重庆 申请时间：{DAY}\n"
            "软件专业笔试 未开始 2026.09.08 - 2027.09.08 点击这里开始 应用软件专业面试 综合面试")
    repository, _, _, _, run = case(tmp_path, monkeypatch,
        observation={"application_records": [], "vision": _reading(title, text, current=True, label="未开始")},
        applications=[{"id": "24", "title": title, "record_url": URL}],
        candidates=[candidate(title, observed_status="unknown", label="未开始", ref=None,
                              quote="", evidence_ref="source:0")])
    result = run(visual=True)["24"]
    assert result["state"] == "unresolved" and result["reason"] == "model_uncertain", result
    assert not result.get("wrote") and repository.list_applications()[0].stage == "applied"
