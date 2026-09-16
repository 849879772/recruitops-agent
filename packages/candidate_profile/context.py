from __future__ import annotations

from .models import CandidateEvidence, CandidateProfile, CandidateScoringContext


def build_scoring_context(profile: CandidateProfile) -> CandidateScoringContext:
    evidence: list[CandidateEvidence] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, values: list[str], path: str) -> None:
        for index, value in enumerate(values):
            key = (kind, value.casefold())
            if key in seen:
                continue
            seen.add(key)
            evidence.append(
                CandidateEvidence(
                    kind=kind,
                    text=value,
                    source_ref=f"{profile.source_ref}#profile.{path}[{index}]",
                )
            )

    add("skill", profile.skills, "skills")
    add("project", profile.matching.project_evidence, "matching.project_evidence")
    add(
        "supporting_skill",
        profile.matching.supporting_skills,
        "matching.supporting_skills",
    )

    directions = list(profile.matching.primary_directions)
    for direction in profile.matching.secondary_directions:
        if direction not in directions:
            directions.append(direction)
    if not directions and profile.direction:
        directions.append(profile.direction)

    return CandidateScoringContext(
        profile_hash=profile.content_hash,
        source_ref=profile.source_ref,
        degree=profile.degree,
        job_type=profile.job_type,
        direction_policy=profile.matching.direction_policy,
        target_directions=directions,
        evidence=evidence,
        learning_targets=profile.matching.learning_targets,
        excluded_unverified_skills=profile.matching.unverified_skills,
    )


__all__ = ["build_scoring_context"]
