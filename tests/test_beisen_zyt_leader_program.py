from __future__ import annotations

import json
from pathlib import Path

from packages.recruitment_core.crawlers.beisen import BeisenRecruitCrawler


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "beisen"
    / "zyt_leader_program_page_0.json"
)


def _fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_zyt_numbered_program_route_selects_beisen_category_five() -> None:
    crawler = BeisenRecruitCrawler("卓驭-领航者计划", "https://we.zyt.com/5/jobs")

    assert crawler._list_url() == "https://we.zyt.com/5/jobs"
    assert crawler._api_url() == "https://we.zyt.com/api/Jobad/GetJobAdPageList"
    assert crawler._api_payload(0)["Category"] == ["5"]
    assert crawler._detail_url("job-guid") == "https://we.zyt.com/5/detail?jobAdId=job-guid"


def test_zyt_fixture_uses_count_total_and_preserves_job_kind(monkeypatch) -> None:
    payload = _fixture_payload()
    crawler = BeisenRecruitCrawler("卓驭-领航者计划", "https://we.zyt.com/5/jobs")
    requested_pages: list[int] = []

    def fixture_page(_session, _headers, page_index: int) -> dict:
        requested_pages.append(page_index)
        return payload

    monkeypatch.setattr(crawler, "_request_api_page", fixture_page)

    jobs = crawler._fetch_api_jobs()

    assert requested_pages == [0]
    assert len(jobs) == payload["Count"] == 17
    assert {job["job_type"] for job in jobs} == {"实习", "全职"}
    assert sum(job["job_type"] == "实习" for job in jobs) == 7
    assert sum(job["job_type"] == "全职" for job in jobs) == 10
    assert jobs[0]["published_at"] == "2026-07-15T16:31:44"
    assert jobs[0]["jd_url"] == (
        "https://we.zyt.com/5/detail?jobAdId=ee319017-a555-4069-ab72-de89ba664b1b"
    )
    assert "岗位职责" in jobs[0]["jd_raw"]
    assert crawler.pagination_complete is True
    assert crawler.pagination_termination_reason == "api_total_reached"
    assert crawler.pages_seen == 1
    assert crawler.total_pages == 1
    assert crawler.advertised_total == 17
    assert crawler.has_more is False
