from __future__ import annotations

from packages.recruitment_core.crawlers.generic_render import GenericRenderCrawler


def test_render_crawler_extracts_jobs_and_completeness_from_spa_json() -> None:
    crawler = GenericRenderCrawler("示例公司", "https://jobs.example.com/campus")
    jobs, totals, has_more = crawler._extract_api_payload(
        {
            "data": {
                "list": [
                    {
                        "positionName": "算法工程师",
                        "positionId": "p1",
                        "workLocation": "上海",
                        "description": "负责算法研发",
                        "detailUrl": "/campus/job/p1",
                    },
                    {
                        "jobName": "软件开发工程师",
                        "jobId": "p2",
                        "cityName": "深圳",
                        "requirement": "熟悉 Python",
                    },
                ],
                "totalCount": 2,
                "hasMore": False,
            }
        },
        "https://jobs.example.com/api/positions",
    )

    assert [job["title"] for job in jobs] == ["算法工程师", "软件开发工程师"]
    assert jobs[0]["jd_url"] == "https://jobs.example.com/campus/job/p1"
    assert totals == [2]
    assert has_more == [False]


def test_render_crawler_ignores_category_cards_and_reads_advertised_total() -> None:
    crawler = GenericRenderCrawler("示例公司", "https://jobs.example.com/campus")
    jobs, totals, _ = crawler._extract_api_payload(
        {"items": [{"title": "技术类", "category": "技术"}], "total": 1},
        "https://jobs.example.com/api/categories",
    )

    assert jobs == []
    assert totals == []
    assert crawler._advertised_job_total("<h2>开启新的工作（121）</h2>") == 121


def test_render_crawler_ranks_campus_entry_above_social_and_generic_links() -> None:
    urls = GenericRenderCrawler._recruitment_entry_urls(
        """
        <a href='/about'>关于我们</a>
        <a href='/career/social'>社会招聘</a>
        <a href='https://ats.example.com/campus/jobs'>校园招聘</a>
        <a href='/join'>加入我们</a>
        """,
        "https://www.example.com/",
    )

    assert urls[0] == "https://ats.example.com/campus/jobs"
    assert "https://www.example.com/about" not in urls


def test_render_crawler_keeps_normal_job_card_metadata() -> None:
    crawler = GenericRenderCrawler("示例公司", "https://jobs.example.com/campus")
    html = """
    <section>
      <article><h3>算法工程师</h3><p>工作地点：上海 发布时间：2026-08-28 招聘人数：3人</p></article>
      <article><h3>软件工程师</h3><p>工作地点：深圳 发布时间：2026-08-28 招聘人数：2人</p></article>
      <article><h3>测试工程师</h3><p>工作地点：苏州 发布时间：2026-08-28 招聘人数：1人</p></article>
    </section>
    """
    selector = crawler._pick_selector(html)

    assert selector == "h3"
    assert crawler._selector_parsed_count(html, selector) == 3
