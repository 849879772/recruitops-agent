from __future__ import annotations

import json
from typing import Any

import pytest

from packages.matching import AnalysisStatus, DecisionAction, DeepSeekResponse, MatchingService
from packages.matching.models import AnalysisRecord
from packages.matching.title_policy import screen_title_job
from packages.matching import service as matching_service


def _profile() -> dict[str, Any]:
    return {"matching": {"primary_directions": ["C++软件开发"]}}


def _job(**overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": "job-1",
        "company": "示例公司",
        "title": "C++软件开发工程师",
        "jd_raw": "短正文",
    }
    job.update(overrides)
    return job


def _model_payload() -> dict[str, Any]:
    return {
        "matched_directions": ["cpp_software"],
        "primary_match_direction": "cpp_software",
        "score_breakdown": {
            "core_direction": 24,
            "required_skills": 22,
            "project_evidence": 20,
            "engineering_stack": 12,
        },
        "evidence_level": "partial",
        "evidence": [
            {
                "jd_requirement": "C++软件开发",
                "profile_evidence": "C++项目",
                "relation": "direct",
                "requirement_type": "core",
            }
        ],
        "advantages": ["有 C++ 项目证据"],
        "gaps": [],
        "summary": "标题通过后使用详情进行评分。",
    }


class _FakeClient:
    model = "title-first-test-model"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> DeepSeekResponse:
        self.calls.append(kwargs)
        return DeepSeekResponse(
            content=json.dumps(_model_payload(), ensure_ascii=False),
            model=self.model,
            input_tokens=1,
            output_tokens=1,
        )


def test_title_first_allows_short_nonempty_jd_and_skips_legacy_screen(monkeypatch) -> None:
    def fail_legacy_screen(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("title-first flow must not call screen_job")

    monkeypatch.setattr(matching_service, "screen_job", fail_legacy_screen)
    client = _FakeClient()

    outcome = MatchingService(client).analyze_title_first(_job(jd_raw="x"), _profile())

    assert outcome.decision.action is DecisionAction.ANALYZE
    assert outcome.result.analysis_status is AnalysisStatus.COMPLETE
    assert outcome.result.match_score == 78
    assert len(client.calls) == 1


def test_passed_title_screening_is_reused_without_a_second_title_screen(monkeypatch) -> None:
    job = _job(jd_raw="x")
    profile = _profile()
    screening = screen_title_job(job, profile)

    def fail_title_screen(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("provided screening must be reused")

    monkeypatch.setattr(matching_service, "screen_title_job", fail_title_screen)
    outcome = MatchingService(_FakeClient()).analyze_title_first(
        job,
        profile,
        screening=screening,
    )

    assert outcome.result.analysis_status is AnalysisStatus.COMPLETE


@pytest.mark.parametrize(
    ("overrides", "status", "reason"),
    [
        (
            {"capture_status": "failed", "capture_failure_reason": "timeout"},
            AnalysisStatus.FAILED,
            "capture_failed",
        ),
        ({"jd_raw": "   "}, AnalysisStatus.JD_INCOMPLETE, "jd_empty"),
    ],
)
def test_title_first_rejects_failed_or_empty_details_without_model_call(
    overrides: dict[str, Any], status: AnalysisStatus, reason: str
) -> None:
    client = _FakeClient()

    outcome = MatchingService(client).analyze_title_first(_job(**overrides), _profile())

    assert outcome.decision.action is DecisionAction.FILTER
    assert outcome.decision.reason == reason
    assert outcome.result.analysis_status is status
    assert client.calls == []


def test_title_first_rejects_unknown_title_without_reading_jd_for_direction() -> None:
    client = _FakeClient()
    outcome = MatchingService(client).analyze_title_first(
        _job(title="行政专员", jd_raw="C++ 软件开发详情"),
        _profile(),
    )

    assert outcome.decision.action is DecisionAction.FILTER
    assert outcome.result.analysis_status is AnalysisStatus.DIRECTION_OUT
    assert client.calls == []


def test_title_first_reuses_any_existing_complete_finite_score_before_gates() -> None:
    existing = AnalysisRecord(
        job_id="job-1",
        analysis_status=AnalysisStatus.COMPLETE,
        match_score=0,
        analysis_version="old-version",
        prompt_version="old-prompt",
        content_fingerprint="0" * 64,
        profile_fingerprint="1" * 64,
        model="old-model",
    )
    client = _FakeClient()

    outcome = MatchingService(
        client,
        analysis_version="new-version",
        prompt_version="new-prompt",
    ).analyze_title_first(
        _job(title="未知岗位", jd_raw="", capture_status="failed"),
        {"matching": {"primary_directions": ["Agent开发"]}},
        existing_analysis=existing,
    )

    assert outcome.decision.action is DecisionAction.REUSE
    assert outcome.decision.reason == "existing_complete_score_reuse"
    assert outcome.result is existing
    assert client.calls == []
