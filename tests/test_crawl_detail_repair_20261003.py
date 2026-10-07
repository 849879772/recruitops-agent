from __future__ import annotations

from copy import deepcopy

import pytest
import requests

from packages.recruitment_core import job_details
from packages.recruitment_core.crawlers.generic_render import GenericRenderCrawler
from packages.recruitment_core.crawlers.meituan import MeituanCrawler
from packages.recruitment_core.jd_capture import assess_jd_capture


MEITUAN_LIST = "https://zhaopin.meituan.com/web/campus"
MEITUAN_ID = "4697320043"
MEITUAN_TITLE = "AI全栈工程师"
MEITUAN_DETAIL = f"https://zhaopin.meituan.com/web/position/detail?jobUnionId={MEITUAN_ID}&highlightType=campus"
# Public endpoint field names and ID/title reflect the observed official API;
# the descriptive content is deterministic synthetic text.
ROW = {
    "jobUnionId": MEITUAN_ID,
    "name": MEITUAN_TITLE,
    "jobType": "1",
    "cityList": [{"name": "北京"}, {"name": "上海"}],
    "jobDuty": "1. 参与 AI 应用全栈开发。\n2. 构建 Agent 工作流及 RAG 检索。",
    "jobRequirement": "1. 本科及以上学历。\n2. 熟悉 Python 或 TypeScript，具备完整项目开发经验。",
}


class Response:
    def __init__(self, payload: dict):
        self.payload = payload
        self.status_code = 200
        self.url = ""

    def json(self):
        return deepcopy(self.payload)

    def raise_for_status(self):
        return None


def listing(rows: list[dict], *, page: int = 1, pages: int = 1, total: int | None = None) -> Response:
    return Response({"status": 1, "data": {"list": rows, "page": {"pageNo": page, "totalPage": pages, "totalCount": len(rows) if total is None else total}}})


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Detail regression must not access the network or browser")

    monkeypatch.setattr(job_details.requests, "get", unexpected)
    monkeypatch.setattr(job_details.requests, "post", unexpected)
    monkeypatch.setattr(job_details, "render_page", unexpected)
    monkeypatch.setattr("packages.recruitment_core.crawlers.meituan.time.sleep", lambda *_: None)


def test_legacy_city_placeholder_does_not_create_a_false_location_constraint():
    assert job_details._meituan_city_tokens("—") == set()
    assert job_details._meituan_city_tokens(" ") == set()
    assert job_details._meituan_city_tokens("北京市、上海") == {"北京", "上海"}


def test_render_meituan_uses_existing_api_with_scoped_filters_and_complete_jd(monkeypatch):
    source = MEITUAN_LIST + "?bg=BGCLC&jfJgList=11010_1101001"
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return listing([ROW])

    monkeypatch.setattr(job_details.requests, "post", post)
    crawler = GenericRenderCrawler("美团核心本地商业", source)
    jobs = crawler.fetch()
    assert len(jobs) == 1
    assert calls[0][0] == MeituanCrawler.API
    assert calls[0][1]["department"] == [{"code": "BGCLC"}]
    assert calls[0][1]["jfJgList"] == [{"code": "11010", "subCode": ["1101001"]}]
    assert calls[0][1]["jobType"] == [{"code": "1", "subCode": []}]
    assert jobs[0]["jd_url"] == MEITUAN_DETAIL
    assert jobs[0]["source_job_id"] == jobs[0]["native_job_id"] == MEITUAN_ID
    assert jobs[0]["id"] == f"meituan-job-{MEITUAN_ID}"
    assert jobs[0]["source_list_url"] == source
    assert assess_jd_capture(jobs[0]).complete
    assert crawler.pagination_complete is True
    assert crawler.advertised_total == 1
    assert crawler.pages_seen == 1


def test_meituan_all_advertised_pages_are_collected(monkeypatch):
    second = {**ROW, "jobUnionId": "4697280304", "name": "AI后端开发工程师"}
    calls = []

    def post(_url, **kwargs):
        page = kwargs["json"]["page"]["pageNo"]
        calls.append(page)
        return listing([ROW if page == 1 else second], page=page, pages=2, total=2)

    monkeypatch.setattr(job_details.requests, "post", post)
    crawler = MeituanCrawler("美团", MEITUAN_LIST)
    assert len(crawler.fetch()) == 2
    assert calls == [1, 2]
    assert crawler.pagination_complete and not crawler.has_more


def test_meituan_partial_page_failure_keeps_rows_and_does_not_claim_complete(monkeypatch):
    def post(_url, **kwargs):
        if kwargs["json"]["page"]["pageNo"] == 1:
            return listing([ROW], page=1, pages=2, total=2)
        raise requests.Timeout("synthetic timeout")

    monkeypatch.setattr(job_details.requests, "post", post)
    crawler = MeituanCrawler("美团", MEITUAN_LIST)
    assert len(crawler.fetch()) == 1
    assert crawler.pagination_complete is False
    assert crawler.fetch_failed is True
    assert crawler.crawl_error_code == "timeout"
    assert crawler.pagination_termination_reason == "list_request_failed_page_2"


