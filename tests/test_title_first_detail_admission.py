from copy import deepcopy
from hashlib import sha256

import pytest

from packages.recruitment_core.job_details import fetch_full_job_description_result


def job(status="unknown", cohort=0, policy="title_first_v2"):
    text = "Official short detail"
    return {
        "title": "C++ Engineer", "cohort": cohort, "cohort_status": status,
        "detail_capture_policy": policy, "jd_raw": text,
        "jd_url": "https://example.test/jobs/1",
        "capture_evidence": {
            "status": "complete", "identity_verified": True,
            "terminal_observed": True, "remaining_controls": [],
            "source_url": "https://example.test/jobs/1", "method": "official_api",
            "content_sha256": sha256(text.encode()).hexdigest(),
        },
    }


@pytest.mark.parametrize("status", ["unknown", "unconfirmed", ""])
def test_title_first_allows_unknown_without_mutating_cohort(status):
    record = job(status=status)
    before = deepcopy(record)
    assert fetch_full_job_description_result(record).status == "complete"
    assert record == before


@pytest.mark.parametrize("policy", [None, "legacy", "invalid"])
def test_legacy_unknown_still_rejected(policy):
    assert fetch_full_job_description_result(job(policy=policy)).status == "cohort_ineligible"


@pytest.mark.parametrize("status,cohort", [("confirmed", 2026), ("unknown", 2026), ("conflict", 2027)])
def test_explicit_other_cohort_and_conflict_not_overridden(status, cohort):
    assert fetch_full_job_description_result(job(status, cohort)).status == "cohort_ineligible"


def test_title_first_does_not_bypass_capture_checks():
    record = job()
    record['jd_url'] = ''
    record['capture_evidence']['content_sha256'] = 'invalid'
    assert fetch_full_job_description_result(record).status == 'no_detail_url'


def test_policy_survives_real_isolated_worker_transport():
    from packages.pipeline.isolation import fetch_job_detail_result_isolated

    record = job()
    before = deepcopy(record)
    result = fetch_job_detail_result_isolated(record, timeout_seconds=15)
    assert result['status'] == 'complete'
    assert result['source'] == 'stored'
    assert result['capture_evidence'] == record['capture_evidence']
    assert record == before
