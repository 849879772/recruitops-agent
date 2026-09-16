from __future__ import annotations

from contextlib import nullcontext

from bs4 import BeautifulSoup

from packages.recruitment_core.crawlers.generic_render import GenericRenderCrawler


def test_nonstandard_engineering_optimization_title_is_a_job_candidate() -> None:
    crawler = GenericRenderCrawler("灵心巧手", "https://www.linkerbot.cn/about/join/")

    assert crawler._clean_title("具身端侧模型工程优化") == "具身端侧模型工程优化"


def test_table_row_is_the_card_boundary_and_records_exact_dom_link_source() -> None:
    crawler = GenericRenderCrawler("沛睿", "https://jobs.example.test/Home/Position")
    html = """
    <table>
      <thead><tr><th>序号</th><th>岗位名称</th></tr></thead>
      <tbody>
        <tr><td>1</td><td><a href="/Home/PositionDetails/95">固件/软件开发工程师</a></td></tr>
        <tr><td>2</td><td><a href="/Home/PositionDetails/96">算法工程师</a></td></tr>
        <tr><td>3</td><td><a href="/Home/PositionDetails/97">测试工程师</a></td></tr>
      </tbody>
    </table>
    """

    soup = crawler._card_context(BeautifulSoup(html, "html.parser").find("a"))
    selector = crawler._pick_selector(html)
    jobs: list[dict] = []

    assert "序号" not in soup
    assert selector == "a"
    assert crawler._parse(
        html,
        selector,
        jobs,
        set(),
        source_url="https://jobs.example.test/Home/Position?project=2027",
    ) == 3
    assert jobs[0]["jd_url"] == "https://jobs.example.test/Home/PositionDetails/95"
    assert jobs[0]["detail_link_observed"] is True
    assert jobs[0]["detail_link_source_url"] == "https://jobs.example.test/Home/Position?project=2027"


def test_wkai_li_cards_accept_observed_nonstandard_detail_paths() -> None:
    source = "https://www.wkai.cc/hr/zp-campus.html"
    crawler = GenericRenderCrawler("万凯新材", source)
    html = """
    <ul class="job-list">
      <li><a href="/hr/zpinfo.html?id=2090709992214499329"><p>
        <span>能源地质技术岗</span><span>5</span><span>应届生</span><span>硕士及以上</span>
        <span>2026-08-21 15:58:00</span>
      </p><div class="txt"></div></a></li>
      <li><a href="/hr/zpinfo.html?id=2090709992214499330"><p>
        <span>生产技术岗</span><span>3</span><span>应届生</span><span>本科及以上</span>
        <span>2026-08-21 15:58:00</span>
      </p><div class="txt"></div></a></li>
      <li><a href="/hr/zpinfo.html?id=2090709992214499331"><p>
        <span>质量管理岗</span><span>2</span><span>应届生</span><span>硕士及以上</span>
        <span>2026-08-21 15:58:00</span>
      </p><div class="txt"></div></a></li>
    </ul>
    """

    selector = crawler._pick_selector(html)
    jobs: list[dict] = []
    assert crawler._parse(html, selector, jobs, set(), source_url=source) == 3
    assert selector == "span"
    assert [job["title"] for job in jobs] == ["能源地质技术岗", "生产技术岗", "质量管理岗"]
    assert jobs[0]["jd_url"] == "https://www.wkai.cc/hr/zpinfo.html?id=2090709992214499329"
    assert jobs[0]["detail_link_observed"] is True
    assert jobs[0]["detail_link_source_url"] == source
    assert jobs[0]["source_job_id"] == "2090709992214499329"


def test_observed_detail_query_binds_source_job_id_without_guessing() -> None:
    source = "https://hr.example.test/weixin/?r=job/agent"
    crawler = GenericRenderCrawler("示例", source)
    html = """
    <ul class="job-list">
      <li class="job-card">
        <a href="/weixin/?r=job/view&id=JO20260831002&type=agent">
          <h4 class="job-title">【2027校招】游戏运营培训生</h4>
          <span>广州</span>
        </a>
      </li>
    </ul>
    """
    selector = "h4.job-title"
    jobs: list[dict] = []

    assert crawler._parse(html, selector, jobs, set(), source_url=source) == 1
    assert jobs[0]["source_job_id"] == "JO20260831002"
    assert jobs[0]["detail_link_observed"] is True
    assert crawler._observed_job_id_from_url("https://example.test/jobs") == ""


