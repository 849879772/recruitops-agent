from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from packages.recruitment_core import job_details
from packages.recruitment_core.crawlers.tonghuashun import TonghuashunCampusCrawler
from packages.recruitment_core.entry import diagnose_candidate_entry


FULL_JD = (
    "岗位职责\n"
    + "负责招聘系统的接口开发、测试、部署和持续优化，参与跨团队协作与问题定位。" * 4
    + "\n任职要求\n"
    + "熟悉 Python、Linux、数据库和常用数据结构，具备良好的工程实践能力。" * 4
)


class Response:
    def __init__(self, *, text: str = "", payload: object = None, url: str = ""):
        self.text = text
        self._payload = payload
        self.url = url
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload


def _success(data: object) -> dict:
    return {"success": True, "erro_code": "0", "ex_data": data}


def _captured_page(body: str, *, method: str) -> str:
    return (
        '<html data-recruitops-capture-status="complete" '
        f'data-recruitops-capture-method="{method}" '
        'data-recruitops-terminal-observed="true" '
        'data-recruitops-remaining-controls="[]">'
        f"{body}</html>"
    )


def _tong_row(job_id: int, title: str, *, sparse: bool = False, series: str = "2027届校园招聘") -> dict:
    return {
        "id": job_id,
        "name": title,
        "base": "杭州",
        "intro": "负责系统开发与测试。" if sparse else "负责系统开发、测试和持续优化。" * 8,
        "requirement": "熟悉 Python。" if sparse else "熟悉 Python、Linux、数据库和工程实践。" * 8,
        "apply_recruitment_series_name": series,
    }


@pytest.fixture(autouse=True)
def offline_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(*_args, **_kwargs):
        raise AssertionError("route repair tests must not make live requests")

    monkeypatch.setattr(job_details.requests, "get", unexpected)
    monkeypatch.setattr(job_details.requests, "post", unexpected)
    monkeypatch.setattr(job_details, "render_page", unexpected)


def test_mobile_tonghuashun_without_sid_is_unfiltered_and_keeps_series(monkeypatch) -> None:
    url = "https://campus.10jqka.com.cn/mobile/job/list"
    calls = []

    def fake_get(request_url, *, params, **_kwargs):
        calls.append((request_url, dict(params)))
        assert request_url.endswith("/apply_list")
        return Response(payload=_success({
            "apply_show_do_list": [
                _tong_row(2160, "算法工程师", series="AIME计划"),
                _tong_row(2161, "实习开发工程师", series="日常实习"),
            ],
            "total": 2,
            "pages": 1,
            "size": 50,
        }))

    monkeypatch.setattr(
        "packages.recruitment_core.crawlers.tonghuashun.requests.get", fake_get,
    )
    crawler = TonghuashunCampusCrawler("同花顺-AIME计划", url)

    jobs = crawler.fetch()

    assert crawler.supports(url)
    assert len(calls) == 1
    assert calls[0][1]["applyRecruitmentSeriesIds"] == ""
    assert [job["source_job_id"] for job in jobs] == ["2160", "2161"]
    assert [job["recruitment_series"] for job in jobs] == ["AIME计划", "日常实习"]
    assert all(urlsplit(job["jd_url"]).path == "/mobile/job/detail" for job in jobs)


def test_tonghuashun_mobile_sparse_row_uses_observed_detail_contract(monkeypatch) -> None:
    url = "https://campus.10jqka.com.cn/mobile/job/list"
    calls = []

    def fake_get(request_url, *, params, **_kwargs):
        calls.append((request_url, dict(params)))
        if request_url.endswith("/apply_detail"):
            assert params == {"id": "2160"}
            return Response(payload=_success(_tong_row(2160, "算法工程师")))
        return Response(payload=_success({
            "apply_show_do_list": [_tong_row(2160, "算法工程师", sparse=True)],
            "total": 1,
            "pages": 1,
            "size": 50,
        }))

    monkeypatch.setattr(
        "packages.recruitment_core.crawlers.tonghuashun.requests.get", fake_get,
    )
    jobs = TonghuashunCampusCrawler("同花顺", url).fetch()

    assert [request_url.rsplit("/", 1)[-1] for request_url, _ in calls] == [
        "apply_list", "apply_detail",
    ]
    assert len(jobs[0]["jd_raw"]) > 300
    assert jobs[0]["recruitment_series"] == "2027届校园招聘"


