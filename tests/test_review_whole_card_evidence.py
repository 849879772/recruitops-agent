"""Multiline live-failure shapes with synthetic identities, no website calls."""
import pytest

from packages.tools.application_page_evidence import page_sources, validate_page_candidate
from packages.domain.application_status_semantics import explicit_label_status
from tests.test_application_page_model_fallback import case, candidate, URL


def reading(title, text, label, *, current=True):
    return {"text": text, "confidence": 0.97, "reading_version": "literal-cards-v1",
            "cards": [{"title": title, "text": text, "current_label": label, "current": current}]}


@pytest.mark.parametrize("title,text,label", [
    ("AI工程师（AI Agent方向）", "示例2027届校招\nAI工程师（AI Agent方向）｜应届生｜软件类\n意向岗位：第一意向　意向城市：深圳市、成都市\n筛选 待评估（简历投递 成功）", "筛选 待评估（简历投递 成功）"),
    ("Agent开发工程师（FDE）", "Agent开发工程师（FDE） 第1志愿 官网投递\n投递简历", "投递简历"),
    ("测试设备开发工程师", "公司名称: 示例技术\n职位名称: 测试设备开发工程师\n简历筛选 · 进行中", "简历筛选 · 进行中"),
])
def test_whole_visual_card_binds_metadata_and_state_without_synthetic_prefix(title, text, label):
    app = {"id": "one", "job_title": title}
    observation = {"vision": reading(title, text, label)}
    sources = page_sources(observation, visual=True)
    assert sources[1]["ref"] == "vision:card:0"
    card, error = validate_page_candidate(observation, app, [app], card_title=title,
        source_ref="vision:card:0", quotation=text, label=label, status="applied", current=True, visual=True)
    assert error is None, error
    assert card["context"] == text and card["label"] == label


def test_status_before_title_inside_same_visual_card_is_supported():
    title, label = "研发工程师", "面试中"
    text = f"{label}\n{title}\n2026-09-23 投递"
    app = {"id": "one", "job_title": title}
    card, error = validate_page_candidate({"vision": reading(title, text, label)}, app, [app],
        card_title=title, source_ref="vision:card:0", quotation=text, label=label,
        status="interview", current=True, visual=True)
    assert error is None and card["status"] == "interview"


def test_dated_completed_submission_is_applied_baseline():
    title = "AI应用开发工程师"
    text = f"已完成的投递 (1)\n{title}\n校园招聘 | 2026-09-23 18:19 投递"
    observation = {"vision": reading(title, text, "投递", current=False)}
    app = {"id": "one", "job_title": title}
    card, error = validate_page_candidate(observation, app, [app], card_title=title,
        source_ref="vision:card:0", quotation=text, label="投递", status="applied", current=True, visual=True)
    assert error is None and card['status'] == 'applied'


def test_literal_current_assertion_outranks_incorrect_visual_metadata():
    title, label = "研发工程师", "面试中"
    text = f"{title}\n当前状态：{label}"
    app = {"id": "one", "job_title": title}
    card, error = validate_page_candidate({"vision": reading(title, text, label, current=False)}, app, [app],
        card_title=title, source_ref="vision:card:0", quotation=text, label=label,
        status="interview", current=True, visual=True)
    assert error is None and card["status"] == "interview"
    assert card["context"] == text


def test_generated_current_assertion_cannot_promote_an_unselected_visual_ladder():
    title, label = "研发工程师", "面试"
    text = f"{title}\n申请成功 → 筛选 → 笔试 → 面试 → Offer"
    app = {"id": "one", "job_title": title}
    card, error = validate_page_candidate({"vision": reading(title, text, label, current=False)}, app, [app],
        card_title=title, source_ref="vision:card:0", quotation=text, label=label,
        status="interview", current=True, visual=True)
    assert card is None and error == "record_present_status_unknown"


def test_visual_card_cannot_include_other_target_status():
    title, other, label = "研发工程师", "产品经理", "面试中"
    text = f"{title}\n已投递\n{other}\n{label}"
    app = {"id": "one", "job_title": title}
    card, error = validate_page_candidate({"vision": reading(title, text, label)}, app,
        [app, {"id": "two", "job_title": other}], card_title=title, source_ref="vision:card:0",
        quotation=text, label=label, status="interview", current=True, visual=True)
    assert card is None and error == "target_record_ambiguous"


@pytest.mark.parametrize("anchor,duplicate", [(False, False), (True, True), (True, False)])
def test_ocr_glyph_needs_unique_same_dom_record_and_timestamp(anchor, duplicate):
    title, ocr = "智能工具&AIOps平台开发", "智能工具&AlOps平台开发"
    text = f"{ocr}\n2026-09-14 投递简历"
    record = {"title": title, "context": f"{title}\n2026-09-14 投递简历" if anchor else title}
    observation = {"vision": reading(ocr, text, "投递简历"),
                   "application_records": [record, record.copy()] if duplicate else [record]}
    source = next(source for source in page_sources(observation, visual=True) if source["ref"] == "vision:card:0")
    assert source.get("identity_title") == (title if anchor and not duplicate else None)