def test_bluetrum_card_anchor_search_ignores_apply_and_pagination_links() -> None:
    source = "https://www.bluetrum.com/job/index.php?class2=77"
    crawler = GenericRenderCrawler("中科蓝讯", source)
    titles = [
        "嵌入式软件工程师（视频方向）-珠海",
        "嵌入式软件工程师（物联网方向）-深圳",
        "嵌入式软件工程师（蓝牙方向）-珠海",
        "嵌入式软件工程师（wifi方向）-深圳",
        "图像算法工程师-珠海",
        "模拟电路设计工程师-珠海",
    ]
    cards = "".join(
        f'''<div class="card col-md-6"><div class="card-body card-shadow">
          <h4 class="card-title p-0 font-size-24"><span>{title}</span></h4>
          <p class="card-metas">2026-08-21 珠海 不限</p>
          <div class="met-editor"><p>岗位职责：负责相关研发工作。</p><p>任职资格：符合校招要求。</p></div>
          <div class="card-body-footer"><a href="javascript:;" data-jobid="{416 + index}">在线应聘</a></div>
        </div></div>'''
        for index, title in enumerate(titles)
    )
    html = f'''<div class="met-job-list met-pager-ajax clearfix">{cards}
      <div class="pagination"><a href="/job/index.php?class2=77&page=2">2</a></div>
    </div>'''

    selector = crawler._pick_selector(html)
    jobs: list[dict] = []
    assert crawler._parse(html, selector, jobs, set(), source_url=source) == 6
    assert selector == "h4.card-title.p-0.font-size-24"
    assert len(jobs) == 6
    assert all(job["link_kind"] == "list" for job in jobs)
    assert all(job["jd_url"] == source for job in jobs)
    assert [job["native_job_id"] for job in jobs] == [str(416 + index) for index in range(6)]
    assert all(job.get("detail_link_observed") is not True for job in jobs)
    assert all("岗位职责" in job["jd_raw"] for job in jobs)


def test_min_hits_does_not_promote_navigation_or_developer_docs() -> None:
    crawler = GenericRenderCrawler("中望", "https://www.example.test/job/campus")
    html = """
    <nav class="site-menu">
      <a href="/developer">成为开发者</a>
      <a href="/developer/community">开发者社区</a>
      <a href="/developer/docs">中望 CAD/3D 开发文档</a>
      <a href="/service">定制开发服务</a>
    </nav>
    """

    assert crawler._pick_selector(html) == ""
    assert crawler._clean_title("成为开发者") == ""
    assert crawler._clean_title("中望 CAD/3D 开发文档") == ""


def test_inline_jd_stays_inside_its_own_job_card() -> None:
    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    html = """
    <section class="job-list">
      <article class="job-card">
        <h3>算法工程师</h3>
        <p>岗位职责：负责算法研发与落地。</p>
        <p>任职要求：熟悉 Python 和机器学习。</p>
      </article>
      <article class="job-card">
        <h3>软件开发工程师</h3>
        <p>岗位职责：负责后端服务开发。</p>
        <p>任职要求：熟悉 Go 服务治理。</p>
      </article>
      <article class="job-card">
        <h3>测试工程师</h3>
        <p>岗位职责：负责自动化质量保障。</p>
        <p>任职要求：熟悉测试框架。</p>
      </article>
    </section>
    """

    selector = crawler._pick_selector(html)
    jobs: list[dict] = []
    crawler._parse(html, selector, jobs, set(), source_url="https://jobs.example.test/campus")

    assert selector == "h3"
    assert len(jobs) == 3
    assert "后端服务开发" not in jobs[0]["jd_raw"]
    assert "机器学习" not in jobs[1]["jd_raw"]
    assert "测试框架" not in jobs[1]["jd_raw"]
    assert "自动化质量保障" in jobs[2]["jd_raw"]


def test_api_jobs_do_not_claim_dom_detail_link_observation() -> None:
    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    jobs, totals, has_more = crawler._extract_api_payload(
        {
            "data": {
                "items": [
                    {
                        "title": "算法工程师",
                        "jobId": "p1",
                        "detailUrl": "https://api.example.test/guess/p1",
                    }
                ],
                "total": 1,
                "hasMore": False,
            }
        },
        "https://api.example.test/positions",
    )

    assert jobs[0]["jd_url"] == "https://api.example.test/guess/p1"
    assert "detail_link_observed" not in jobs[0]
    assert "detail_link_source_url" not in jobs[0]
    assert totals == [1]
    assert has_more == [False]


