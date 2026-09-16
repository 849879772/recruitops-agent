from __future__ import annotations

from pathlib import Path

import pytest

from packages.candidate_profile import (
    CandidateProfileError,
    CandidateProfileProvider,
    build_scoring_context,
    load_candidate_profile,
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_profile_loads_only_allowlisted_structured_fields(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
profile:
  degree: 研究生
  job_type: 校招
  direction: 机器人开发
  skills: [C++, ROS, C++]
  ignored_secret: do-not-load
  matching:
    direction_policy: parallel
    primary_directions: [机械臂开发, Agent开发]
    project_evidence: [机械臂 VLA 微调]
    supporting_skills: [Linux]
    learning_targets: [ROS2]
    unverified_skills: [WBC]
deepseek:
  api_key: secret
""".strip(),
    )

    profile = load_candidate_profile(path)

    assert profile.skills == ["C++", "ROS"]
    assert profile.matching.project_evidence == ["机械臂 VLA 微调"]
    assert "secret" not in profile.model_dump_json()
    assert len(profile.content_hash) == 64


def test_scoring_context_excludes_learning_and_unverified_skills_from_evidence(
    tmp_path: Path,
) -> None:
    path = _write(
        tmp_path / "config.yaml",
        """
profile:
  skills: [C++, ROS]
  matching:
    primary_directions: [机器人开发]
    project_evidence: [机械臂控制项目]
    supporting_skills: [Linux]
    learning_targets: [ROS2]
    unverified_skills: [WBC]
""".strip(),
    )

    context = build_scoring_context(load_candidate_profile(path))
    evidence = {item.text for item in context.evidence}

    assert evidence == {"C++", "ROS", "机械臂控制项目", "Linux"}
    assert "ROS2" not in evidence
    assert "WBC" not in evidence
    assert context.learning_targets == ["ROS2"]
    assert context.excluded_unverified_skills == ["WBC"]
    assert all(item.source_ref.startswith(path.resolve().as_posix()) for item in context.evidence)


def test_profile_hash_is_stable_for_irrelevant_top_level_changes(tmp_path: Path) -> None:
    first = _write(
        tmp_path / "first.yaml",
        "profile: {skills: [C++], matching: {primary_directions: [开发]}}\nother: 1\n",
    )
    second = _write(
        tmp_path / "second.yaml",
        "profile: {skills: [C++], matching: {primary_directions: [开发]}}\nother: 2\n",
    )

    assert load_candidate_profile(first).content_hash == load_candidate_profile(second).content_hash


def test_provider_reloads_after_profile_source_changes(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", "profile: {skills: [C++]}\n")
    provider = CandidateProfileProvider(path)

    first = provider.scoring_context()
    _write(path, "profile: {skills: [Python, ROS]}\n")
    second = provider.scoring_context()

    assert first.profile_hash != second.profile_hash
    assert {item.text for item in second.evidence} == {"Python", "ROS"}


@pytest.mark.parametrize(
    "content",
    (
        "companies: []",
        "profile: []",
        "profile: {skills: C++}",
        "profile: {matching: {direction_policy: random}}",
    ),
)
def test_invalid_profile_fails_closed(tmp_path: Path, content: str) -> None:
    with pytest.raises(CandidateProfileError):
        load_candidate_profile(_write(tmp_path / "config.yaml", content))
