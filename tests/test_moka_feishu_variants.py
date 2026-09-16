from __future__ import annotations

import html
import json
from urllib.parse import parse_qs, urlsplit

from packages.recruitment_core.crawlers import moka as moka_module
from packages.recruitment_core.crawlers.feishu import GenericFeishuCrawler
from packages.recruitment_core.crawlers.moka import MokaRecruitCrawler


def test_moka_entry_variants_keep_filters_and_use_jobs_route() -> None:
    crawler = MokaRecruitCrawler(
        "Example",
        "https://app.mokahr.com/campus_apply/example/123?locale=zh-CN"
        "#/activity?keyword=robot&anchorName=jobsList",
    )

    resolved = crawler._jobs_url(2)
    parts = urlsplit(resolved)

    assert parts.path == "/campus_apply/example/123"
    assert parts.query == "locale=zh-CN"
    assert parts.fragment.startswith("/jobs?")
    assert parse_qs(parts.fragment.partition("?")[2]) == {
        "keyword": ["robot"],
        "anchorName": ["jobsList"],
        "page": ["2"],
    }


def test_moka_init_data_is_authoritative_and_embedded_rows_are_normalized() -> None:
    init_data = {
        "jobStats": {"total": 211},
        "jobs": [
            {
                "id": "moka-1",
                "title": "Embedded Engineer",
                "city": "上海",
                "description": "Design systems",
            }
        ],
    }
    encoded = html.escape(json.dumps(init_data), quote=True)
    page_html = f"""
    <input id="init-data" value="{encoded}">
    <div class="jobs-list">
      <a href="#/job/moka-1"><div class="job-title">Embedded Engineer</div></a>
      <a href="#/job/moka-2">
        <div class="job-title">Rendered Engineer</div>
        <div class="info-row"><span class="hiddenContent">全职</span>
              <span class="hiddenContent">上海</span></div>
      </a>
    </div>
    """
    crawler = MokaRecruitCrawler("Example", "https://app.mokahr.com/campus-recruitment/example/123#/")
    seen: set[str] = set()

    embedded = crawler._embedded_jobs(
        crawler._extract_init_data(page_html),
        crawler._base_url().split("?")[0],
        seen,
        crawler._make_job,
    )
    rendered = crawler._parse_page(page_html, seen)

    assert crawler._result_count(page_html) == 211
    assert [job["title"] for job in embedded] == ["Embedded Engineer"]
    assert [job["title"] for job in rendered] == ["Rendered Engineer"]
    assert rendered[0]["city"] == "上海"
    assert rendered[0]["employment_type"] == "全职"


def test_moka_official_zero_total_remains_a_safe_complete_result(monkeypatch) -> None:
    init_data = html.escape(json.dumps({"jobStats": {"total": 0}}), quote=True)
    page_html = f'<input id="init-data" value="{init_data}">'
    crawler = MokaRecruitCrawler("Example", "https://app.mokahr.com/campus_apply/example/123#/")

    def unavailable_browser():
        raise ImportError("test fallback")

    monkeypatch.setattr(crawler, "_fetch_with_reused_browser", unavailable_browser)
    monkeypatch.setattr(moka_module, "render_page", lambda *args, **kwargs: page_html)

    assert crawler.fetch() == []
    assert crawler.pagination_complete is True
    assert crawler.advertised_total == 0
    assert crawler.has_more is False
    assert crawler.pagination_termination_reason == "advertised_zero"


def test_feishu_mobile_application_and_referral_entries_expand_to_same_host_lists() -> None:
    mobile = GenericFeishuCrawler(
        "Example",
        "https://kwh0jtf778.jobs.feishu.cn/229043/m/?spread=KHWW5AC",
    )
    referral = GenericFeishuCrawler(
        "Example",
        "https://agirobot.jobs.feishu.cn/referral/campus/position/share/?spread=ABC",
    )
    application = GenericFeishuCrawler(
        "Example",
        "https://xiaopeng.jobs.feishu.cn/campus/position/application?spread=XYZ",
    )

    assert mobile.LIST_URL == "https://kwh0jtf778.jobs.feishu.cn/229043/position/list?spread=KHWW5AC"
    assert "/229043/m/" not in " ".join(mobile._candidate_list_urls(mobile.careers_url))
    assert any(
        url == "https://agirobot.jobs.feishu.cn/campusrecruitment/position?spread=ABC"
        for url in referral._candidate_list_urls(referral.careers_url)
    )
    assert application.LIST_URL == "https://xiaopeng.jobs.feishu.cn/campus/position/list?spread=XYZ"