def test_api_jobs_accept_generic_endpoint_names_and_common_record_fields() -> None:
    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    jobs, totals, has_more = crawler._extract_api_payload(
        {
            "data": {
                "items": [
                    {
                        "name": "嵌入式软件工程师",
                        "id": "p1",
                        "address": "深圳",
                        "link": "/society/55.html",
                    }
                ],
                "total": 1,
                "hasMore": False,
            }
        },
        "https://api.example.test/schedule",
    )

    assert [job["title"] for job in jobs] == ["嵌入式软件工程师"]
    assert jobs[0]["city"] == "深圳"
    assert jobs[0]["jd_url"] == "https://api.example.test/society/55.html"
    assert totals == [1]
    assert has_more == [False]


def test_bounded_job_links_accept_nonstandard_detail_paths() -> None:
    source = "https://jobs.example.test/schedule.html"
    crawler = GenericRenderCrawler("示例", source)
    html = """
    <table><tbody>
      <tr><td><a href="/society/55.html">FAE工程师</a></td></tr>
      <tr><td><a href="/society/1093.html">IC数字验证工程师</a></td></tr>
      <tr><td><a href="/society/1097.html">硬件设计工程师</a></td></tr>
    </tbody></table>
    """

    selector = crawler._pick_selector(html)
    jobs: list[dict] = []

    assert selector == "a"
    assert crawler._parse(html, selector, jobs, set(), source_url=source) == 3
    assert [job["jd_url"] for job in jobs] == [
        "https://jobs.example.test/society/55.html",
        "https://jobs.example.test/society/1093.html",
        "https://jobs.example.test/society/1097.html",
    ]


def test_fetch_collects_job_shaped_json_from_a_generic_endpoint(monkeypatch) -> None:
    class EmptyLocator:
        @property
        def first(self):
            return self

        def count(self):
            return 0

        def is_visible(self):
            return False

    class Response:
        url = "https://jobs.example.test/schedule"
        headers = {"content-type": "application/json"}
        request = None

        @staticmethod
        def json():
            return {
                "data": {
                    "items": [
                        {
                            "name": "嵌入式软件工程师",
                            "id": "p1",
                            "address": "深圳",
                            "link": "/society/55.html",
                        }
                    ],
                    "total": 1,
                    "hasMore": False,
                }
            }

    class Page:
        def __init__(self):
            self.url = ""
            self.response_handler = None

        def on(self, _event, callback):
            self.response_handler = callback

        def goto(self, url, **_kwargs):
            self.url = url
            self.response_handler(Response())

        def wait_for_timeout(self, _timeout):
            pass

        def content(self):
            return "<main><p>职位加载中</p></main>"

        def evaluate(self, _script):
            return 100

        def locator(self, _selector):
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
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: nullcontext(object()),
    )
    monkeypatch.setattr(
        "packages.recruitment_core.crawlers.generic_render.launch_browser",
        lambda *_args, **_kwargs: browser,
    )
    monkeypatch.setattr(GenericRenderCrawler, "_settle_lazy_list", lambda *_args: None)

    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    jobs = crawler.fetch()

    assert [job["title"] for job in jobs] == ["嵌入式软件工程师"]
    assert crawler.advertised_total == 1
    assert crawler.pagination_complete is True


def test_entry_interaction_continues_after_dismissing_a_notice(monkeypatch) -> None:
    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    calls: list[str] = []
    monkeypatch.setattr(
        crawler,
        "_dismiss_recruitment_notice",
        lambda _page: calls.append("dismiss") or True,
    )
    monkeypatch.setattr(
        crawler,
        "_submit_empty_search",
        lambda _page: calls.append("search") or False,
    )
    monkeypatch.setattr(
        crawler,
        "_click_campus_entry",
        lambda _page: calls.append("campus") or True,
    )

    assert crawler._run_observed_entry_interaction(object()) is True
    assert calls == ["dismiss", "campus", "search"]


def test_entry_interaction_searches_only_after_campus_selection(monkeypatch) -> None:
    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/join")
    calls: list[str] = []
    monkeypatch.setattr(crawler, "_dismiss_recruitment_notice", lambda _page: False)
    monkeypatch.setattr(
        crawler,
        "_submit_empty_search",
        lambda _page: calls.append("search") or True,
    )
    monkeypatch.setattr(
        crawler,
        "_click_campus_entry",
        lambda _page: calls.append("campus") or True,
    )

    assert crawler._run_observed_entry_interaction(object()) is True
    assert calls == ["campus", "search"]


