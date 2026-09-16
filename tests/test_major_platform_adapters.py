from __future__ import annotations

from packages.recruitment_core.crawlers.baidu import BaiduCrawler
from packages.recruitment_core.crawlers.mihoyo import MihoyoCrawler
from packages.recruitment_core.crawlers.netease import NetEaseCrawler


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


def test_baidu_adapter_reports_complete_multi_page_evidence(monkeypatch) -> None:
    def fake_post(_url, *, data, **_kwargs):
        page = int(data["curPage"])
        row = {
            "postId": f"job-{page}",
            "name": f"软件工程师-{page}",
            "workPlace": "北京",
            "workContent": "负责平台软件开发与性能优化。" * 12,
            "serviceCondition": "熟悉 Python、C++ 与计算机基础。" * 8,
        }
        return _Response({"status": "ok", "data": {"list": [row], "pages": 2, "total": 2}})

    monkeypatch.setattr("packages.recruitment_core.crawlers.baidu.requests.post", fake_post)
    crawler = BaiduCrawler("百度", "https://talent.baidu.com/jobs/")

    jobs = crawler.fetch()

    assert len(jobs) == 2
    assert crawler.pagination_complete is True
    assert crawler.pages_seen == 2
    assert crawler.total_pages == 2
    assert crawler.advertised_total == 2
    assert crawler.has_more is False


def test_netease_adapter_reports_complete_project_total(monkeypatch) -> None:
    row = {
        "id": 101,
        "positionName": "服务端开发工程师",
        "workPlaceName": "杭州",
        "positionDescription": "负责在线服务设计、开发、测试及性能优化。" * 10,
        "positionRequirement": "熟悉数据结构、网络和至少一种编程语言。" * 8,
    }

    def fake_get(_url, **_kwargs):
        return _Response({"data": {"list": [row], "total": 1}})

    monkeypatch.setattr("packages.recruitment_core.crawlers.netease.requests.get", fake_get)
    crawler = NetEaseCrawler("网易", "https://campus.163.com/app/job/position?id=103")

    jobs = crawler.fetch()

    assert len(jobs) == 1
    assert crawler.pagination_complete is True
    assert crawler.pages_seen == 1
    assert crawler.total_pages == 1
    assert crawler.advertised_total == 1


def test_mihoyo_adapter_keeps_long_jd_and_reports_complete_total(monkeypatch) -> None:
    long_summary = "岗位职责：负责游戏基础设施开发。任职要求：熟悉 C++ 和网络编程。" * 30
    row = {
        "id": "mh-1",
        "title": "客户端开发工程师",
        "addressDetailList": [{"addressDetail": "上海"}],
        "competencyType": "技术",
        "jobNature": "校招",
        "projectName": "2027校园招聘",
        "jobSummary": long_summary,
    }

    def fake_post(_url, **_kwargs):
        if _url.endswith("/job/info"):
            return _Response({
                "code": 0,
                "data": {
                    "description": "负责游戏基础设施开发。" * 20,
                    "jobRequire": "熟悉 C++、网络编程与数据结构。" * 20,
                    "addition": "具备良好的工程能力。" * 10,
                },
            })
        return _Response({"data": {"list": [row], "total": 1}})

    monkeypatch.setattr("packages.recruitment_core.crawlers.mihoyo.requests.post", fake_post)
    crawler = MihoyoCrawler("米哈游", "https://jobs.mihoyo.com/#/campus/position")

    jobs = crawler.fetch()

    assert len(jobs) == 1
    assert len(jobs[0]["jd_raw"]) > 300
    assert "任职要求" in jobs[0]["jd_raw"]
    assert crawler.pagination_complete is True
    assert crawler.advertised_total == 1
    assert crawler.detail_failures == 0
