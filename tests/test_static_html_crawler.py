from __future__ import annotations

from packages.recruitment_core.crawlers.static_html import StaticHtmlCrawler


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"


def test_static_html_uses_local_hiring_cards_and_rejects_product_navigation(monkeypatch) -> None:
    html = """
    <nav>
      <a href="/products/wireless-communication-test-platform.html">无线通信测试平台</a>
      <a href="/solutions/auto-testing.html">自动测试</a>
      <a href="/products/software-radio-development-platform.html">软件无线电开发平台</a>
    </nav>
    <main>
      <div class="job-row">
        <span>软件工程师</span><span>薪资待遇：14-30W</span><span>招聘人数：2名</span>
        <a href="/information/software-engineer.html">详情&gt;&gt;</a>
      </div>
      <div class="job-row">
        <span>FPGA逻辑工程师</span><span>薪资待遇：14-30W</span><span>招聘人数：2名</span>
        <a href="/information/fpga-engineer.html">查看详情</a>
      </div>
    </main>
    """
    crawler = StaticHtmlCrawler("冻结自建站", "https://jobs.example.test/campus")
    monkeypatch.setattr(crawler, "_get", lambda *args, **kwargs: _Response(html))

    jobs = crawler.fetch()

    assert [job["title"] for job in jobs] == ["软件工程师", "FPGA逻辑工程师"]
    assert [job["jd_url"] for job in jobs] == [
        "https://jobs.example.test/information/software-engineer.html",
        "https://jobs.example.test/information/fpga-engineer.html",
    ]
    assert all(job["link_kind"] == "detail" for job in jobs)
