from __future__ import annotations

from packages.recruitment_core.crawlers.declarative import DeclarativeRecruitCrawler


def test_html_list_recipe_extracts_detail_jd_and_verifies_terminal_page() -> None:
    listing = "https://careers.example.test/jobs"
    pages = {
        listing: """
            <main>
              <article class='job'><h3>算法工程师</h3><a href='/jobs/1'>详情</a></article>
              <article class='job'><h3>软件工程师</h3><a href='/jobs/2'>详情</a></article>
            </main>
        """,
        "https://careers.example.test/jobs/1": (
            "<section class='jd'>岗位职责：负责算法研发与落地。任职要求：熟悉 Python、机器学习和工程实践。"
            + "参与模型训练、评测、部署和持续优化。" * 8 + "</section>"
        ),
        "https://careers.example.test/jobs/2": (
            "<section class='jd'>岗位职责：负责软件系统设计开发。任职要求：熟悉数据结构、数据库和测试。"
            + "完成系统开发、联调、测试和持续优化。" * 8 + "</section>"
        ),
    }

    crawler = DeclarativeRecruitCrawler(
        "测试公司",
        listing,
        {
            "type": "html_list",
            "listing_url": listing,
            "list_selector": "article.job",
            "title_selector": "h3",
            "detail_link_selector": "a[href]",
            "jd_selector": ".jd",
        },
        page_renderer=lambda url, **_kwargs: pages[url],
    )

    jobs = crawler.fetch()

    assert [job["title"] for job in jobs] == ["算法工程师", "软件工程师"]
    assert all("任职要求" in job["jd_raw"] for job in jobs)
    assert crawler.pagination_complete is True
    assert crawler.pagination_termination_reason == "single_page"


def test_api_campaign_recipe_paginates_to_total_and_hydrates_detail() -> None:
    calls = []

    def request_json(request, body):
        calls.append((request["url"], dict(body)))
        if "/detail/" in request["url"]:
            return {"data": {"description": "岗位职责与任职要求：" + "完成软件研发与测试。" * 20}}
        page = body["page"]
        return {
            "data": {
                "rows": ([{"id": "1", "title": "软件工程师"}] if page == 1 else []),
                "total": 1,
            }
        }

    crawler = DeclarativeRecruitCrawler(
        "API公司",
        "https://careers.example.test/jobs",
        {
            "type": "api_campaigns",
            "request": {"method": "POST", "url": "https://careers.example.test/api/jobs", "body": {"page": 1, "pageSize": 10}},
            "pagination": {"page_key": "page", "size_key": "pageSize", "page_size": 10},
            "items_path": "$.data.rows",
            "total_path": "$.data.total",
            "field_map": {"id": "id", "title": "title", "jd": []},
            "detail_url_template": "https://careers.example.test/jobs/{id}",
            "detail_api": {"url_template": "https://careers.example.test/detail/{id}", "record_path": "$.data", "jd_fields": ["description"]},
            "scopes": [{"include": True, "cohort": 2027, "label": "2027届", "evidence": "OC 2027届秋招"}],
        },
        json_requester=request_json,
    )

    jobs = crawler.fetch()

    assert len(jobs) == 1
    assert jobs[0]["source_job_id"] == "1"
    assert "任职要求" in jobs[0]["jd_raw"]
    assert crawler.pagination_complete is True


def test_html_list_recipe_replays_next_page_selector_until_no_new_jobs() -> None:
    listing = "https://careers.example.test/jobs"
    calls: list[list[str]] = []

    def render(url, **kwargs):
        if url != listing:
            return "<main>岗位职责和任职要求：" + "软件开发与测试。" * 30 + "</main>"
        selectors = list(kwargs.get("click_selectors") or [])
        calls.append(selectors)
        page = min(len(selectors) + 1, 2)
        return (
            "<article class='job'><h3>岗位"
            f"{page}</h3><a href='/jobs/{page}'>详情</a></article>"
        )

    crawler = DeclarativeRecruitCrawler(
        "分页公司",
        listing,
        {
            "type": "html_list",
            "list_selector": ".job",
            "title_selector": "h3",
            "next_page_selector": "button.next",
            "max_pages": 5,
        },
        page_renderer=render,
    )

    jobs = crawler.fetch()

    assert [job["title"] for job in jobs] == ["岗位1", "岗位2"]
    assert calls == [[], ["button.next"], ["button.next", "button.next"]]
    assert crawler.pagination_complete is True
    assert crawler.pagination_termination_reason == "next_page_exhausted"
