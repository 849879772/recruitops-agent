from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


FINGERPRINT_PATTERN = r"^[0-9a-f]{64}$"


class MatchingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Direction(StrEnum):
    CPP_SOFTWARE = "cpp_software"
    ROBOT_ARM = "robot_arm"
    EMBODIED_LEARNING = "embodied_learning"
    LLM_AGENT = "llm_agent"


DIRECTION_LABELS: dict[Direction, str] = {
    Direction.CPP_SOFTWARE: "C++软件开发（含测开）",
    Direction.ROBOT_ARM: "机械臂开发",
    Direction.EMBODIED_LEARNING: "具身/VLA/模仿学习/强化学习",
    Direction.LLM_AGENT: "大模型/Agent/RAG/训练部署/AI应用",
}


class AnalysisStatus(StrEnum):
    ELIGIBLE = "eligible"
    COMPLETE = "complete"
    REFUSED = "refused"
    FAILED = "failed"
    COHORT_UNCONFIRMED = "cohort_unconfirmed"
    EARLY_BATCH = "early_batch"
    INTERNSHIP = "internship"
    DOCTORATE_ONLY = "doctorate_only"
    JD_INCOMPLETE = "jd_incomplete"
    DIRECTION_OUT = "direction_out"


class DecisionAction(StrEnum):
    ANALYZE = "analyze"
    REUSE = "reuse"
    FILTER = "filter"


class EvidenceRelation(StrEnum):
    DIRECT = "direct"
    ADJACENT = "adjacent"
    MISSING = "missing"


class RequirementType(StrEnum):
    CORE = "core"
    SUPPORTING = "supporting"
    BASIC = "basic"


class EvidenceLevel(StrEnum):
    DIRECT = "direct"
    PARTIAL = "partial"
    ADJACENT = "adjacent"
    INSUFFICIENT = "insufficient"


class ScoreBreakdown(MatchingModel):
    core_direction: int = Field(default=0, ge=0, le=30)
    required_skills: int = Field(default=0, ge=0, le=30)
    project_evidence: int = Field(default=0, ge=0, le=25)
    engineering_stack: int = Field(default=0, ge=0, le=15)


class MatchEvidence(MatchingModel):
    jd_requirement: str = ""
    profile_evidence: str = ""
    relation: EvidenceRelation = EvidenceRelation.MISSING
    requirement_type: RequirementType = RequirementType.SUPPORTING
    source: str | None = None
    source_ref: str | None = None
    direction: Direction | None = None


class ScreeningEvidence(MatchingModel):
    source: str
    signal: str
    excerpt: str = ""
    reason: str = ""
    direction: Direction | None = None


class DirectionClassification(MatchingModel):
    matched_directions: list[Direction] = Field(default_factory=list)
    primary_match_direction: Direction | None = None
    evidence: list[ScreeningEvidence] = Field(default_factory=list)
    supporting_evidence: list[ScreeningEvidence] = Field(default_factory=list)


class ScreeningResult(MatchingModel):
    eligible: bool
    analysis_status: AnalysisStatus
    reasons: list[str] = Field(default_factory=list)
    evidence: list[ScreeningEvidence] = Field(default_factory=list)
    matched_directions: list[Direction] = Field(default_factory=list)
    primary_match_direction: Direction | None = None
    supporting_evidence: list[ScreeningEvidence] = Field(default_factory=list)


class DeepSeekMatchPayload(MatchingModel):
    matched_directions: list[Direction] = Field(default_factory=list)
    primary_match_direction: Direction | None = None
    score_breakdown: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    evidence_level: EvidenceLevel = EvidenceLevel.INSUFFICIENT
    evidence: list[MatchEvidence] = Field(default_factory=list)
    missing_core_requirements: list[str] = Field(default_factory=list)
    advantages: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    summary: str = ""


class DeepSeekResponse(MatchingModel):
    content: str
    model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AnalysisRecord(MatchingModel):
    job_id: str
    analysis_status: AnalysisStatus
    match_score: int | None = Field(default=None, ge=0, le=100)
    advantages: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    summary: str | None = None
    recommendation: str | None = None
    score_breakdown: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    evidence: list[MatchEvidence] = Field(default_factory=list)
    screening_evidence: list[ScreeningEvidence] = Field(default_factory=list)
    evidence_level: EvidenceLevel | None = None
    matched_directions: list[Direction] = Field(default_factory=list)
    primary_match_direction: Direction | None = None
    filter_reasons: list[str] = Field(default_factory=list)
    refusal_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    analysis_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    content_fingerprint: str = Field(pattern=FINGERPRINT_PATTERN)
    profile_fingerprint: str = Field(pattern=FINGERPRINT_PATTERN)
    model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    analyzed_at: datetime = Field(default_factory=utc_now)

    @property
    def status(self) -> str:
        return self.analysis_status.value

    @property
    def jd_fingerprint(self) -> str:
        """Compatibility name for callers that use the old persisted field."""
        return self.content_fingerprint


class AnalysisDecision(MatchingModel):
    action: DecisionAction
    reason: str
    analysis_version: str
    prompt_version: str
    content_fingerprint: str = Field(pattern=FINGERPRINT_PATTERN)
    profile_fingerprint: str = Field(pattern=FINGERPRINT_PATTERN)
    existing_status: AnalysisStatus | None = None


class AnalysisOutcome(MatchingModel):
    decision: AnalysisDecision
    result: AnalysisRecord

    @property
    def analysis(self) -> AnalysisRecord:
        return self.result


def model_dump_json_safe(value: Any) -> Any:
    """Return a JSON-shaped value for prompt construction and test fakes."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


__all__ = [
    "AnalysisDecision",
    "AnalysisOutcome",
    "AnalysisRecord",
    "AnalysisStatus",
    "DecisionAction",
    "DeepSeekMatchPayload",
    "DeepSeekResponse",
    "Direction",
    "DIRECTION_LABELS",
    "DirectionClassification",
    "EvidenceLevel",
    "EvidenceRelation",
    "MatchEvidence",
    "RequirementType",
    "ScoreBreakdown",
    "ScreeningEvidence",
    "ScreeningResult",
    "model_dump_json_safe",
]
