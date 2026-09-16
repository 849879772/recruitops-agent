from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from packages.recruitment_core.jd_capture import assess_jd_capture

from .models import (
    AnalysisStatus,
    Direction,
    DIRECTION_LABELS,
    ScreeningEvidence,
    ScreeningResult,
)
from .rules import DEFAULT_DIRECTIONS, requested_directions


def normalize_job_title_key(title: Any) -> str:
    """Normalize only surrounding and repeated whitespace in a job title."""
    if title is None:
        return ""
    return " ".join(str(title).split())


def company_title_key(company_id: Any, title: Any) -> tuple[str, str]:
    """Return the exact same-company/title identity used by title-first capture."""
    return normalize_job_title_key(company_id), normalize_job_title_key(title)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def stored_detail_retry_required(job: Any) -> bool:
    """Retry rediscovered missing/failed details, not legacy metadata gaps."""
    if not str(_field(job, "jd_raw") or "").strip():
        return True
    evidence = _field(job, "capture_evidence")
    if evidence:
        return not assess_jd_capture(job).complete
    status = str(_field(job, "capture_status") or "").strip().casefold()
    return status in {"failed", "pending", "incomplete", "error"} or bool(
        str(_field(job, "capture_failure_reason") or "").strip()
    )


def _literal_pattern(value: str) -> re.Pattern[str]:
    """Match a configured keyword without allowing an ASCII word prefix/suffix."""
    escaped = re.escape(value).replace(r"\ ", r"\s+")
    boundary = r"(?<![A-Za-z0-9_])" if value[:1].isascii() and value[:1].isalpha() else ""
    end_boundary = r"(?![A-Za-z0-9_])" if value[-1:].isascii() and value[-1:].isalnum() else ""
    return re.compile(f"{boundary}{escaped}{end_boundary}", re.IGNORECASE)


def _english_pattern(value: str) -> re.Pattern[str]:
    escaped = re.escape(value).replace(r"\ ", r"\s+")
    return re.compile(
        rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])",
        re.IGNORECASE,
    )


