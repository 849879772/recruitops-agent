from __future__ import annotations

from typing import Any

from packages.recruitment_core.crawlers.tencent import TencentCrawler


MAPPING_PAYLOAD = {
    "status": 0,
    "data": [
        {
            "subProjectList": [
                {
                    "mappingId": 1,
                    "projectName": "2027校园招聘",
                    "recruitYear": "2027",
                },
            ],
        },
        {
            "subProjectList": [
                {
                    "mappingId": 2,
                    "projectName": "应届实习",
                    "recruitYear": "2026",
                },
            ],
        },
        {
            "subProjectList": [
                {
                    "mappingId": 14,
                    "projectName": "青云计划-应届生",
                    "recruitYear": "2027",
                },
                {
                    "mappingId": 20,
                    "projectName": "青云计划-实习生",
                    "recruitYear": "2026",
                },
                {
                    "mappingId": 9,
                    "projectName": "AI产品经理培训生",
                    "recruitYear": "2027",
                },
            ],
        },
    ],
}


def _row(post_id: str, title: str = "算法工程师", project: str = "2027校园招聘") -> dict[str, str]:
    return {
        "postId": post_id,
        "positionTitle": title,
        "projectName": project,
        "workCities": "深圳",
    }


def _detail(
    post_id: str,
    title: str = "算法工程师",
    *,
    topic: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "postId": post_id,
        "title": title,
        "workCityList": ["深圳", "北京"],
        "projectName": "2027校园招聘",
    }
    if topic:
        payload.update(
            {
                "topicDetail": "参与核心产品研发",
                "topicRequirement": "具备良好的工程基础",
                "projectName": "青云计划-应届生",
            }
        )
    else:
        payload.update(
            {
                "desc": "负责推荐系统研发",
                "request": "熟悉 Python 或 C++",
            }
        )
    return payload


def _stub_api(
    monkeypatch,
    crawler: TencentCrawler,
    pages: dict[int, list[dict[str, Any]]],
    details: dict[str, dict[str, Any]],
    total: int,
    *,
    mapping: dict[str, Any] = MAPPING_PAYLOAD,
) -> list[dict[str, Any]]:
    list_bodies: list[dict[str, Any]] = []

    def fake_get(url: str, params: dict[str, Any] | None = None, referer: str | None = None):
        assert url in {crawler.PROJECT_MAPPING_API, crawler.DETAIL_API}
        if url == crawler.PROJECT_MAPPING_API:
            return mapping
        post_id = str((params or {}).get("postId"))
        detail = details.get(post_id)
        return {"status": 0, "data": detail} if detail is not None else None

    def fake_post(url: str, body: dict[str, Any], referer: str | None = None):
        assert url == crawler.LIST_API
        list_bodies.append(body)
        page = int(body["pageIndex"])
        return {
            "status": 0,
            "data": {"positionList": pages.get(page, []), "count": total},
        }

    monkeypatch.setattr(crawler, "_get_json", fake_get)
    monkeypatch.setattr(crawler, "_post_json", fake_post)
    crawler._test_list_bodies = list_bodies
    return list_bodies


def test_parse_post_query_decodes_ampersand_and_filters() -> None:
    parsed = TencentCrawler.parse_source_url(
        "https://join.qq.com/post.html?query=p_1,b_78&amp;activity=128&amp;activityLink=256"
    )

    assert parsed["source_kind"] == "post_query"
    assert parsed["project_ids"] == [1]
    assert parsed["bg_ids"] == [78]
    assert "&amp;" not in parsed["normalized_url"]
    assert "amp;activity" not in parsed["normalized_url"]


def test_login_share_state_is_parsed_and_uses_explicit_public_fallback(monkeypatch) -> None:
    url = "https://join.qq.com/login.html?state=httpsjoin.qq.commshare.htmlshareId1148475860877320192"
    parsed = TencentCrawler.parse_source_url(url)

    assert parsed["source_kind"] == "login_share"
    assert parsed["share_id"] == "1148475860877320192"
    assert parsed["share_requires_login"] is True
    assert parsed["share_url"] == "https://join.qq.com/share.html?shareId=1148475860877320192"

    crawler = TencentCrawler("腾讯", url)
    _stub_api(monkeypatch, crawler, {1: [_row("share-1")]}, {"share-1": _detail("share-1")}, 1)
    jobs = crawler.fetch()

    assert jobs == []
    assert crawler.share_scope == "login_required"
    assert crawler.resolved_source_url == parsed["share_url"]
    assert crawler.pagination_complete is False
    assert crawler.pagination_termination_reason == "share_requires_login"
    assert crawler.metrics["read_only"] is True
    assert crawler.metrics["share_requires_login"] is True
    assert crawler.request_log == []


