from __future__ import annotations

from contextlib import nullcontext

import pytest

from packages.recruitment_core.crawlers import moka as moka_module
from packages.recruitment_core.crawlers.moka import MokaRecruitCrawler


def _page(ids=(), *, number=1, disabled=None, total=None, modern=False):
    rows = "".join(
        f'<a href="#/job/{job_id}"><div class="title-job">Engineer {job_id}</div></a>'
        for job_id in ids
    )
    count = f'<div>{total} \u7ed3\u679c</div>' if total is not None else ""
    pager = ""
    if disabled is not None:
        if modern:
            attr = 'disabled=""' if disabled else 'aria-disabled="false"'
            pager = (
                '<div class="sd-Pagination-pagination-9Tli5">'
                f'<button class="sd-Pagination-is-active-1EFsg" data-page="{number}">{number}</button>'
                f'<button class="sd-Pagination-forward-2Dk1n" {attr}></button></div>'
            )
        else:
            state = " disabled-1N_lD40T5F" if disabled else ""
            pager = (
                '<div class="container-1b_F_qpY6E theme-pagination">'
                f'<span class="pagination-item page-qL_0YNWsx3 active-2punE9UQ7w">{number}</span>'
                f'<span class="pagination-item next-page-1ksQAFhCzQ{state}"></span></div>'
            )
    return count + f'<div class="jobs-list">{rows}</div>' + pager


class FakePage:
    def __init__(self, pages):
        self.pages = pages
        self.number = 0
        self.reads = 0
        self.navigations = []
        self.clicks = []
        self.wait_selectors = []
        self.url = ""

    def route(self, *args):
        pass

    def goto(self, url, **kwargs):
        self.navigations.append(url)
        self._advance()
        self.url = url

    def _advance(self):
        self.number += 1
        self.reads = 0

    def wait_for_selector(self, selector, **kwargs):
        self.wait_selectors.append(selector)

    def wait_for_function(self, expression, **kwargs):
        assert kwargs["arg"]["selector"] == MokaRecruitCrawler._JOB_SELECTOR

    def wait_for_timeout(self, timeout):
        pass

    def content(self):
        snapshots = self.pages[self.number]
        snapshot = snapshots[min(self.reads, len(snapshots) - 1)]
        self.reads += 1
        return snapshot

    def locator(self, selector):
        page = self

        class Locator:
            @property
            def first(self):
                return self

            def click(self, **kwargs):
                page.clicks.append(selector)
                page._advance()

        return Locator()


def _browser(monkeypatch, pages):
    page = FakePage(pages)

    class Context:
        def add_init_script(self, script):
            pass

        def new_page(self):
            return page

        def close(self):
            pass

    class Browser:
        def new_context(self, **kwargs):
            return Context()

        def close(self):
            pass

    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: nullcontext(object()))
    monkeypatch.setattr(moka_module, "launch_browser", lambda *args, **kwargs: Browser())
    crawler = MokaRecruitCrawler("Example", "https://app.mokahr.com/campus_apply/example/123#/")
    return crawler, page


@pytest.mark.parametrize("modern", [False, True])
def test_disabled_next_proves_single_page_without_inventing_total(monkeypatch, modern):
    crawler, page = _browser(monkeypatch, {1: [_page(range(14), disabled=True, modern=modern)]})

    jobs = crawler.fetch()

    assert len(jobs) == 14
    assert len(page.navigations) == 1
    assert page.clicks == []
    assert crawler.pagination_complete is True
    assert crawler.pagination_termination_reason == "next_disabled"
    assert crawler.total_pages == crawler.pages_seen == 1
    assert crawler.advertised_total is None
    assert crawler.has_more is False
    evidence = crawler.pagination_metrics()["evidence"][0]
    assert evidence["pagination_control"]["next_disabled"] is True
    assert evidence["pagination_control"]["current_page"] == 1
    assert "disabled" in evidence["pagination_control"]["next_html"]
    assert evidence["has_more"] is False


@pytest.mark.parametrize("modern", [False, True])
def test_enabled_next_is_clicked_and_both_pages_are_collected(monkeypatch, modern):
    crawler, page = _browser(monkeypatch, {
        1: [_page(["one", "two"], disabled=False, modern=modern)],
        2: [_page(["three"], number=2, disabled=True, modern=modern)],
    })

    assert len(crawler.fetch()) == 3
    assert crawler.pagination_complete is True
    assert crawler.pages_seen == crawler.total_pages == 2
    assert len(page.navigations) == len(page.clicks) == 1
    assert crawler.pagination_evidence[0]["pagination_control"]["next_disabled"] is False


