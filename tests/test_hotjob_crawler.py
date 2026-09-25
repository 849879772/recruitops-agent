from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import requests

import packages.recruitment_core.crawlers.hotjob as hotjob_module
from packages.recruitment_core.crawlers.hotjob import HotjobRecruitCrawler


SUITE = "SU63eed9fd2f9d246c468eb43d"
ORIGIN = f"https://wecruit.hotjob.cn/{SUITE}"


class _Response:
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


def _row(post_id: str, title: str = "软件开发工程师") -> dict[str, str]:
    return {
        "postId": post_id,
        "externalKey": f"external-{post_id}",
        "postName": title,
        "workPlaceStr": "北京市-大兴区",
        "postTypeName": "技术研发类",
        "company": "测试公司",
        "department": "研发中心",
        "projectName": "2027秋季校园招聘",
        "educationStr": "硕士研究生及以上",
        "publishDate": "2026-08-25 11:54:37",
    }


def _install_api(
    monkeypatch: pytest.MonkeyPatch,
    crawler: HotjobRecruitCrawler,
    page_forms: dict[int, dict[str, Any] | str],
) -> list[dict[str, str]]:
    calls: list[dict[str, str]] = []

    def fake_post(url: str, *, params: dict[str, str], data: dict[str, str], headers, timeout: int):
        assert url == crawler._api_url()
        calls.append(dict(data))
        page = int(data["currentPage"])
        page_form = page_forms.get(page, {"pageData": []})
        if page_form == "error":
            raise requests.RequestException("page unavailable")
        return _Response({"state": "200", "data": {"pageForm": page_form}})

    def fake_detail(url: str) -> tuple[str, str, str]:
        return "职位描述\n负责研发\n任职要求\n具备工程基础", url, "active"

    monkeypatch.setattr(hotjob_module.requests, "post", fake_post)
    monkeypatch.setattr(hotjob_module, "_fetch_hotjob_position_detail", fake_detail)
    crawler.DETAIL_WORKERS = 1
    return calls


def test_mc_api_reuses_effective_page_size_and_closes_on_total(monkeypatch: pytest.MonkeyPatch) -> None:
    crawler = HotjobRecruitCrawler("北方集成电路技术创新中心", f"{ORIGIN}/mc/position/campus")
    calls = _install_api(
        monkeypatch,
        crawler,
        {
            1: {"totalPage": 2, "pageSize": 2, "dataCount": 3, "pageData": [_row("p1"), _row("p2")]},
            2: {"totalPage": 2, "pageSize": 2, "dataCount": 3, "pageData": [_row("p3", "算法工程师")]},
        },
    )

    jobs = crawler.fetch()

    assert [call["currentPage"] for call in calls] == ["1", "2"]
    assert [call["pageSize"] for call in calls] == ["50", "2"]
    assert [job["source_job_id"] for job in jobs] == ["p1", "p2", "p3"]
    assert jobs[0]["id"] == f"hotjob:{SUITE}:p1"
    assert jobs[0]["jd_url"] == (
        f"{ORIGIN}/pb/posDetail.html?postId=p1&postType=campus"
    )
    assert jobs[0]["link_kind"] == "detail"
    assert crawler.pages_seen == 2
    assert crawler.pages_fetched == 2
    assert crawler.total_pages == 2
    assert crawler.advertised_total == 3
    assert crawler.raw_listed_count == 3
    assert crawler.unique_listed_count == 3
    assert crawler.has_more is False
    assert crawler.pagination_complete is True
    assert crawler.pagination_termination_reason == "api_total_pages_and_count_reached"
    assert crawler.fetch_failed is False
    assert crawler.completeness_evidence["pagination_complete"] is True
    assert crawler.completeness_evidence["advertised_total"] == 3


