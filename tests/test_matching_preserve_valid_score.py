from __future__ import annotations

import pytest

from packages.domain.models import Company, Job, JobAnalysis, JobDetail
from packages.matching.models import (
    AnalysisDecision, AnalysisOutcome, AnalysisRecord, AnalysisStatus, DecisionAction,
)
from packages.matching.resume import AnalysisResumeCandidate, resume_pending_analyses
from packages.pipeline.daily import (
    DailyRecruitmentPipeline, PipelineCompany, _CompanyWork, _StagedWork, _TitleFirstCandidate,
)
from packages.storage import JobAnalysisSnapshot, JobSnapshot, Storage
from packages.storage.sync import upsert_company_snapshot, upsert_job_detail_snapshot


def _fixture(*, scored: bool = True):
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    job = Job(
        id="job-1", company_id="company-1", title="C++ Engineer",
        detail_url="https://example.test/jobs/1", jd_raw="C++ Linux requirements",
        source="test", source_ref="job-1", match_score=82 if scored else None,
    )
    with storage.transaction(write=True) as session:
        upsert_company_snapshot(session, Company(
            id="company-1", name="Example", source="test", source_ref="company-1",
            integration_status="connected",
        ))
        upsert_job_detail_snapshot(session, JobDetail(
            job=job,
            analysis=JobAnalysis(
                analysis_status="complete", match_score=82,
                summary="Previously verified score", model="previous-model",
            ) if scored else None,
        ))
    return storage, job


def _assert_score(storage, score=82, status="complete"):
    with storage.session() as session:
        assert session.get(JobSnapshot, "job-1").match_score == score
        analysis = session.get(JobAnalysisSnapshot, "job-1")
        assert analysis.match_score == score
        assert analysis.analysis_status == status
        if status == "complete" and score == 82:
            assert analysis.summary == "Previously verified score"
            assert analysis.model == "previous-model"


@pytest.mark.parametrize("status", ["failed", "refused"])
def test_sync_failed_attempt_keeps_current_complete_score(status):
    storage, job = _fixture()
    # The stale caller can carry an old numeric score; preserve the current DB value.
    with storage.transaction(write=True) as session:
        upsert_job_detail_snapshot(session, JobDetail(
            job=job.model_copy(update={"match_score": 38}),
            analysis=JobAnalysis(analysis_status=status, error_code="model_output_invalid"),
        ))
    _assert_score(storage)


@pytest.mark.parametrize("status", ["failed", "refused"])
def test_new_failed_attempt_has_no_numeric_score(status):
    storage, job = _fixture(scored=False)
    with storage.transaction(write=True) as session:
        upsert_job_detail_snapshot(session, JobDetail(
            job=job.model_copy(update={"match_score": 38}),
            analysis=JobAnalysis(analysis_status=status, match_score=38),
        ))
    _assert_score(storage, None, status)


def test_successful_rescore_still_replaces_previous_score():
    storage, job = _fixture()
    with storage.transaction(write=True) as session:
        upsert_job_detail_snapshot(session, JobDetail(
            job=job.model_copy(update={"match_score": 91}),
            analysis=JobAnalysis(analysis_status="complete", match_score=91),
        ))
    _assert_score(storage, 91)


@pytest.mark.parametrize("title_first", [False, True])
@pytest.mark.parametrize("status", ["failed", "refused"])
def test_pipeline_failure_with_stale_plan_preserves_score(title_first, status):
    storage, job = _fixture()
    pipeline = DailyRecruitmentPipeline(storage=storage)
    company = PipelineCompany("company-1", "Example", "https://example.test", "test", "connected")
    analysis = JobAnalysis(analysis_status=status, error_code="model_output_invalid")
    stale_job = job.model_copy(update={"match_score": None}).model_dump(mode="python")
    if title_first:
        pipeline._persist_title_first(
            companies=(), existing_updates=(), new_candidates=(), inactive_ids=(),
            scored_candidates=(_TitleFirstCandidate(
                work=_CompanyWork(company), job=stale_job, title_key="c++ engineer",
                analysis=analysis,
            ),), dry_run=False,
        )
    else:
        pipeline._persist(companies=(), staged=(_StagedWork(
            company=company, job=stale_job, fingerprint="0" * 64,
            category="changed", analysis=analysis, existing=None,
        ),), dry_run=False)
    _assert_score(storage)


def test_resume_stale_plan_does_not_erase_complete_score_or_repeat_output_retries():
    storage, job = _fixture()
    candidate = AnalysisResumeCandidate(job=job, payload=job.model_dump(), previous_status="failed")

    class FailedService:
        calls = 0

        def analyze_title_first(self, *_args):
            self.calls += 1
            return AnalysisOutcome(
                decision=AnalysisDecision(
                    action=DecisionAction.ANALYZE, reason="previous_failure_retry",
                    analysis_version="matching-v1", prompt_version="new-prompt",
                    content_fingerprint="0" * 64, profile_fingerprint="1" * 64,
                ),
                result=AnalysisRecord(
                    job_id="job-1", analysis_status=AnalysisStatus.FAILED,
                    analysis_version="matching-v1", prompt_version="new-prompt",
                    content_fingerprint="0" * 64, profile_fingerprint="1" * 64,
                    error_code="model_output_invalid",
                ),
            )

    service = FailedService()
    result = resume_pending_analyses(
        storage, {}, service, [candidate], concurrency=1, retry_backoff_seconds=0,
    )
    assert result.failed == 1
    assert service.calls == 1
    _assert_score(storage)
