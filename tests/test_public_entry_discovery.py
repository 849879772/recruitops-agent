from __future__ import annotations

import base64

from packages.discovery.public_entries import (
    BingHtmlSearchProvider,
    DefaultPublicSearchProvider,
    EntryIdentityEvidence,
    JinaBaiduSearchProvider,
    PublicSearchError,
    PublicSearchHit,
    build_company_queries,
    discover_company_entry_candidates,
    normalize_search_result_url,
    observe_public_entry_identity,
    rank_entry_candidate,
)
from packages.tools.public_entry_discovery import (
    PublicEntryDiscoveryInput,
    PublicEntryValidationInput,
    discover_public_recruitment_entries,
    validate_public_recruitment_entry,
)
from packages.tools.oc_candidates import OcCandidateCrawlItem
from packages.tools.typed import ToolErrorCode, ToolStatus


def _bing_redirect(url: str) -> str:
    value = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?u=a1{value}&x=1"


def test_normalizes_bing_redirect_and_rejects_search_host() -> None:
    target = "https://example.jobs.feishu.cn/1234/?spread=abc#fragment"

    assert normalize_search_result_url(_bing_redirect(target)) == target.split("#")[0]
    assert normalize_search_result_url("https://www.bing.com/search?q=test") is None


def test_rank_requires_company_and_recruitment_evidence() -> None:
    valid = PublicSearchHit(
        provider="fixture",
        query="query",
        url="https://example.jobs.feishu.cn/1234/",
        title="中际旭创 2027 校园招聘官网",
        snippet="应届毕业生岗位",
    )
    unrelated = PublicSearchHit(
        provider="fixture",
        query="query",
        url="https://example.jobs.feishu.cn/1234/",
        title="其他公司校园招聘",
        snippet="2027 届岗位",
    )

    ranked = rank_entry_candidate("中际旭创", valid)

    assert ranked is not None
    assert ranked.crawler_key == "feishu"
    assert ranked.score == 95
    assert rank_entry_candidate("中际旭创", unrelated) is None


def test_query_uses_company_stem_for_search_coverage() -> None:
    assert build_company_queries("安世博集团") == (
        "安世博集团 校园招聘 官网",
        "安世博 2027 校园招聘",
    )
    assert build_company_queries("楚能汽车技术研发中心")[1] == "楚能汽车 2027 校园招聘"


def test_rank_rejects_third_party_university_job_pages() -> None:
    school_page = PublicSearchHit(
        provider="fixture",
        query="超星集团 校园招聘 官网",
        url="https://job.sicau.edu.cn/employment/zwss/zwss_info/ind",
        title="超星集团 2024届春季校园招聘",
        snippet="四川农业大学就业信息网招聘岗位",
    )
    official_page = PublicSearchHit(
        provider="fixture",
        query="超星集团 校园招聘 官网",
        url="https://careers.example.com/campus/jobs",
        title="超星集团校园招聘官网",
        snippet="2027届校园招聘岗位",
    )

    assert rank_entry_candidate("超星集团", school_page) is None
    assert rank_entry_candidate("超星集团", official_page) is not None


def test_aggregator_and_form_results_are_never_candidates() -> None:
    aggregator = PublicSearchHit(
        provider="fixture", query="q", url="https://www.nowcoder.com/jobs/1",
        title="中际旭创 2027 校招", snippet="招聘岗位",
    )
    form = PublicSearchHit(
        provider="fixture", query="q", url="https://www.wenjuan.com/s/abc/",
        title="中际旭创 2027 校招", snippet="招聘岗位",
    )

    assert rank_entry_candidate("中际旭创", aggregator) is None
    assert rank_entry_candidate("中际旭创", form) is None


class _Provider:
    name = "fixture"

    def __init__(self, hits: list[PublicSearchHit] | None = None, *, fail: bool = False):
        self.hits = hits or []
        self.fail = fail
        self.queries: list[str] = []

    def search(self, query: str, timeout_seconds: float) -> list[PublicSearchHit]:
        self.queries.append(query)
        if self.fail:
            raise PublicSearchError("offline")
        return [{**hit.__dict__, "query": query} for hit in []] or [
            PublicSearchHit(
                provider=hit.provider,
                query=query,
                url=hit.url,
                title=hit.title,
                snippet=hit.snippet,
            )
            for hit in self.hits
        ]


