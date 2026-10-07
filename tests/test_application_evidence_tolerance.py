"""Presentation tolerance is not permission to borrow or invent evidence."""
import pytest

from packages.domain.application_evidence_text import evidence_spans, evidence_text_key, localize_evidence
from packages.domain.application_status_semantics import explicit_label_status
from packages.tools.application_page_evidence import validate_page_candidate


def _validate(text, *, title="软件工程师（深圳）(J12262)", quote=None, label="初筛中",
              status="applied", applications=None, records=None, application=None):
    application = application or {"id": "one", "job_title": title}
    return validate_page_candidate(
        {"page": {"text": text}, "application_records": records or []}, application,
        applications or [application], card_title=title, source_ref="page:text",
        quotation=quote or text, label=label, status=status, current=True,
    )


def test_normalized_evidence_returns_original_offsets_and_text():
    text = "前文\n软件工程师（深圳）(J12262) 当前状态：初筛中\n深圳 互联网 / 电子 / 网游 - 研发 项目：27届校招"
    quote = "软件工程师(深圳)(J12262)当前状态:初筛中深圳互联网/电子/网游-研发项目:27届校招"
    original = localize_evidence(text, quote)
    assert original == text.split("\n", 1)[1]
    start, end = evidence_spans(text, quote)[0]
    assert text[start:end] == original
    assert evidence_text_key("Ｃ＋＋") != evidence_text_key("C#")
    assert localize_evidence("面试未通过", "面试通过") is None
    assert localize_evidence("工程师(J12262)", "工程师(J12263)") is None


def test_whitespace_and_fullwidth_quote_restore_original_evidence():
    text = "软件工程师（深圳）(J12262) 当前状态：初筛中\n深圳 互联网 / 电子 / 网游 - 研发 项目：27届校招"
    quote = "软件工程师(深圳)(J12262)当前状态:初筛中深圳互联网/电子/网游-研发项目:27届校招"
    card, error = _validate(text, title="软件工程师(深圳)(J12262)", quote=quote)
    assert error is None
    assert card["context"] == text
    assert card["title"] == "软件工程师（深圳）(J12262)"


def test_other_roles_on_page_do_not_veto_a_bound_local_quote():
    first, second = "软件工程师（应用软件部）-27届校招(J11510)", "机器人软件工程师-27届校招(J11519)"
    quote = f"{first} 当前状态：初筛中"
    text = f"{quote}\n{second} 当前状态：面试中"
    apps = [{"id": "one", "job_title": first}, {"id": "two", "job_title": second}]
    card, error = _validate(text, title=first, quote=quote, applications=apps)
    assert error is None and card["status"] == "applied"


@pytest.mark.parametrize("foreign", ["产品经理", "软件工程师（北京）(J12263)"])
def test_known_or_unregistered_intervening_card_cannot_lend_status(foreign):
    title = "软件工程师（深圳）(J12262)"
    text = f"{title} 投递于昨日 {foreign} 当前状态：面试中"
    card, error = _validate(text, label="面试中", status="interview")
    assert card is None and error == "record_present_status_unknown"


def test_quotation_cannot_cross_a_known_other_card():
    title, other = "软件工程师（深圳）(J12262)", "产品经理"
    apps = [{"id": "one", "job_title": title}, {"id": "two", "job_title": other}]
    card, error = _validate(f"{title} 当前状态：初筛中\n{other} 当前状态：面试中", applications=apps)
    assert card is None and error == "target_record_ambiguous"


@pytest.mark.parametrize("actual", ["高级软件工程师", "软件工程师（北京）(J12263)", "C#工程师"])
def test_partial_role_location_and_language_names_are_not_fuzzy_identity(actual):
    requested = "C++工程师" if actual == "C#工程师" else "软件工程师"
    card, error = _validate(f"{actual} 当前状态：初筛中", title=requested)
    assert card is None and error == "model_identity_mismatch"


