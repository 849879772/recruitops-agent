from __future__ import annotations

import requests

import packages.recruitment_core.crawlers.tonghuashun as tonghuashun_module
from packages.recruitment_core.crawlers.generic_render import GenericRenderCrawler
from packages.recruitment_core.crawlers.tonghuashun import TonghuashunCampusCrawler


URL = "https://campus.10jqka.com.cn/job/list?type=school&sid=1"


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def _success(ex_data):
    return {"success": True, "erro_code": "0", "ex_data": ex_data}


def _row(job_id: int, title: str) -> dict:
    return {
        "id": job_id,
        "name": title,
        "base": "杭州",
        "intro": "负责金融大模型训练、评测与工程落地。" * 8,
        "requirement": "熟悉 Python、PyTorch 和分布式训练框架。" * 8,
        "apply_recruitment_series_name": "AIME计划",
    }


def test_resolves_stale_aimie_sid_and_fetches_all_pages_with_full_jd(monkeypatch) -> None:
    calls = []

    def fake_get(url, *, params, **_kwargs):
        calls.append((url, dict(params)))
        if url.endswith("/recruitmentSeries/list"):
            return _Response(_success([
                {"id": 61, "series_name": "2027届校园招聘"},
                {"id": 52, "series_name": "AIME计划"},
            ]))
        page = int(params["page"])
        rows = [_row(2115, "算法工程师（金融 AI Agent 平台）")] if page == 1 else [
            _row(2148, "算法工程师（deep research）")
        ]
        return _Response(_success({
            "apply_show_do_list": rows,
            "total": 2,
            "current": page,
            "pages": 2,
            "size": 1,
        }))

    monkeypatch.setattr(tonghuashun_module.requests, "get", fake_get)
    crawler = TonghuashunCampusCrawler("同花顺-AIMIE计划", URL)

    jobs = crawler.fetch()

    list_calls = [params for url, params in calls if url.endswith("/apply_list")]
    assert [call["page"] for call in list_calls] == [1, 2]
    assert {call["applyRecruitmentSeriesIds"] for call in list_calls} == {"52"}
    assert [job["source_job_id"] for job in jobs] == ["2115", "2148"]
    assert jobs[0]["jd_url"] == "https://campus.10jqka.com.cn/job/detail?id=2115"
    assert "岗位职责" in jobs[0]["jd_raw"]
    assert "任职要求" in jobs[0]["jd_raw"]
    assert len(jobs[0]["jd_raw"]) > 300
    assert crawler.pages_seen == 2
    assert crawler.total_pages == 2
    assert crawler.advertised_total == 2
    assert crawler.pagination_complete is True
    assert crawler.has_more is False
    assert crawler.pagination_termination_reason == "api_total_pages_and_count_reached"


def test_list_failure_after_first_page_is_incomplete(monkeypatch) -> None:
    def fake_get(url, *, params, **_kwargs):
        if url.endswith("/recruitmentSeries/list"):
            return _Response(_success([{"id": 52, "series_name": "AIME计划"}]))
        if int(params["page"]) == 2:
            raise requests.RequestException("temporary failure")
        return _Response(_success({
            "apply_show_do_list": [_row(2115, "算法工程师（金融 AI Agent 平台）")],
            "total": 2,
            "current": 1,
            "pages": 2,
            "size": 1,
        }))

    monkeypatch.setattr(tonghuashun_module.requests, "get", fake_get)
    crawler = TonghuashunCampusCrawler("同花顺-AIMIE计划", URL)

    assert len(crawler.fetch()) == 1
    assert crawler.pagination_complete is False
    assert crawler.fetch_failed is True
    assert crawler.has_more is True
    assert crawler.pagination_termination_reason == "list_request_failed_page_2"


def test_render_entry_delegates_tonghuashun_series_url(monkeypatch) -> None:
    expected = [_row(2115, "算法工程师（金融 AI Agent 平台）")]

    def fake_fetch(self):
        self.pages_seen = 1
        self.total_pages = 1
        self.advertised_total = 1
        self.pagination_complete = True
        self.pagination_termination_reason = "api_total_pages_and_count_reached"
        return expected

    monkeypatch.setattr(TonghuashunCampusCrawler, "fetch", fake_fetch)
    crawler = GenericRenderCrawler("同花顺-AIMIE计划", URL)

    assert crawler.fetch() == expected
    assert crawler.pagination_complete is True
    assert crawler.pages_seen == 1
    assert crawler.advertised_total == 1


def test_adapter_does_not_claim_unrelated_render_urls() -> None:
    assert TonghuashunCampusCrawler.supports(URL) is True
    no_series_url = "https://campus.10jqka.com.cn/job/list?type=school"
    assert TonghuashunCampusCrawler.supports(no_series_url) is False
    assert TonghuashunCampusCrawler.supports("https://example.com/job/list?sid=1") is False