def test_discovery_is_query_and_candidate_bounded() -> None:
    hits = [PublicSearchHit(
        provider="fixture", query="", url=f"https://brand.example.com/campus/jobs/{index}",
        title=f"测试公司 2027 校园招聘官网 {index}", snippet="应届岗位",
    ) for index in range(8)]
    provider = _Provider(hits)

    queries, candidates = discover_company_entry_candidates(
        "测试公司", provider=provider, max_queries=2, max_candidates=3,
    )

    assert len(queries) == len(provider.queries) == 2
    assert len(candidates) == 3
    assert all(item.hit.query in queries for item in candidates)


def test_typed_tool_isolates_search_failure_by_company() -> None:
    response = discover_public_recruitment_entries(
        PublicEntryDiscoveryInput(company_names=["甲公司", "乙公司"], timeout_ms=1_000),
        provider=_Provider(fail=True),
    )

    assert response.status == ToolStatus.FAILURE
    assert response.error_code == ToolErrorCode.SOURCE_UNAVAILABLE
    assert response.data is not None
    assert response.data.failed_count == 2
    assert all(item.status == "search_failed" for item in response.data.companies)


class _Response:
    text = """
      <li class='b_algo'><h2><a href='https://brand.example.com/campus/jobs'>
      测试公司 2027 校园招聘官网</a></h2><div class='b_caption'><p>应届岗位</p></div></li>
    """

    def raise_for_status(self) -> None:
        return None


class _Session:
    def get(self, *_args, **_kwargs):
        return _Response()


def test_bing_html_parser_returns_provenance() -> None:
    hits = BingHtmlSearchProvider(session=_Session()).search("测试公司", 1)

    assert len(hits) == 1
    assert hits[0].provider == "bing_html"
    assert hits[0].query == "测试公司"
    assert hits[0].url == "https://brand.example.com/campus/jobs"


class _JinaResponse:
    text = """
      中际旭创 2027届校园招聘正式启动
      更多校招QA，详情可至中际旭创校园招聘官网(https://zj-innolight.jobs.feishu.cn/300308)
      [image](https://t8.baidu.com/result.jpg)
    """

    def raise_for_status(self) -> None:
        return None


def test_jina_baidu_extracts_literal_external_url_only() -> None:
    hits = JinaBaiduSearchProvider(session=_Session()).search("中际旭创", 1)

    assert [item.url for item in hits] == ["https://brand.example.com/campus/jobs"]


class _JinaSession:
    def get(self, *_args, **_kwargs):
        return _JinaResponse()


def test_jina_baidu_extracts_official_url_and_filters_image() -> None:
    hits = JinaBaiduSearchProvider(session=_JinaSession()).search("中际旭创", 1)

    assert [item.url for item in hits] == ["https://zj-innolight.jobs.feishu.cn/300308"]
    assert "2027届校园招聘" in hits[0].snippet


class _FailingProvider:
    name = "failed"

    def search(self, _query: str, _timeout_seconds: float):
        raise PublicSearchError("failed")


def test_default_provider_falls_back_after_source_failure() -> None:
    fallback = _Provider([PublicSearchHit(
        provider="fallback", query="", url="https://brand.example.com/campus/jobs",
        title="测试公司校园招聘", snippet="2027届岗位",
    )])
    provider = DefaultPublicSearchProvider((_FailingProvider(), fallback))

    hits = provider.search("测试公司", 10)

    assert len(hits) == 1
    assert hits[0].provider == "fallback"


def test_default_provider_reserves_timeout_for_fallback() -> None:
    budgets = []

    class EmptyProvider:
        name = "empty"

        def search(self, _query: str, timeout_seconds: float):
            budgets.append(timeout_seconds)
            return []

    fallback = _Provider([PublicSearchHit(
        provider="fallback", query="", url="https://brand.example.com/campus/jobs",
        title="测试公司校园招聘", snippet="2027届岗位",
    )])

    hits = DefaultPublicSearchProvider((EmptyProvider(), fallback)).search("测试公司", 10)

    assert 4.9 <= budgets[0] <= 5.0
    assert len(hits) == 1


def test_discovery_continues_after_school_only_source_for_official_candidate() -> None:
    school = PublicSearchHit(
        provider="school-source",
        query="",
        url="https://job.sicau.edu.cn/employment/zwss/zwss_info/ind",
        title="超星集团 2024届春季校园招聘",
        snippet="四川农业大学就业信息网招聘岗位",
    )
    official = PublicSearchHit(
        provider="official-source",
        query="",
        url="https://careers.example.com/campus/jobs",
        title="超星集团校园招聘官网",
        snippet="2027届校园招聘岗位",
    )
    first = _Provider([school])
    second = _Provider([official])

    queries, candidates = discover_company_entry_candidates(
        "超星集团",
        provider=DefaultPublicSearchProvider((first, second)),
        max_queries=1,
    )

    assert len(queries) == 1
    assert first.queries == second.queries == queries
    assert [candidate.hit.url for candidate in candidates] == [
        "https://careers.example.com/campus/jobs",
    ]


