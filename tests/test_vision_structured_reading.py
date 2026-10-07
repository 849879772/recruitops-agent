from __future__ import annotations

import json

import pytest

from packages.vision import VisionError, VisionResult, VisionService
from packages.vision import service as vision
from packages.vision.observation import analyze_observation
from test_vision_service import IMAGE, prepare, response


def structured(value):
    raw = response()
    raw["choices"][0]["message"]["content"] = json.dumps(value, ensure_ascii=False)
    return raw


def card(title="软件工程师", label="简历筛选", current=True):
    return {"title": title, "text": title + " " + label, "current_label": label, "current": current}


def test_literal_ocr_and_visual_current_judgment_are_separate():
    raw_text = "软件工程师\n申请成功 简历筛选 笔试 面试"
    raw = structured({"text": raw_text, "confidence": 0.9,
        "cards": [{"title": "软件工程师", "text": raw_text, "current_label": "", "current": False}],
        "explanation": "当前状态不明确", "reading_version": "provider-invented"})
    calls = []
    result = VisionService(api_key="fixture", transport=lambda *args: calls.append(args) or raw).analyze(IMAGE)
    assert result.text == raw_text
    assert "当前状态不明确" not in result.text
    assert result.cards[0].current is False and result.cards[0].current_label == ""
    assert result.cards[0].text == raw_text
    assert result.reading_version == "literal-cards-v1"
    assert "explanation" not in result.model_dump()
    assert len(calls) == 1


def test_invalid_cards_are_isolated_and_literals_recovered_without_type_coercion():
    valid = card("软件工程师（深圳）")
    invalid = [
        {**valid, "current": "true"},
        {**valid, "current": True, "current_label": ""},
        {**valid, "current_label": "已录用"},
        {**valid, "title": "算法工程师"},
        card("私自拼接的岗位"),
    ]
    raw = structured({"text": valid["text"], "confidence": 0.9,
        "cards": [{**valid, "title": "软件工程师(深圳)", "text": valid["text"].replace(" ", "")}, *invalid]})
    result = VisionService(api_key="fixture", transport=lambda *_: raw).analyze(IMAGE)
    assert len(result.cards) == 1
    assert result.cards[0].model_dump() == valid
    assert result.text == valid["text"]
    assert {item["card_index"] for item in result.diagnostics if "card_index" in item} == {1, 2, 3, 4, 5}
    assert "私自拼接" not in json.dumps(result.diagnostics, ensure_ascii=False)


@pytest.mark.parametrize("cards", [None, "not an array", {}])
def test_malformed_optional_cards_do_not_discard_valid_legacy_ocr(cards):
    raw = structured({"text": "软件工程师 已投递", "confidence": 0.9, "cards": cards})
    result = VisionService(api_key="fixture", transport=lambda *_: raw).analyze(IMAGE)
    assert result.text == "软件工程师 已投递" and result.cards == []
    assert result.diagnostics[:2] == [{"code": "cards_type", "attempt": 1}, {"code": "cards_type", "attempt": 2}]


def test_old_cached_reading_is_not_given_new_literal_contract_authority():
    old = VisionResult(text="旧版OCR", confidence=0.9, model="deepseek-flash", image_sha256="a" * 64)
    assert old.reading_version is None and old.cards == []


@pytest.mark.parametrize("raw,expected", [
    ({}, "envelope_invalid"),
    (structured({"text": "SECRET-PRIVATE", "confidence": "0.9"}), "field_validation"),
    (structured({"text": "SECRET-PRIVATE"}), "field_validation"),
    ({"choices": [{"finish_reason": "stop", "message": {"content": "not JSON SECRET-PRIVATE"}}]}, "invalid_json"),
    ({"choices": [{"finish_reason": "length", "message": {"content": "SECRET-PRIVATE"}}]}, "response_truncated"),
    ({"choices": [{"finish_reason": "stop", "message": {"content": ["SECRET-PRIVATE"]}}]}, "content_type"),
    (structured(["SECRET-PRIVATE"]), "root_type"),
])
def test_format_failure_has_safe_structural_diagnostics_and_only_one_repair(raw, expected):
    calls = []
    with pytest.raises(VisionError) as caught:
        VisionService(api_key="secret-key", transport=lambda *args: calls.append(args) or raw).analyze(IMAGE)
    assert len(calls) == 2
    assert {item["attempt"] for item in caught.value.diagnostics} == {1, 2}
    assert all(item["code"] == expected for item in caught.value.diagnostics)
    diagnostic = json.dumps(caught.value.diagnostics)
    assert "SECRET-PRIVATE" not in diagnostic and "secret-key" not in diagnostic
    assert "SECRET-PRIVATE" not in json.dumps(calls[1][2])
    assert str(caught.value) in {"response_invalid", "response_incomplete"}


