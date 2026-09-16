import pytest
from pydantic import ValidationError

from packages.domain.models import Job, RecruitmentBatch


def test_confirmed_job_accepts_valid_match_score() -> None:
    job = Job(
        id="job-1",
        company_id="company-1",
        title="C++ Software Engineer",
        detail_url="https://example.com/jobs/1",
        cohort=2027,
        cohort_status="confirmed",
        batch=RecruitmentBatch.FORMAL,
        match_score=82,
        source="fixture",
    )

    assert job.cohort == 2027
    assert job.match_score == 82


def test_job_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Job(
            id="job-1",
            company_id="company-1",
            title="Engineer",
            detail_url="https://example.com/jobs/1",
            source="fixture",
            invented_field="not allowed",
        )
