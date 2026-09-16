from __future__ import annotations

from typing import Any

import pytest

from packages.recruitment_core.crawlers.huawei import HuaweiCrawler


ENTRY_URLS = [
    "https://career.huawei.com/cn/campus-recruitment",
    "https://career.huawei.com/cn/campus-recruitment-job-list?recruitmentType=FRESH_GRADUATE",
    "https://career.huawei.com/cn",
    "https://career.huawei.com/cn/campus-recruitment-job-list?recruitmentType=FRESH_GRADUATE",
    "https://career.huawei.com/cn/campus-recruitment-job-list?recruitmentType=FRESH_GRADUATE",
    "https://career.huawei.com/cn/campus-recruitment-job-list?recruitmentType=FRESH_GRADUATE",
    "https://career.huawei.com/cn/campus-recruitment-job-list?recruitmentType=FRESH_GRADUATE",
    "https://career.huawei.com/cn/campus-recruitment",
    "https://career.huawei.com/cn",
    "https://career.huawei.com/cn",
    "https://career.huawei.com/cn/campus-recruitment-job-list?recruitmentType=FRESH_GRADUATE",
    "https://career.huawei.com/cn/campus-recruitment-job-list?recruitmentType=FRESH_GRADUATE",
    "https://career.huawei.com/cn/campus-recruitment-job-list?recruitmentType=FRESH_GRADUATE",
    "https://career.huawei.com/cn",
    "https://career.huawei.com/cn/campus-recruitment",
    "https://career.huawei.com/cn",
    "https://career.huawei.com/cn/campus-recruitment-job-list?recruitmentType=FRESH_GRADUATE",
    "https://career.huawei.com/cn/campus-recruitment-job-list?recruitmentType=FRESH_GRADUATE",
    "https://career.huawei.com/cn/campus-recruitment-job-list?recruitmentType=FRESH_GRADUATE",
    "https://career.huawei.com/cn/campus-recruitment-job-list?recruitmentType=FRESH_GRADUATE",
]


def _row(
    advertisement_id: int,
    title: str | None = None,
    city: str = "上海",
) -> dict[str, Any]:
    return {
        "advertisementId": advertisement_id,
        "advertisementsIntegrationId": advertisement_id + 100_000,
        "jobName": title or f"岗位 {advertisement_id}",
        "workPlace": city,
        "mainBusiness": "负责岗位职责。",
        "jobRequire": "满足岗位要求。",
        "lastUpdateDate": "2026-08-17",
        "scenarioName": "应届生",
    }


def _payload(
    page: int,
    total_pages: int,
    total: int,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "status": "SUCCESS",
        "data": {
            "pageVO": {
                "totalRows": total,
                "curPage": page,
                "pageSize": 2,
                "totalPages": total_pages,
            },
            "result": rows,
        },
    }


def _parsed_pages(*payloads: dict[str, Any]) -> dict[int, dict[str, Any]]:
    pages = {}
    for payload in payloads:
        parsed = HuaweiCrawler._parse_page_payload(payload)
        assert parsed is not None
        pages[int(parsed["page"])] = parsed
    return pages


def test_all_huawei_oc_entries_share_one_canonical_list() -> None:
    canonical = {HuaweiCrawler.canonical_source_url(url) for url in ENTRY_URLS}

    assert len(ENTRY_URLS) == 20
    assert canonical == {
        "https://career.huawei.com/cn/campus-recruitment-job-list?recruitmentType=FRESH_GRADUATE"
    }
    assert HuaweiCrawler.canonical_source_url(
        "https://career.huawei.com/cn/campus-recruitment-job-list?"
        "recruitmentType=FRESH_GRADUATE&department=2012"
    ) in canonical
    assert HuaweiCrawler.canonical_source_url(
        "https://career.huawei.com/reccampportal/portal5/campus-recruitment.html"
    ) in canonical


def test_page_payload_requires_official_total_and_normalizes_page_metadata() -> None:
    parsed = HuaweiCrawler._parse_page_payload(
        _payload(2, 7, 69, [_row(2)]),
        requested_page=99,
    )

    assert parsed is not None
    assert parsed["page"] == 2
    assert parsed["page_size"] == 2
    assert parsed["total"] == 69
    assert parsed["total_pages"] == 7
    assert parsed["has_more"] is True
    assert parsed["raw_row_count"] == 1

    assert HuaweiCrawler._parse_page_payload({"status": "ERROR", "data": {}}) is None
    assert HuaweiCrawler._parse_page_payload({"status": "SUCCESS", "data": {"result": []}}) is None