def test_identity_observation_requires_company_and_recruitment_on_page() -> None:
    valid = observe_public_entry_identity(
        "中际旭创",
        "https://zj-innolight.jobs.feishu.cn/300308",
        render=lambda *_args, **_kwargs: "<title>中际旭创</title><main>2027校园招聘 职位列表</main>",
    )
    wrong_company = observe_public_entry_identity(
        "中际旭创",
        "https://other.example.com/campus/jobs",
        render=lambda *_args, **_kwargs: "<title>其他公司</title><main>2027校园招聘</main>",
    )

    assert valid.valid is True
    assert valid.company_evidence == "中际旭创"
    assert wrong_company.valid is False
    assert wrong_company.reason == "company_identity_not_observed"


def test_identity_observation_rejects_third_party_school_host_before_fetch() -> None:
    rendered = False

    def render(*_args, **_kwargs):
        nonlocal rendered
        rendered = True
        return "<main>超星集团 2027校园招聘</main>"

    result = observe_public_entry_identity(
        "超星集团",
        "https://job.sicau.edu.cn/employment/zwss/zwss_info/ind",
        render=render,
    )

    assert result.valid is False
    assert result.reason == "third_party_school_host"
    assert rendered is False


class _ValidationRunner:
    def __init__(self, crawl: OcCandidateCrawlItem):
        self.crawl = crawl
        self.calls = []

    def crawl_public_candidate(self, company_name, source_url, request, *, hydrate_details=False):
        self.calls.append((company_name, source_url, request, hydrate_details))
        return self.crawl


class _BrokenValidationRunner:
    def crawl_public_candidate(self, *_args, **_kwargs):
        raise ValueError("snapshot unavailable")


def test_validation_rejects_page_identity_before_crawl() -> None:
    runner = _ValidationRunner(OcCandidateCrawlItem(company="中际旭创", status="failed"))

    response = validate_public_recruitment_entry(
        PublicEntryValidationInput(
            company_name="中际旭创",
            candidate_url="https://other.example.com/campus/jobs",
        ),
        runner,
        identity_observer=lambda *_args, **_kwargs: EntryIdentityEvidence(
            False,
            "https://other.example.com/campus/jobs",
            reason="company_identity_not_observed",
        ),
    )

    assert response.status == ToolStatus.FAILURE
    assert response.error_code == ToolErrorCode.INVALID_SOURCE
    assert runner.calls == []


def test_validation_returns_partial_crawl_as_incomplete_without_writing() -> None:
    crawl = OcCandidateCrawlItem(
        company="中际旭创",
        status="failed",
        source_url="https://zj-innolight.jobs.feishu.cn/300308",
        integration_status="connected_partial",
        raw_job_count=8,
        accepted_count=8,
        pagination_complete=False,
    )
    runner = _ValidationRunner(crawl)

    response = validate_public_recruitment_entry(
        PublicEntryValidationInput(
            company_name="中际旭创",
            candidate_url="https://zj-innolight.jobs.feishu.cn/300308",
        ),
        runner,
        identity_observer=lambda *_args, **_kwargs: EntryIdentityEvidence(
            True,
            "https://zj-innolight.jobs.feishu.cn/300308",
            company_evidence="中际旭创",
            recruitment_evidence="2027校园招聘",
            reason="page_identity_verified",
        ),
    )

    assert response.status == ToolStatus.FAILURE
    assert response.error_code == ToolErrorCode.PAGINATION_INCOMPLETE
    assert response.data is not None
    assert response.data.crawl is not None
    assert response.data.crawl.integration_status == "connected_partial"
    assert response.read_only is True


def test_validation_converts_snapshot_failure_to_typed_response() -> None:
    response = validate_public_recruitment_entry(
        PublicEntryValidationInput(
            company_name="中际旭创",
            candidate_url="https://zj-innolight.jobs.feishu.cn/300308",
        ),
        _BrokenValidationRunner(),
        identity_observer=lambda *_args, **_kwargs: EntryIdentityEvidence(
            True,
            "https://zj-innolight.jobs.feishu.cn/300308",
            company_evidence="中际旭创",
            recruitment_evidence="2027校园招聘",
        ),
    )

    assert response.status == ToolStatus.FAILURE
    assert response.error_code == ToolErrorCode.SOURCE_UNAVAILABLE
    assert response.error_message == "snapshot unavailable"