def test_feishu_website_config_can_supply_a_hidden_position_route() -> None:
    config = {"pageConfigs": [{"path": "/campus/position/list", "isJobList": True}]}
    website_info = {
        "website_info": {"web_ui_config": json.dumps(config)},
    }
    page_html = (
        '<script id="js-websiteInfo" type="text/json">'
        + json.dumps(website_info)
        + "</script>"
    )

    urls = GenericFeishuCrawler._candidate_list_urls(
        "https://tenant.jobs.feishu.cn/300308", page_html
    )

    assert "https://tenant.jobs.feishu.cn/campus/position/list" in urls


def test_feishu_public_api_payload_proves_total_and_derived_has_more() -> None:
    crawler = GenericFeishuCrawler(
        "Example", "https://tenant.jobs.feishu.cn/campus/"
    )
    payload = {
        "code": 0,
        "data": {
            "count": 23,
            "job_post_list": [
                {
                    "id": 101,
                    "title": "Robotics Engineer",
                    "description": "参与 2027届校园招聘研发。",
                    "requirement": "Python and Linux",
                    "city_list": [{"name": "Shenzhen"}],
                    "recruit_type": {"name": "校招"},
                    "job_category": {"name": "技术"},
                    "publish_time": 1_735_689_600_000,
                }
            ],
        },
    }

    parsed = crawler._parse_api_payload(
        payload,
        "https://tenant.jobs.feishu.cn/campus/position/list",
        "https://tenant.jobs.feishu.cn/api/v1/search/job/posts?offset=10&limit=10",
    )

    assert parsed is not None
    assert parsed["total"] == 23
    assert parsed["offset"] == 10
    assert parsed["limit"] == 10
    assert parsed["raw_count"] == 1
    assert parsed["has_more"] is True
    assert parsed["jobs"][0]["jd_url"] == (
        "https://tenant.jobs.feishu.cn/campus/position/101/detail"
    )
    assert parsed["jobs"][0]["city"] == "Shenzhen"
    assert "2027届校园招聘" in parsed["jobs"][0]["campaign_text"]


def test_feishu_api_cursor_rewrites_only_offset_and_limit() -> None:
    crawler = GenericFeishuCrawler("Example", "https://tenant.jobs.feishu.cn/campus/")
    url = crawler._api_page_url(
        "https://tenant.jobs.feishu.cn/api/v1/search/job/posts?offset=0&limit=10&_signature=signed",
        20,
        10,
    )

    query = parse_qs(urlsplit(url).query)
    assert query["offset"] == ["20"]
    assert query["limit"] == ["10"]
    assert query["_signature"] == ["signed"]


def test_feishu_public_api_payload_accepts_explicit_terminal_has_more() -> None:
    crawler = GenericFeishuCrawler("Example", "https://tenant.jobs.feishu.cn/campus/")
    payload = {
        "code": 0,
        "data": {
            "count": 2,
            "hasMore": False,
            "job_post_list": [
                {"id": 1, "title": "A position"},
                {"id": 2, "title": "B position"},
            ],
        },
    }

    parsed = crawler._parse_api_payload(
        payload,
        "https://tenant.jobs.feishu.cn/campus/position/list",
        "https://tenant.jobs.feishu.cn/api/v1/search/job/posts?offset=0&limit=10",
    )

    assert parsed is not None
    assert parsed["explicit_has_more"] is True
    assert parsed["has_more"] is False
    assert parsed["raw_count"] == parsed["total"] == 2


def test_feishu_enabled_next_button_is_not_confused_with_missing_disabled_attribute() -> None:
    class Button:
        def is_disabled(self) -> bool:
            return False

        def get_attribute(self, name: str):
            return {
                "class": "atsx-pagination-next",
                "aria-disabled": "false",
                "disabled": None,
            }[name]

    assert GenericFeishuCrawler._button_disabled(Button()) is False
