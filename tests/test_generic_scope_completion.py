from __future__ import annotations

from packages.recruitment_core.crawlers.generic_render import GenericRenderCrawler


def _category_html(active: int) -> str:
    tabs = "".join(
        f'<li data-category-id="cat-{index}" class="{"active" if index == active else ""}">类别{index}</li>'
        for index in range(6)
    )
    title = ("生产管理", "出口操作员", "供应商质量管理", "工艺管理", "销售助理", "软件开发")[active]
    return (
        f'<div class="tabOriginal"><ul class="tit">{tabs}</ul></div>'
        '<div class="cont" data-list-complete="true">'
        f'<div class="items"><a href="/joinUs/inner.aspx?id={active}">'
        f'<span class="position-title">{title}</span></a>'
        "</div></div>"
    )


class _CategoryPage:
    def __init__(self) -> None:
        self.active = 0
        self.url = "https://jobs.example.test/pc/join"

    def content(self) -> str:
        return _category_html(self.active)

    def evaluate(self, script: str, argument=None):
        if "recruitops-category-controls" in script:
            return [
                {
                    "index": index,
                    "key": f"cat-{index}",
                    "text": f"类别{index}",
                    "selected": index == self.active,
                    "disabled": False,
                }
                for index in range(6)
            ]
        if "recruitops-category-click" in script:
            self.active = int(str(argument["key"]).rsplit("-", 1)[1])
            return True
        return 0

    def locator(self, _selector):
        return _EmptyLocator()


class _EmptyLocator:
    @property
    def first(self):
        return self

    def count(self):
        return 0


def test_structured_titles_are_kept_without_role_word_and_non_job_navigation_is_dropped() -> None:
    html = "".join(
        [
            '<a href="/joinUs/organization.aspx">组织架构</a>',
            '<div class="job_hot"><a href="/joinUs/inner.aspx?id=1">'
            '<span class="title">供应商质量管理</span></a></div>',
            '<div class="job_trend"><a href="/joinUs/inner.aspx?id=2">'
            '<span class="title">生产管理</span></a></div>',
            '<div class="job-item"><a href="/joinUs/inner.aspx?id=3">'
            '<span class="title">出口操作员</span></a></div>',
        ]
    )
    crawler = GenericRenderCrawler("公司甲", "https://jobs.example.test/joinUs/job.aspx")

    selector = crawler._pick_selector(html)
    jobs: list[dict] = []
    crawler._parse(html, selector, jobs, set(), source_url=crawler.careers_url)

    assert crawler._clean_title("供应商质量管理") == "供应商质量管理"
    assert {job["title"] for job in jobs} == {"供应商质量管理", "生产管理", "出口操作员"}
    assert all("organization" not in job["jd_url"] for job in jobs)


def test_category_tabs_are_all_visited_and_each_scope_has_structured_completion() -> None:
    page = _CategoryPage()
    crawler = GenericRenderCrawler("公司乙", page.url)
    crawler._settle_lazy_list = lambda _page: {
        "html_snapshots": [page.content()],
        "termination": "lazy_scroll_stable",
        "scroll_moved": False,
        "complete_evidence": False,
    }

    result = crawler._traverse_category_lists(page, crawler._settle_lazy_list(page))

    assert result["category_controls_seen"] == 6
    assert result["categories_completed"] is True
    assert [item["text"] for item in result["categories"]] == [f"类别{index}" for index in range(6)]
    assert len(result["html_snapshots"]) == 6

    selector = crawler._pick_selector("\n".join(result["html_snapshots"]))
    jobs: list[dict] = []
    seen: set[tuple[str, ...]] = set()
    for html in result["html_snapshots"]:
        crawler._parse(html, selector, jobs, seen, source_url=page.url)
    assert {job["title"] for job in jobs} == {
        "生产管理", "出口操作员", "供应商质量管理", "工艺管理", "销售助理", "软件开发"
    }


def test_static_single_page_needs_list_structure_and_virtual_windows_remain_incomplete() -> None:
    crawler = GenericRenderCrawler("公司丙", "https://jobs.example.test/jobs")
    static_html = """
    <table id="jobs" data-list-complete="true"><tbody>
      <tr><td><a href="/job/1">供应商质量管理</a></td></tr>
      <tr><td><a href="/job/2">生产管理</a></td></tr>
      <tr><td><a href="/job/3">出口操作员</a></td></tr>
    </tbody></table>
    """
    virtual_html = """
    <article class="job-card"><a href="/job/1"><h3>供应商质量管理</h3></a></article>
    """

    assert crawler._static_list_completion_evidence(
        static_html,
        lazy_result={
            "termination": "lazy_scroll_stable",
            "scroll_moved": False,
            "html_snapshots": [static_html],
        },
    ) is True
    assert crawler._static_list_completion_evidence(
        static_html.replace(' data-list-complete="true"', ''),
        lazy_result={"termination": "lazy_scroll_stable", "scroll_moved": False},
    ) is False
    assert crawler._static_list_completion_evidence(
        virtual_html,
        lazy_result={
            "termination": "lazy_scroll_stable",
            "scroll_moved": True,
            "html_snapshots": [virtual_html],
        },
    ) is False


def test_api_total_is_bound_to_the_outer_job_list_scope() -> None:
    crawler = GenericRenderCrawler("公司丁", "https://jobs.example.test/jobs")
    jobs, totals, has_more = crawler._extract_api_payload(
        {
            "data": {
                "categorySummary": {
                    "total": 99,
                    "items": [{"name": "技术类", "id": "category-1"}],
                },
                "items": [
                    {"title": "供应商质量管理", "jobId": "job-1"},
                    {"title": "生产管理", "jobId": "job-2"},
                ],
                "total": 2,
                "hasMore": False,
            }
        },
        "https://jobs.example.test/api/list",
    )

    assert [job["title"] for job in jobs] == ["供应商质量管理", "生产管理"]
    assert totals == [2]
    assert has_more == [False]


def test_edgeone_security_verification_is_access_blocked_not_missing_selector() -> None:
    crawler = GenericRenderCrawler("公司戊", "https://jobs.example.test/campus")
    challenge = """
    <html><head><title>EdgeOne Security Verification</title></head>
    <body><main><h1>Security Verification</h1><p>Checking your browser</p></main></body></html>
    """

    assert crawler._classify_access_block(challenge, crawler.careers_url) == "accessblocked"
    assert crawler._classify_access_block(
        '<article class="job-card"><h3>供应商质量管理</h3><p>安全验证流程说明</p></article>',
        crawler.careers_url,
    ) == ""