@pytest.mark.parametrize("payload", [
    {"status": 0, "message": "unavailable"},
    {"status": 1, "data": {"list": [ROW]}},
])
def test_meituan_http_200_does_not_hide_api_or_pagination_failure(monkeypatch, payload):
    monkeypatch.setattr(job_details.requests, "post", lambda *a, **k: Response(payload))
    crawler = MeituanCrawler("美团", MEITUAN_LIST)
    assert crawler.fetch() == []
    assert not crawler.pagination_complete
    assert crawler.fetch_failed


def test_meituan_missing_native_id_or_duplicate_rows_are_not_complete(monkeypatch):
    monkeypatch.setattr(job_details.requests, "post", lambda *a, **k: listing([ROW, {**ROW, "jobUnionId": ""}], total=2))
    crawler = MeituanCrawler("美团", MEITUAN_LIST)
    assert len(crawler.fetch()) == 1
    assert not crawler.pagination_complete
    assert crawler.pagination_termination_reason == "api_total_count_mismatch"


def test_meituan_full_official_text_is_not_truncated():
    duty = "参与系统设计和开发。" * 1500 + "最后一项职责"
    row = MeituanCrawler("美团", MEITUAN_LIST)._parse_job({**ROW, "jobDuty": duty})
    assert "最后一项职责" in row["jd_raw"]
    assert len(row["jd_raw"]) > 12000
    assert assess_jd_capture(row).complete


def test_meituan_incomplete_api_fields_keep_incomplete_capture():
    row = MeituanCrawler("美团", MEITUAN_LIST)._parse_job({**ROW, "jobRequirement": ""})
    assert not assess_jd_capture(row).complete
    assert row["capture_evidence"]["status"] == "incomplete"


def test_legacy_meituan_list_url_resolves_unique_official_job_before_hydrating(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs["json"]))
        return listing([ROW]) if url == MeituanCrawler.API else Response({"status": 1, "data": ROW})

    monkeypatch.setattr(job_details.requests, "post", post)
    result = job_details.fetch_full_job_description_result({"company": "美团", "title": MEITUAN_TITLE, "jd_url": MEITUAN_LIST, "link_kind": "list"})
    assert result.complete
    assert result.detail_url == MEITUAN_DETAIL
    assert result.source == "meituan_official_api"
    assert calls[0][1]["keywords"] == MEITUAN_TITLE
    assert calls[1][1] == {"jobUnionId": MEITUAN_ID, "jobShareType": "1"}
    assert f"native_id:{MEITUAN_ID}" in result.identity_evidence


def test_meituan_same_title_multiple_native_jobs_requires_identity(monkeypatch):
    rows = [ROW, {**ROW, "jobUnionId": "another-official-id"}]
    monkeypatch.setattr(job_details.requests, "post", lambda url, **k: listing(rows) if url == MeituanCrawler.API else pytest.fail("Ambiguous rows must not call detail"))
    result = job_details.fetch_full_job_description_result({"title": MEITUAN_TITLE, "jd_url": MEITUAN_LIST, "link_kind": "list"})
    assert result.status == "identity_ambiguous"
    assert not result.complete


def test_meituan_wrong_api_native_id_is_rejected_with_observation(monkeypatch):
    monkeypatch.setattr(job_details.requests, "post", lambda *a, **k: Response({"status": 1, "data": {**ROW, "jobUnionId": "wrong-official-id"}}))
    result = job_details.fetch_full_job_description_result({"title": MEITUAN_TITLE, "jd_url": MEITUAN_DETAIL, "native_job_id": MEITUAN_ID})
    assert result.status == "identity_mismatch"
    assert result.identity_diagnostic["observed_job_ids"] == ["wrong-official-id"]


@pytest.mark.parametrize("city,native_id,expected", [
    ("北京", "", "identity_mismatch"),
    ("上海市", "", "complete"),
    ("北京、上海", "", "complete"),
    ("北京", MEITUAN_ID, "complete"),
])
def test_meituan_legacy_title_binding_checks_city_but_trusted_native_id_can_move(monkeypatch, city, native_id, expected):
    row = {**ROW, "cityList": [{"name": "上海"}]}
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return listing([row]) if url == MeituanCrawler.API else Response({"status": 1, "data": row})

    monkeypatch.setattr(job_details.requests, "post", post)
    result = job_details.fetch_full_job_description_result({"title": MEITUAN_TITLE, "jd_url": MEITUAN_LIST, "link_kind": "list", "city": city, "native_job_id": native_id})
    assert result.status == expected
    if expected == "identity_mismatch":
        assert calls == [MeituanCrawler.API]
        assert "reason:meituan_legacy_city_mismatch" in result.identity_evidence


