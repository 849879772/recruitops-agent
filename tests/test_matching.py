from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

import pytest

from packages.matching import (
    ANALYSIS_VERSION,
    DEFAULT_ENDPOINT,
    DeepSeekClient,
    DeepSeekClientError,
    DeepSeekResponse,
    Direction,
    DecisionAction,
    AnalysisStatus,
    MatchingService,
    classify_job_directions,
    content_fingerprint,
    profile_fingerprint,
    screen_job,
)


def _jd(extra: str = "") -> str:
    return (
        "岗位职责：负责研发和维护核心系统，参与方案设计、编码、测试和线上问题定位。\n"
        "任职要求：具备良好的编程基础，能够阅读技术文档并完成工程协作。\n"
        f"{extra}"
    )


def _job(**overrides: Any) -> dict[str, Any]:
    job = {
        "id": "job-1",
        "company": "示例公司",
        "title": "C++软件开发工程师",
        "city": "深圳",
        "job_type": "校园招聘",
        "batch": "formal",
        "cohort": 2027,
        "cohort_status": "confirmed",
        "jd_raw": _jd("使用 C++、Linux 完成系统软件开发。"),
    }
    job.update(overrides)
    job.setdefault(
        "capture_evidence",
        {
            "status": "complete",
            "method": "test_fixture",
            "source_url": "https://example.test/jobs/1",
            "identity_verified": True,
            "terminal_observed": True,
            "remaining_controls": [],
            "content_sha256": sha256(job["jd_raw"].encode("utf-8")).hexdigest(),
        },
    )
    return job


def _profile(**matching: Any) -> dict[str, Any]:
    return {
        "degree": "硕士",
        "skills": ["C++", "Linux"],
        "matching": {
            "direction_policy": "parallel",
            "primary_directions": [
                "C++软件开发",
                "机械臂开发",
                "具身智能",
                "Agent开发",
            ],
            "project_evidence": ["C++机器人软件项目", "Agent/RAG项目"],
            "supporting_skills": ["Linux", "ROS2"],
        },
    } | {"matching": matching} if matching else {
        "degree": "硕士",
        "skills": ["C++", "Linux"],
        "matching": {
            "direction_policy": "parallel",
            "primary_directions": [
                "C++软件开发",
                "机械臂开发",
                "具身智能",
                "Agent开发",
            ],
            "project_evidence": ["C++机器人软件项目", "Agent/RAG项目"],
            "supporting_skills": ["Linux", "ROS2"],
        },
    }