def test_campus_entry_requires_exact_control_and_records_scope_change(monkeypatch) -> None:
    class Parent:
        def get_attribute(self, _name):
            return ""

    class Locator:
        first = None

        def __init__(self, page, found: bool):
            self.page = page
            self.found = found
            self.first = self

        def count(self):
            return int(self.found)

        def is_visible(self):
            return self.found

        def get_attribute(self, name):
            if name == "class" and self.page.campus:
                return "tab select-color"
            return ""

        def locator(self, _selector):
            return Parent()

        def click(self, **_kwargs):
            self.page.campus = True

    class Page:
        campus = False

        def get_by_text(self, pattern):
            return Locator(self, bool(pattern.fullmatch("校园招聘")))

        def content(self):
            title = "具身 AI Infra 研发工程师" if self.campus else "机器人产品经理"
            return f"<main><h3>{title}</h3></main>"

    crawler = GenericRenderCrawler("灵心巧手", "https://www.linkerbot.cn/about/join/")
    monkeypatch.setattr(crawler, "_wait_for_async_list", lambda _page: True)

    assert crawler._click_campus_entry(Page()) is True
    assert crawler.recruitment_scope_changed is True


def test_network_total_is_selected_from_one_listing_scope_not_global_max() -> None:
    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    campus_scope = crawler._listing_scope_key("https://jobs.example.test/campus?project=2027&page=1")
    other_scope = crawler._listing_scope_key("https://jobs.example.test/campus?project=social&page=1")
    selected_job = crawler._make_job(title="算法工程师", city="上海", jd_url="https://jobs.example.test/job/1")
    unrelated_job = crawler._make_job(title="研发工程师", city="北京", jd_url="https://jobs.example.test/job/2")

    jobs, total, has_more, _, conflict = crawler._select_network_observation(
        [
            {
                "source_scope": campus_scope,
                "api_scope": crawler._api_scope_key("https://api.example.test/jobs?project=2027&page=1"),
                "jobs": [selected_job],
                "totals": [10],
                "has_more": [False],
            },
            {
                "source_scope": other_scope,
                "api_scope": crawler._api_scope_key("https://api.example.test/jobs?project=social&page=1"),
                "jobs": [unrelated_job],
                "totals": [999],
                "has_more": [True],
            },
        ],
        [selected_job],
    )

    assert [job["title"] for job in jobs] == ["算法工程师"]
    assert total == 10
    assert has_more is True
    assert conflict is False


def test_network_has_more_uses_latest_scope_termination_and_unique_total() -> None:
    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    source_scope = crawler._listing_scope_key("https://jobs.example.test/campus?project=2027&page=1")
    api_scope = crawler._api_scope_key("https://api.example.test/jobs?project=2027&page=1")
    first_job = crawler._make_job(
        title="算法工程师", city="上海", jd_url="https://jobs.example.test/job/1"
    )
    second_job = crawler._make_job(
        title="软件工程师", city="上海", jd_url="https://jobs.example.test/job/2"
    )

    jobs, total, has_more, _, conflict = crawler._select_network_observation(
        [
            {
                "source_scope": source_scope,
                "api_scope": api_scope,
                "jobs": [first_job],
                "totals": [2],
                "has_more": [True],
            },
            {
                "source_scope": source_scope,
                "api_scope": api_scope,
                "jobs": [second_job],
                "totals": [2],
                "has_more": [False],
            },
        ],
        [first_job, second_job],
    )

    assert len(jobs) == 2
    assert total == 2
    assert has_more is False
    assert conflict is False


def test_network_total_changes_in_one_scope_are_conflicting_not_majority_voted() -> None:
    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    source_scope = crawler._listing_scope_key("https://jobs.example.test/campus?project=2027&page=1")
    api_scope = crawler._api_scope_key("https://api.example.test/jobs?project=2027&page=1")
    first_job = crawler._make_job(
        title="算法工程师", city="上海", jd_url="https://jobs.example.test/job/1"
    )
    second_job = crawler._make_job(
        title="软件工程师", city="上海", jd_url="https://jobs.example.test/job/2"
    )

    _, total, has_more, _, conflict = crawler._select_network_observation(
        [
            {
                "source_scope": source_scope,
                "api_scope": api_scope,
                "jobs": [first_job],
                "totals": [2],
                "has_more": [True],
            },
            {
                "source_scope": source_scope,
                "api_scope": api_scope,
                "jobs": [second_job],
                "totals": [3],
                "has_more": [False],
            },
        ],
        [first_job, second_job],
    )

    assert total == 3
    assert has_more is True
    assert conflict is True


