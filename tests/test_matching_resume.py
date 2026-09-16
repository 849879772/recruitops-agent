from __future__ import annotations

import json
from hashlib import sha256

from packages.domain.models import Company, Job, JobAnalysis, RecruitmentBatch
from packages.matching import DeepSeekResponse, MatchingService
from packages.matching.resume import build_analysis_resume_plan, resume_pending_analyses
from packages.storage import JobAnalysisSnapshot, JobSnapshot, Storage
from packages.storage.sync import (
    upsert_company_snapshot,
    upsert_job_analysis_snapshot,
    upsert_job_snapshot,
)


PROFILE = {
    "degree": "硕士",
    "skills": ["C++", "Linux"],
    "matching": {
        "primary_directions": ["C++软件开发"],
        "project_evidence": ["C++机器人软件项目"],
        "supporting_skills": ["Linux"],
    },
}


def _job(job_id: str, *, cohort: int = 2027, batch: RecruitmentBatch = RecruitmentBatch.FORMAL):
    jd_raw = (
        "岗位职责：负责核心软件系统设计、编码、测试和性能优化。"
        "任职要求：熟悉 C++、Linux、多线程并发，具备良好的工程实践能力。"
        "参与需求评审、代码审查、故障定位和持续交付。"
    )
    return Job(
        id=job_id,
        company_id="company-1",
        title="C++软件开发工程师",
        detail_url=f"https://example.com/jobs/{job_id}",
        jd_raw=jd_raw,
        cohort=cohort,
        cohort_status="confirmed",
        batch=batch,
        source="test",
        source_ref=f"job:{job_id}",
        capture_evidence={
            "status": "complete",
            "method": "test_fixture",
            "source_url": f"https://example.com/jobs/{job_id}",
            "identity_verified": True,
            "terminal_observed": True,
            "remaining_controls": [],
            "content_sha256": sha256(jd_raw.encode("utf-8")).hexdigest(),
        },
    )


def _analysis(job: Job, status: str, *, error: str | None = None) -> JobAnalysis:
    del job
    return JobAnalysis(
        match_score=80 if status == "complete" else None,
        analysis_status=status,
        error_code=error,
    )


def _storage() -> Storage:
    storage = Storage.from_url("sqlite+pysqlite:///:memory:", initialize=True)
    company = Company(
        id="company-1",
        name="示例公司",
        campus_url="https://example.com/jobs",
        integration_status="connected",
        source="test",
        source_ref="company:1",
    )
    complete = _job("complete")
    failed = _job("failed")
    missing = _job("missing")
    old = _job("old", cohort=2026)
    early = _job("early", batch=RecruitmentBatch.EARLY)
    with storage.transaction(write=True) as session:
        upsert_company_snapshot(session, company)
        for job in (complete, failed, missing, old, early):
            upsert_job_snapshot(session, job)
        upsert_job_analysis_snapshot(session, complete, _analysis(complete, "complete"))
        upsert_job_analysis_snapshot(
            session,
            failed,
            _analysis(failed, "failed", error="http_402"),
        )
    return storage


class FakeClient:
    model = "deepseek-v4-flash"

    def __init__(self, *, error: str | None = None):
        self.error = error
        self.calls = 0

    def complete(self, **_kwargs):
        self.calls += 1
        if self.error:
            from packages.matching.client import DeepSeekClientError

            raise DeepSeekClientError(self.error)
        return DeepSeekResponse(
            content=json.dumps(
                {
                    "matched_directions": ["cpp_software"],
                    "primary_match_direction": "cpp_software",
                    "score_breakdown": {
                        "core_direction": 25,
                        "required_skills": 25,
                        "project_evidence": 20,
                        "engineering_stack": 10,
                    },
                    "evidence_level": "partial",
                    "evidence": [
                        {
                            "jd_requirement": "C++ Linux",
                            "profile_evidence": "C++机器人软件项目",
                            "relation": "direct",
                            "requirement_type": "core",
                        }
                    ],
                    "summary": "方向与工程技能匹配。",
                },
                ensure_ascii=False,
            ),
            input_tokens=100,
            output_tokens=50,
        )


def test_plan_uses_title_and_capture_gates_then_skips_successful_scores() -> None:
    plan = build_analysis_resume_plan(_storage(), PROFILE)

    assert plan.total_jobs == 5
    assert plan.eligible_jobs == 5
    assert plan.completed_jobs == 1
    assert [item.job.id for item in plan.pending_jobs] == [
        "failed",
        "early",
        "missing",
        "old",
    ]
    assert plan.pending_by_previous == {"failed:http_402": 1, "missing:none": 3}


def test_resume_persists_success_and_updates_job_score() -> None:
    storage = _storage()
    plan = build_analysis_resume_plan(storage, PROFILE, limit=1)
    result = resume_pending_analyses(
        storage,
        PROFILE,
        MatchingService(FakeClient()),
        plan.pending_jobs,
        concurrency=1,
    )

    assert result.completed == 1
    assert result.input_tokens == 100
    with storage.session() as session:
        analysis = session.get(JobAnalysisSnapshot, "failed")
        job = session.get(JobSnapshot, "failed")
        assert analysis is not None and analysis.analysis_status == "complete"
        assert job is not None and job.match_score == 80


def test_quota_error_stops_after_bounded_wave() -> None:
    storage = _storage()
    plan = build_analysis_resume_plan(storage, PROFILE)
    client = FakeClient(error="http_402")
    result = resume_pending_analyses(
        storage,
        PROFILE,
        MatchingService(client),
        plan.pending_jobs,
        concurrency=1,
    )

    assert result.processed == 1
    assert result.stopped_reason == "http_402"
    assert client.calls == 1
    with storage.session() as session:
        untouched = session.get(JobAnalysisSnapshot, "missing")
        assert untouched is None


def test_empty_model_response_is_not_retried_by_outer_resume_loop() -> None:
    class FlakyClient(FakeClient):
        def complete(self, **kwargs):
            if self.calls == 0:
                self.calls += 1
                from packages.matching.client import DeepSeekClientError

                raise DeepSeekClientError("response_empty")
            return super().complete(**kwargs)

    storage = _storage()
    plan = build_analysis_resume_plan(storage, PROFILE, limit=1)
    client = FlakyClient()
    result = resume_pending_analyses(
        storage,
        PROFILE,
        MatchingService(client),
        plan.pending_jobs,
        concurrency=1,
        retry_backoff_seconds=0,
    )

    assert result.failed == 1
    assert client.calls == 1
    with storage.session() as session:
        assert session.get(JobSnapshot, "failed").jd_raw == _job("failed").jd_raw