def test_repair_success_shares_deadline_and_accumulates_usage(monkeypatch):
    now, calls = [0.0], []
    monkeypatch.setattr(vision, "monotonic", lambda: now[0])

    def transport(_endpoint, _headers, payload, timeout):
        calls.append((json.loads(json.dumps(payload)), timeout))
        now[0] += 20
        return structured({"text": "bad", "confidence": "0.9"}) if len(calls) == 1 else response()

    result = VisionService(api_key="fixture", timeout=45, transport=transport).analyze(IMAGE)
    assert [item[1] for item in calls] == [45, 25]
    assert result.usage["total_tokens"] == 400
    assert result.diagnostics[0]["field"] == "confidence"
    assert calls[0][0]["messages"][0]["content"][1] == calls[1][0]["messages"][0]["content"][1]
    assert "structural validation" not in calls[0][0]["messages"][0]["content"][0]["text"]
    assert "structural validation" in calls[1][0]["messages"][0]["content"][0]["text"]


def test_exhausted_budget_does_not_send_format_repair(monkeypatch):
    now, calls = [0.0], []
    monkeypatch.setattr(vision, "monotonic", lambda: now[0])

    def transport(*args):
        calls.append(args)
        now[0] += 45
        return {}

    with pytest.raises(VisionError) as caught:
        VisionService(api_key="fixture", timeout=45, transport=transport).analyze(IMAGE)
    assert len(calls) == 1
    assert caught.value.diagnostics[-1]["code"] == "repair_budget_exhausted"


@pytest.mark.parametrize("finish", ["content_filter", "PRIVATE-REASON", ["malformed"]])
def test_non_format_completion_failures_are_not_retried_or_echoed(finish):
    calls = []
    raw = {"choices": [{"finish_reason": finish, "message": {"content": "PRIVATE-TEXT"}}]}
    with pytest.raises(VisionError, match="response_incomplete") as caught:
        VisionService(api_key="fixture", transport=lambda *args: calls.append(args) or raw).analyze(IMAGE)
    assert len(calls) == 1
    assert "PRIVATE" not in json.dumps(caught.value.diagnostics)


def test_each_billed_format_attempt_is_audited_but_client_replay_is_not_rebilled(tmp_path):
    store, kwargs = prepare(tmp_path)
    calls = []
    raw = structured({"text": "bad", "confidence": "0.9"})

    def transport(*args):
        calls.append(args)
        return raw if len(calls) == 1 else response()

    service = VisionService(api_key="fixture", transport=transport)
    result = analyze_observation(store, service, **kwargs)
    assert analyze_observation(store, service, **kwargs) == result
    events = store.get_events(kwargs["operation_id"])
    requests = [event.payload for event in events if event.event_type == "vision_request"]
    assert len(calls) == len(requests) == 2
    assert [item["attempt"] for item in requests] == [1, 2]
    assert [item["format_repair"] for item in requests] == [False, True]
    assert sum(event.event_type == "vision_analysis" for event in events) == 1


def test_cancellation_after_invalid_output_prevents_repair(tmp_path):
    store, kwargs = prepare(tmp_path)
    calls = []

    def transport(*args):
        calls.append(args)
        store.cancel(kwargs["operation_id"], reason="user_cancelled")
        return {}

    with pytest.raises(VisionError, match="observation_not_extracting"):
        analyze_observation(store, VisionService(api_key="fixture", transport=transport), **kwargs)
    assert len(calls) == 1


