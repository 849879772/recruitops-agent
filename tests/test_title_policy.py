from __future__ import annotations

import pytest

from packages.matching.models import AnalysisStatus, Direction
from packages.matching.title_policy import (
    company_title_key,
    normalize_job_title_key,
    screen_title_job,
)


def test_title_key_normalization_only_collapses_whitespace() -> None:
    assert normalize_job_title_key("  C++\n  Software\tEngineer  ") == "C++ Software Engineer"
    assert normalize_job_title_key("c++ Software Engineer") == "c++ Software Engineer"
    assert normalize_job_title_key(None) == ""


def test_company_title_key_keeps_company_and_title_as_exact_components() -> None:
    assert company_title_key(" company-a ", " C++  Engineer ") == (
        "company-a",
        "C++ Engineer",
    )
    assert company_title_key("company-a", "C++ Engineer") != company_title_key(
        "company-b", "C++ Engineer"
    )
    assert company_title_key("company-a", "C++ Engineer") != company_title_key(
        "company-a", "c++ Engineer"
    )


@pytest.mark.parametrize(
    ("title", "direction"),
    [
        ("C++软件开发工程师", Direction.CPP_SOFTWARE),
        ("ROS机器人软件工程师", Direction.ROBOT_ARM),
        ("具身智能算法工程师", Direction.EMBODIED_LEARNING),
        ("Agent研发工程师", Direction.LLM_AGENT),
        ("AI应用开发工程师", Direction.LLM_AGENT),
    ],
)
def test_title_keywords_admit_one_configured_direction_without_jd(
    title: str, direction: Direction
) -> None:
    result = screen_title_job({"title": title, "jd_raw": "完全无关的正文"})

    assert result.eligible is True
    assert result.analysis_status is AnalysisStatus.ELIGIBLE
    assert direction in result.matched_directions
    assert result.primary_match_direction is direction


def test_generic_software_engineer_title_is_eligible() -> None:
    result = screen_title_job({"title": "软件工程师", "jd_raw": ""})
    assert result.eligible is True
    assert result.primary_match_direction is Direction.CPP_SOFTWARE
    assert all(item.source == "title" for item in result.evidence)


def test_title_screen_does_not_use_jd_or_other_job_fields() -> None:
    target = screen_title_job(
        {
            "title": "C++软件开发工程师",
            "jd_raw": "产品运营，不涉及任何研发工作",
            "job_type": "实习",
            "cohort": 2026,
        }
    )
    unknown = screen_title_job(
        {
            "title": "客户运营专员",
            "jd_raw": "负责 C++ 软件开发、Linux 和机器人系统",
            "job_type": "校园招聘",
            "cohort": 2027,
        }
    )

    assert target.eligible is False
    assert target.analysis_status is AnalysisStatus.INTERNSHIP
    assert unknown.eligible is False
    assert unknown.analysis_status is AnalysisStatus.DIRECTION_OUT


def test_profile_direction_configuration_narrows_title_matches() -> None:
    profile = {"matching": {"primary_directions": ["Agent开发"]}}

    assert screen_title_job({"title": "Agent研发工程师"}, profile).eligible is True
    result = screen_title_job({"title": "C++软件开发工程师"}, profile)
    assert result.eligible is False
    assert result.analysis_status is AnalysisStatus.DIRECTION_OUT


@pytest.mark.parametrize(
    ("title", "status", "eligible"),
    [
        ("Agent实习生", AnalysisStatus.INTERNSHIP, False),
        ("Agent博士研究员", AnalysisStatus.DOCTORATE_ONLY, False),
        ("Agent博士优先", AnalysisStatus.ELIGIBLE, True),
        ("Agent硕博均可", AnalysisStatus.ELIGIBLE, True),
        ("Agent硕士/博士", AnalysisStatus.ELIGIBLE, True),
        ("Agent PhD preferred", AnalysisStatus.ELIGIBLE, True),
    ],
)
def test_title_only_internship_and_doctorate_gates(
    title: str, status: AnalysisStatus, eligible: bool
) -> None:
    result = screen_title_job({"title": title, "jd_raw": ""})

    assert result.analysis_status is status
    assert result.eligible is eligible


@pytest.mark.parametrize(
    "title", ["Detail Engineer", "Tailor Engineer", "Management Engineer"]
)
def test_english_keywords_use_word_boundaries(title: str) -> None:
    result = screen_title_job({"title": title})

    assert result.eligible is False
    assert result.analysis_status is AnalysisStatus.DIRECTION_OUT


def test_ai_is_a_target_token_but_not_an_ascii_substring() -> None:
    assert screen_title_job({"title": "AI Platform Engineer"}).eligible is True
    assert screen_title_job({"title": "Tailor Platform Engineer"}).eligible is False


def test_unknown_title_is_excluded_even_when_jd_contains_target_terms() -> None:
    result = screen_title_job(
        {
            "title": "行政专员",
            "jd_raw": "负责 C++ 软件开发、ROS 机器人和 RAG 应用",
        }
    )

    assert result.eligible is False
    assert result.reasons == [AnalysisStatus.DIRECTION_OUT.value]
