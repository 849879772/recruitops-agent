"""Sanitized failed-review regressions; all storage is temporary SQLite."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from packages.domain.application_identity import matching_records, title_in_context, unique_record
from packages.storage.models import ApplicationSnapshot, BrowserOperation, BrowserOperationEvent
from packages.tools.application_identity_binding import identity_candidates
from packages.tools.application_page_evidence import validate_page_candidate
from packages.tools.browser_status_update import BrowserStatusEntry, _match_entry
from test_application_identity_binding import case


@pytest.mark.parametrize("suffix", ["（接受调剂）", "(接受调剂)", "（服从调剂）"])
def test_transfer_preference_is_closed_display_metadata(suffix):
    card = {"raw_title": "Agent开发工程师" + suffix}
    assert unique_record({"job_title": "Agent开发工程师"}, [card]) is card


@pytest.mark.parametrize("title", [
    "Agent开发工程师（嵌入式方向）", "Agent开发工程师（深圳研发中心）",
    "Agent开发工程师(J12262)", "Agent开发工程师（接受调剂到算法岗）",
    "高级Agent开发工程师（接受调剂）",
])
def test_transfer_cleanup_does_not_erase_real_identity(title):
    assert matching_records({"job_title": "Agent开发工程师"}, [{"raw_title": title}]) == []


def test_transfer_preference_keeps_explicit_city_and_volunteer_constraints():
    cards = [{"raw_title": "Agent开发工程师（深圳）第1志愿（接受调剂）"},
             {"raw_title": "Agent开发工程师（北京）第2志愿（接受调剂）"}]
    assert matching_records({"job_title": "Agent开发工程师（深圳）第1志愿"}, cards) == [cards[0]]
    assert not matching_records({"job_title": "Agent开发工程师（深圳）第2志愿"}, cards)
    assert unique_record({"job_title": "Agent开发工程师"}, cards) is None


@pytest.mark.parametrize("title,text,expected", [
    ("AI测试开发工程师", "AI测试开发工程师 投递时间：2026/09/30 22:00:58", True),
    ("测试开发工程师", "AI测试开发工程师 投递时间：2026/09/30 22:00:58", False),
    ("AI 测试开发工程师", "ＡＩ测试开发工程师\n待开启", True),
    ("Agent开发工程师", "高级Agent开发工程师 流程中", False),
    ("Agent开发工程师", "Agent开发工程师（AI-Coding方向） 流程中", False),
    ("C++开发工程师", "C#开发工程师 当前状态：笔试中", False),
])
def test_context_matching_uses_full_original_title_boundaries(title, text, expected):
    assert title_in_context(title, text) is expected


def test_meituan_final_rule_does_not_count_shorter_other_role_as_an_owner():
    apps = [SimpleNamespace(id="ai-test", job_title="AI测试开发工程师"),
            SimpleNamespace(id="test", job_title="测试开发工程师")]
    text = "AI测试开发工程师 投递时间：2026/09/30 22:00:58"
    entry = BrowserStatusEntry(application_id="ai-test", title=apps[0].job_title,
        status="applied", label="投递时间：2026/09/30 22:00:58", context=text, evidence=text)
    match, reason = _match_entry(apps[0], apps, [entry])
    assert match is not None and reason == ""


def test_final_rule_still_rejects_evidence_crossing_two_complete_roles():
    apps = [SimpleNamespace(id="ai-test", job_title="AI测试开发工程师"),
            SimpleNamespace(id="test", job_title="测试开发工程师")]
    text = "AI测试开发工程师 待开启\n测试开发工程师 当前状态：笔试中"
    entry = BrowserStatusEntry(application_id="ai-test", title=apps[0].job_title,
        status="written", label="笔试中", context=text, evidence=text)
    match, reason = _match_entry(apps[0], apps, [entry])
    assert match is None and reason == "target_job_mismatch"


def _reading(cards):
    return {"reading_version": "literal-cards-v1", "text": "\n".join(card["text"] for card in cards),
            "confidence": .98, "cards": cards}


def test_tonghuashun_short_model_title_recovers_literal_scoped_transfer_title():
    full_title = "Agent开发工程师（接受调剂）"
    text = f"2027届校园招聘 人工智能集群 {full_title} INFP 流程中"
    app = {"id": "ths", "job_title": "Agent开发工程师"}
    observation = {"vision": _reading([{"title": full_title, "text": text,
        "current": True, "current_label": "流程中"}])}
    card, reason = validate_page_candidate(observation, app, [app], card_title=app["job_title"],
        source_ref="vision:card:0", quotation=text, label="流程中", status="unknown", current=True, visual=True)
    assert card is None and reason == "record_present_status_unknown"


def test_transfer_title_recovery_never_upgrades_generic_ongoing_wording():
    full_title = "Agent开发工程师（接受调剂）"
    text = f"{full_title} 流程中"
    app = {"id": "ths", "job_title": "Agent开发工程师"}
    observation = {"vision": _reading([{"title": full_title, "text": text,
        "current": True, "current_label": "流程中"}])}
    card, reason = validate_page_candidate(observation, app, [app], card_title=app["job_title"],
        source_ref="vision:card:0", quotation=text, label="流程中", status="written", current=True, visual=True)
    assert card is None and reason


@pytest.mark.parametrize("full_title", ["Agent开发工程师（深圳）", "Agent开发工程师（AI-Coding方向）", "Agent开发工程师(J12262)"])
def test_visual_short_title_cannot_hide_a_substantive_qualifier(full_title):
    text = f"{full_title} 当前状态：笔试中"
    app = {"id": "ths", "job_title": "Agent开发工程师"}
    observation = {"vision": _reading([{"title": full_title, "text": text,
        "current": True, "current_label": "笔试中"}])}
    card, reason = validate_page_candidate(observation, app, [app], card_title=app["job_title"],
        source_ref="vision:card:0", quotation=text, label="笔试中", status="written", current=True, visual=True)
    assert card is None and reason == "model_identity_mismatch"


def _store_reading(storage, cards, *, dom, audited=True):
    reading = _reading(cards)
    with storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "a").job_title = "AI测试开发工程师"
        operation = session.get(BrowserOperation, "op")
        operation.result = {**operation.result, "application_records": dom, "vision": reading}
        if audited:
            session.add(BrowserOperationEvent(event_id="vision-repair", operation_id="op", sequence=1,
                status="EXTRACTING", event_type="vision_analysis", payload=reading,
                occurred_at=datetime.now(timezone.utc)))


def _meituan_cards():
    times = ["22:00:17", "22:00:58", "22:01:51"]
    return [{"title": title, "text": f"{title} 志愿{index + 1} 投递时间：2026/09/30 {times[index]} 待开启",
             "current": False, "current_label": "待开启"}
            for index, title in enumerate(["AI Agent开发工程师", "AI测试开发工程师", "测试开发工程师"])]


def test_missing_meituan_visual_roles_are_offered_even_when_dom_has_another_card(case):
    storage, _, _ = case
    cards = _meituan_cards()
    _store_reading(storage, cards, dom=[{"title": cards[0]["title"], "context": cards[0]["text"]}])
    result = identity_candidates(storage, "a")
    assert [item["raw_title"] for item in result["candidates"]] == [card["title"] for card in cards]
    assert [item["evidence_source"] for item in result["candidates"]] == ["dom", "vision", "vision"]
    assert result["requires_user_confirmation"] and all(item["selectable"] for item in result["candidates"])
    with storage.session() as session:
        assert session.get(ApplicationSnapshot, "a").stage == "applied"


def test_unaudited_visual_roles_never_merge_into_dom_choices(case):
    storage, _, _ = case
    cards = _meituan_cards()
    _store_reading(storage, cards, dom=[{"title": cards[0]["title"], "context": cards[0]["text"]}], audited=False)
    result = identity_candidates(storage, "a")
    assert [item["raw_title"] for item in result["candidates"]] == [cards[0]["title"]]


def test_repeated_visual_roles_are_not_collapsed_against_a_single_dom_card(case):
    storage, _, _ = case
    card = _meituan_cards()[1]
    _store_reading(storage, [card, dict(card)], dom=[{"title": card["title"], "context": card["text"]}])
    result = identity_candidates(storage, "a")
    assert len(result["candidates"]) == 3
    assert result["requires_user_confirmation"] and all(not item["selectable"] for item in result["candidates"])


def test_same_title_with_different_capture_body_remains_a_manual_ambiguity(case):
    storage, _, _ = case
    card = _meituan_cards()[1]
    _store_reading(storage, [card], dom=[{"title": card["title"],
        "context": "AI测试开发工程师 投递时间：2026/09/29 10:00:00 面试中"}])
    result = identity_candidates(storage, "a")
    assert len(result["candidates"]) == 2 and result["requires_user_confirmation"]
    assert all(not item["selectable"] for item in result["candidates"])


@pytest.mark.parametrize("stage", ["applied", "written", "interview1"])
def test_meituan_visual_date_baseline_survives_final_verifier_without_borrowing_first_choice(tmp_path, monkeypatch, stage):
    from tests.test_application_page_model_fallback import case as model_case, URL
    from packages.tools.application_status_evidence import VerifyApplicationStatusEvidenceInput, verify_application_status_evidence

    cards = _meituan_cards()
    cards[0] = {**cards[0], "text": cards[0]["text"].replace("待开启", "笔试"),
                "current": True, "current_label": "笔试"}
    repository, store, operation, _, _ = model_case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": "AI测试开发工程师", "record_url": URL, "stage": stage},
                      {"id": "25", "title": "测试开发工程师", "record_url": URL},
                      {"id": "26", "title": "AI Agent开发工程师", "record_url": URL}],
        observation={"vision": _reading(cards), "application_records": [{
            "title": "AI Agent开发工程师", "context": cards[0]["text"], "label": "笔试",
            "status": "written", "signals": {"current_step_identified": True}}]})
    response = verify_application_status_evidence(VerifyApplicationStatusEvidenceInput(
        application_id="24", observation_operation_id=operation.operation_id, observed_status="applied",
        observed_label="投递时间：2026/09/30 22:00:58", evidence=cards[1]["text"], confidence=.98,
        captured_at="2026-09-29T01:00:00Z", source_ref="vision:card:1",
        source_title="AI测试开发工程师", current=False), store)
    assert response.success and not (response.verification and response.verification.data.wrote), response.model_dump()
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "24").stage == stage
        assert session.get(ApplicationSnapshot, "25").stage == "applied"
        assert session.get(ApplicationSnapshot, "26").stage == "applied"


def test_tonghuashun_visual_display_metadata_can_verify_specific_stage_but_not_rename_local_role(tmp_path, monkeypatch):
    from tests.test_application_page_model_fallback import case as model_case, URL
    from packages.tools.application_status_evidence import VerifyApplicationStatusEvidenceInput, verify_application_status_evidence

    title = "Agent开发工程师（接受调剂）"
    text = f"2027届校园招聘 {title} 当前状态：笔试中"
    repository, store, operation, _, _ = model_case(tmp_path, monkeypatch,
        applications=[{"id": "24", "title": "Agent开发工程师", "record_url": URL}],
        observation={"vision": _reading([{"title": title, "text": text,
            "current": True, "current_label": "笔试中"}])})
    response = verify_application_status_evidence(VerifyApplicationStatusEvidenceInput(
        application_id="24", observation_operation_id=operation.operation_id, observed_status="written",
        observed_label="笔试中", evidence=text, confidence=.98, captured_at="2026-09-29T01:00:00Z",
        source_ref="vision:card:0", source_title="Agent开发工程师", current=True), store)
    assert response.success, response.model_dump()
    with repository.storage.session() as session:
        application = session.get(ApplicationSnapshot, "24")
        assert application.stage == "written" and application.job_title == "Agent开发工程师"


@pytest.mark.parametrize("generic", ["流程中", "进行中"])
def test_scoped_visual_current_stage_overrides_only_generic_dom_wording(generic):
    title = "AI应用工程师"
    app = {"id": "a", "job_title": title}
    text = f"{title}\n笔试中"
    dom = {"title": title, "label": generic, "status": "applied", "context": f"{title}\n{generic}",
           "raw_status_labels": [generic]}
    observation = {"application_records": [dom], "vision": _reading([
        {"title": title, "text": text, "current": True, "current_label": "笔试中"}])}
    card, reason = validate_page_candidate(observation, app, [app], card_title=title,
        source_ref="vision:card:0", quotation=text, label="笔试中", status="written", current=True, visual=True)
    assert card and card["status"] == "written" and reason is None


@pytest.mark.parametrize("dom_label,extra_context,raw_labels,expected_conflict", [
    ("已投递", "", ["已投递"], True),
    ("面试中", "", ["面试中"], True),
    ("投递时间：2026/09/30 22:00:58", "", ["投递时间：2026/09/30 22:00:58"], False),
    ("投递时间：2026/02/30", "", ["投递时间：2026/02/30"], True),
    ("流程中", "\n投递时间：2026/09/30 22:00:58", ["流程中"], False),
    ("流程中", "", ["流程中", "面试中"], True),
])
def test_visual_current_stage_ignores_valid_history_but_not_current_conflicts(dom_label, extra_context, raw_labels, expected_conflict):
    title = "AI应用工程师"
    app = {"id": "a", "job_title": title}
    text = f"{title}\n笔试中"
    observation = {"application_records": [{"title": title, "label": dom_label,
        "context": f"{title}\n{dom_label}{extra_context}", "raw_status_labels": raw_labels}],
        "vision": _reading([{"title": title, "text": text, "current": True, "current_label": "笔试中"}])}
    card, reason = validate_page_candidate(observation, app, [app], card_title=title,
        source_ref="vision:card:0", quotation=text, label="笔试中", status="written", current=True, visual=True)
    if expected_conflict:
        assert card is None and reason == "status_evidence_conflict"
    else:
        assert card and card["status"] == "written" and reason is None


@pytest.mark.parametrize("scope", ["inactive", "unscoped", "false_literal"])
def test_generic_dom_exception_still_requires_a_real_scoped_current_visual_assertion(scope):
    title = "AI应用工程师"
    app = {"id": "a", "job_title": title}
    label = "笔试未通过" if scope == "false_literal" else "笔试中"
    text = f"{title} 当前状态: {label}"
    observation = {"application_records": [{"title": title, "label": "流程中",
        "context": f"{title}\n流程中", "raw_status_labels": ["流程中"]}],
        "vision": _reading([{"title": title, "text": text,
            "current": scope != "inactive", "current_label": label}])}
    card, reason = validate_page_candidate(observation, app, [app], card_title=title,
        source_ref="vision:text" if scope == "unscoped" else "vision:card:0",
        quotation=text, label=label, status="written", current=scope != "inactive", visual=True)
    assert card is None and reason in {"record_present_status_unknown", "status_evidence_conflict", "status_semantics_unsupported"}
