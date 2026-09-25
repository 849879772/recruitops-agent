from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

import pytest

from packages.matching.client import DeepSeekClient, DeepSeekClientError
from packages.matching.models import AnalysisStatus, ScreeningResult
from packages.matching.service import MatchingService


def _valid_output() -> dict[str, Any]:
    return {
        "matched_directions": [],
        "primary_match_direction": None,
        "score_breakdown": {
            "core_direction": 26,
            "required_skills": 24,
            "project_evidence": 20,
            "engineering_stack": 12,
        },
        "evidence_level": "partial",
        "evidence": [{
            "jd_requirement": "使用 Python 开发检索服务",
            "profile_evidence": "使用 Python 实现过文档检索项目",
            "relation": "direct",
            "requirement_type": "core",
        }],
        "missing_core_requirements": [],
        "advantages": ["有相关检索项目实践"],
        "gaps": ["生产规模实践有限"],
        "summary": "方向匹配，主要技能有项目证据。",
    }


def _analyze(
    responses: list[dict[str, Any] | str | DeepSeekClientError],
    *,
    model: str = "deepseek-flash",
    thinking_enabled: bool = False,
):
    calls: list[dict[str, Any]] = []

    def transport(endpoint, headers, payload, timeout):
        calls.append(deepcopy(payload))
        response = responses[len(calls) - 1]
        if isinstance(response, DeepSeekClientError):
            raise response
        content = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        usage = {"input_tokens": 10 + len(calls), "output_tokens": 5 + len(calls)}
        return {
            "model": model, "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text",
                       "text": '{"result":' + content + '}'}]}],
            "usage": usage,
        }

    client = DeepSeekClient(
        api_key="test-only",
        model=model,

        thinking_enabled=thinking_enabled,
        transport=transport,
        max_attempts=1,
        retry_backoff_seconds=0,
    )
    result = MatchingService(client).analyze(
        {"id": "contract-job", "title": "检索工程师", "jd_raw": "使用 Python 开发检索服务"},
        {
            "skills": ["Python"],
            "matching": {"project_evidence": ["使用 Python 实现过文档检索项目"]},
        },
        screening=ScreeningResult(eligible=True, analysis_status=AnalysisStatus.ELIGIBLE),
    ).result
    return result, calls


def _system(call: dict[str, Any]) -> str:
    return call["instructions"]


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_observed_alias_output_is_corrected_instead_of_becoming_38_points(model):
    observed = _valid_output()
    observed["score_breakdown"] = {
        "direction_match": 22,
        "project_evidence": 24,
        "engineering_stack": 14,
    }
    observed["total"] = 83

    result, calls = _analyze([observed, _valid_output()], model=model)

    assert result.analysis_status is AnalysisStatus.COMPLETE
    assert result.match_score == 82
    assert result.score_breakdown.core_direction == 26
    assert result.score_breakdown.required_skills == 24
    assert result.prompt_version == "matching-prompt-v3"
    assert len(calls) == 2
    correction = _system(calls[1])[len(_system(calls[0])):]
    assert "core_direction" in correction
    assert "required_skills" in correction
    assert calls[0]["input"] == calls[1]["input"]
    assert calls[0]["max_output_tokens"] < calls[1]["max_output_tokens"]
    assert result.input_tokens == 23
    assert result.output_tokens == 13


@pytest.mark.parametrize("thinking_enabled", [False, True])
def test_correction_preserves_configured_thinking_mode(thinking_enabled):
    malformed = _valid_output()
    del malformed["score_breakdown"]["core_direction"]

    result, calls = _analyze([malformed, _valid_output()], thinking_enabled=thinking_enabled)

    assert result.analysis_status is AnalysisStatus.COMPLETE
    assert len(calls) == 2
    expected = "high" if thinking_enabled else "none"
    assert [call["reasoning"]["effort"] for call in calls] == [expected, expected]
    assert calls[0]["reasoning"] == calls[1]["reasoning"]


@pytest.mark.parametrize("bad_breakdown", [
    {"core_direction": {"score": 20, "max": 25}, "required_skills": 20, "project_evidence": 24, "engineering_stack": 14},
    {"core_direction": 40, "required_skills": 20, "project_evidence": 24, "engineering_stack": 14},
    {"core_direction": 20, "required_skills": 31, "project_evidence": 24, "engineering_stack": 14},
    {"core_direction": 20, "required_skills": 20, "project_evidence": 26, "engineering_stack": 14},
    {"core_direction": 20, "required_skills": 20, "project_evidence": 24, "engineering_stack": 16},
    {"core_direction": "20", "required_skills": 20, "project_evidence": 24, "engineering_stack": 14},
    {"core_direction": True, "required_skills": 20, "project_evidence": 24, "engineering_stack": 14},
    {"core_direction": 20.5, "required_skills": 20, "project_evidence": 24, "engineering_stack": 14},
    {"core_direction": -1, "required_skills": 20, "project_evidence": 24, "engineering_stack": 14},
    {"core_direction": 20, "required_skills": 20, "project_evidence": 24, "engineering_stack": 14, "total": 78},
])
def test_invalid_dimensions_exhaust_bounded_corrections_without_persistable_score(bad_breakdown):
    malformed = _valid_output()
    malformed["score_breakdown"] = bad_breakdown

    result, calls = _analyze([malformed, malformed, malformed])

    assert len(calls) == 3
    assert result.analysis_status is AnalysisStatus.FAILED
    assert result.match_score is None
    assert result.error_code == "model_output_invalid"
    assert result.recommendation == "未评估"


