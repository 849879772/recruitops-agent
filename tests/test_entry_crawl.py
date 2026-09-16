from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from packages.recruitment_core import entry_crawl, job_cohorts
from packages.recruitment_core.entry import diagnose_candidate_entry
from packages.recruitment_core.models import CompanyConfig
from packages.tools.oc_candidates import diagnose_candidate_entry as oc_diagnose


ROOT = "https://www.entry-example.test/"
FEISHU = "https://entry-example.jobs.feishu.cn/campus/position"


@pytest.mark.parametrize("url", ["https://hr.tp-link.com.cn", "https://hr.tp-link.com.cn/"])
def test_tplink_home_routes_to_registered_adapter(url):
    from packages.recruitment_core import CRAWLER_MAP
    from packages.recruitment_core.crawlers.tplink import TPLinkCrawler

    assert diagnose_candidate_entry(url).crawler_key == "tplink"
    assert CRAWLER_MAP["tplink"] is TPLinkCrawler


@pytest.mark.parametrize("url", [
    "https://join.tplinkglobal.com/campus/jobs", "https://hr.tp-link.com.cn/jobDetail/7517",
    "https://hr.tp-link.com.cn/socialJobList", "https://hr.tp-link.com.cn/?page=1",
    "https://hr.tp-link.com.cn.evil.invalid/", "http://hr.tp-link.com.cn/",
])
def test_tplink_adapter_does_not_capture_other_company_or_scope(url):
    assert diagnose_candidate_entry(url).crawler_key != "tplink"


def evidence(jobs=(), **overrides):
    return {
        "jobs": list(jobs), "pagination_complete": False, "completeness_known": False,
        "pages_seen": 0, "total_pages": None, "has_more": False,
        "advertised_total": None, "termination_reasons": ["adapter_did_not_report_completeness"],
        "source_runs": [], **overrides,
    }


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("Unexpected live network or browser call")

    monkeypatch.setattr(entry_crawl, "render_page", forbidden)
    monkeypatch.setattr(entry_crawl.requests, "get", forbidden)
    monkeypatch.setattr(job_cohorts, "inspect_official_campaign", lambda *_: job_cohorts.unknown_cohort())


def test_core_root_routes_to_feishu_and_retains_runner_evidence(monkeypatch):
    calls = []

    class RootCrawler:
        def __init__(self, name, url):
            calls.append(("render", name, url))

        def fetch(self):
            return []

    class FeishuCrawler:
        pagination_complete = False
        pages_seen = 1
        total_pages = 3
        advertised_total = 9
        has_more = True
        pagination_termination_reason = "next_navigation_failed"
        resolved_source_url = FEISHU + "?project=2027"

        def __init__(self, name, url):
            calls.append(("feishu", name, url))

        def fetch(self):
            return [{"title": "Software Engineer", "jd_url": FEISHU + "/1"}]

    rendered = []

    def render(url, **kwargs):
        rendered.append((url, kwargs))
        return f'<a href="{FEISHU}">Campus jobs</a>'

    monkeypatch.setattr(entry_crawl, "render_page", render)
    company = {
        "name": "Example", "careers_url": ROOT, "crawler": "render",
        "source_cohort": 2027, "source_cohort_source": job_cohorts.OC_TRUSTED_SOURCE,
        "source_cohort_evidence": "OC 2027 filtered record", "source_cohort_url": ROOT,
    }
    before = deepcopy(company)
    result = entry_crawl.crawl_company_with_entry_discovery(
        company, crawler_map={"render": RootCrawler, "feishu": FeishuCrawler},
    )
    assert company == before
    assert calls == [("render", "Example", ROOT), ("feishu", "Example", FEISHU)]
    assert rendered[0][0] == ROOT
    assert result["source_url"] == ROOT
    assert result["crawl_source_url"] == result["discovered_entry_url"] == FEISHU
    assert result["effective_source_urls"] == [ROOT, FEISHU, FeishuCrawler.resolved_source_url]
    assert result["crawler_key"] == "feishu"
    assert result["raw_job_count"] == 1
    assert result["jobs"][0]["cohort"] == 2027
    assert result["jobs"][0]["cohort_status"] == "confirmed"
    assert result["jobs"][0]["campaign_url"] == ROOT
    assert result["pagination_complete"] is False
    assert result["completeness_known"] is True
    assert result["has_more"] is True
    assert result["advertised_total"] == 9
    assert result["total_pages"] == 3
    assert result["failures"] == ["next_navigation_failed"]
    assert result["source_runs"][0]["source_url"] == FEISHU
    assert result["entry_attempts"][0]["source_url"] == ROOT
    assert result["error_code"] is None


