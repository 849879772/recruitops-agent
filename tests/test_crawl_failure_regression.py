from scripts.run_crawl_failure_regression import summarize_results


def test_classified_failures_do_not_count_as_success_or_jd_validation():
    summary = summarize_results([
        {"failure_reason": "pagination_incomplete", "raw_job_count": 14,
         "crawl_evidence": {"pagination_complete": False, "completeness_known": True}},
        {"failure_reason": "login_required", "raw_job_count": 0},
    ])
    assert summary["complete_crawls_with_jobs"] == 0
    assert summary["failed_companies"] == 2
    assert summary["jd_validation"] == "not_exercised"


def test_verified_empty_is_separate_from_jobs_and_unsampled_details():
    complete = {"pagination_complete": True, "completeness_known": True, "has_more": False}
    summary = summarize_results([
        {"run_reason": "activity_empty", "raw_job_count": 0, "crawl_evidence": complete},
        {"raw_job_count": 2, "crawl_evidence": complete,
         "jd_results": [{"status": "complete"}, {"status": "not_sampled"}]},
    ])
    assert summary["complete_crawls_with_jobs"] == 1
    assert summary["verified_empty_activities"] == 1
    assert summary["jd_attempted"] == summary["jd_completed"] == 1
    assert summary["jd_not_sampled"] == 1