def test_stable_id_cannot_borrow_another_same_named_card():
    title = "软件工程师"
    first = {"title": title, "job_id": "101", "context": f"{title} 当前状态：初筛中"}
    second = {"title": title, "job_id": "102", "context": f"{title} 当前状态：面试中"}
    application = {"id": "one", "job_title": title, "external_job_id": "101"}
    card, error = _validate(first["context"] + "\n" + second["context"], title=title,
                            quote=second["context"], label="面试中", status="interview",
                            records=[first, second], application=application)
    assert card is None and error == "target_record_ambiguous"


@pytest.mark.parametrize("text,quote,label,status", [
    ("软件工程师 当前状态：面试未通过", "软件工程师 当前状态：面试", "面试", "interview"),
    ("软件工程师 当前状态：面试 未安排", "软件工程师 当前状态：面试", "面试", "interview"),
    ("软件工程师 当前状态：如果通过初筛将安排面试", None, "如果通过初筛将安排面试", "interview"),
    ("软件工程师 已投递 → 初筛 → 笔试 → 面试", None, "面试", "interview"),
])
def test_cropped_negation_future_and_unmarked_ladder_are_not_current(text, quote, label, status):
    card, error = _validate(text, title="软件工程师", quote=quote, label=label, status=status)
    assert card is None


@pytest.mark.parametrize("label,expected", [
    ("初筛中", "applied"), ("简历初筛中", "applied"), ("初筛未通过", "rejected"),
    ("尚未初筛", None), ("预计初筛", None), ("初筛通过后将安排面试", None),
])
def test_initial_screening_uses_shared_semantics_and_negation(label, expected):
    assert explicit_label_status(label) == expected


def _badge_observation(metadata="2026-09-24 投递 官网主投 初筛中", *, tag="article", role="", frame=0, ladder=False):
    title = "示例工程师【2027届】"
    text = f"{metadata} {title} 示例城市 技术类全职"
    card = {"title": title, "context": text, "signals": {"has_progress_timeline": ladder}}
    nodes = [{"text": text, "tag": "article", "frameId": 0,
              "rect": {"x": 0, "y": 0, "width": 300, "height": 100}},
             {"text": metadata, "tag": tag, "role": role, "frameId": frame,
              "rect": {"x": 0, "y": 0, "width": 300, "height": 20}}]
    return title, {"application_records": [card], "page": {"text": text}, "semantic_nodes": nodes}


@pytest.mark.parametrize("metadata", ["初筛中", "2026-09-24 投递 官网主投 初筛中", "投递 内推 初筛中"])
def test_unique_card_ongoing_badge_needs_no_current_prefix_or_active_class(metadata):
    title, observation = _badge_observation(metadata)
    app = {"id": "one", "job_title": title}
    card, error = validate_page_candidate(observation, app, [app], card_title=title,
        source_ref="node:0", quotation=observation["page"]["text"], label="初筛中", status="applied",
        current=True, current_node_ref="node:1")
    assert error is None and card["label"] == "初筛中"


@pytest.mark.parametrize("extra", [
    {"tag": "button"}, {"role": "button"}, {"frame": 1}, {"ladder": True},
    {"metadata": "历史状态 初筛中"}, {"metadata": "将进入 初筛中"},
    {"metadata": "2026-09-24 初筛中"}, {"metadata": "已投递 初筛中 笔试中 面试中"},
])
def test_badge_never_borrows_buttons_frames_history_future_or_unmarked_ladder(extra):
    title, observation = _badge_observation(**extra)
    app = {"id": "one", "job_title": title}
    card, error = validate_page_candidate(observation, app, [app], card_title=title,
        source_ref="node:0", quotation=observation["page"]["text"], label="初筛中", status="applied",
        current=True, current_node_ref="node:1")
    assert card is None and error == "record_present_status_unknown"


def test_multiple_different_ongoing_badges_do_not_prove_current_stage():
    title, observation = _badge_observation("初筛中")
    observation["semantic_nodes"].append({"text": "面试中", "tag": "span", "frameId": 0,
        "rect": {"x": 0, "y": 25, "width": 50, "height": 20}})
    app = {"id": "one", "job_title": title}
    card, error = validate_page_candidate(observation, app, [app], card_title=title,
        source_ref="node:0", quotation=observation["page"]["text"], label="初筛中", status="applied",
        current=True, current_node_ref="node:1")
    assert card is None and error == "record_present_status_unknown"