def test_post_api_scope_hash_ignores_paging_secrets_but_separates_filters() -> None:
    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    endpoint = "https://api.example.test/jobs"
    first_page = crawler._api_scope_key(
        endpoint,
        {
            "page": 1,
            "cursor": "cursor-a",
            "secret": "secret-a",
            "filters": {"city": "上海", "category": "研发"},
        },
    )
    later_page = crawler._api_scope_key(
        endpoint,
        {
            "page": 2,
            "cursor": "cursor-b",
            "secret": "secret-b",
            "filters": {"category": "研发", "city": "上海"},
        },
    )
    other_filter = crawler._api_scope_key(
        endpoint,
        {
            "page": 1,
            "cursor": "cursor-c",
            "secret": "secret-c",
            "filters": {"city": "北京", "category": "研发"},
        },
    )

    assert first_page == later_page
    assert first_page != other_filter
    assert "上海" not in first_page
    assert crawler._api_scope_key(
        endpoint, object(), post_data_parsed=False, unparsed_marker="response-a"
    ) != crawler._api_scope_key(
        endpoint, object(), post_data_parsed=False, unparsed_marker="response-b"
    )


def test_native_disabled_and_page_change_helpers_are_conservative() -> None:
    class DisabledLocator:
        def __init__(self, **attrs):
            self.attrs = attrs

        def is_disabled(self):
            return False

        def get_attribute(self, name):
            return self.attrs.get(name)

    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")

    assert crawler._next_is_disabled(DisabledLocator(disabled="")) is True
    assert crawler._next_is_disabled(DisabledLocator(**{"aria-disabled": "true"})) is True
    assert crawler._next_is_disabled(DisabledLocator(**{"aria-disabled": "false"})) is False
    first = crawler._page_snapshot(
        '<span class="active">1</span><h3 class="title">算法工程师</h3>',
        "h3.title",
        "https://jobs.example.test/campus?page=1",
    )
    second = crawler._page_snapshot(
        '<span class="active">2</span><h3 class="title">软件开发工程师</h3>',
        "h3.title",
        "https://jobs.example.test/campus?page=2",
    )
    assert crawler._snapshot_changed(first, second) is True
    same_rows_new_page = (2, first[1], "https://jobs.example.test/campus?page=2")
    assert crawler._snapshot_changed(first, same_rows_new_page) is False


def test_next_control_skips_hidden_clone_and_requires_pagination_owner() -> None:
    class Owner:
        def __init__(self, count):
            self._count = count

        def count(self):
            return self._count

    class Candidate:
        def __init__(self, visible, owner_count):
            self.visible = visible
            self.owner_count = owner_count

        def is_visible(self):
            return self.visible

        def locator(self, _selector):
            return Owner(self.owner_count)

    class Collection:
        def __init__(self):
            self.items = [Candidate(False, 1), Candidate(True, 1)]

        def count(self):
            return len(self.items)

        def nth(self, index):
            return self.items[index]

    class Page:
        def locator(self, _selector):
            return Collection()

    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    assert crawler._find_next_control(Page()).is_visible() is True


def test_click_detail_is_separate_from_anchor_observation_and_is_bounded() -> None:
    class Locator:
        def __init__(self, page):
            self.page = page

        def nth(self, _index):
            return self

        def count(self):
            return 1

        def is_visible(self):
            return True

        def click(self, **_kwargs):
            self.page.url = "https://jobs.example.test/campus#/positionDetail?positionId=87037"

    class Page:
        def __init__(self, source_url):
            self.url = source_url

        def locator(self, _selector):
            return Locator(self)

        def go_back(self, **_kwargs):
            self.url = "https://jobs.example.test/campus"

        def wait_for_timeout(self, _timeout):
            pass

    crawler = GenericRenderCrawler("拓邦", "https://jobs.example.test/campus")
    page = Page(crawler.careers_url)
    html = '<div class="position-card" pid="87037"><h4 class="title">软件工程师</h4><button>立即申请</button></div>'
    detail_links = crawler._observe_click_detail_links(page, html, "h4.title", page.url)
    jobs: list[dict] = []
    crawler._parse(
        html,
        "h4.title",
        jobs,
        set(),
        source_url=page.url,
        detail_links=detail_links,
    )

    assert detail_links["软件工程师"].endswith("positionId=87037")
    assert jobs[0]["jd_url"] == detail_links["软件工程师"]
    assert jobs[0]["detail_link_click_observed"] is True
    assert "detail_link_observed" not in jobs[0]
    assert "detail_link_source_url" not in jobs[0]
    assert crawler._click_details_attempted == 1