@pytest.mark.parametrize("url", [
    "https://campus.10jqka.com.cn/job/list?type=school&sid=1",
    "https://campus.10jqka.com.cn/mobile/job/list",
])
def test_entry_diagnosis_routes_both_tonghuashun_list_variants(url) -> None:
    diagnosis = diagnose_candidate_entry(url)
    assert (diagnosis.entry_kind, diagnosis.crawler_key) == ("existing_adapter", "tonghuashun")


def test_moka_hash_direct_fallback_reads_hash_job_route(monkeypatch) -> None:
    captured_detail = _captured_page(
        f'<main><h1>算法工程师</h1>{FULL_JD}</main>',
        method="detail_dom",
    )
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(text=captured_detail),
    )
    url = "https://app.mokahr.com/#/job/78086983-48dd-4914-bbc1-90302918825b"

    detail, status = job_details.fetch_moka_job_description_status(
        url,
        title="算法工程师",
    )

    assert status == "complete"
    assert detail == FULL_JD


def test_custom_moka_api_requires_same_host_list_provenance(monkeypatch) -> None:
    job_details._moka_site_context.cache_clear()
    calls = []
    page = (
        '<input id="init-data" value="{&quot;orgId&quot;:&quot;bigo&quot;,'
        '&quot;siteId&quot;:1018,&quot;aesIv&quot;:&quot;fedcba9876543210&quot;}">'
    )

    def fake_get(url, **_kwargs):
        calls.append(("get", url))
        return Response(text=page)

    def fake_post(url, **_kwargs):
        calls.append(("post", url))
        return Response(payload={
            "code": 0,
            "data": {"id": "78086983-48dd-4914-bbc1-90302918825b", "title": "算法工程师", "jobDescription": FULL_JD},
        })

    monkeypatch.setattr(job_details.requests, "get", fake_get)
    monkeypatch.setattr(job_details.requests, "post", fake_post)
    job = {
        "title": "算法工程师",
        "jd_raw": "",
        "jd_url": "https://campus.bigo.sg/campus_apply/bigo/1018/#/job/78086983-48dd-4914-bbc1-90302918825b",
        "source_list_url": "https://campus.bigo.sg/campus_apply/bigo/1018/#/jobs",
    }

    result = job_details.fetch_full_job_description_result(job)

    assert result.complete
    assert result.source == "moka_provenance"
    assert result.identity_status == "matched"
    assert "native_id:78086983-48dd-4914-bbc1-90302918825b" in result.identity_evidence
    assert "title:算法工程师" in result.identity_evidence
    assert calls[0][0] == "get"
    assert calls[1][0] == "post"


def test_list_shell_uses_one_bounded_render_fallback_and_scopes_the_card(monkeypatch) -> None:
    source = "https://www.wkai.cc/hr/zp-campus.html"
    job = {
        "company": "万凯新材",
        "title": "信息技术岗",
        "jd_raw": "",
        "jd_url": source,
        "link_kind": "list",
        "native_job_id": "wk-1",
        "source_url": source,
    }
    render_calls = []
    rendered = (
        '<main class="jobs">'
        '<article data-job-id="other"><h2>财务岗</h2><p>岗位职责：错误岗位内容。</p></article>'
        f'<article data-job-id="wk-1"><h2>信息技术岗</h2><div>{FULL_JD}</div></article>'
        "</main>"
    )
    rendered = _captured_page(rendered, method="list_card")
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(text="<main><div>loading</div></main>"),
    )

    def render(url, **kwargs):
        render_calls.append((url, kwargs))
        return rendered

    monkeypatch.setattr(job_details, "render_page", render)

    result = job_details.fetch_full_job_description_result(job)

    assert result.complete
    assert result.source == "configured_page_render"
    assert "错误岗位内容" not in result.detail
    assert render_calls[0][1]["timeout_ms"] == 30000
    assert result.attempts == (
        "configured_page:list_url",
        "configured_page_render:complete",
    )


def test_list_identity_conflict_stops_before_render_fallback(monkeypatch) -> None:
    source = "https://www.bluetrum.com/job/job.php?class2=77"
    job = {
        "company": "中科蓝讯",
        "title": "软件工程师",
        "jd_raw": "",
        "jd_url": source,
        "link_kind": "list",
        "native_job_id": "expected",
        "source_url": source,
    }
    render_called = []
    conflicting = (
        '<article data-job-id="other"><h2>软件工程师</h2>'
        f"<div>{FULL_JD}</div></article>"
    )
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: Response(text=conflicting),
    )
    monkeypatch.setattr(
        job_details,
        "render_page",
        lambda *_args, **_kwargs: render_called.append(True),
    )

    result = job_details.fetch_full_job_description_result(job)

    assert result.status == "identity_mismatch"
    assert result.detail == ""
    assert render_called == []