def test_failure_event_contains_safe_diagnostics_not_raw_provider_content(tmp_path):
    store, kwargs = prepare(tmp_path)
    service = VisionService(api_key="fixture", transport=lambda *_: structured({"secret": "PRIVATE-RESUME"}))
    with pytest.raises(VisionError):
        analyze_observation(store, service, **kwargs)
    failure = next(event.payload for event in store.get_events(kwargs["operation_id"]) if event.event_type == "vision_failure")
    assert failure["diagnostics"]
    assert "PRIVATE-RESUME" not in json.dumps(failure)
    assert "secret" not in json.dumps(failure)


def partial_reading():
    accepted = card("软件工程师", "已投递", False)
    corrected = card("测试开发工程师", "笔试中", True)
    text = accepted["text"] + "\n" + corrected["text"]
    bad = {**corrected, "text": "测试开发工程师 与整页原文不一致"}
    return structured({"text": text, "confidence": 0.9, "cards": [accepted, bad]}), accepted, corrected, text


def test_partial_repair_only_adds_cards_bound_to_frozen_text_without_overwriting_valid_cards():
    first, accepted, corrected, text = partial_reading()
    outside = card("额外编造岗位", "已录用", True)
    second = structured({"text": text + "\n" + outside["text"], "confidence": 1.0,
        "cards": [{**accepted, "current": True}, corrected, outside]})
    calls = []

    def transport(*args):
        calls.append(args)
        return first if len(calls) == 1 else second

    result = VisionService(api_key="fixture", transport=transport).analyze(IMAGE)
    assert len(calls) == 2
    assert result.text == text and result.confidence == 0.9
    assert [item.model_dump() for item in result.cards] == [accepted, corrected]
    assert any(item["code"] == "repair_outside_original_text" for item in result.diagnostics)
    assert result.diagnostics[-1] == {"code": "partial_cards_repaired", "attempt": 2, "card_count": 1}


@pytest.mark.parametrize("failure", ["invalid_json", "provider", "timeout", "incomplete"])
def test_partial_repair_failure_preserves_successful_first_reading(failure):
    from packages.matching.client import DeepSeekClientError
    first, accepted, _corrected, text = partial_reading()
    calls = []

    def transport(*args):
        calls.append(args)
        if len(calls) == 1:
            return first
        if failure == "provider":
            raise DeepSeekClientError("http_503")
        if failure == "timeout":
            raise TimeoutError()
        if failure == "incomplete":
            return {"choices": [{"finish_reason": "length"}]}
        return {"choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]}

    result = VisionService(api_key="fixture", transport=transport).analyze(IMAGE)
    assert len(calls) == 2
    assert result.text == text and result.confidence == 0.9
    assert [item.model_dump() for item in result.cards] == [accepted]
    assert result.diagnostics[0]["code"] == "card_evidence_unbound"
    assert result.diagnostics[-1]["attempt"] == 2


def test_partial_repair_cannot_extend_budget(monkeypatch):
    first, accepted, _corrected, text = partial_reading()
    now, calls = [0.0], []
    monkeypatch.setattr(vision, "monotonic", lambda: now[0])

    def transport(*args):
        calls.append(args)
        now[0] += 45
        return first

    result = VisionService(api_key="fixture", timeout=45, transport=transport).analyze(IMAGE)
    assert len(calls) == 1
    assert result.text == text and [item.model_dump() for item in result.cards] == [accepted]
    assert result.diagnostics[-1]["code"] == "repair_budget_exhausted"


def test_top_level_format_repair_does_not_allow_a_third_partial_card_attempt():
    partial, accepted, _corrected, text = partial_reading()
    calls = []

    def transport(*args):
        calls.append(args)
        return {} if len(calls) == 1 else partial

    result = VisionService(api_key="fixture", transport=transport).analyze(IMAGE)
    assert len(calls) == 2
    assert result.text == text and [item.model_dump() for item in result.cards] == [accepted]
