from bs4 import BeautifulSoup
import pytest

from packages.domain.models import RecruitmentBatch
from packages.recruitment_core import runner
from packages.recruitment_core.entry_crawl import _sms_login_wall
from packages.tools.crawler_audit import CrawlerAcceptanceInput, ObservedCrawlerJob, accept_crawler_run
from packages.tools.oc_candidates import _observed_ats_detail_urls, _empty_result_status
from packages.tools.typed import ToolErrorCode


SOURCE = "https://www.zwsoft.cn/job/campus"
DETAIL = "https://app.mokahr.com/campus_apply/zwcad/28356#/job/775f8887-4720-494c-bc6f-e0a001b75ff6"


def job(**changes):
    return ObservedCrawlerJob(**{
        "id": "one", "title": "Software Engineer", "detail_url": SOURCE + "/one",
        "cohort": 2027, "cohort_status": "confirmed", "batch": RecruitmentBatch.FORMAL,
        **changes,
    })


def audit(**changes):
    return accept_crawler_run(CrawlerAcceptanceInput(**{
        "company": "Example", "source_url": SOURCE, "jobs": [job()],
        "pages_seen": 1, "require_complete_jd": False, **changes,
    }))


def test_missing_pagination_evidence_is_not_a_success_or_a_proven_gap():
    result = audit()
    assert result.error_code == ToolErrorCode.PAGINATION_EVIDENCE_MISSING
    assert result.data.pagination_state == "unknown"
    assert not result.data.pagination_complete
    assert audit(pagination_complete=False, completeness_known=False).data.pagination_state == "unknown"
    assert audit(has_more=True).data.pagination_state == "incomplete"
    assert audit(pagination_complete=True).data.pagination_state == "complete"


def test_counts_use_unique_observed_jobs_before_business_filtering():
    duplicate = audit(jobs=[job(), job()], advertised_total=2, pagination_complete=True)
    assert duplicate.data.pagination_state == "incomplete"
    assert duplicate.data.unique_observed_count == 1
    filtered = audit(jobs=[job(), job(id="intern", batch=RecruitmentBatch.INTERNSHIP)],
                     advertised_total=2, pagination_complete=True)
    assert filtered.data.pagination_state == "complete"
    assert filtered.data.accepted_count == 1


def test_exact_ats_link_authorization_does_not_expand_the_platform_origin():
    raw = {"jd_url": DETAIL, "detail_link_observed": True, "detail_link_source_url": SOURCE}
    allowed = _observed_ats_detail_urls([raw], SOURCE, {})
    assert allowed == [DETAIL]
    other = DETAIL.replace("zwcad/28356", "other/123")
    result = audit(jobs=[job(detail_url=DETAIL), job(id="two", detail_url=other)],
                   pagination_complete=True, allowed_detail_urls=allowed)
    assert result.data.accepted_count == 1
    assert result.data.rejection_reasons == {"detail_origin_not_allowed": 1}


@pytest.mark.parametrize("changes", [
    {"detail_link_observed": False}, {"detail_link_source_url": "https://unknown.test/"},
    {"jd_url": "https://app.mokahr.com.evil.test/campus_apply/zwcad/28356#/job/123"},
    {"jd_url": "https://app.mokahr.com/campus_apply/zwcad/28356#/jobs"},
    {"jd_url": "http://127.0.0.1/jobs"},
    {"jd_url": "https://user:secret@app.mokahr.com/campus_apply/zwcad/28356#/job/123"},
])
def test_unproven_or_unsafe_detail_destinations_remain_rejected(changes):
    raw = {"jd_url": DETAIL, "detail_link_observed": True, "detail_link_source_url": SOURCE, **changes}
    assert _observed_ats_detail_urls([raw], SOURCE, {}) == []


def test_sms_login_wall_is_not_just_a_login_link():
    login = '<form><input type="tel"><input autocomplete="one-time-code"><button>登录</button></form>'
    assert _sms_login_wall(BeautifulSoup(login, "html.parser"))
    assert not _sms_login_wall(BeautifulSoup(login.replace('<form>', '<form style="display:none">'), "html.parser"))
    assert not _sms_login_wall(BeautifulSoup('<a>登录</a><h2>Software Engineer</h2>', "html.parser"))
    assert _empty_result_status(SOURCE, process_result={"error_code": "login_required"})[0] == "access_blocked"


def test_overlapping_complete_campaigns_keep_per_source_totals(monkeypatch):
    class Complete:
        pagination_complete = True
        pages_seen = total_pages = advertised_total = 1
        has_more = False

        def __init__(self, *_args):
            pass

        def fetch(self):
            return [{"title": "Software Engineer", "jd_url": SOURCE + "/one"}]

    monkeypatch.setattr(runner.job_cohorts, "annotate_company_jobs", lambda jobs, *_a, **_k: jobs)
    result = runner.crawl_company_with_evidence({
        "name": "Example", "crawler": "render", "careers_url": SOURCE,
        "campaign_urls": [SOURCE + "?project=2"],
    }, crawler_map={"render": Complete})
    assert result["pagination_complete"]
    assert len(result["jobs"]) == 1
    assert result["advertised_total"] is None
    assert result["advertised_total_scope"] == "per_source"
    assert [run["advertised_total"] for run in result["source_runs"]] == [1, 1]


def test_overlapping_campaign_union_cannot_hide_source_count_conflict(monkeypatch):
    class ClaimedComplete:
        pagination_complete = True
        pages_seen = total_pages = 1
        advertised_total = 2
        has_more = False

        def __init__(self, *_args):
            pass

        def fetch(self):
            return [{"title": "Engineer", "jd_url": SOURCE + "/one"}] * 2

    monkeypatch.setattr(runner.job_cohorts, "annotate_company_jobs", lambda jobs, *_a, **_k: jobs)
    result = runner.crawl_company_with_evidence({
        "name": "Example", "crawler": "render", "careers_url": SOURCE,
        "campaign_urls": [SOURCE + "?project=2"],
    }, crawler_map={"render": ClaimedComplete})
    assert result["pagination_state"] == "incomplete"
    assert result["termination_reasons"] == ["source_evidence_conflict"]