class FakeDeepSeek:
    model = "deepseek-fake"

    def __init__(self, *responses: str | dict[str, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> DeepSeekResponse:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        content = (
            response
            if isinstance(response, str)
            else json.dumps(response, ensure_ascii=False)
        )
        return DeepSeekResponse(
            content=content,
            model=self.model,
            input_tokens=11,
            output_tokens=7,
        )


def _model_result() -> dict[str, Any]:
    return {
        "matched_directions": ["cpp_software", "llm_agent"],
        "primary_match_direction": "cpp_software",
        "score_breakdown": {
            "core_direction": 28,
            "required_skills": 24,
            "project_evidence": 20,
            "engineering_stack": 12,
        },
        "evidence_level": "partial",
        "evidence": [
            {
                "jd_requirement": "C++系统软件开发",
                "profile_evidence": "C++机器人软件项目",
                "relation": "direct",
                "requirement_type": "core",
            }
        ],
        "missing_core_requirements": ["分布式训练"],
        "advantages": ["有直接 C++ 项目证据"],
        "gaps": ["缺少分布式训练经验"],
        "summary": "岗位核心方向与已有项目部分匹配。",
    }


def test_direction_classification_keeps_parallel_matches_and_auxiliary_evidence() -> None:
    job = _job(
        title="C++机械臂与VLA/Agent开发",
        jd_raw=_jd("负责 ROS2 机械臂控制、VLA 模仿学习、强化学习和 RAG 应用开发，使用 Linux。"),
    )

    classification = classify_job_directions(job)

    assert classification.matched_directions == [
        Direction.CPP_SOFTWARE,
        Direction.ROBOT_ARM,
        Direction.EMBODIED_LEARNING,
        Direction.LLM_AGENT,
    ]
    assert {item.signal for item in classification.supporting_evidence} == {"Linux", "ROS"}
    assert all(item.direction is None for item in classification.supporting_evidence)


@pytest.mark.parametrize(
    ("overrides", "status"),
    [
        ({"cohort": 2026}, AnalysisStatus.COHORT_UNCONFIRMED),
        ({"cohort_status": "unconfirmed"}, AnalysisStatus.COHORT_UNCONFIRMED),
        ({"title": "C++软件开发实习生"}, AnalysisStatus.INTERNSHIP),
        ({"batch": "internship"}, AnalysisStatus.INTERNSHIP),
        ({"jd_raw": _jd("学历要求：仅限博士。")}, AnalysisStatus.DOCTORATE_ONLY),
    ],
)
def test_deterministic_gates_filter_before_model_call(
    overrides: dict[str, Any], status: AnalysisStatus
) -> None:
    fake = FakeDeepSeek(_model_result())
    outcome = MatchingService(fake).analyze(_job(**overrides), _profile())

    assert outcome.decision.action is DecisionAction.FILTER
    assert outcome.result.analysis_status is status
    assert outcome.result.filter_reasons == [status.value]
    assert outcome.result.screening_evidence
    assert fake.calls == []


def test_doctorate_pre_screen_keeps_roles_that_accept_master_candidates() -> None:
    screening = screen_job(
        _job(jd_raw=_jd("学历要求：本科及以上，硕士优先，博士优先。")),
        _profile(),
    )

    assert screening.eligible is True


def test_structured_analysis_retains_evidence_versions_and_fingerprints() -> None:
    fake = FakeDeepSeek(_model_result())
    job = _job()
    profile = _profile()

    outcome = MatchingService(fake).analyze(job, profile)
    result = outcome.result

    assert outcome.decision.action is DecisionAction.ANALYZE
    assert result.analysis_status is AnalysisStatus.COMPLETE
    assert result.analysis_version == ANALYSIS_VERSION
    assert result.prompt_version == "matching-prompt-v1"
    assert result.content_fingerprint == content_fingerprint(job)
    assert result.profile_fingerprint == profile_fingerprint(profile)
    assert result.match_score == 84
    assert result.evidence[0].relation.value == "direct"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert len(fake.calls) == 1


def test_same_version_and_fingerprints_reuse_without_second_model_call() -> None:
    fake = FakeDeepSeek(_model_result(), _model_result())
    service = MatchingService(fake)
    first = service.analyze(_job(), _profile())
    second = service.analyze(_job(), _profile(), existing_analysis=first.result)

    assert second.decision.action is DecisionAction.REUSE
    assert second.decision.reason == "existing_complete_score_reuse"
    assert second.result == first.result
    assert len(fake.calls) == 1


def test_completed_score_is_reused_across_client_models() -> None:
    first_client = FakeDeepSeek(_model_result())
    first = MatchingService(first_client).analyze(_job(), _profile())

    same_model = MatchingService(FakeDeepSeek()).analyze(
        _job(), _profile(), existing_analysis=first.result
    )
    assert same_model.decision.action is DecisionAction.REUSE

    different_client = FakeDeepSeek(_model_result())
    different_client.model = "deepseek-other"
    different_model = MatchingService(different_client).analyze(
        _job(), _profile(), existing_analysis=first.result
    )
    assert different_model.decision.action is DecisionAction.REUSE
    assert different_model.decision.reason == "existing_complete_score_reuse"
    assert different_model.result.model == first.result.model
    assert len(different_client.calls) == 0

    unknown_client = FakeDeepSeek(_model_result())
    unknown_client.model = None
    unknown_model = MatchingService(unknown_client).analyze(
        _job(), _profile(), existing_analysis=first.result
    )
    assert unknown_model.decision.action is DecisionAction.REUSE
    assert unknown_model.decision.reason == "existing_complete_score_reuse"
    assert unknown_model.result.model == first.result.model
    assert len(unknown_client.calls) == 0


def test_completed_score_is_reused_before_legacy_screening() -> None:
    first_client = FakeDeepSeek(_model_result())
    first = MatchingService(first_client).analyze(_job(), _profile())

    replacement_client = FakeDeepSeek()
    reused = MatchingService(replacement_client).analyze(
        _job(title="C++软件开发实习生"),
        _profile(),
        existing_analysis=first.result,
    )

    assert reused.decision.action is DecisionAction.REUSE
    assert reused.decision.reason == "existing_complete_score_reuse"
    assert reused.result == first.result
    assert replacement_client.calls == []


def test_same_job_completed_score_is_reused_after_content_or_version_change() -> None:
    fake = FakeDeepSeek(_model_result(), _model_result(), _model_result(), _model_result())
    first = MatchingService(fake).analyze(_job(), _profile())

    changed_job = _job(jd_raw=_jd("使用 C++、Linux 完成系统软件开发，并负责多线程性能优化。"))
    changed = MatchingService(fake).analyze(
        changed_job,
        _profile(),
        existing_analysis=first.result,
    )
    assert changed.decision.action is DecisionAction.REUSE
    assert changed.decision.reason == "existing_complete_score_reuse"

    changed_profile = dict(_profile())
    changed_profile["skills"] = ["C++", "Python"]
    profile_changed = MatchingService(fake).analyze(
        _job(), changed_profile, existing_analysis=first.result
    )
    assert profile_changed.decision.action is DecisionAction.REUSE
    assert profile_changed.decision.reason == "existing_complete_score_reuse"

    versioned = MatchingService(fake, analysis_version="matching-v2").analyze(
        _job(),
        _profile(),
        existing_analysis=first.result,
    )
    assert versioned.decision.action is DecisionAction.REUSE
    assert versioned.decision.reason == "existing_complete_score_reuse"
    assert len(fake.calls) == 1


def test_refusal_and_failure_are_explicit_and_failure_can_retry() -> None:
    fake = FakeDeepSeek(
        {"status": "refused", "refusal_reason": "证据不足"},
        "not-json",
        _model_result(),
    )
    service = MatchingService(fake)
    refused = service.analyze(_job(), _profile())
    versioned_service = MatchingService(fake, analysis_version="matching-v2")
    failed = versioned_service.analyze(_job(), _profile(), existing_analysis=refused.result)
    retried = versioned_service.analyze(_job(), _profile(), existing_analysis=failed.result)

    assert refused.result.analysis_status is AnalysisStatus.REFUSED
    assert refused.result.refusal_reason == "证据不足"
    assert failed.result.analysis_status is AnalysisStatus.FAILED
    assert failed.result.error_code == "model_output_invalid"
    assert retried.result.analysis_status is AnalysisStatus.COMPLETE
    assert retried.decision.reason == "previous_failure_retry"
    assert len(fake.calls) == 3


def test_deepseek_client_uses_injected_transport_without_real_api() -> None:
    calls: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def transport(endpoint: str, headers: dict[str, str], payload: dict[str, Any], timeout: float):
        calls.append((endpoint, headers, payload, timeout))
        return {
            "model": "deepseek-test",
            "content": [{"type": "text", "text": "{\"status\": \"refused\"}"}],
            "usage": {
                "input_tokens": 3,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 7,
                "output_tokens": 2,
            },
        }

    client = DeepSeekClient(
        api_key="fake-key",
        model="deepseek-test",
        transport=transport,
    )
    response = client.complete(system_prompt="system", user_prompt="user")

    assert response.content == '{"status": "refused"}'
    assert response.input_tokens == 3
    assert response.cache_creation_input_tokens == 5
    assert response.cache_read_input_tokens == 7
    assert calls[0][0] == DEFAULT_ENDPOINT
    assert calls[0][1]["x-api-key"] == "fake-key"
    assert calls[0][2]["reasoning"] == {"effort": "none"}
    assert calls[0][2]["thinking"] == {"type": "disabled"}


def test_deepseek_client_enables_high_effort_thinking_for_matching() -> None:
    calls: list[tuple[str, dict[str, str], dict[str, Any], float]] = []

    def transport(endpoint: str, headers: dict[str, str], payload: dict[str, Any], timeout: float):
        calls.append((endpoint, headers, payload, timeout))
        return {
            "model": "deepseek-v4-flash",
            "content": [{"type": "text", "text": "{}"}],
            "usage": {"input_tokens": 3, "output_tokens": 2},
        }

    client = DeepSeekClient(
        api_key="fake-key",
        model="deepseek-v4-flash",
        thinking_enabled=True,
        reasoning_effort="high",
        transport=transport,
    )
    client.complete(system_prompt="system", user_prompt="user")

    assert calls[0][2]["reasoning"] == {"effort": "high"}
    assert calls[0][2]["thinking"] == {"type": "enabled"}
    assert calls[0][2]["output_config"] == {"effort": "high"}


def test_deepseek_client_retries_empty_transient_response() -> None:
    calls: list[dict[str, Any]] = []

    def transport(_endpoint, _headers, payload, _timeout):
        calls.append(payload)
        if len(calls) == 1:
            return {"model": "deepseek-v4-flash", "content": []}
        return {
            "model": "deepseek-v4-flash",
            "content": [{"type": "text", "text": "{}"}],
        }

    client = DeepSeekClient(
        api_key="fake-key",
        model="deepseek-v4-flash",
        transport=transport,
        retry_backoff_seconds=0,
    )

    assert client.complete(system_prompt="system", user_prompt="user").content == "{}"
    assert len(calls) == 2
    assert all(call["reasoning"] == {"effort": "none"} for call in calls)
    assert all(call["thinking"] == {"type": "disabled"} for call in calls)


def test_deepseek_client_accepts_output_text_content_blocks() -> None:
    def transport(_endpoint, _headers, _payload, _timeout):
        return {
            "model": "deepseek-flash",
            "content": [
                {"type": "thinking", "thinking": "internal reasoning"},
                {"type": "output_text", "text": "{\"status\": \"refused\"}"},
            ],
        }

    client = DeepSeekClient(
        api_key="fake-key",
        model="deepseek-flash",
        transport=transport,
    )

    response = client.complete(system_prompt="system", user_prompt="user")

    assert response.content == '{"status": "refused"}'


def test_matching_does_not_enable_thinking_when_disabled() -> None:
    calls: list[dict[str, Any]] = []

    def transport(_endpoint, _headers, payload, _timeout):
        calls.append(payload)
        text = "not-json" if len(calls) == 1 else json.dumps(_model_result(), ensure_ascii=False)
        return {
            "model": "deepseek-v4-flash",
            "content": [{"type": "text", "text": text}],
        }

    client = DeepSeekClient(
        api_key="fake-key",
        model="deepseek-v4-flash",
        thinking_enabled=False,
        transport=transport,
        max_attempts=1,
    )
    result = MatchingService(client).analyze(_job(), _profile())

    assert result.result.analysis_status is AnalysisStatus.FAILED
    assert result.result.error_code == "model_output_invalid"
    assert len(calls) == 1
    assert calls[0]["reasoning"] == {"effort": "none"}
    assert calls[0]["thinking"] == {"type": "disabled"}


def test_deepseek_client_reports_thinking_only_token_exhaustion_as_truncated() -> None:
    def transport(_endpoint, _headers, _payload, _timeout):
        return {
            "model": "deepseek-flash",
            "stop_reason": "max_tokens",
            "content": [{"type": "thinking", "thinking": "reasoning only"}],
            "usage": {"input_tokens": 100, "output_tokens": 2000},
        }

    client = DeepSeekClient(
        api_key="fake-key",
        model="deepseek-flash",
        transport=transport,
        max_attempts=1,
    )

    with pytest.raises(DeepSeekClientError, match="response_truncated"):
        client.complete(system_prompt="system", user_prompt="user")
