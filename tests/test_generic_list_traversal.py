from __future__ import annotations

from contextlib import nullcontext

import pytest

from packages.recruitment_core.crawlers.generic_render import GenericRenderCrawler


def _job_html(job_id: str, *, load_more: str = "") -> str:
    control = f'<button class="load-more">{load_more}</button>' if load_more else ""
    return (
        '<section class="job-list">'
        f'<article class="job-card" data-job-id="{job_id}">'
        f'<a href="/campus/job/{job_id}"><h3 class="title">算法工程师 {job_id}</h3></a>'
        "<p>工作地点：上海</p>"
        "</article>"
        f"</section>{control}"
    )


class FakeLazyPage:
    def __init__(self, html_states: list[str], *, load_more: bool = False):
        self.html_states = html_states
        self.index = 0
        self.load_more = load_more
        self.scroll_evaluations = 0
        self.url = "https://jobs.example.test/campus"

    def content(self) -> str:
        return self.html_states[self.index]

    def wait_for_timeout(self, _timeout: int) -> None:
        return None

    def evaluate(self, script: str):
        if "recruitops-load-more" in script:
            if not self.load_more:
                return {"found": False, "clicked": False, "disabled": False}
            if self.index < len(self.html_states) - 1:
                self.index += 1
                return {"found": True, "clicked": True, "disabled": False}
            return {"found": True, "clicked": False, "disabled": True}
        if "recruitops-lazy-scroll" in script:
            self.scroll_evaluations += 1
            if not self.load_more and self.index < len(self.html_states) - 1:
                self.index += 1
                return 1
            return 0
        return 0


def test_lazy_traversal_visits_nested_scroll_rounds_and_keeps_each_snapshot() -> None:
    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    page = FakeLazyPage([_job_html("nested-1"), _job_html("nested-2")])

    result = crawler._settle_lazy_list(page)

    assert page.scroll_evaluations >= 1
    assert result["termination"] == "lazy_scroll_stable"
    assert result["complete_evidence"] is False
    assert result["observed_job_keys"] == {
        ("native_job_id", "nested-1"),
        ("native_job_id", "nested-2"),
    }
    assert len(result["html_snapshots"]) == 2


def test_virtual_dom_replacement_is_replayed_and_deduplicated_by_native_id() -> None:
    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    page = FakeLazyPage([_job_html("v-1"), _job_html("v-2"), _job_html("v-3")])

    result = crawler._settle_lazy_list(page)
    selector = crawler._pick_selector("\n".join(result["html_snapshots"]))
    jobs: list[dict] = []
    seen: set[tuple[str, ...]] = set()
    for html in result["html_snapshots"]:
        crawler._parse(html, selector, jobs, seen, source_url=page.url)

    assert selector in {"a", "h3.title"}
    assert [job["native_job_id"] for job in jobs] == ["v-1", "v-2", "v-3"]


def test_load_more_is_clicked_until_disabled_with_bounded_completion_evidence() -> None:
    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    page = FakeLazyPage(
        [_job_html("more-1", load_more="加载更多"), _job_html("more-2", load_more="加载更多"), _job_html("more-3", load_more="加载更多")],
        load_more=True,
    )

    result = crawler._settle_lazy_list(page)

    assert result["load_more_seen"] is True
    assert result["load_more_clicked"] is True
    assert result["termination"] == "load_more_disabled"
    assert result["complete_evidence"] is True
    assert {key[1] for key in result["observed_job_keys"]} == {"more-1", "more-2", "more-3"}


def test_repeated_lazy_dom_without_termination_evidence_stays_unknown() -> None:
    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    page = FakeLazyPage([_job_html("same"), _job_html("same")])

    result = crawler._settle_lazy_list(page)

    assert result["termination"] == "lazy_scroll_stalled"
    assert result["complete_evidence"] is False
    assert result["observed_job_keys"] == {("native_job_id", "same")}