@pytest.mark.parametrize("url", [
    ROOT, ROOT + "#/jobs", "https://career.entry-example.test/index.html",
])
def test_root_hash_and_career_host_entries_explore_without_changing_url(url):
    calls = []

    def crawl(source, key, timeout):
        calls.append((source, key, timeout))
        return evidence([{"title": "Engineer"}] if source == FEISHU else [])

    result = entry_crawl.crawl_with_entry_discovery(url, crawl=crawl, discover=lambda *_: [FEISHU])
    assert [call[:2] for call in calls] == [(url, "render"), (FEISHU, "feishu")]
    assert result["source_url"] == url
    assert result["discovered_entry_url"] == FEISHU
    assert oc_diagnose is diagnose_candidate_entry
    assert diagnose_candidate_entry(url).crawler_key == "render"


@pytest.mark.parametrize(("url", "code"), [
    ("https://yunbiz.wps.cn/form/abc", "form_application_only"),
    ("https://kdocs.cn/l/abc", "form_application_only"),
    ("https://docs.qq.com/form/abc", "form_application_only"),
    ("https://office.chaoxing.com/apps/forms/mobile/apply.html?id=1", "form_application_only"),
    ("https://www.givemeoc.com/signed", "invalid_entry"),
    ("http://127.0.0.1/jobs", "invalid_entry"),
    ("http://[invalid", "invalid_entry"),
    ("https://example.test:invalid/jobs", "invalid_entry"),
    ("https://user:password@example.test/jobs", "invalid_entry"),
    ("https://example.test/#/login", "login_required"),
])
def test_forms_invalid_and_login_entries_do_not_crawl(url, code):
    def forbidden(*_args):
        pytest.fail("Invalid or manual entry must not be fetched")

    result = entry_crawl.crawl_with_entry_discovery(url, crawl=forbidden, discover=forbidden)
    assert result["jobs"] == []
    assert result["source_url"] == url
    assert result["discovered_entry_url"] is None
    assert result["effective_source_urls"] == []
    assert result["error_code"] == code


def test_unresolved_root_renders_then_http_without_guessing(monkeypatch):
    calls = []

    def render(url, **kwargs):
        calls.append(("render", url, kwargs))
        return None

    def http_get(url, **kwargs):
        calls.append(("http", url, kwargs))
        return SimpleNamespace(status_code=200, text="<h1>Example company</h1>", raise_for_status=lambda: None)

    monkeypatch.setattr(entry_crawl, "render_page", render)
    monkeypatch.setattr(entry_crawl.requests, "get", http_get)
    result = entry_crawl.crawl_with_entry_discovery(ROOT, crawl=lambda *_: evidence())
    assert [call[:2] for call in calls] == [("render", ROOT), ("http", ROOT)]
    assert result["error_code"] == "recruitment_entry_discovery_required"
    assert result["discovered_entry_url"] is None
    assert result["completeness_known"] is False
    assert len(result["entry_attempts"]) == 1


def test_link_discovery_filters_before_five_entry_limit():
    links = ["/jobs", "/careers", "https://docs.qq.com/form/1", "http://localhost/jobs"]
    platforms = [f"https://acme{i}.jobs.feishu.cn/campus/position" for i in range(8)]
    html = "".join(f'<a href="{url}">Campus jobs</a>' for url in [*links, *platforms, platforms[0]])
    found = entry_crawl.discover_recruitment_entries(ROOT, 20, render=lambda *_a, **_k: html)
    assert found == platforms[:5]


def test_shared_helper_never_attempts_more_than_five_known_links():
    platforms = [f"https://acme{i}.jobs.feishu.cn/campus/position" for i in range(8)]
    calls = []

    def crawl(url, *_):
        calls.append(url)
        return evidence()

    entry_crawl.crawl_with_entry_discovery(
        ROOT, crawl=crawl,
        discover=lambda *_: [ROOT, ROOT + "jobs", "https://docs.qq.com/form/1", platforms[0], *platforms],
    )
    assert calls == [ROOT, *platforms[:5]]


