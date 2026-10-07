"""Display suffix recovery keeps different roles, cities and volunteers apart."""

import pytest

from packages.domain.application_identity import matching_records, unique_record


def _card(title, **values):
    return {"title": title, "raw_title": title, **values}


def test_anker_three_volunteers_match_the_named_second_role():
    cards = [
        _card("嵌入式 AI Agent 工程师-深圳第 1 志愿"),
        _card("系统开发工程师-深圳第 2 志愿"),
        _card("软件测试工程师-深圳第 3 志愿"),
    ]
    application = {"job_title": "系统开发工程师"}
    assert matching_records(application, cards) == [cards[1]]
    assert unique_record(application, cards) is cards[1]


def test_anker_matching_flows_through_existing_status_verifier(tmp_path, monkeypatch):
    from packages.storage import ApplicationSnapshot
    from tests.test_application_status_model_fallback import _case

    titles = ["嵌入式 AI Agent 工程师-深圳第1志愿", "系统开发工程师-深圳第2志愿", "软件测试工程师-深圳第3志愿"]
    cards = [_card(title, status="written" if index == 1 else "applied",
                   label="笔试中" if index == 1 else "申请成功",
                   context=f"{title} 当前状态: {'笔试中' if index == 1 else '申请成功'}", confidence=0.98)
             for index, title in enumerate(titles)]
    repository, _, client, run, _ = _case(tmp_path, monkeypatch, cards=cards, entries=cards)
    with repository.storage.write_transaction() as session:
        session.get(ApplicationSnapshot, "25").job_title = "系统开发工程师"
    result = run()
    assert [row.application_id for row in result.updated] == ["25"], result.model_dump()
    assert {row.application_id for row in result.unchanged} == {"24", "26"}
    assert not result.unresolved and not result.failed and not client.calls
    with repository.storage.session() as session:
        assert session.get(ApplicationSnapshot, "25").stage == "written"
        assert session.get(ApplicationSnapshot, "24").stage == "applied"
        assert session.get(ApplicationSnapshot, "26").stage == "applied"


@pytest.mark.parametrize("title", [
    "系统开发工程师-深圳第2志愿",
    "系统开发工程师（深圳）网申第二志愿",
    "NO.2708 【校招】系统开发工程师-深圳第 2 志愿",
    "2027届-系统开发工程师(深圳市)第2志愿",
])
def test_bounded_city_and_volunteer_formats(title):
    card = _card(title)
    assert unique_record({"job_title": "系统开发工程师"}, [card]) is card


def test_same_role_in_multiple_cities_requires_confirmation_without_local_city():
    cards = [_card("系统开发工程师-深圳第1志愿"), _card("系统开发工程师-北京第2志愿")]
    assert matching_records({"job_title": "系统开发工程师"}, cards) == cards
    assert unique_record({"job_title": "系统开发工程师"}, cards) is None


@pytest.mark.parametrize("target", ["系统开发工程师-深圳", "系统开发工程师（深圳市）"])
def test_explicit_local_city_selects_only_its_city(target):
    cards = [_card("系统开发工程师-北京第1志愿"), _card("系统开发工程师(深圳)第2志愿")]
    assert matching_records({"job_title": target}, cards) == [cards[1]]


@pytest.mark.parametrize("captured", ["系统开发工程师-北京第2志愿", "系统开发工程师第2志愿"])
def test_local_city_cannot_be_inferred_from_another_or_missing_city(captured):
    assert not matching_records({"job_title": "系统开发工程师（深圳）"}, [_card(captured)])


@pytest.mark.parametrize("target,captured", [
    ("系统开发工程师", "系统开发工程师（嵌入式方向）-深圳第2志愿"),
    ("系统开发工程师", "系统开发工程师（深圳研发中心）第2志愿"),
    ("系统开发工程师", "系统开发工程师-深圳方向第2志愿"),
    ("系统开发工程师", "系统开发工程师-深圳-北京第2志愿"),
    ("系统开发工程师", "系统开发工程师--深圳第2志愿"),
    ("系统开发工程师", "系统开发工程师(J12262)-深圳第2志愿"),
    ("系统开发工程师(J12262)", "系统开发工程师(J12263)-深圳第2志愿"),
    ("系统开发工程师-客户端", "系统开发工程师-服务端-深圳第2志愿"),
    ("C++开发工程师", "C#开发工程师-深圳第2志愿"),
    ("C++开发工程师", "C开发工程师-深圳第2志愿"),
    ("C++开发工程师", "C++开发工程师-资深-深圳第2志愿"),
    ("2026届-系统开发工程师", "2027届-系统开发工程师-深圳第2志愿"),
])
def test_city_suffix_does_not_erase_substantive_identity(target, captured):
    assert not matching_records({"job_title": target}, [_card(captured)])


def test_preserved_job_number_and_language_can_match_a_city_suffix():
    card = _card("C++开发工程师(J12262)-深圳第2志愿")
    assert unique_record({"job_title": "C++开发工程师(J12262)"}, [card]) is card


def test_repeated_volunteers_stay_ambiguous_without_an_explicit_local_volunteer():
    cards = [_card("系统开发工程师-深圳第1志愿"), _card("系统开发工程师-深圳第2志愿")]
    assert matching_records({"job_title": "系统开发工程师"}, cards) == cards
    assert unique_record({"job_title": "系统开发工程师"}, cards) is None
    assert unique_record({"job_title": "系统开发工程师（深圳）网申第二志愿"}, cards) is cards[1]


def test_explicit_volunteer_cannot_match_another_explicit_volunteer():
    assert not matching_records({"job_title": "系统开发工程师网申第二志愿"}, [_card("系统开发工程师第1志愿")])


def test_scoped_canonical_title_can_omit_volunteer_but_duplicate_cards_stay_ambiguous():
    card = _card("系统开发工程师")
    application = {"job_title": "系统开发工程师网申第二志愿"}
    assert matching_records(application, [card]) == [card]
    assert unique_record(application, [card, dict(card)]) is None


def test_raw_exact_and_external_id_precede_city_suffix_candidates():
    exact = _card("系统开发工程师")
    other = _card("系统开发工程师-深圳第2志愿", job_id="ats-2")
    assert matching_records({"job_title": "系统开发工程师"}, [other, exact]) == [exact]
    assert matching_records({"job_title": "系统开发工程师", "external_job_id": "ats-2"}, [exact, other]) == [other]


def test_manual_binding_precedes_city_suffix_and_does_not_fall_back():
    application = {"id": "local-1", "job_title": "系统开发工程师", "record_url": "https://ats.example/records",
        "verified_identity_bindings": [{"verified": True, "application_id": "local-1",
            "page_url": "https://ats.example/records", "raw_title": "系统开发工程师-北京第1志愿",
            "external_job_id": "ats-1"}]}
    bound = _card("系统开发工程师-北京第1志愿", job_id="ats-1")
    other = _card("系统开发工程师-深圳第2志愿", job_id="ats-2")
    assert matching_records(application, [other, bound]) == [bound]
    assert not matching_records(application, [other])


def test_cleaned_title_does_not_hide_raw_title_direction_or_city_conflict():
    assert not matching_records({"job_title": "系统开发工程师(深圳)"}, [
        {"raw_title": "系统开发工程师(北京)第2志愿", "title": "系统开发工程师(深圳)"}])
    assert not matching_records({"job_title": "系统开发工程师"}, [
        {"raw_title": "系统开发工程师(嵌入式方向)-深圳第2志愿", "title": "系统开发工程师"}])