def test_homepage_uses_all_current_formal_projects_and_completes(monkeypatch) -> None:
    crawler = TencentCrawler("腾讯", "https://join.qq.com/")
    bodies = _stub_api(monkeypatch, crawler, {1: []}, {}, 0)

    assert crawler.fetch() == []
    assert bodies[0]["projectMappingIdList"] == [1, 14, 9]
    assert bodies[0]["pageIndex"] == 1
    assert crawler.pagination_complete is True
    assert crawler.pagination_termination_reason == "empty_result"
    assert crawler.campaign_validated is True
    assert crawler.cohort_status == "confirmed"
    assert crawler.integrity_evidence()["complete"] is True


def test_post_query_sends_current_search_contract_and_stable_job_identity(monkeypatch) -> None:
    crawler = TencentCrawler(
        "腾讯",
        "https://join.qq.com/post.html?query=p_1,b_78",
    )
    crawler.PAGE_SIZE = 2
    bodies = _stub_api(
        monkeypatch,
        crawler,
        {1: [_row("1001"), _row("1002", "后端开发")], 2: [_row("1003", "产品经理")]},
        {
            "1001": _detail("1001"),
            "1002": _detail("1002", "后端开发"),
            "1003": _detail("1003", "产品经理", topic=True),
        },
        3,
    )

    jobs = crawler.fetch()

    assert bodies[0] == {
        "projectIdList": [],
        "projectMappingIdList": [1],
        "keyword": "",
        "bgList": [78],
        "workCountryType": 0,
        "workCityList": [],
        "recruitCityList": [],
        "positionFidList": [],
        "pageIndex": 1,
        "pageSize": 2,
    }
    assert len(bodies) == 2
    assert [job["id"] for job in jobs] == ["tencent:1001", "tencent:1002", "tencent:1003"]
    assert jobs[0]["source_job_id"] == "1001"
    assert jobs[0]["detail_url"] == "https://join.qq.com/post_detail.html?postid=1001"
    assert jobs[2]["jd_raw"].startswith("岗位职责\n参与核心产品研发")
    assert crawler.pagination_complete is True
    assert crawler.pages_seen == 2
    assert crawler.expected_total == 3
    assert crawler.detail_complete is True
    assert crawler.adapter_complete is True


def test_short_or_empty_page_before_advertised_total_is_incomplete(monkeypatch) -> None:
    crawler = TencentCrawler("腾讯", "https://join.qq.com/post.html")
    crawler.PAGE_SIZE = 2
    _stub_api(
        monkeypatch,
        crawler,
        {1: [_row("2001"), _row("2002")], 2: []},
        {"2001": _detail("2001"), "2002": _detail("2002")},
        3,
    )

    jobs = crawler.fetch()

    assert len(jobs) == 2
    assert crawler.pagination_complete is False
    assert crawler.has_more is True
    assert crawler.pagination_termination_reason == "empty_page_before_total"
    assert crawler.detail_complete is True
    assert crawler.adapter_complete is False
    assert crawler.integrity_evidence()["pagination"]["advertised_total"] == 3


def test_detail_id_mismatch_is_recorded_as_incomplete(monkeypatch) -> None:
    crawler = TencentCrawler("腾讯", "https://join.qq.com/post.html?query=p_1")
    _stub_api(
        monkeypatch,
        crawler,
        {1: [_row("3001")]},
        {"3001": _detail("other-id")},
        1,
    )

    jobs = crawler.fetch()
    assert len(jobs) == 1
    assert jobs[0]["source_job_id"] == "3001"
    assert jobs[0]["jd_raw"] == ""
    assert crawler.pagination_complete is True
    assert crawler.detail_complete is False
    assert crawler.detail_failures == [
        {"post_id": "3001", "reason": "detail_request_failed_or_id_mismatch"}
    ]
    assert crawler.adapter_complete is False


def test_listing_preserves_internships_for_shared_batch_audit(monkeypatch) -> None:
    crawler = TencentCrawler("腾讯", "https://join.qq.com/post.html")
    internship = _row("intern-1", project="2027实习生招聘")
    _stub_api(
        monkeypatch,
        crawler,
        {1: [_row("formal-1"), internship]},
        {
            "formal-1": _detail("formal-1"),
            "intern-1": _detail("intern-1"),
        },
        2,
    )

    jobs = crawler.fetch()

    assert len(jobs) == 2
    assert crawler.filtered_internship_count == 1
    assert crawler.advertised_total == 2
    assert crawler.pagination_complete is True