def test_all_attempts_share_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(entry_crawl, "perf_counter", lambda: now[0])
    calls = []

    def crawl(url, key, remaining):
        calls.append((url, key, remaining))
        now[0] += 4
        return evidence()

    def discover(url, remaining):
        assert (url, remaining) == (ROOT, 6)
        now[0] += 2
        return [FEISHU, "https://acme.zhiye.com/campus/jobs"]

    result = entry_crawl.crawl_with_entry_discovery(ROOT, crawl=crawl, discover=discover, timeout_seconds=10)
    assert calls == [(ROOT, "render", 10), (FEISHU, "feishu", 4)]
    assert result["error_code"] == "timeout"
    assert now[0] == 10


def test_company_wrapper_passes_remaining_budget_to_current_attempt(monkeypatch):
    captured = []

    def fake_runner(company, *, crawler_map=None):
        captured.append(dict(company))
        return evidence([{"title": "Engineer"}])

    def fake_entry_wrapper(source_url, *, crawl, crawler_key, timeout_seconds, **_kwargs):
        assert timeout_seconds == 10
        return crawl(source_url, crawler_key, 3.25)

    monkeypatch.setattr(entry_crawl.runner, "crawl_company_with_evidence", fake_runner)
    monkeypatch.setattr(entry_crawl, "crawl_with_entry_discovery", fake_entry_wrapper)

    entry_crawl.crawl_company_with_entry_discovery({
        "name": "Example",
        "careers_url": FEISHU,
        "crawler": "feishu",
        "crawl_timeout_seconds": 10,
    })

    assert captured[0]["crawl_timeout_seconds"] == 3.25


@pytest.mark.parametrize("render_elapsed", [4, 10])
def test_http_fallback_uses_only_render_budget_remainder(monkeypatch, render_elapsed):
    now = [0.0]
    monkeypatch.setattr(entry_crawl, "perf_counter", lambda: now[0])
    http_calls = []

    def render(_url, **kwargs):
        assert kwargs["timeout_ms"] <= 10_000
        now[0] += render_elapsed
        raise TimeoutError("render timeout")

    def http_get(_url, **kwargs):
        http_calls.append(kwargs["timeout"])
        return SimpleNamespace(status_code=200, text=f'<a href="{FEISHU}">Jobs</a>', raise_for_status=lambda: None)

    if render_elapsed == 10:
        with pytest.raises(RuntimeError, match="deadline"):
            entry_crawl.discover_recruitment_entries(ROOT, 10, render=render, http_get=http_get)
        assert http_calls == []
    else:
        assert entry_crawl.discover_recruitment_entries(ROOT, 10, render=render, http_get=http_get) == [FEISHU]
        assert http_calls == [6]


@pytest.mark.parametrize("html", [
    '<form><input type="password"></form>',
    '<title>Security verification</title>',
    '<iframe src="/captcha/challenge"></iframe>',
])
def test_access_controls_stop_discovery_and_http_fallback(monkeypatch, html):
    monkeypatch.setattr(entry_crawl, "render_page", lambda *_a, **_k: html + f'<a href="{FEISHU}">Jobs</a>')
    result = entry_crawl.crawl_with_entry_discovery(ROOT, crawl=lambda *_: evidence())
    assert result["error_code"] in {"login_required", "captcha_required"}
    assert len(result["entry_attempts"]) == 1


@pytest.mark.parametrize("html", [
    '<div hidden><input type="password"></div>',
    '<div style="display: none"><input type="password"></div>',
    '<dialog><input type="password"></dialog>',
    '<input type="password" data-recruitops-visible="false">',
    '<iframe src="/captcha/challenge" data-recruitops-visible="false"></iframe>',
])
def test_hidden_login_and_captcha_widgets_do_not_block_public_entry(html):
    found = entry_crawl.discover_recruitment_entries(
        ROOT, 20, render=lambda *_a, **_k: html + f'<a href="{FEISHU}">Campus jobs</a>',
    )
    assert found == [FEISHU]


def test_http_redirect_to_form_is_not_followed():
    calls = []

    def http_get(url, **_kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=302, headers={"Location": "https://docs.qq.com/form/1"})

    with pytest.raises(RuntimeError, match="form"):
        entry_crawl.discover_recruitment_entries(ROOT, 10, render=lambda *_a, **_k: None, http_get=http_get)
    assert calls == [ROOT]