@pytest.mark.parametrize(("field", "value", "issue_field"), [
    ("profile_evidence", None, "profile_evidence"),
    ("profile_evidence", "", "profile_evidence"),
    ("jd_requirement", " ", "jd_requirement"),
    ("relation", "strong", "relation"),
    ("requirement_type", "required", "requirement_type"),
])
def test_invalid_evidence_gets_field_specific_correction(field, value, issue_field):
    malformed = _valid_output()
    if value is None:
        malformed["evidence"][0]["item"] = malformed["evidence"][0].pop(field)
    else:
        malformed["evidence"][0][field] = value

    result, calls = _analyze([malformed, _valid_output()])

    assert result.analysis_status is AnalysisStatus.COMPLETE
    assert len(calls) == 2
    correction = _system(calls[1])[len(_system(calls[0])):]
    assert issue_field in correction
    assert result.evidence[0].profile_evidence == "使用 Python 实现过文档检索项目"


@pytest.mark.parametrize("contradiction", [
    "zero_skills_with_direct_core",
    "zero_direction_with_high_match_summary",
    "direct_level_without_direct_item",
    "adjacent_level_with_direct_item",
    "positive_skills_without_evidence",
    "unsupported_evidence_level",
])
def test_score_evidence_contradictions_trigger_correction(contradiction):
    malformed = _valid_output()
    if contradiction == "zero_skills_with_direct_core":
        malformed["score_breakdown"]["required_skills"] = 0
    elif contradiction == "zero_direction_with_high_match_summary":
        malformed["score_breakdown"]["core_direction"] = 0
        malformed["summary"] = "岗位方向高度匹配。"
    elif contradiction == "direct_level_without_direct_item":
        malformed["evidence_level"] = "direct"
        malformed["evidence"][0]["relation"] = "adjacent"
    elif contradiction == "adjacent_level_with_direct_item":
        malformed["evidence_level"] = "adjacent"
    elif contradiction == "positive_skills_without_evidence":
        malformed["evidence"] = []
        malformed["evidence_level"] = "insufficient"
    else:
        malformed["evidence_level"] = "strong"

    result, calls = _analyze([malformed, _valid_output()])

    assert len(calls) == 2
    assert result.analysis_status is AnalysisStatus.COMPLETE
    assert result.match_score == 82


@pytest.mark.parametrize("missing_item", [False, True])
def test_genuine_zero_scores_and_negative_match_description_are_accepted(missing_item):
    valid = _valid_output()
    valid["score_breakdown"] = dict.fromkeys(valid["score_breakdown"], 0)
    valid["evidence_level"] = "insufficient"
    valid["summary"] = "方向并不高度匹配，缺少技能和项目证据。"
    valid["evidence"] = [{
        "jd_requirement": "使用 Python 开发检索服务",
        "profile_evidence": "",
        "relation": "missing",
        "requirement_type": "core",
    }] if missing_item else []
    valid["advantages"] = []
    valid["missing_core_requirements"] = ["缺少检索项目证据"]

    result, calls = _analyze([valid])

    assert len(calls) == 1
    assert result.analysis_status is AnalysisStatus.COMPLETE
    assert result.match_score == 0
    assert result.recommendation == "不推荐"


def test_model_total_does_not_override_computed_dimension_sum():
    output = _valid_output()
    output["match_score"] = 100
    output["total"] = 100
    output["recommendation"] = "满分推荐"

    result, calls = _analyze([output])

    assert len(calls) == 1
    assert result.match_score == 82
    assert result.recommendation == "推荐"


@pytest.mark.parametrize("code", ["http_401", "http_403"])
def test_authorization_failure_does_not_trigger_output_repair(code):
    result, calls = _analyze([DeepSeekClientError(code)])

    assert len(calls) == 1
    assert result.analysis_status is AnalysisStatus.FAILED
    assert result.match_score is None
    assert result.error_code == code


def test_explicit_refusal_is_not_retried_as_bad_json():
    result, calls = _analyze([{"refused": True, "reason": "cannot assess this input"}])

    assert len(calls) == 1
    assert result.analysis_status is AnalysisStatus.REFUSED
    assert result.match_score is None


@pytest.mark.parametrize("invalid", ["not JSON", '{"score_breakdown":', DeepSeekClientError("response_truncated")])
def test_incomplete_response_can_be_corrected_without_a_default_score(invalid):
    result, calls = _analyze([invalid, _valid_output()])

    assert len(calls) == 2
    assert result.analysis_status is AnalysisStatus.COMPLETE
    assert result.match_score == 82