_CXX_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])C\s*\+\+(?:\s*\d+)?(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_QT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])Qt(?:\s*\d+)?(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_ROS_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])ROS(?:\s*2)?(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


_DIRECTION_TITLE_PATTERNS: dict[
    Direction, tuple[tuple[str, re.Pattern[str]], ...]
] = {
    Direction.CPP_SOFTWARE: (
        ("C++", _CXX_PATTERN),
        ("C语言", _literal_pattern("C语言")),
        ("CPP", _english_pattern("CPP")),
        ("Qt", _QT_PATTERN),
        ("Linux", _english_pattern("Linux")),
        ("Ubuntu", _english_pattern("Ubuntu")),
        ("软件开发", _literal_pattern("软件开发")),
        ("软件研发", _literal_pattern("软件研发")),
        ("系统软件", _literal_pattern("系统软件")),
        ("客户端", _literal_pattern("客户端")),
        ("服务端", _literal_pattern("服务端")),
        ("测试开发", _literal_pattern("测试开发")),
        ("测开", _literal_pattern("测开")),
        ("SDET", _english_pattern("SDET")),
        ("自动化测试", _literal_pattern("自动化测试")),
        ("软件测试", _literal_pattern("软件测试")),
        ("研发测试", _literal_pattern("研发测试")),
    ),
    Direction.ROBOT_ARM: (
        ("机械臂", _literal_pattern("机械臂")),
        ("机器人", _literal_pattern("机器人")),
        ("robot arm", _english_pattern("robot arm")),
        ("robotics", _english_pattern("robotics")),
        ("ROS", _ROS_PATTERN),
        ("运动控制", _literal_pattern("运动控制")),
        ("运动规划", _literal_pattern("运动规划")),
        ("机器人视觉", _literal_pattern("机器人视觉")),
        ("系统集成", _literal_pattern("系统集成")),
        ("轨迹规划", _literal_pattern("轨迹规划")),
        ("路径规划", _literal_pattern("路径规划")),
        ("SLAM", _english_pattern("SLAM")),
    ),
    Direction.EMBODIED_LEARNING: (
        ("具身", _literal_pattern("具身")),
        ("VLA", _english_pattern("VLA")),
        ("VLM", _english_pattern("VLM")),
        ("embodied", _english_pattern("embodied")),
        ("模仿学习", _literal_pattern("模仿学习")),
        ("行为克隆", _literal_pattern("行为克隆")),
        ("强化学习", _literal_pattern("强化学习")),
        ("imitation learning", _english_pattern("imitation learning")),
        ("reinforcement learning", _english_pattern("reinforcement learning")),
    ),
    Direction.LLM_AGENT: (
        ("大模型", _literal_pattern("大模型")),
        ("语言模型", _literal_pattern("语言模型")),
        ("LLM", _english_pattern("LLM")),
        ("AIGC", _english_pattern("AIGC")),
        ("Agent", _english_pattern("Agent")),
        ("Agents", _english_pattern("Agents")),
        ("Agentic", _english_pattern("Agentic")),
        ("智能体", _literal_pattern("智能体")),
        ("RAG", _english_pattern("RAG")),
        ("RAGFlow", _english_pattern("RAGFlow")),
        ("AI应用", _literal_pattern("AI应用")),
        ("AI", _english_pattern("AI")),
        ("多模态", _literal_pattern("多模态")),
        ("模型部署", _literal_pattern("模型部署")),
        ("模型训练", _literal_pattern("模型训练")),
        ("模型推理", _literal_pattern("模型推理")),
        ("function calling", _english_pattern("function calling")),
        ("tool use", _english_pattern("tool use")),
    ),
}


_INTERNSHIP_TITLE_PATTERN = re.compile(
    r"实习|(?<![A-Za-z0-9_])intern(?:ship|s)?(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_DOCTORATE_MARKER_PATTERN = re.compile(
    r"博士|博后|(?<![A-Za-z0-9_])Ph\.?\s*D\.?(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_DOCTORATE_ALLOWED_PATTERN = re.compile(
    r"硕博(?:均可|可)?|"
    r"博士\s*(?:优先|优先考虑|可优先|preferred)|"
    r"博士\s*[（(]\s*优先\s*[）)]|"
    r"博士\s*(?:或|/|、)\s*(?:硕士|研究生|master(?:'s)?)\s*(?:均可|可)?|"
    r"(?:硕士|研究生|master(?:'s)?)\s*(?:或|/|、)\s*博士\s*(?:均可|可)?|"
    r"Ph\.?\s*D\.?\s*(?:preferred|优先)|"
    r"(?:master(?:'s)?|硕士)\s*(?:or|/)\s*Ph\.?\s*D\.?(?:\s*(?:可|均可))?",
    re.IGNORECASE,
)


def _doctorate_title_reason(title: str) -> str | None:
    residual = _DOCTORATE_ALLOWED_PATTERN.sub(" ", title)
    if _DOCTORATE_MARKER_PATTERN.search(residual):
        return "标题明确限定博士"
    return None


def _matched_directions(
    title: str,
    allowed: list[Direction],
) -> tuple[list[Direction], list[ScreeningEvidence]]:
    matched: list[Direction] = []
    evidence: list[ScreeningEvidence] = []
    for direction in allowed:
        for signal, pattern in _DIRECTION_TITLE_PATTERNS[direction]:
            match = pattern.search(title)
            if match:
                matched.append(direction)
                evidence.append(
                    ScreeningEvidence(
                        source="title",
                        signal=signal,
                        excerpt=match.group(0),
                        reason=DIRECTION_LABELS[direction],
                        direction=direction,
                    )
                )
                break
    if not matched and Direction.CPP_SOFTWARE in allowed:
        generic_software = _literal_pattern("软件工程师").search(title)
        if generic_software:
            matched.append(Direction.CPP_SOFTWARE)
            evidence.append(
                ScreeningEvidence(
                    source="title",
                    signal="软件工程师",
                    excerpt=generic_software.group(0),
                    reason=DIRECTION_LABELS[Direction.CPP_SOFTWARE],
                    direction=Direction.CPP_SOFTWARE,
                )
            )
    return matched, evidence


def screen_title_job(job: Any, profile: Any | None = None) -> ScreeningResult:
    """Screen a job using only its title and the configured target directions."""
    title = normalize_job_title_key(_field(job, "title", ""))
    allowed = list(DEFAULT_DIRECTIONS) if profile is None else requested_directions(profile)
    matched, evidence = _matched_directions(title, allowed)
    matching = _field(profile, "matching", {})
    keywords = _field(matching, "title_keywords", []) or []
    excluded = _field(matching, "excluded_title_keywords", []) or []
    if any(_literal_pattern(word).search(title) for word in excluded):
        return ScreeningResult(eligible=False, analysis_status=AnalysisStatus.DIRECTION_OUT,
                               reasons=["excluded_title_keyword"], evidence=[])

    if _INTERNSHIP_TITLE_PATTERN.search(title):
        evidence.insert(
            0,
            ScreeningEvidence(
                source="title",
                signal="internship_title",
                excerpt=title,
                reason="标题明确标注实习",
            ),
        )
        return ScreeningResult(
            eligible=False,
            analysis_status=AnalysisStatus.INTERNSHIP,
            reasons=[AnalysisStatus.INTERNSHIP.value],
            evidence=evidence,
            matched_directions=matched,
            primary_match_direction=matched[0] if matched else None,
        )

    doctorate = _doctorate_title_reason(title)
    if doctorate:
        evidence.insert(
            0,
            ScreeningEvidence(
                source="title",
                signal="doctorate_title",
                excerpt=title,
                reason=doctorate,
            ),
        )
        return ScreeningResult(
            eligible=False,
            analysis_status=AnalysisStatus.DOCTORATE_ONLY,
            reasons=[AnalysisStatus.DOCTORATE_ONLY.value],
            evidence=evidence,
            matched_directions=matched,
            primary_match_direction=matched[0] if matched else None,
        )

    custom_match = any(_literal_pattern(word).search(title) for word in keywords)
    if (keywords and not custom_match) or (not keywords and not matched):
        evidence.insert(
            0,
            ScreeningEvidence(
                source="title",
                signal="direction_keyword_missing",
                excerpt=title,
                reason="标题未命中配置的目标方向关键词",
            ),
        )
        return ScreeningResult(
            eligible=False,
            analysis_status=AnalysisStatus.DIRECTION_OUT,
            reasons=[AnalysisStatus.DIRECTION_OUT.value],
            evidence=evidence,
        )

    return ScreeningResult(
        eligible=True,
        analysis_status=AnalysisStatus.ELIGIBLE,
        reasons=[],
        evidence=evidence,
        matched_directions=matched,
        primary_match_direction=matched[0] if matched else None,
    )


__all__ = [
    "company_title_key",
    "normalize_job_title_key",
    "screen_title_job",
]