@pytest.mark.parametrize("partial_source", [ROOT, FEISHU])
def test_partial_jobs_stop_later_empty_candidates(partial_source):
    partial = evidence(
        [{"title": "Engineer"}], pagination_complete=False, completeness_known=True,
        pages_seen=1, total_pages=3, has_more=True, advertised_total=9,
        error_code="next_page_failed", termination_reasons=["next_page_failed"],
        source_runs=[{"source_url": partial_source, "observed_total": 1}],
    )
    calls = []

    def crawl(url, *_):
        calls.append(url)
        return partial if url == partial_source else evidence()

    result = entry_crawl.crawl_with_entry_discovery(
        ROOT, crawl=crawl, discover=lambda *_: [FEISHU, "https://empty.zhiye.com/campus/jobs"],
    )
    for key, value in partial.items():
        assert result[key] == value
    assert calls == ([ROOT] if partial_source == ROOT else [ROOT, FEISHU])
    assert result["source_url"] == ROOT


def test_empty_later_attempt_does_not_erase_partial_pagination_evidence():
    partial = evidence(
        completeness_known=True, pagination_complete=False, pages_seen=1,
        has_more=True, advertised_total=10, termination_reasons=["page_2_failed"],
    )
    result = entry_crawl.crawl_with_entry_discovery(
        ROOT, crawl=lambda url, *_: partial if url == ROOT else evidence(),
        discover=lambda *_: [FEISHU],
    )
    assert result["pages_seen"] == 1
    assert result["advertised_total"] == 10
    assert result["termination_reasons"] == ["page_2_failed"]
    assert len(result["entry_attempts"]) == 2
    assert result["crawl_source_url"] == ROOT


def test_verified_empty_campaign_survives_shared_wrapper_without_failure():
    result = entry_crawl.crawl_with_entry_discovery(
        "https://app.mokahr.com/campus_apply/empty/1", crawler_key="moka",
        crawl=lambda *_: evidence(
            pagination_complete=True, completeness_known=True, advertised_total=0,
            pages_seen=1, has_more=False,
        ),
    )
    assert result["error_code"] is None
    assert result["run_reason"] == "activity_empty"


def test_zero_total_without_completeness_is_not_an_empty_campaign_success():
    result = entry_crawl.crawl_with_entry_discovery(
        "https://app.mokahr.com/campus_apply/empty/1", crawler_key="moka",
        crawl=lambda *_: evidence(advertised_total=0),
    )
    assert result["error_code"] == "adapter_variant_unsupported"
    assert result.get("run_reason") != "activity_empty"


def test_existing_adapter_execution_failure_is_not_mislabeled_as_unsupported():
    result = entry_crawl.crawl_with_entry_discovery(
        "https://jobs.bilibili.com/campus/positions",
        crawler_key="bilibili",
        crawl=lambda *_: evidence(
            completeness_known=True,
            pagination_complete=False,
            termination_reasons=["render_failed"],
            failures=["render_failed"],
            source_runs=[{"fetch_failed": True, "termination_reason": "render_failed"}],
        ),
    )

    assert result["error_code"] == "render_failed"
    assert result.get("run_reason") != "activity_empty"


def test_configured_adapter_and_source_context_are_preserved(monkeypatch):
    company = CompanyConfig.from_legacy({
        "name": "Example", "careers_url": FEISHU, "crawler": "custom_adapter",
        "campaign_urls": [FEISHU + "?project=2027"],
        "source_cohort_url": ROOT, "source_cohort": 2027,
        "source_cohort_source": job_cohorts.OC_TRUSTED_SOURCE,
    })
    calls = []
    registry = {"custom_adapter": object}

    def crawl(config, *, crawler_map):
        calls.append((config, crawler_map))
        return evidence([{"title": "Engineer"}])

    monkeypatch.setattr(entry_crawl.runner, "crawl_company_with_evidence", crawl)
    result = entry_crawl.crawl_company_with_entry_discovery(company, crawler_map=registry)
    assert calls == [(company.to_dict(), registry)]
    assert result["crawler_key"] == "custom_adapter"
    assert result["source_url"] == FEISHU
    assert result["discovered_entry_url"] is None
