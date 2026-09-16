from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml

from .models import CandidateProfile, MatchingProfile


class CandidateProfileError(ValueError):
    """Raised when the local profile cannot be loaded safely."""


def _string_list(value: object, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CandidateProfileError(f"profile.{field_name} must be a list")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise CandidateProfileError(f"profile.{field_name} must contain non-empty strings")
        normalized = " ".join(item.split())
        if normalized not in items:
            items.append(normalized)
    return items


def _optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CandidateProfileError(f"profile.{field_name} must be a string")
    normalized = " ".join(value.split())
    return normalized or None


def _canonical_hash(profile: dict[str, Any]) -> str:
    encoded = json.dumps(
        profile,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def load_candidate_profile(path: Path) -> CandidateProfile:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise CandidateProfileError(f"candidate profile source does not exist: {resolved}")
    try:
        payload = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CandidateProfileError(f"unable to read candidate profile: {resolved}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("profile"), dict):
        raise CandidateProfileError("config must contain a profile mapping")

    raw = payload["profile"]
    matching_raw = raw.get("matching") or {}
    if not isinstance(matching_raw, dict):
        raise CandidateProfileError("profile.matching must be a mapping")
    direction_policy = matching_raw.get("direction_policy", "parallel")
    if direction_policy not in {"parallel", "priority"}:
        raise CandidateProfileError("profile.matching.direction_policy is unsupported")

    matching = MatchingProfile(
        title_keywords=_string_list(matching_raw.get("title_keywords"), field_name="matching.title_keywords"),
        excluded_title_keywords=_string_list(matching_raw.get("excluded_title_keywords"), field_name="matching.excluded_title_keywords"),
        direction_policy=direction_policy,
        primary_directions=_string_list(
            matching_raw.get("primary_directions"), field_name="matching.primary_directions"
        ),
        secondary_directions=_string_list(
            matching_raw.get("secondary_directions"), field_name="matching.secondary_directions"
        ),
        project_evidence=_string_list(
            matching_raw.get("project_evidence"), field_name="matching.project_evidence"
        ),
        supporting_skills=_string_list(
            matching_raw.get("supporting_skills"), field_name="matching.supporting_skills"
        ),
        learning_targets=_string_list(
            matching_raw.get("learning_targets"), field_name="matching.learning_targets"
        ),
        unverified_skills=_string_list(
            matching_raw.get("unverified_skills"), field_name="matching.unverified_skills"
        ),
    )
    normalized_profile = {
        "degree": _optional_string(raw.get("degree"), field_name="degree"),
        "job_type": _optional_string(raw.get("job_type"), field_name="job_type"),
        "direction": _optional_string(raw.get("direction"), field_name="direction"),
        "skills": _string_list(raw.get("skills"), field_name="skills"),
        "matching": matching.model_dump(mode="json"),
    }
    return CandidateProfile(
        **normalized_profile,
        source_ref=resolved.as_posix(),
        content_hash=_canonical_hash(normalized_profile),
    )


__all__ = ["CandidateProfileError", "load_candidate_profile"]