def test_bigo_loading_zero_and_recent_jobs_do_not_freeze_total(monkeypatch):
    loading = _page(total=0) + '<div>\u6570\u636e\u8bfb\u53d6\u4e2d</div>'
    loading += '<aside><a href="#/job/recent"><div class="title-job">Recent job</div></a></aside>'
    crawler, page = _browser(monkeypatch, {
        1: [loading, loading, _page(range(30), disabled=False, total=36, modern=True)],
        2: [_page(range(30, 36), number=2, disabled=True, total=36, modern=True)],
    })

    jobs = crawler.fetch()

    assert len(jobs) == 36
    assert all(not job["jd_url"].endswith("/recent") for job in jobs)
    assert crawler.advertised_total == crawler.expected_total == 36
    assert crawler.pagination_complete is True
    assert crawler.pages_seen == 2
    assert crawler.pagination_evidence[0]["retries"] == 2
    assert page.wait_selectors == [crawler._JOB_SELECTOR]


@pytest.mark.parametrize("second, reason", [
    (_page(["one"]), "page_stalled_2"),
    (_page(), "empty_page_2"),
])
def test_duplicate_or_empty_page_without_terminal_evidence_is_incomplete(monkeypatch, second, reason):
    crawler, _ = _browser(monkeypatch, {1: [_page(["one"])], 2: [second]})

    assert len(crawler.fetch()) == 1
    assert crawler.pagination_complete is False
    assert crawler.has_more is True
    assert crawler.pagination_termination_reason == reason


def test_disabled_next_does_not_override_total_shortfall(monkeypatch):
    crawler, _ = _browser(monkeypatch, {1: [_page(["one"], disabled=True, total=2)]})

    assert len(crawler.fetch()) == 1
    assert crawler.pagination_complete is False
    assert crawler.has_more is True
    assert crawler.pagination_termination_reason == "total_shortfall"


def test_enabled_next_does_not_allow_premature_total_reached(monkeypatch):
    crawler, _ = _browser(monkeypatch, {1: [_page(["one"], disabled=False, total=1)]})

    assert len(crawler.fetch()) == 1
    assert crawler.pagination_complete is False
    assert crawler.has_more is True
    assert crawler.pagination_termination_reason == "pagination_evidence_conflict"


def test_pagination_must_match_requested_page(monkeypatch):
    crawler, _ = _browser(monkeypatch, {1: [_page(["one"], number=2, disabled=True)]})

    assert len(crawler.fetch()) == 1
    assert crawler.pagination_complete is False
    assert crawler.pagination_termination_reason == "pagination_page_mismatch"


def test_unconfirmed_rendered_zero_is_not_a_successful_empty_crawl(monkeypatch):
    crawler, _ = _browser(monkeypatch, {1: [_page(total=0)]})

    assert crawler.fetch() == []
    assert crawler.pagination_complete is False
    assert crawler.advertised_total is None
    assert crawler.pagination_termination_reason == "zero_total_unconfirmed"
    assert crawler.pagination_evidence[0]["rendered_total"] == 0


def test_hidden_terminal_control_is_not_accepted():
    markup = '<div style="display: none">' + _page(["one"], disabled=True) + '</div>'

    assert MokaRecruitCrawler._pagination_control(markup) == {}


def test_detail_links_use_observed_redirect_project_path(monkeypatch):
    crawler, page = _browser(monkeypatch, {1: [_page(["native-id"], disabled=True)]})
    crawler.careers_url = "https://campus.example.com/"
    navigate = page.goto

    def redirected_goto(url, **kwargs):
        navigate(url, **kwargs)
        page.url = "https://campus.example.com/campus_apply/example/123/#/jobs?page=1"

    monkeypatch.setattr(page, "goto", redirected_goto)

    jobs = crawler.fetch()

    assert jobs[0]["jd_url"] == (
        "https://campus.example.com/campus_apply/example/123/#/job/native-id"
    )
    embedded = crawler._embedded_jobs(
        {"jobs": [{"id": "embedded-id", "title": "Engineer"}]},
        crawler._base_url(), set(), crawler._make_job,
    )
    assert embedded[0]["jd_url"].endswith("/campus_apply/example/123/#/job/embedded-id")


@pytest.mark.parametrize("total, ids, complete", [(None, ["one"], True), (0, [], False)])
def test_render_fallback_uses_the_same_terminal_evidence(monkeypatch, total, ids, complete):
    crawler = MokaRecruitCrawler("Example", "https://app.mokahr.com/campus_apply/example/123#/")

    def unavailable():
        raise ImportError("test fallback")

    monkeypatch.setattr(crawler, "_fetch_with_reused_browser", unavailable)
    monkeypatch.setattr(moka_module, "render_page", lambda *args, **kwargs: _page(ids, disabled=True, total=total))

    assert len(crawler.fetch()) == len(ids)
    assert crawler.pagination_complete is complete
