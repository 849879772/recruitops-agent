from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import yaml

from packages.recruitment_core.crawlers import CRAWLER_MAP
from packages.recruitment_core.crawlers.job4399 import Job4399Crawler


SOURCE = "https://hr.4399om.com/weixin/?r=job/agent&type=2&isOpen=0&jobTableType=1&code=ctg5d"
CATEGORIES = (("2", "策划"), ("8", "开发"), ("6", "职能"), ("5", "美术"), ("4", "运营市场"), ("7", "实习"))


def _html(type_id: str, ids: list[str], *, controls: bool = True) -> str:
    tabs = "".join(
        f'<a class="searchItems_item" data-type="{value}"><span>{label}</span></a>'
        for value, label in CATEGORIES
    ) if controls else ""
    rows = "".join(
        '<li class="postItem">'
        f'<a href="?r=job/view&id={job_id}&type=agent&jobTableType=1&code=ctg5d">'
        f'<span class="postItem_name">岗位{job_id}</span>'
        f'<span class="postItem_category">类别{type_id}</span>'
        '<span class="postItem_location"><i></i><span>广州</span></span>'
        "</a></li>"
        for job_id in ids
    )
    return f'<div class="searchItems">{tabs}</div><div class="postList"><ul>{rows}</ul></div>'


class _Response:
    def __init__(self, *, url: str, text: str = "", payload=None):
        self.url = url
        self.text = text
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _payload(*ids: str):
    return {
        job_id: {"name": f"岗位{job_id}", "type": "平台分类", "workCity": "广州"}
        for job_id in ids
    }


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def _install_complete_site(monkeypatch, crawler: Job4399Crawler) -> None:
    def fake_get(url: str, **_kwargs):
        query = _query(url)
        type_id = query.get("type", "2")
        if query.get("r") == "job/agentMore":
            page = int(query["p"])
            if type_id == "4" and page == 2:
                return _Response(url=url, payload=_payload("4-extra"))
            return _Response(url=url, payload={})
        return _Response(url=url, text=_html(type_id, [f"{type_id}-base"]))

    monkeypatch.setattr(crawler, "_get", fake_get)


def test_registered_as_dedicated_adapter_and_configured_for_4399() -> None:
    assert CRAWLER_MAP["job4399"] is Job4399Crawler
    with open("config/companies.yaml", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    row = next(item for item in config["companies"] if item["name"] == "4399游戏")
    assert row["crawler"] == "job4399"


def test_fetches_all_six_categories_until_each_api_returns_empty(monkeypatch) -> None:
    crawler = Job4399Crawler("4399游戏", SOURCE)
    _install_complete_site(monkeypatch, crawler)

    jobs = crawler.fetch()

    assert len(jobs) == 7
    assert crawler.categories_seen == 6
    assert crawler.pagination_complete is True
    assert crawler.has_more is False
    assert crawler.pagination_termination_reason == "all_categories_explicit_empty_terminal"
    assert crawler.completeness_evidence["categories_completed"] == 6
    assert all(job["jd_status"] == "list_only_not_hydrated" for job in jobs)
    assert all("job/view" in job["jd_url"] for job in jobs)


def test_repeated_incremental_page_is_incomplete(monkeypatch) -> None:
    crawler = Job4399Crawler("4399游戏", SOURCE)

    def fake_get(url: str, **_kwargs):
        query = _query(url)
        if query.get("r") == "job/agentMore":
            return _Response(url=url, payload=_payload("repeat"))
        return _Response(url=url, text=_html(query.get("type", "2"), ["base"]))

    monkeypatch.setattr(crawler, "_get", fake_get)
    crawler.fetch()

    assert crawler.pagination_complete is False
    assert crawler.has_more is True
    assert crawler.pagination_termination_reason == "category_page_repeated"


def test_category_request_failure_is_incomplete(monkeypatch) -> None:
    crawler = Job4399Crawler("4399游戏", SOURCE)

    def fake_get(url: str, **_kwargs):
        query = _query(url)
        if query.get("r") == "job/agent" and query.get("type") == "8":
            return None
        if query.get("r") == "job/agentMore":
            return _Response(url=url, payload={})
        return _Response(url=url, text=_html(query.get("type", "2"), ["base"]))

    monkeypatch.setattr(crawler, "_get", fake_get)
    crawler.fetch()

    assert crawler.pagination_complete is False
    assert crawler.fetch_failed is True
    assert crawler.pagination_termination_reason == "category_page_fetch_failed"


def test_missing_category_controls_never_claims_complete(monkeypatch) -> None:
    crawler = Job4399Crawler("4399游戏", SOURCE)
    monkeypatch.setattr(
        crawler,
        "_get",
        lambda url, **_kwargs: _Response(url=url, text=_html("2", ["one"], controls=False)),
    )

    assert crawler.fetch() == []
    assert crawler.pagination_complete is False
    assert crawler.pagination_termination_reason == "category_controls_missing"


def test_static_parser_deduplicates_native_job_id() -> None:
    crawler = Job4399Crawler("4399游戏", SOURCE)
    jobs = crawler._parse_jobs(_html("2", ["same", "same"]), source_url=SOURCE)

    assert len(jobs) == 1
    assert jobs[0]["source_job_id"] == "same"


def test_response_bytes_are_decoded_as_utf8_even_with_wrong_header_encoding() -> None:
    response = _Response(url=SOURCE, text="mojibake")
    response.content = "【2027校招】开发工程师".encode("utf-8")

    assert Job4399Crawler._response_text(response) == "【2027校招】开发工程师"
