from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from .client import DeepSeekClientError
from .models import (
    AnalysisDecision,
    AnalysisOutcome,
    AnalysisRecord,
    AnalysisStatus,
    DecisionAction,
    DeepSeekMatchPayload,
    DeepSeekResponse,
    EvidenceLevel,
    EvidenceRelation,
    MatchEvidence,
    RequirementType,
    ScoreBreakdown,
    ScreeningEvidence,
    ScreeningResult,
)
from .title_policy import screen_title_job
from .rules import (
    canonical_direction,
    content_fingerprint,
    job_id,
    internship_reason,
    profile_content_payload,
    profile_fingerprint,
    screen_job,
)


ANALYSIS_VERSION = "matching-v1"
PROMPT_VERSION = "matching-prompt-v1"
DEFAULT_MAX_TOKENS = 1_000

_EVIDENCE_CAPS = {
    EvidenceLevel.DIRECT: 100,
    EvidenceLevel.PARTIAL: 89,
    EvidenceLevel.ADJACENT: 79,
    EvidenceLevel.INSUFFICIENT: 49,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_model(value: Any) -> str | None:
    text = str(value).strip() if value else ""
    return text or None


def _client_model(client: Any) -> str | None:
    return _normalize_model(getattr(client, "model", None))


def analysis_metadata(
    job: Any,
    profile: Any,
    model: str | None = None,
    *,
    analysis_version: str = ANALYSIS_VERSION,
    prompt_version: str = PROMPT_VERSION,
) -> dict[str, str | None]:
    return {
        "analysis_version": analysis_version,
        "prompt_version": prompt_version,
        "content_fingerprint": content_fingerprint(job),
        "profile_fingerprint": profile_fingerprint(profile),
        "model": _normalize_model(model),
    }


def _coerce_existing(value: Any) -> AnalysisRecord | None:
    if isinstance(value, AnalysisRecord):
        return value
    if not isinstance(value, Mapping):
        return None
    payload = dict(value)
    container_defaults: dict[str, Any] = {
        "advantages": [],
        "gaps": [],
        "score_breakdown": {},
        "evidence": [],
        "screening_evidence": [],
        "matched_directions": [],
        "filter_reasons": [],
    }
    for field, default in container_defaults.items():
        stored = payload.get(field)
        if stored is None:
            payload[field] = default
        elif isinstance(stored, str):
            try:
                payload[field] = json.loads(stored)
            except json.JSONDecodeError:
                pass
    if "content_fingerprint" not in payload and payload.get("jd_fingerprint"):
        payload["content_fingerprint"] = payload["jd_fingerprint"]
    if "analysis_status" not in payload and payload.get("status"):
        payload["analysis_status"] = payload["status"]
    if "prompt_version" not in payload:
        payload["prompt_version"] = PROMPT_VERSION
    payload = {
        key: item
        for key, item in payload.items()
        if key in AnalysisRecord.model_fields
    }
    if isinstance(payload.get("matched_directions"), list):
        payload["matched_directions"] = [
            direction.value
            for item in payload["matched_directions"]
            if (direction := canonical_direction(item)) is not None
        ]
    if payload.get("primary_match_direction"):
        direction = canonical_direction(payload["primary_match_direction"])
        payload["primary_match_direction"] = direction.value if direction else None
    try:
        return AnalysisRecord.model_validate(payload)
    except (ValidationError, TypeError, ValueError):
        return None


def decide_reuse(
    existing_analysis: Any,
    job: Any,
    profile: Any,
    *,
    analysis_version: str = ANALYSIS_VERSION,
    prompt_version: str = PROMPT_VERSION,
    model: str | None = None,
) -> AnalysisDecision:
    metadata = analysis_metadata(
        job,
        profile,
        model=model,
        analysis_version=analysis_version,
        prompt_version=prompt_version,
    )
    existing = _coerce_existing(existing_analysis)
    if existing is None:
        reason = "no_existing_analysis"
        status = None
        action = DecisionAction.ANALYZE
    elif existing.analysis_status is not AnalysisStatus.COMPLETE:
        status = existing.analysis_status
        if existing.analysis_status is AnalysisStatus.FAILED:
            reason = "previous_failure_retry"
        else:
            reason = "previous_analysis_not_complete"
        action = DecisionAction.ANALYZE
    elif existing.match_score is not None:
        reason = "existing_complete_score_reuse"
        status = existing.analysis_status
        action = DecisionAction.REUSE
    else:
        reason = "completed_score_missing"
        status = existing.analysis_status
        action = DecisionAction.ANALYZE
    return AnalysisDecision(
        action=action,
        reason=reason,
        analysis_version=analysis_version,
        prompt_version=prompt_version,
        content_fingerprint=str(metadata["content_fingerprint"]),
        profile_fingerprint=str(metadata["profile_fingerprint"]),
        existing_status=status,
    )


def can_reuse_analysis(
    existing_analysis: Any,
    job: Any,
    profile: Any,
    *,
    analysis_version: str = ANALYSIS_VERSION,
    prompt_version: str = PROMPT_VERSION,
    model: str | None = None,
) -> bool:
    return decide_reuse(
        existing_analysis,
        job,
        profile,
        model=model,
        analysis_version=analysis_version,
        prompt_version=prompt_version,
    ).action is DecisionAction.REUSE


def _safe_list(value: Any, limit: int | None = None) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = " ".join(str(item or "").split()).strip()
        if text and text not in result:
            result.append(text)
    return result[:limit] if limit is not None else result


def _safe_int(value: Any, maximum: int) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        number = 0
    return max(0, min(maximum, number))


def _prompt_profile(profile: Any) -> str:
    return json.dumps(profile_content_payload(profile), ensure_ascii=False, sort_keys=True)


def _prompt_system() -> str:
    return (
        "你是严格的校招岗位匹配审计员，只依据候选人和岗位给出的证据。"
        "岗位已经通过确定性筛选：cohort=2027、cohort_status=confirmed、非实习且非博士限定。"
        "仅依据候选人配置的 title_keywords、方向和简历事实评分，不套用开发者的职业方向。"
        "并列方向命中任一即可，不因未命中其他方向扣分；工程栈不能替代核心项目证据。"
        "未映射到内置方向枚举的自定义职业仍正常评分，matched_directions 返回空列表，primary_match_direction 返回 null。"
        "project_evidence 才能作为直接项目证据；supporting_skills只能作为工程栈证据；"
        "learning_targets和unverified_skills不能写成已掌握能力。"
        "当前阶段只负责评分：匹配较弱时给出低分，不得再次排除、跳过或延后岗位。"
        "只返回 JSON，不要 markdown。字段为 matched_directions、primary_match_direction、"
        "score_breakdown、evidence_level、evidence、missing_core_requirements、advantages、gaps、summary。"
        "evidence 中 relation 只能是 direct、adjacent、missing，requirement_type只能是 core、supporting、basic。"
        "保持输出紧凑：evidence最多6项，missing_core_requirements、advantages、gaps各最多4项，"
        "每个字符串不超过80个汉字，summary不超过160个汉字，不要复述完整JD或候选人资料。"
    )


def _prompt_user(job: Any, profile: Any) -> str:
    company = _field(job, "company", None) or _field(job, "company_id", "")
    payload = {
        "company": str(company or ""),
        "title": str(_field(job, "title", "") or ""),
        "city": str(_field(job, "city", "") or ""),
        "jd_raw": str(_field(job, "jd_raw", "") or "")[:12_000],
        "candidate": json.loads(_prompt_profile(profile)),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _response_content(response: Any) -> str:
    if isinstance(response, DeepSeekResponse):
        return response.content
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        content = response.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text = "".join(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, Mapping) and item.get("type") == "text"
            ).strip()
            if text:
                return text
        output = response.get("output_text")
        if isinstance(output, str):
            return output
        return json.dumps(response, ensure_ascii=False)
    raise ValueError("model response must be text or a mapping")


def _response_model(response: Any, fallback: str | None) -> str | None:
    if isinstance(response, DeepSeekResponse):
        return _normalize_model(response.model) or fallback
    return fallback


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("model response has no JSON object")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("model response is not a JSON object")
    return value


def _normalize_evidence(value: Any) -> list[MatchEvidence]:
    if not isinstance(value, list):
        return []
    result: list[MatchEvidence] = []
    for item in value[:6]:
        if not isinstance(item, Mapping):
            continue
        relation = str(item.get("relation") or "").casefold()
        if relation not in {item.value for item in EvidenceRelation}:
            relation = EvidenceRelation.MISSING.value
        requirement_type = str(item.get("requirement_type") or "").casefold()
        if requirement_type not in {item.value for item in RequirementType}:
            requirement_type = RequirementType.SUPPORTING.value
        raw_direction = item.get("direction")
        direction = canonical_direction(raw_direction) if raw_direction else None
        result.append(
            MatchEvidence(
                jd_requirement=str(item.get("jd_requirement") or "").strip(),
                profile_evidence=str(item.get("profile_evidence") or "").strip(),
                relation=relation,
                requirement_type=requirement_type,
                source=str(item.get("source") or "").strip() or None,
                source_ref=str(item.get("source_ref") or "").strip() or None,
                direction=direction,
            )
        )
    return result


def _normalize_payload(
    raw: Mapping[str, Any],
    allowed_directions: list[Any],
) -> tuple[DeepSeekMatchPayload | None, str | None]:
    raw_status = str(raw.get("analysis_status") or raw.get("status") or "").casefold()
    if raw.get("refused") is True or raw_status in {"refused", "rejected", "declined"}:
        return None, str(raw.get("refusal_reason") or raw.get("reason") or "model_refused")[:500]

    allowed = list(allowed_directions)
    directions: list[Any] = []
    raw_directions = raw.get("matched_directions")
    if isinstance(raw_directions, list):
        for value in raw_directions:
            direction = canonical_direction(value)
            if direction is not None and direction in allowed and direction not in directions:
                directions.append(direction)
    if not directions:
        directions = allowed
    primary = canonical_direction(raw.get("primary_match_direction"))
    if primary not in directions:
        primary = directions[0] if directions else None

    raw_breakdown = raw.get("score_breakdown") or {}
    if not isinstance(raw_breakdown, Mapping):
        raw_breakdown = {}
    breakdown = {
        "core_direction": _safe_int(raw_breakdown.get("core_direction"), 30),
        "required_skills": _safe_int(raw_breakdown.get("required_skills"), 30),
        "project_evidence": _safe_int(raw_breakdown.get("project_evidence"), 25),
        "engineering_stack": _safe_int(raw_breakdown.get("engineering_stack"), 15),
    }
    evidence_level = str(raw.get("evidence_level") or "insufficient").casefold()
    if evidence_level not in {item.value for item in EvidenceLevel}:
        evidence_level = EvidenceLevel.INSUFFICIENT.value
    payload = DeepSeekMatchPayload(
        matched_directions=directions,
        primary_match_direction=primary,
        score_breakdown=ScoreBreakdown(**breakdown),
        evidence_level=evidence_level,
        evidence=_normalize_evidence(raw.get("evidence")),
        missing_core_requirements=_safe_list(raw.get("missing_core_requirements")),
        advantages=_safe_list(raw.get("advantages"), 4),
        gaps=_safe_list(raw.get("gaps"), 4),
        summary=" ".join(str(raw.get("summary") or "").split())[:200],
    )
    return payload, None


def _score(payload: DeepSeekMatchPayload) -> tuple[int, str]:
    score = min(100, sum(payload.score_breakdown.model_dump().values()))
    score = min(score, _EVIDENCE_CAPS[payload.evidence_level])
    direct_core_count = sum(
        item.relation is EvidenceRelation.DIRECT
        and item.requirement_type is RequirementType.CORE
        for item in payload.evidence
    )
    if direct_core_count == 0:
        score = min(score, 79)
    missing = len(payload.missing_core_requirements)
    if missing == 1:
        score = min(score, 84)
    elif missing >= 2:
        score = min(score, 74 if missing == 2 else 64)
    if score >= 90 and direct_core_count < 2:
        score = 89
    recommendation = "推荐" if score >= 80 else "考虑" if score >= 60 else "不推荐"
    return score, recommendation


def _base_record(
    job: Any,
    profile: Any,
    *,
    status: AnalysisStatus,
    analysis_version: str,
    prompt_version: str,
    model: str | None,
    screening_evidence: list[ScreeningEvidence],
    **values: Any,
) -> AnalysisRecord:
    return AnalysisRecord(
        job_id=job_id(job),
        analysis_status=status,
        analysis_version=analysis_version,
        prompt_version=prompt_version,
        content_fingerprint=content_fingerprint(job),
        profile_fingerprint=profile_fingerprint(profile),
        model=model,
        screening_evidence=screening_evidence,
        analyzed_at=_now(),
        **values,
    )


_FILTER_SUMMARIES = {
    AnalysisStatus.COHORT_UNCONFIRMED: "届别待确认，不进行匹配评分",
    AnalysisStatus.EARLY_BATCH: "提前批岗位保留展示，不进行匹配评分",
    AnalysisStatus.INTERNSHIP: "实习岗位不进行校招匹配评分",
    AnalysisStatus.DOCTORATE_ONLY: "博士限定岗位不进入匹配评分",
    AnalysisStatus.JD_INCOMPLETE: "JD不完整，待补全后评分",
    AnalysisStatus.DIRECTION_OUT: "岗位未命中配置的目标方向",
}

_TITLE_FIRST_CAPTURE_FAILURE_STATES = {
    "failed",
    "fetch_failed",
    "capture_failed",
    "detail_failed",
    "jd_hydration_fetch_failed",
}


def _capture_failure_reason(job: Any) -> str | None:
    for field_name in ("capture_status", "detail_status", "status"):
        value = _field(job, field_name, None)
        state = str(value or "").strip().casefold()
        if state in _TITLE_FIRST_CAPTURE_FAILURE_STATES or state.endswith("_failed"):
            reason = str(_field(job, "capture_failure_reason", "") or "").strip()
            return reason or state
    return None


def _has_nonempty_jd(job: Any) -> bool:
    return bool(str(_field(job, "jd_raw", "") or "").strip())


def _has_completed_finite_score(existing: AnalysisRecord | None) -> bool:
    if existing is None or existing.analysis_status is not AnalysisStatus.COMPLETE:
        return False
    score = existing.match_score
    return isinstance(score, int) and not isinstance(score, bool) and 0 <= score <= 100


class MatchingService:
    """Pure job-to-analysis orchestration with an injectable model client."""

    def __init__(
        self,
        client: Any,
        *,
        analysis_version: str = ANALYSIS_VERSION,
        prompt_version: str = PROMPT_VERSION,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        if not hasattr(client, "complete"):
            raise TypeError("matching client must provide complete()")
        self.client = client
        self.analysis_version = analysis_version
        self.prompt_version = prompt_version
        self.max_tokens = max(128, min(int(max_tokens), 4_000))

    def analyze(
        self,
        job: Any,
        profile: Any,
        *,
        existing_analysis: Any = None,
        screening: ScreeningResult | None = None,
    ) -> AnalysisOutcome:
        model = _client_model(self.client)
        decision = decide_reuse(
            existing_analysis,
            job,
            profile,
            analysis_version=self.analysis_version,
            prompt_version=self.prompt_version,
            model=model,
        )
        existing = _coerce_existing(existing_analysis)
        if decision.action is DecisionAction.REUSE and existing is not None:
            return AnalysisOutcome(decision=decision, result=existing)

        screening = screen_job(job, profile) if screening is None else screening
        if not screening.eligible:
            decision = AnalysisDecision(
                action=DecisionAction.FILTER,
                reason=screening.analysis_status.value,
                analysis_version=self.analysis_version,
                prompt_version=self.prompt_version,
                content_fingerprint=content_fingerprint(job),
                profile_fingerprint=profile_fingerprint(profile),
            )
            result = _base_record(
                job,
                profile,
                status=screening.analysis_status,
                analysis_version=self.analysis_version,
                prompt_version=self.prompt_version,
                model=model,
                screening_evidence=screening.evidence,
                summary=_FILTER_SUMMARIES[screening.analysis_status],
                recommendation="未评估",
                filter_reasons=screening.reasons,
                matched_directions=screening.matched_directions,
                primary_match_direction=screening.primary_match_direction,
            )
            return AnalysisOutcome(decision=decision, result=result)

        try:
            response = self.client.complete(
                system_prompt=_prompt_system(),
                user_prompt=_prompt_user(job, profile),
                max_tokens=self.max_tokens,
            )
            try:
                raw = _parse_json_object(_response_content(response))
            except (TypeError, ValueError):
                thinking_retry = getattr(self.client, "complete_with_thinking", None)
                if not callable(thinking_retry) or not bool(
                    getattr(self.client, "thinking_enabled", False)
                ):
                    raise
                response = thinking_retry(
                    system_prompt=_prompt_system(),
                    user_prompt=_prompt_user(job, profile),
                    max_tokens=self.max_tokens,
                )
                raw = _parse_json_object(_response_content(response))
            payload, refusal_reason = _normalize_payload(raw, screening.matched_directions)
            if refusal_reason is not None:
                result = _base_record(
                    job,
                    profile,
                    status=AnalysisStatus.REFUSED,
                    analysis_version=self.analysis_version,
                    prompt_version=self.prompt_version,
                    model=_response_model(response, model),
                    screening_evidence=screening.evidence,
                    summary="模型拒答，待人工复核",
                    recommendation="未评估",
                    refusal_reason=refusal_reason,
                    matched_directions=screening.matched_directions,
                    primary_match_direction=screening.primary_match_direction,
                    input_tokens=(
                        response.input_tokens
                        if isinstance(response, DeepSeekResponse)
                        else None
                    ),
                    output_tokens=(
                        response.output_tokens
                        if isinstance(response, DeepSeekResponse)
                        else None
                    ),
                )
                return AnalysisOutcome(decision=decision, result=result)
            if payload is None:
                raise ValueError("model response payload is empty")
            score, recommendation = _score(payload)
            result = _base_record(
                job,
                profile,
                status=AnalysisStatus.COMPLETE,
                analysis_version=self.analysis_version,
                prompt_version=self.prompt_version,
                model=_response_model(response, model),
                screening_evidence=screening.evidence,
                match_score=score,
                advantages=payload.advantages,
                gaps=[*payload.gaps, *[
                    item
                    for item in payload.missing_core_requirements
                    if item not in payload.gaps
                ]],
                summary=payload.summary,
                recommendation=recommendation,
                score_breakdown=payload.score_breakdown,
                evidence=payload.evidence,
                evidence_level=payload.evidence_level,
                matched_directions=payload.matched_directions,
                primary_match_direction=payload.primary_match_direction,
                input_tokens=(
                    response.input_tokens
                    if isinstance(response, DeepSeekResponse)
                    else None
                ),
                output_tokens=(
                    response.output_tokens
                    if isinstance(response, DeepSeekResponse)
                    else None
                ),
            )
            return AnalysisOutcome(decision=decision, result=result)
        except DeepSeekClientError as exc:
            return AnalysisOutcome(
                decision=decision,
                result=self._failed_result(job, profile, screening, model, exc.code),
            )
        except Exception:
            return AnalysisOutcome(
                decision=decision,
                result=self._failed_result(job, profile, screening, model, "model_output_invalid"),
            )

    def analyze_title_first(
        self,
        job: Any,
        profile: Any,
        *,
        existing_analysis: Any = None,
        screening: ScreeningResult | None = None,
    ) -> AnalysisOutcome:
        """Run the title-first flow without changing the legacy ``analyze`` path."""
        if internship_reason(job):
            return self._title_first_filter(
                job, profile, screen_title_job(job, profile),
                status=AnalysisStatus.INTERNSHIP, reason="internship",
            )
        existing = _coerce_existing(existing_analysis)
        if _has_completed_finite_score(existing):
            decision = AnalysisDecision(
                action=DecisionAction.REUSE,
                reason="existing_complete_score_reuse",
                analysis_version=self.analysis_version,
                prompt_version=self.prompt_version,
                content_fingerprint=content_fingerprint(job),
                profile_fingerprint=profile_fingerprint(profile),
                existing_status=existing.analysis_status,
            )
            return AnalysisOutcome(decision=decision, result=existing)

        title_screening = screen_title_job(job, profile) if screening is None else screening
        if not title_screening.eligible:
            return self._title_first_filter(
                job,
                profile,
                title_screening,
                status=title_screening.analysis_status,
                reason=(title_screening.reasons or [title_screening.analysis_status.value])[0],
            )
        capture_failure = _capture_failure_reason(job)
        if capture_failure:
            return self._title_first_filter(
                job,
                profile,
                title_screening,
                status=AnalysisStatus.FAILED,
                reason="capture_failed",
                evidence=[
                    ScreeningEvidence(
                        source="capture",
                        signal="capture_failed",
                        excerpt=capture_failure,
                        reason=capture_failure,
                    )
                ],
            )
        if not _has_nonempty_jd(job):
            return self._title_first_filter(
                job,
                profile,
                title_screening,
                status=AnalysisStatus.JD_INCOMPLETE,
                reason="jd_empty",
                evidence=[
                    ScreeningEvidence(
                        source="jd_raw",
                        signal="jd_empty",
                        excerpt="",
                        reason="详情正文为空",
                    )
                ],
            )

        # The title-first caller has already screened this row; passing the result
        # keeps the model-backed path from invoking the legacy JD-aware screener.
        return self.analyze(
            job,
            profile,
            existing_analysis=None,
            screening=title_screening,
        )

    def _title_first_filter(
        self,
        job: Any,
        profile: Any,
        screening: ScreeningResult,
        *,
        status: AnalysisStatus,
        reason: str,
        evidence: list[ScreeningEvidence] | None = None,
    ) -> AnalysisOutcome:
        screening_evidence = [*(evidence or []), *screening.evidence]
        decision = AnalysisDecision(
            action=DecisionAction.FILTER,
            reason=reason,
            analysis_version=self.analysis_version,
            prompt_version=self.prompt_version,
            content_fingerprint=content_fingerprint(job),
            profile_fingerprint=profile_fingerprint(profile),
        )
        result = _base_record(
            job,
            profile,
            status=status,
            analysis_version=self.analysis_version,
            prompt_version=self.prompt_version,
            model=_client_model(self.client),
            screening_evidence=screening_evidence,
            summary=_FILTER_SUMMARIES.get(status, "详情抓取失败，不进行匹配评分"),
            recommendation="未评估",
            filter_reasons=[reason],
            matched_directions=screening.matched_directions,
            primary_match_direction=screening.primary_match_direction,
        )
        return AnalysisOutcome(decision=decision, result=result)

    def _failed_result(
        self,
        job: Any,
        profile: Any,
        screening: Any,
        model: str | None,
        error_code: str,
    ) -> AnalysisRecord:
        return _base_record(
            job,
            profile,
            status=AnalysisStatus.FAILED,
            analysis_version=self.analysis_version,
            prompt_version=self.prompt_version,
            model=model,
            screening_evidence=screening.evidence,
            summary="分析失败，待重试",
            recommendation="未评估",
            error_code=error_code,
            error_message=f"DeepSeek matching failed: {error_code}",
            matched_directions=screening.matched_directions,
            primary_match_direction=screening.primary_match_direction,
        )


def analyze_job(
    job: Any,
    profile: Any,
    client: Any,
    *,
    existing_analysis: Any = None,
    analysis_version: str = ANALYSIS_VERSION,
    prompt_version: str = PROMPT_VERSION,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> AnalysisOutcome:
    return MatchingService(
        client,
        analysis_version=analysis_version,
        prompt_version=prompt_version,
        max_tokens=max_tokens,
    ).analyze(job, profile, existing_analysis=existing_analysis)


def analyze_title_first(
    job: Any,
    profile: Any,
    client: Any,
    *,
    existing_analysis: Any = None,
    screening: ScreeningResult | None = None,
    analysis_version: str = ANALYSIS_VERSION,
    prompt_version: str = PROMPT_VERSION,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> AnalysisOutcome:
    return MatchingService(
        client,
        analysis_version=analysis_version,
        prompt_version=prompt_version,
        max_tokens=max_tokens,
    ).analyze_title_first(
        job,
        profile,
        existing_analysis=existing_analysis,
        screening=screening,
    )


__all__ = [
    "ANALYSIS_VERSION",
    "DEFAULT_MAX_TOKENS",
    "PROMPT_VERSION",
    "MatchingService",
    "analysis_metadata",
    "analyze_job",
    "analyze_title_first",
    "can_reuse_analysis",
    "decide_reuse",
]