@pytest.mark.parametrize("final_url,status_code", [
    ("https://untrusted.example/api", 200),
    (MeituanCrawler.API.replace("https:", "http:"), 200),
    (MeituanCrawler.API, 302),
])
@pytest.mark.parametrize("detail", [False, True])
def test_meituan_redirect_or_non_official_origin_is_not_capture_evidence(monkeypatch, final_url, status_code, detail):
    response = Response({"status": 1, "data": ROW} if detail else {"status": 1, "data": {"list": [ROW], "page": {"pageNo": 1, "totalPage": 1, "totalCount": 1}}})
    response.url = final_url
    response.status_code = status_code

    def post(url, **kwargs):
        assert kwargs["allow_redirects"] is False
        return response

    monkeypatch.setattr(job_details.requests, "post", post)
    if detail:
        result = job_details.fetch_full_job_description_result({"title": MEITUAN_TITLE, "jd_url": MEITUAN_DETAIL, "native_job_id": MEITUAN_ID})
        assert result.status == "fetch_failed"
        assert result.error_type == "official_origin_changed"
        assert not result.complete
    else:
        crawler = MeituanCrawler("美团", MEITUAN_LIST)
        assert crawler.fetch() == []
        assert crawler.crawl_error_code == "official_origin_changed"
        assert not crawler.pagination_complete


def test_real_requests_response_with_missing_url_cannot_prove_official_origin():
    assert not MeituanCrawler.is_official_api_response(requests.Response())


def test_meituan_same_official_id_is_stable_across_company_sources():
    first = MeituanCrawler("美团", MEITUAN_LIST)._parse_job(ROW)
    second = MeituanCrawler("美团核心本地商业", MEITUAN_LIST + "?bg=BGCLC")._parse_job(ROW)
    assert first["id"] == second["id"]
    assert first["native_job_id"] == second["native_job_id"]
    different_native = MeituanCrawler("美团", MEITUAN_LIST)._parse_job({**ROW, "jobUnionId": "another-official-id"})
    assert first["id"] != different_native["id"]


@pytest.mark.parametrize("heading,status", [
    ("无法访问此网站", "render_failed"),
    ("嗯… 无法访问此页面", "render_failed"),
    ("This site can’t be reached", "render_failed"),
    ("ERR_CONNECTION_TIMED_OUT", "render_failed"),
    ("Page not found", "job_offline"),
])
def test_browser_error_heading_never_becomes_identity_mismatch(monkeypatch, heading, status):
    monkeypatch.setattr(job_details, "render_page", lambda *a, **k: f"<main><h1>{heading}</h1></main>")
    result = job_details.fetch_full_job_description_result({"title": "C++工程师", "jd_url": "https://example.test/job/101"})
    assert result.status == status
    assert result.detail == ""
    assert result.identity_diagnostic == {}


@pytest.mark.parametrize("host", ["example.zhiye.com", "job.orbbec.com.cn"])
def test_beisen_numeric_native_id_and_uuid_route_use_distinct_namespaces(monkeypatch, host):
    route = "19ed4fb1-c00a-4c49-a64d-327bf71a8a7e"
    url = f"https://{host}/campus/detail?jobAdId={route}"
    requested = {"company_id": "example-company", "title": "C++软件工程师（27届校招）(J11225)", "native_job_id": "270968801", "jd_url": url, "company_campus_url": f"https://{host}/campus/jobs"}
    calls = []

    def get(api_url, **kwargs):
        calls.append(api_url)
        return Response({"Data": {"Id": route, "JobAdId": 270968801, "JobAdName": "C++软件工程师(27届校招)(J11225)", "Duty": ROW["jobDuty"], "Require": ROW["jobRequirement"]}})

    monkeypatch.setattr(job_details.requests, "get", get)
    result = job_details.fetch_full_job_description_result(requested)
    assert result.complete
    assert result.source == "beisen_api"
    assert calls == [f"https://{host}/api/JobAd/GetJobAdInfo"]
    assert f"observed_id:{route}" in result.identity_evidence
    assert "job_ad_id:JobAdId:270968801" in result.identity_evidence


def test_beisen_missing_identity_is_incomplete_not_observed_conflict(monkeypatch):
    url = "https://example.zhiye.com/campus/detail?jobAdId=official-uuid"
    monkeypatch.setattr(job_details.requests, "get", lambda *a, **k: Response({"Data": {"Duty": ROW["jobDuty"], "Require": ROW["jobRequirement"]}}))
    result = job_details.fetch_beisen_job_description_status(url, identity={"company_id": "example-company", "title": "C++工程师"})
    assert result[1] == "content_incomplete"
    assert result.identity_status == "unverified"
    assert "reason:beisen_request_id_missing" in result.identity_evidence