def test_real_local_playwright_virtual_list_captures_middle_windows() -> None:
    playwright = pytest.importorskip("playwright.sync_api")
    html = """
    <style>
      #viewport { width: 240px; height: 120px; overflow-y: auto; position: relative; }
      #spacer { height: 4000px; position: relative; }
      #rows { position: absolute; inset: 0; }
      .job-card { height: 40px; }
    </style>
    <div id="viewport"><div id="spacer"><div id="rows"></div></div></div>
    <script>
      const viewport = document.querySelector('#viewport');
      const rows = document.querySelector('#rows');
      const rowHeight = 40;
      function render() {
        const start = Math.floor(viewport.scrollTop / rowHeight);
        const end = Math.min(100, start + 5);
        rows.innerHTML = Array.from({length: end - start}, (_, offset) => {
          const id = start + offset;
          return `<article class="job-card" data-job-id="job-${id}">
            <a href="/campus/job/${id}"><h3 class="title">算法工程师 ${id}</h3></a>
            <p>工作地点：上海</p>
          </article>`;
        }).join('');
      }
      viewport.addEventListener('scroll', render);
      render();
    </script>
    """

    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page()
        page.set_content(html)
        crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
        crawler.PAGE_CHANGE_WAIT_MS = 25
        crawler.LAZY_SIGNATURE_POLLS = 4
        result = crawler._settle_lazy_list(page)
        browser.close()

    native_ids = {
        key[1]
        for key in result["observed_job_keys"]
        if key[0] == "native_job_id"
    }
    assert len(native_ids) == 100
    assert {"job-0", "job-50", "job-99"} <= native_ids
    assert result["termination"] == "lazy_scroll_stable"
    assert result["complete_evidence"] is False


def test_selectorless_line_fallback_still_advances_an_observed_next_page(monkeypatch) -> None:
    def page_html(title: str, page_number: int, *, disabled: bool = False) -> str:
        state = " disabled" if disabled else ""
        return (
            f"<main><p>职位：{title}</p>"
            f'<div class="pagination"><span class="active">{page_number}</span>'
            f'<button class="btn-next"{state}>下一页</button></div></main>'
        )

    class EmptyLocator:
        @property
        def first(self):
            return self

        def count(self):
            return 0

    class NextControl:
        def __init__(self, page):
            self.page = page

        def count(self):
            return 1

        def is_visible(self):
            return True

        def is_disabled(self):
            return self.page.index == 1

        def get_attribute(self, name):
            if name == "disabled" and self.page.index == 1:
                return ""
            return None

        def locator(self, _selector):
            return self

        def click(self, **_kwargs):
            self.page.index = 1
            self.page.url = "https://jobs.example.test/campus?page=2"

    class NextCollection:
        def __init__(self, page):
            self.page = page

        def count(self):
            return 1

        def nth(self, _index):
            return NextControl(self.page)

    class Page:
        def __init__(self):
            self.index = 0
            self.url = ""
            self.html_states = [
                page_html("供应商质量管理", 1),
                page_html("出口操作员", 2, disabled=True),
            ]

        def on(self, _event, _callback):
            pass

        def goto(self, url, **_kwargs):
            self.url = url + "?page=1"

        def wait_for_timeout(self, _timeout):
            pass

        def content(self):
            return self.html_states[self.index]

        def evaluate(self, _script):
            return []

        def locator(self, selector):
            if "pagination" in selector and "pagination" in self.content():
                return NextCollection(self)
            return EmptyLocator()

        def get_by_role(self, **_kwargs):
            return EmptyLocator()

        def get_by_text(self, *_args, **_kwargs):
            return EmptyLocator()

    class Context:
        def __init__(self, page):
            self.page = page

        def new_page(self):
            return self.page

        def close(self):
            pass

    class Browser:
        def __init__(self, page):
            self.page = page

        def new_context(self, **_kwargs):
            return Context(self.page)

        def close(self):
            pass

    page = Page()
    browser = Browser(page)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: nullcontext(object()))
    monkeypatch.setattr(
        "packages.recruitment_core.crawlers.generic_render.launch_browser",
        lambda *_args, **_kwargs: browser,
    )
    monkeypatch.setattr(GenericRenderCrawler, "_settle_lazy_list", lambda *_args: None)

    crawler = GenericRenderCrawler("甲公司", "https://jobs.example.test/campus")
    jobs = crawler.fetch()

    assert [job["title"] for job in jobs] == ["供应商质量管理", "出口操作员"]
    assert crawler.pages_seen == 2
    assert crawler.pagination_termination_reason == "next_disabled"
    assert crawler.pagination_complete is True
