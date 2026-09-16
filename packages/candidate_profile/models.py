from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MatchingProfile(ProfileModel):
    title_keywords: list[str] = Field(default_factory=list)
    excluded_title_keywords: list[str] = Field(default_factory=list)
    direction_policy: Literal["parallel", "priority"] = "parallel"
    primary_directions: list[str] = Field(default_factory=list)
    secondary_directions: list[str] = Field(default_factory=list)
    project_evidence: list[str] = Field(default_factory=list)
    supporting_skills: list[str] = Field(default_factory=list)
    learning_targets: list[str] = Field(default_factory=list)
    unverified_skills: list[str] = Field(default_factory=list)


class CandidateProfile(ProfileModel):
    schema_version: int = Field(default=1, ge=1)
    degree: str | None = None
    job_type: str | None = None
    direction: str | None = None
    skills: list[str] = Field(default_factory=list)
    matching: MatchingProfile = Field(default_factory=MatchingProfile)
    source_ref: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class CandidateEvidence(ProfileModel):
    kind: Literal["skill", "project", "supporting_skill"]
    text: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)


class CandidateScoringContext(ProfileModel):
    schema_version: int = Field(default=1, ge=1)
    profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ref: str
    degree: str | None = None
    job_type: str | None = None
    direction_policy: Literal["parallel", "priority"] = "parallel"
    target_directions: list[str] = Field(default_factory=list)
    evidence: list[CandidateEvidence] = Field(default_factory=list)
    learning_targets: list[str] = Field(default_factory=list)
    excluded_unverified_skills: list[str] = Field(default_factory=list)


__all__ = [
    "CandidateEvidence",
    "CandidateProfile",
    "CandidateScoringContext",
    "MatchingProfile",
]