@pytest.mark.parametrize(
    ("html", "status"),
    [
        ("<form><input type='password'></form>", "login_required"),
        ("<iframe src='/captcha/challenge'></iframe>", "captcha_required"),
    ],
)
def test_render_access_controls_stop_list_hydration(monkeypatch, html, status) -> None:
    source = "https://www.wkai.cc/hr/zp-campus.html"
    job = {
        "title": "信息技术岗",
        "jd_raw": "",
        "jd_url": source,
        "link_kind": "list",
        "source_url": source,
    }
    monkeypatch.setattr(
        job_details.requests,
        "get",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("fixture")),
    )
    monkeypatch.setattr(job_details, "render_page", lambda *_args, **_kwargs: html)

    result = job_details.fetch_full_job_description_result(job)

    assert result.status == status
    assert result.attempts == (
        "configured_page:fetch_failed",
        f"configured_page_render:{status}",
    )


def test_generic_detail_reserves_time_for_parsing_and_cleanup(monkeypatch) -> None:
    calls = []

    def render(url, **kwargs):
        calls.append((url, kwargs))
        return f"<main><h1>软件工程师</h1><div>{FULL_JD}</div></main>"

    monkeypatch.setattr(job_details, "render_page", render)
    result = job_details.fetch_full_job_description_result({
        "title": "软件工程师", "jd_raw": "",
        "jd_url": "https://careers.example.com/jobs/42",
    })

    assert result.complete
    assert result.identity_status == "matched"
    assert len(calls) == 1
    assert calls[0][1] == {
        "timeout_ms": 30000, "extra_wait_ms": 1500, "wait_until": "domcontentloaded",
    }


@pytest.mark.parametrize("ready_state", ["networkidle", "domcontentloaded"])
def test_renderer_respects_navigation_ready_state(monkeypatch, ready_state) -> None:
    from types import SimpleNamespace
    from packages.recruitment_core.crawlers import render as renderer

    calls = []
    page = SimpleNamespace(
        route=lambda *args: None,
        goto=lambda url, **kwargs: calls.append((url, kwargs)),
        content=lambda: "<main>ready</main>",
    )
    context = SimpleNamespace(
        add_init_script=lambda *args: None,
        new_page=lambda: page,
        close=lambda: None,
    )
    browser = SimpleNamespace(new_context=lambda **kwargs: context, close=lambda: None)

    class PlaywrightContext:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("playwright.sync_api.sync_playwright", PlaywrightContext)
    monkeypatch.setattr(renderer, "launch_browser", lambda *args, **kwargs: browser)
    options = {} if ready_state == "networkidle" else {"wait_until": ready_state}

    assert renderer.render_page("https://example.com/jobs/42", **options) == "<main>ready</main>"
    assert calls[0][1] == {"wait_until": ready_state, "timeout": 30000}


def test_renderer_selector_uses_dom_ready_unless_networkidle_is_explicit(monkeypatch) -> None:
    from types import SimpleNamespace
    from packages.recruitment_core.crawlers import render as renderer

    calls = []
    page = SimpleNamespace(
        route=lambda *args: None,
        goto=lambda url, **kwargs: calls.append(kwargs),
        wait_for_selector=lambda selector, **kwargs: calls.append((selector, kwargs)),
        content=lambda: "<main><a class='job'>职位</a></main>",
    )
    context = SimpleNamespace(add_init_script=lambda *args: None, new_page=lambda: page, close=lambda: None)
    browser = SimpleNamespace(new_context=lambda **kwargs: context, close=lambda: None)

    class PlaywrightContext:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return None

    monkeypatch.setattr("playwright.sync_api.sync_playwright", PlaywrightContext)
    monkeypatch.setattr(renderer, "launch_browser", lambda *args, **kwargs: browser)

    assert renderer.render_page("https://example.com/jobs", wait_for=".job")
    assert calls[0]["wait_until"] == "domcontentloaded"
    assert calls[1][0] == ".job"
    calls.clear()
    assert renderer.render_page("https://example.com/jobs", wait_for=".job", wait_until="networkidle")
    assert calls[0]["wait_until"] == "networkidle"