def test_access_block_requires_real_form_and_diagnostics_are_bounded_and_redacted() -> None:
    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")

    assert crawler._classify_access_block('<button>登录</button>', "https://jobs.example.test/campus") == ""
    assert crawler._classify_access_block(
        '<form><input name="mobile"><input name="code" placeholder="短信验证码">'
        '<button>登录</button></form>',
        "https://jobs.example.test/login",
    ) == "login_required"
    assert crawler._classify_access_block(
        '<div class="captcha-widget">滑动验证</div>',
        "https://jobs.example.test/campus",
    ) == "captcha_required"

    crawler._record_pagination_diagnostic(
        page=1,
        count=3,
        total=3,
        reason="page_observed",
        changed=True,
        source_url="https://jobs.example.test/campus?token=secret&page=1",
    )
    assert len(crawler.pagination_diagnostics) == 1
    assert "secret" not in repr(crawler.pagination_diagnostics)
    assert "https://" not in repr(crawler.pagination_diagnostics)


def test_fetch_requires_stable_content_change_and_keeps_no_next_without_total_unknown(monkeypatch) -> None:
    def page_html(job_ids, page_number, *, with_next=True, disabled=False):
        cards = "".join(
            f'<article class="job-card"><a href="/job/{job_id}"><h3 class="title">算法工程师 {job_id}</h3></a></article>'
            for job_id in job_ids
        )
        pager = ""
        if with_next:
            state = " disabled" if disabled else ""
            pager = f'<div class="pagination"><span class="active">{page_number}</span><button class="btn-next"{state}>下一页</button></div>'
        return f"<section>{cards}</section>{pager}"

    class EmptyLocator:
        def count(self):
            return 0

        @property
        def first(self):
            return self

    class NextControl:
        def __init__(self, page):
            self.page = page

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

        def count(self):
            return 1

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
        def __init__(self, pages):
            self.pages = pages
            self.index = 0
            self.url = ""

        def on(self, _event, _callback):
            pass

        def goto(self, url, **_kwargs):
            self.url = url + "?page=1"

        def wait_for_timeout(self, _timeout):
            pass

        def content(self):
            return self.pages[self.index]

        def evaluate(self, _script):
            return 100

        def locator(self, selector):
            if (
                ("pagination" in selector or "ant-pagination" in selector or "btn-next" in selector)
                and "pagination" in self.content()
            ):
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

    first = page_html([1, 2, 3], 1)
    second = page_html([4], 2, disabled=True)
    page = Page([first, second])
    browser = Browser(page)
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright",
        lambda: nullcontext(object()),
    )
    monkeypatch.setattr(
        "packages.recruitment_core.crawlers.generic_render.launch_browser",
        lambda *_args, **_kwargs: browser,
    )
    monkeypatch.setattr(GenericRenderCrawler, "_settle_lazy_list", lambda *_args: None)

    crawler = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    jobs = crawler.fetch()

    assert len(jobs) == 4
    assert crawler.pages_seen == 2
    assert crawler.pagination_complete is True
    assert crawler.pagination_termination_reason == "next_disabled"

    no_next_page = Page([page_html([1, 2, 3], 1, with_next=False)])
    no_next_browser = Browser(no_next_page)
    monkeypatch.setattr(
        "packages.recruitment_core.crawlers.generic_render.launch_browser",
        lambda *_args, **_kwargs: no_next_browser,
    )
    unknown = GenericRenderCrawler("示例", "https://jobs.example.test/campus")
    unknown_jobs = unknown.fetch()

    assert len(unknown_jobs) == 3
    assert unknown.pagination_complete is None
    assert unknown.has_more is False
    assert unknown.pagination_termination_reason == "completeness_unknown"