@pytest.mark.parametrize("route", ["/pb/school.html", "/mc/position/campus"])
def test_pb_and_mc_routes_use_official_api_scope(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    crawler = HotjobRecruitCrawler("测试公司", f"{ORIGIN}{route}")
    _install_api(
        monkeypatch,
        crawler,
        {1: {"totalPage": 1, "pageSize": 1, "dataCount": 1, "pageData": [_row("route-1")]}},
    )

    jobs = crawler.fetch()

    assert len(jobs) == 1
    assert crawler.pagination_complete is True
    assert crawler.resolved_source_url == f"{ORIGIN}{route}"
    assert jobs[0]["source_list_url"] == f"{ORIGIN}{route}"


def test_missing_total_requires_an_explicit_empty_terminal_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = HotjobRecruitCrawler("测试公司", f"{ORIGIN}/pb/school.html")
    _install_api(
        monkeypatch,
        crawler,
        {
            1: {"pageData": [_row("empty-1")]},
            2: {"pageData": []},
        },
    )

    jobs = crawler.fetch()

    assert len(jobs) == 1
    assert crawler.pages_seen == 2
    assert crawler.total_pages is None
    assert crawler.advertised_total is None
    assert crawler.has_more is False
    assert crawler.pagination_complete is True
    assert crawler.pagination_termination_reason == "api_empty_terminal_page"


def test_api_failure_after_first_page_is_incomplete_and_retains_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = HotjobRecruitCrawler("测试公司", f"{ORIGIN}/mc/position/campus")
    _install_api(
        monkeypatch,
        crawler,
        {
            1: {"totalPage": 2, "pageSize": 2, "dataCount": 3, "pageData": [_row("partial-1"), _row("partial-2")]},
            2: "error",
        },
    )

    jobs = crawler.fetch()

    assert len(jobs) == 2
    assert crawler.pages_seen == 1
    assert crawler.total_pages == 2
    assert crawler.advertised_total == 3
    assert crawler.has_more is True
    assert crawler.pagination_complete is False
    assert crawler.pagination_termination_reason == "api_request_failed_page_2"
    assert crawler.fetch_failed is True
    assert crawler.completeness_evidence["pagination_complete"] is False


def test_root_entry_discovers_suite_before_crawling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = HotjobRecruitCrawler("信步科技", "https://seavo.hotjob.cn/")
    response = SimpleNamespace(
        url="https://seavo.hotjob.cn/",
        text='<a href="/SU123abc/pb/school.html">校园招聘</a>',
        raise_for_status=lambda: None,
    )
    monkeypatch.setattr(hotjob_module.requests, "get", lambda *args, **kwargs: response)
    monkeypatch.setattr(crawler, "_fetch_new_pb_api", lambda: [])
    monkeypatch.setattr(crawler, "_list_url", lambda: "https://seavo.hotjob.cn/SU123abc/pb/school.html")
    monkeypatch.setattr(crawler, "_mc_url", lambda: "https://seavo.hotjob.cn/SU123abc/mc/position/campus")
    monkeypatch.setattr(hotjob_module, "render_page", lambda *args, **kwargs: "")

    assert crawler.fetch() == []
    assert crawler._suite_key() == "SU123abc"
    assert crawler.resolved_source_url == "https://seavo.hotjob.cn/SU123abc/pb/school.html"


def test_rendered_fallback_is_stable_but_never_claims_full_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crawler = HotjobRecruitCrawler("测试公司", f"{ORIGIN}/pb/school.html")
    monkeypatch.setattr(crawler, "_fetch_new_pb_api", lambda: [])
    monkeypatch.setattr(
        hotjob_module,
        "render_page",
        lambda *args, **kwargs: """
        <div class='list-row-item'>
          <div class='list-cell pos-name'><span class='list-cell-span'>软件开发工程师</span></div>
          <div class='list-cell pos-locate'><span class='list-cell-span'>深圳市</span></div>
        </div>
        """,
    )

    jobs = crawler.fetch()

    assert len(jobs) == 1
    assert jobs[0]["source_job_id"].startswith("html-")
    assert jobs[0]["id"].startswith(f"hotjob:{SUITE}:html-")
    assert "#html-" in jobs[0]["jd_url"]
    assert jobs[0]["link_kind"] == "list"
    assert crawler.pages_seen == 1
    assert crawler.has_more is True
    assert crawler.pagination_complete is False
    assert crawler.pagination_termination_reason == "rendered_list_pagination_unverified"


def test_title_first_list_mode_keeps_rows_and_defers_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    crawler = HotjobRecruitCrawler("测试公司", f"{ORIGIN}/pb/school.html")
    calls = _install_api(monkeypatch, crawler, {
        1: {"totalPage": 1, "pageSize": 1, "dataCount": 1, "pageData": [_row("list-1")]},
    })
    details = []
    monkeypatch.setattr(
        hotjob_module, "_fetch_hotjob_position_detail",
        lambda url: details.append(url) or ("已抓详情", url, "active"),
    )
    monkeypatch.setenv("RECRUITOPS_HOTJOB_LIST_ONLY", "1")

    jobs = crawler.fetch()

    assert len(calls) == 1
    assert len(jobs) == 1
    assert details == []
    assert jobs[0]["title"] == "软件开发工程师"
    assert jobs[0]["jd_url"] == f"{ORIGIN}/pb/posDetail.html?postId=list-1&postType=campus"
    assert crawler.pagination_complete is True
    assert crawler.detail_expected_total == 1
    assert crawler.detail_count == 0
    assert crawler.detail_complete is False
    assert crawler.completeness_evidence["detail_complete"] is False


def test_direct_fetch_still_hydrates_hotjob_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    crawler = HotjobRecruitCrawler("测试公司", f"{ORIGIN}/pb/school.html")
    _install_api(monkeypatch, crawler, {
        1: {"totalPage": 1, "pageSize": 1, "dataCount": 1, "pageData": [_row("direct-1")]},
    })
    monkeypatch.delenv("RECRUITOPS_HOTJOB_LIST_ONLY", raising=False)

    jobs = crawler.fetch()

    assert jobs[0]["jd_raw"].startswith("职位描述")
    assert crawler.detail_count == 1
    assert crawler.detail_complete is True


def test_list_only_remains_complete_at_company_listing_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    from packages.recruitment_core.runner import crawl_company_with_evidence

    crawler = HotjobRecruitCrawler("测试公司", f"{ORIGIN}/pb/school.html")
    _install_api(monkeypatch, crawler, {
        1: {"totalPage": 1, "pageSize": 1, "dataCount": 1, "pageData": [_row("boundary-1")]},
    })
    monkeypatch.setenv("RECRUITOPS_HOTJOB_LIST_ONLY", "1")

    result = crawl_company_with_evidence(
        {"name": "测试公司", "crawler": "hotjob", "careers_url": f"{ORIGIN}/pb/school.html"},
        crawler_map={"hotjob": HotjobRecruitCrawler},
    )

    assert len(result["jobs"]) == 1
    assert result["source_runs"][0]["pagination_complete"] is True
    assert result["source_runs"][0]["termination_reason"] == "api_total_pages_and_count_reached"
    assert result["failures"] == []


def test_mobile_source_renders_mobile_route_first_and_stays_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    crawler = HotjobRecruitCrawler("测试公司", f"{ORIGIN}/mc/position/campus")
    monkeypatch.setattr(crawler, "_fetch_new_pb_api", lambda: [])
    rendered = []

    def render(url, **kwargs):
        rendered.append((url, kwargs["timeout_ms"]))
        return """<div class='listItem'><span class='listItemRtTitCon'>算法工程师</span></div>"""

    monkeypatch.setattr(hotjob_module, "render_page", render)
    monkeypatch.setattr(crawler, "_remaining_seconds", lambda fallback: 4.0)

    jobs = crawler.fetch()

    assert len(jobs) == 1
    assert rendered == [(f"{ORIGIN}/mc/position/campus", 4000)]
    assert crawler.pagination_complete is False
    assert crawler.pagination_termination_reason == "rendered_list_pagination_unverified"


def test_api_rate_limit_records_partial_failure_without_browser_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    crawler = HotjobRecruitCrawler("测试公司", f"{ORIGIN}/pb/school.html")
    response = requests.Response()
    response.status_code = 429
    monkeypatch.setattr(hotjob_module.requests, "post", lambda *_a, **_k: response)
    rendered = []
    monkeypatch.setattr(hotjob_module, "render_page", lambda url, **_k: rendered.append(url))

    assert crawler.fetch() == []
    assert rendered == []
    assert crawler.api_rate_limited is True
    assert crawler.crawl_error_code == "rate_limited"
    assert crawler.fetch_failed is True
    assert crawler.pagination_complete is False
    assert crawler.pagination_termination_reason == "api_rate_limited_page_1"


def test_rate_limit_reaches_entry_result_without_browser_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from packages.recruitment_core.entry_crawl import crawl_company_with_entry_discovery

    response = requests.Response()
    response.status_code = 429
    monkeypatch.setattr(hotjob_module.requests, "post", lambda *_a, **_k: response)
    rendered = []
    monkeypatch.setattr(hotjob_module, "render_page", lambda url, **_k: rendered.append(url))

    result = crawl_company_with_entry_discovery(
        {"name": "测试公司", "crawler": "hotjob", "careers_url": f"{ORIGIN}/pb/school.html"},
        crawler_map={"hotjob": HotjobRecruitCrawler},
    )

    assert result["jobs"] == []
    assert result["raw_job_count"] == 0
    assert result["error_code"] == "rate_limited"
    assert result["pagination_complete"] is False
    assert result["source_runs"][0]["error_code"] == "rate_limited"
    assert result["source_runs"][0]["termination_reason"] == "api_rate_limited_page_1"
    assert rendered == []


def test_rendered_rows_preserve_prior_api_failure_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    crawler = HotjobRecruitCrawler("测试公司", f"{ORIGIN}/pb/school.html")

    def failed_post(*_args, **_kwargs):
        raise requests.ConnectionError("fixture connection reset")

    monkeypatch.setattr(hotjob_module.requests, "post", failed_post)
    monkeypatch.setattr(hotjob_module, "render_page", lambda *_a, **_k: """
        <div class='list-row-item'>
          <div class='list-cell pos-name'><span class='list-cell-span'>软件开发工程师</span></div>
        </div>
    """)

    jobs = crawler.fetch()

    assert len(jobs) == 1
    assert crawler.pagination_complete is False
    assert crawler.pagination_termination_reason == "rendered_list_pagination_unverified"
    assert crawler.pagination_diagnostics == [{"reason": "api_request_failed_page_1"}]