def test_direct_visual_verifier_uses_whole_card_and_does_not_roll_back(tmp_path, monkeypatch):
    title, label = "测试设备开发工程师", "简历筛选 · 进行中"
    text = f"{title}\n公司名称: 示例技术\n{label}"
    _, _, _, _, run = case(tmp_path, monkeypatch, applications=[{"id": "24", "title": title,
        "record_url": URL, "stage": "interview1"}], observation={"vision": reading(title, text, label)},
        candidates=[candidate(title, label=label, quote=text, ref="vision:card:0", observed_status="applied")])
    row = run(visual=True)["24"]
    assert not row.get("wrote"), row


def test_excerpt_missing_title_has_distinct_scope_reason():
    title, label = "研发工程师", "面试中"
    text = f"{title} 当前状态：{label}"
    app = {"id": "one", "job_title": title}
    _, error = validate_page_candidate({"page": {"text": text}}, app, [app], card_title=title,
        source_ref="page:text", quotation=label, label=label, status="interview", current=True)
    assert error == "model_evidence_scope_incomplete"


@pytest.mark.parametrize("channel", ["官网投递", "官网主投递", "内推", "内推投递", "投递渠道"])
def test_channel_badge_is_not_a_current_stage(channel):
    assert explicit_label_status(channel) is None


def test_oppo_waiting_assessment_with_adjacent_category_in_ocr_title():
    title, label = "测试开发工程师", "待评估"
    text = f"{title} | 应届生 | 软件类\n意向岗位：第二意向\n简历投递\n成功\n筛选\n待评估\n面试\nOffer\n入职"
    app = {"id": "one", "job_title": title}
    card, error = validate_page_candidate({"vision": reading(title + " | 应届生 | 软件类", text, label)},
        app, [app], card_title=title, source_ref="vision:card:0", quotation=text, label=label,
        status="applied", current=True, visual=True)
    assert error is None and card["status"] == "applied"


def test_corroborated_ocr_is_verified_end_to_end_without_rewriting_text(tmp_path, monkeypatch):
    title, ocr, label = "智能工具&AIOps平台开发", "智能工具&AlOps平台开发", "初筛中"
    text = f"{ocr}\n2026-09-14\n{label}"
    record = {"title": title, "context": f"{title}\n2026-09-14\n初筛中"}
    _, _, _, _, run = case(tmp_path, monkeypatch, applications=[{"id": "24", "title": title, "record_url": URL}],
        observation={"vision": reading(ocr, text, label), "application_records": [record]},
        candidates=[candidate(ocr, label=label, quote=text, ref="vision:card:0", observed_status="applied")])
    row = run(visual=True)["24"]
    assert row["state"] == "unchanged", row.get("reason")


def test_visual_volunteer_suffix_does_not_erase_a_real_current_marker(tmp_path, monkeypatch):
    title, visible, label = "AI研发工程师-风控方向", "AI研发工程师-风控方向 第1志愿", "笔试中"
    text = f"{visible}\n投递简历 2026-09-27\n笔试中 2026-09-27"
    _, _, _, _, run = case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": visible, "record_url": URL}],
        observation={"vision": reading(visible, text, label)},
        candidates=[candidate(title, label=label, quote=text, ref="vision:card:0")])
    row = run(visual=True)["24"]
    assert row["state"] == "updated", row.get("reason")


def test_different_volunteer_suffixes_remain_different_visual_titles():
    from packages.tools.application_page_evidence import _visual_title_agrees
    assert not _visual_title_agrees("AI工程师 第1志愿", "AI工程师 第2志愿")


@pytest.mark.parametrize("visible", ["第1志愿 AI工程师", "2027届应届生校园招聘"])
def test_scoped_ocr_heading_or_volunteer_prefix_does_not_hide_current_state(visible):
    title, label = "AI工程师", "待评估"
    text = f"{visible}\n{title if visible.startswith('2027') else ''}\n意向城市：深圳市\n待评估"
    app = {"id": "one", "job_title": title}
    card, error = validate_page_candidate({"vision": reading(visible, text, label)}, app, [app],
        card_title=title, source_ref="vision:card:0", quotation=text, label=label,
        status="applied", current=True, visual=True)
    assert error is None and card["status"] == "applied"


def test_different_real_ocr_title_cannot_be_treated_as_recruitment_heading():
    from packages.tools.application_page_evidence import _visual_title_agrees
    assert not _visual_title_agrees("产品经理", "AI工程师")
    assert not _visual_title_agrees("第1志愿 AI工程师", "第2志愿 AI工程师")
