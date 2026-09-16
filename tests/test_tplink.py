import copy

import pytest

from packages.recruitment_core.crawlers.tplink import HOME, TPLinkCrawler


def payload():
    return {"jobClasses": [{"Id": 268}], "workPlaces": [], "jobs": [{
        "Id": 7517, "JobName": "Software Engineer", "ClassId": 268,
        "JobAddress": "Shenzhen", "Duty": "Long duty\r\n" + "a" * 5000,
        "Requirement": "Full requirements", "Batch": "2021", "Education": "BS",
    }]}


def test_preserves_native_identity_full_jd_batch_and_unknown_totals():
    crawler = TPLinkCrawler("TP-LINK", HOME)
    data = payload()
    jobs = crawler._consume(data, "2027 campus")
    assert jobs[0]["source_job_id"] == "7517"
    assert jobs[0]["jd_url"] == HOME + "jobDetail/7517"
    cleaned_duty = crawler._clean_jd_field(data["jobs"][0]["Duty"])
    assert jobs[0]["jd_raw"] == cleaned_duty + "\n\nFull requirements"
    assert jobs[0]["raw_batch"] == "2021"
    assert jobs[0]["jd_raw_complete"] is True
    assert crawler.pagination_complete is True
    assert crawler.pages_seen == 1
    assert crawler.total_pages is None
    assert crawler.advertised_total is None
    assert crawler.has_more is False


@pytest.mark.parametrize("url", ["https://join.tplinkglobal.com/campus/",
    "https://hr.tp-link.com.cn.evil.invalid/", HOME + "socialJobList",
    HOME + "jobDetail/7517", "http://hr.tp-link.com.cn/", HOME + "?page=1"])
def test_rejects_other_company_or_non_campus_source(url):
    with pytest.raises(ValueError):
        TPLinkCrawler("TP-LINK", url)


@pytest.mark.parametrize("data", [{}, {"error": "login required"},
    {"jobs": [], "jobClasses": []}, {"jobs": None, "jobClasses": [], "workPlaces": []}])
def test_error_or_missing_array_is_not_empty_success(data):
    with pytest.raises(ValueError):
        TPLinkCrawler.parse_payload(data)


def test_dedupes_only_same_native_id_and_keeps_same_title_distinct_jobs():
    data = payload()
    data["jobs"] += [copy.deepcopy(data["jobs"][0]), {**data["jobs"][0], "Id": 7518}]
    assert len(TPLinkCrawler.parse_payload(data)) == 2
    data["jobs"][1]["Duty"] = "conflict"
    with pytest.raises(ValueError, match="conflicting_job_identity"):
        TPLinkCrawler.parse_payload(data)


@pytest.mark.parametrize("field,value", [("Id", True), ("Id", 0), ("JobName", ""),
    ("JobName", "{{job.JobName}}"), ("Duty", ["not text"])])
def test_invalid_rows_fail_closed(field, value):
    data = payload()
    data["jobs"][0][field] = value
    with pytest.raises(ValueError):
        TPLinkCrawler.parse_payload(data)


@pytest.mark.parametrize("text", ["", "--", "-", None])
def test_missing_jd_not_fabricated(text):
    data = payload()
    data["jobs"][0]["Duty"] = text
    job = TPLinkCrawler("TP-LINK", HOME)._consume(data, "2027 campus")[0]
    assert job["raw_duty"] == (text or "")
    assert job["jd_raw_complete"] is False


def test_cleans_html_jd_fields_but_keeps_raw_evidence():
    data = payload()
    data["jobs"][0]["Duty"] = "<p>岗位职责</p><ul><li>负责开发</li></ul>"
    data["jobs"][0]["Requirement"] = "<p>任职要求</p><p>本科</p>"
    job = TPLinkCrawler("TP-LINK", HOME)._consume(data, "2027 campus")[0]

    assert job["raw_duty"] == data["jobs"][0]["Duty"]
    assert job["raw_requirement"] == data["jobs"][0]["Requirement"]
    assert "<p>" not in job["jd_raw"]
    assert "岗位职责" in job["jd_raw"]
    assert "负责开发" in job["jd_raw"]
    assert "本科" in job["jd_raw"]
    assert job["jd_raw_complete"] is True


def test_unexpected_pagination_not_accepted_as_complete():
    with pytest.raises(ValueError, match="unexpected_pagination_contract"):
        TPLinkCrawler.parse_payload({**payload(), "hasMore": True})