def test_fetch_returns_complete_deduplicated_jobs_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = _parsed_pages(
        _payload(1, 2, 3, [_row(101, "AI Infra工程师", "苏州"), _row(102, "软件开发工程师")]),
        _payload(2, 2, 3, [_row(103, "算法工程师", "深圳")]),
    )
    crawler = HuaweiCrawler("华为云计算BU", ENTRY_URLS[0])
    monkeypatch.setattr(crawler, "_fetch_pages", lambda: pages)

    jobs = crawler.fetch()

    assert [job["title"] for job in jobs] == ["AI Infra工程师", "软件开发工程师", "算法工程师"]
    assert jobs[0]["jd_url"] == (
        "https://career.huawei.com/cn/job-details?advertisementId=101"
    )
    assert jobs[0]["source_job_id"] == "101"
    assert jobs[0]["city"] == "苏州"
    assert jobs[0]["job_type"] == "校招"
    assert jobs[0]["source_advertisement_id"] == "101"
    assert jobs[0]["jd_url"].endswith("advertisementId=101")

    assert crawler.pagination_complete is True
    assert crawler.pages_seen == 2
    assert crawler.pages_fetched == 2
    assert crawler.total_pages == 2
    assert crawler.advertised_total == 3
    assert crawler.page_size == 2
    assert crawler.page_sizes == [2, 1]
    assert crawler.raw_listed_count == 3
    assert crawler.unique_listed_count == 3
    assert crawler.has_more is False
    assert crawler.pagination_termination_reason == "api_total_and_pages_reached"
    assert crawler.completeness_evidence["pagination_complete"] is True
    assert crawler.completeness_evidence["advertised_total"] == 3
    assert crawler.completeness_evidence["total_pages"] == 2


def test_same_list_does_not_change_with_department_project_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = _parsed_pages(_payload(1, 1, 1, [_row(500, "统一官方岗位")]))
    monkeypatch.setattr(HuaweiCrawler, "_fetch_pages", lambda self: pages)
    first = HuaweiCrawler("华为2012实验室", ENTRY_URLS[0]).fetch()
    second = HuaweiCrawler("华为数字能源研发", ENTRY_URLS[15]).fetch()

    def projection(jobs: list[dict[str, Any]]) -> list[tuple[str, str, str, str]]:
        return [
            (job["title"], job["city"], job["jd_url"], job["source_list_url"])
            for job in jobs
        ]
    assert projection(first) == projection(second)
    assert first[0]["company"] != second[0]["company"]


def test_missing_page_is_returned_for_audit_but_not_marked_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = _parsed_pages(_payload(1, 2, 3, [_row(601), _row(602)]))
    crawler = HuaweiCrawler("华为公共开发部", ENTRY_URLS[1])
    monkeypatch.setattr(crawler, "_fetch_pages", lambda: pages)

    jobs = crawler.fetch()

    assert len(jobs) == 2
    assert crawler.pagination_complete is False
    assert crawler.pages_seen == 1
    assert crawler.total_pages == 2
    assert crawler.has_more is True
    assert crawler.pagination_termination_reason == "missing_api_page_2"


def test_duplicate_advertisement_ids_are_removed_and_completeness_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = _parsed_pages(
        _payload(1, 2, 3, [_row(701), _row(702)]),
        _payload(2, 2, 3, [_row(702), _row(703)]),
    )
    crawler = HuaweiCrawler("华为消费者管理培训生", ENTRY_URLS[2])
    monkeypatch.setattr(crawler, "_fetch_pages", lambda: pages)

    jobs = crawler.fetch()

    assert [job["source_job_id"] for job in jobs] == ["701", "702", "703"]
    assert crawler.pagination_complete is False
    assert crawler.unique_listed_count == 3
    assert crawler.raw_listed_count == 4
    assert crawler.pagination_duplicate_ids == ["702"]
    assert crawler.pagination_termination_reason == "duplicate_advertisement_id"
