from __future__ import annotations

import pytest

from packages.recruitment_core.crawlers.generic_render import GenericRenderCrawler


def test_grouped_detail_links_keep_their_own_titles_and_urls() -> None:
    html = """
    <header><ul><li><a href="/technology/detail">Production technology</a></li></ul></header>
    <div class="job_hot">
      <a href="/joinUs/inner.aspx?id=1">供应商质量管理</a>
      <a href="/joinUs/inner.aspx?id=2">生产管理</a>
      <a href="/joinUs/inner.aspx?id=3">出口操作员</a>
    </div>
    <div class="job-item"><a href="/joinUs/inner.aspx?id=4">职位详情</a></div>
    """
    crawler = GenericRenderCrawler("Example", "https://jobs.example.test/jobs")
    jobs = []
    crawler._parse(html, crawler._pick_selector(html), jobs, set())
    assert [(job["title"], job["jd_url"].rsplit("=", 1)[-1]) for job in jobs] == [
        ("供应商质量管理", "1"), ("生产管理", "2"), ("出口操作员", "3"),
    ]


def test_count_labels_exclude_quota_and_hidden_counts() -> None:
    html = """
    <div>每人最多投递3个职位</div>
    <div>在招职位 <span>17</span></div>
    <div style="visibility:hidden">在招职位 999</div>
    """
    assert GenericRenderCrawler._advertised_job_total(html) == 17
    assert GenericRenderCrawler._advertised_job_total('<div>17在招职位</div>') == 17


_CATEGORY_HTML = """
<div class="tabOriginal">
  <ul class="tit">
    <li data-category-id="one" class="active" onclick="this.dataset.clicked='yes'">技术方向</li>
    <li data-category-id="two" onclick="this.dataset.clicked='yes'">业务方向</li>
  </ul>
  <div class="cont"><div class="items"><ul class="recruithd">
    <li>招聘职位</li><li>培养方向</li><li>本科及以上</li><li>在校/应届</li>
  </ul></div></div>
</div>
"""


def test_category_html_fallback_does_not_promote_nested_job_metadata() -> None:
    class Page:
        def evaluate(self, _script):
            return []

        def content(self):
            return _CATEGORY_HTML

    crawler = GenericRenderCrawler("Example", "https://jobs.example.test/jobs")
    assert [item["key"] for item in crawler._category_controls(Page())] == ["one", "two"]


def test_category_browser_controls_use_stable_keys_not_position_fallback() -> None:
    from playwright.sync_api import Error, sync_playwright
    from packages.recruitment_core.crawlers.base import launch_browser

    with sync_playwright() as runtime:
        try:
            browser = launch_browser(runtime, headless=True)
        except Error as exc:
            pytest.skip(f"Configured Playwright browser is unavailable: {exc}")
        try:
            page = browser.new_page()
            page.set_content(_CATEGORY_HTML)
            crawler = GenericRenderCrawler("Example", "https://jobs.example.test/jobs")
            controls = crawler._category_controls(page)
            assert [item["key"] for item in controls] == ["one", "two"]
            # Deliberately wrong ordinal: identity, not index, must select the tab.
            assert crawler._click_category_control(page, {**controls[1], "index": 0})
            assert page.locator('[data-category-id="two"]').get_attribute("data-clicked") == "yes"
            assert page.locator('[data-category-id="one"]').get_attribute("data-clicked") is None
            assert not crawler._click_category_control(page, {"key": "missing", "text": "Missing", "index": 0})
        finally:
            browser.close()
