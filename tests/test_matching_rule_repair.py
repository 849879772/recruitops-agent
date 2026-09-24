from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

import pytest

from packages.matching.jd_quality import assess_jd_quality
from packages.matching.models import (
    AnalysisStatus,
    DecisionAction,
    DeepSeekResponse,
    Direction,
)
from packages.matching.rules import (
    canonical_direction,
    classify_job_directions,
    is_jd_incomplete,
    requested_directions,
    screen_job,
)
from packages.matching.service import MatchingService


COMPOSITE_DIRECTIONS = "C++ 软件开发 / Linux系统软件 / Qt客户端 / ROS机器人软件 / 测试开发"


def _profile() -> dict[str, Any]:
    return {
        "matching": {
            "direction_policy": "parallel",
            "primary_directions": [
                COMPOSITE_DIRECTIONS,
                "机械臂与机器人开发 / 运动控制",
                "具身智能",
                "大模型与Agent开发",
            ],
            "project_evidence": ["C++机器人软件项目", "Agent/RAG项目"],
            "supporting_skills": ["Linux", "ROS2"],
        }
    }


def _complete_jd(extra: str = "") -> str:
    return (
        "岗位职责：负责研发和维护核心系统，参与方案设计、编码、测试和线上问题定位。"
        "参与持续集成、故障定位和性能优化。任职要求：熟悉 C++、Linux，具备良好的编程基础、"
        "工程实践能力和团队协作能力。"
        f"{extra}"
    )


def _job(
    *, title: str = "C++软件开发工程师", jd_raw: str | None = None, **overrides: Any
) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": "rule-repair-job",
        "title": title,
        "job_type": "校园招聘",
        "batch": "formal",
        "cohort": 2027,
        "cohort_status": "confirmed",
        "jd_raw": jd_raw if jd_raw is not None else _complete_jd(),
    }
    job.update(overrides)
    job.setdefault(
        "capture_evidence",
        {
            "status": "complete",
            "method": "test_fixture",
            "source_url": "https://example.test/jobs/rule-repair",
            "identity_verified": True,
            "terminal_observed": True,
            "remaining_controls": [],
            "content_sha256": sha256(job["jd_raw"].encode("utf-8")).hexdigest(),
        },
    )
    return job


def test_composite_profile_directions_keep_cxx_and_four_parallel_targets() -> None:
    assert canonical_direction(COMPOSITE_DIRECTIONS) is Direction.CPP_SOFTWARE
    assert requested_directions(_profile()) == [
        Direction.CPP_SOFTWARE,
        Direction.ROBOT_ARM,
        Direction.EMBODIED_LEARNING,
        Direction.LLM_AGENT,
    ]
    assert canonical_direction("C++") is Direction.CPP_SOFTWARE


@pytest.mark.parametrize(
    "requirement",
    [
        "具备良好的沟通协作意识，能保证稳定的实习投入时间。",
        "6、能够确保持续的实习出勤。",
        "须保障稳定实习到岗。",
    ],
)
def test_current_internship_commitment_excludes_formal_titled_job(requirement: str) -> None:
    from packages.matching.review_import import _non_score_status

    screening = screen_job(_job(jd_raw=_complete_jd(requirement)), _profile())

    assert screening.analysis_status is AnalysisStatus.INTERNSHIP
    assert not screening.eligible
    assert _non_score_status("exclude", screening) == "internship"


@pytest.mark.parametrize(
    "requirement",
    [
        "有相关实习经历者优先。",
        "实习经历中能保证稳定的系统运行者优先。",
        "不要求保证稳定的实习投入时间。",
        "无需保证稳定的实习投入时间。",
        "能够保证稳定的全职投入时间。",
    ],
)
def test_past_or_negated_internship_commitment_keeps_formal_job(requirement: str) -> None:
    screening = screen_job(_job(jd_raw=_complete_jd(requirement)), _profile())

    assert screening.eligible


@pytest.mark.parametrize(
    ("title", "jd_raw"),
    [
        (
            "中台产品经理",
            _complete_jd("加分项：有大模型产品化经验。"),
        ),
        (
            "机器人产品运营",
            _complete_jd("加分项：有机器人行业经验。"),
        ),
        (
            "机器人销售工程师",
            _complete_jd("加分项：熟悉 ROS 和机器人产品。"),
        ),
        (
            "内容运营专员",
            _complete_jd("加分项：有大模型应用经验。"),
        ),
    ],
)
def test_non_technical_roles_do_not_match_from_one_bonus_keyword(
    title: str, jd_raw: str
) -> None:
    classification = classify_job_directions(_job(title=title, jd_raw=jd_raw))
    screening = screen_job(_job(title=title, jd_raw=jd_raw), _profile())

    assert classification.matched_directions == []
    assert screening.analysis_status is AnalysisStatus.DIRECTION_OUT
    assert any(item.signal == "non_technical_role" for item in screening.evidence)


@pytest.mark.parametrize(
    ("title", "jd_raw", "expected"),
    [
        (
            "内容理解算法工程师",
            _complete_jd("负责大模型内容理解算法研发、训练和优化。"),
            Direction.LLM_AGENT,
        ),
        (
            "供应链优化算法工程师",
            _complete_jd("负责模型训练和供应链优化算法研发。"),
            Direction.LLM_AGENT,
        ),
        (
            "物流机器人软件工程师",
            _complete_jd("负责机器人软件开发和系统调试，使用 C++、Linux。"),
            Direction.ROBOT_ARM,
        ),
        (
            "市场营销算法工程师",
            _complete_jd("负责大模型推荐算法研发和模型训练。"),
            Direction.LLM_AGENT,
        ),
    ],
)
def test_explicit_technical_role_survives_business_domain_words(
    title: str, jd_raw: str, expected: Direction
) -> None:
    classification = classify_job_directions(_job(title=title, jd_raw=jd_raw))

    assert expected in classification.matched_directions


def test_exact_500_quality_does_not_override_complete_capture_evidence() -> None:
    source = (
        "岗位职责："
        + "负责核心软件研发、测试和维护。" * 12
        + "任职要求："
        + "熟悉 C++、Linux，具备工程实践能力和团队协作能力。" * 20
    )
    truncated = source[:499] + "、"
    complete = source[:499] + "。"

    truncated_quality = assess_jd_quality(_job(jd_raw=truncated))
    complete_quality = assess_jd_quality(_job(jd_raw=complete))

    assert len(truncated) == 500
    assert truncated_quality.reason_code == "truncated_at_500"
    assert is_jd_incomplete(_job(jd_raw=truncated)) is False
    assert len(complete) == 500
    assert complete_quality.complete is True
    assert is_jd_incomplete(_job(jd_raw=complete)) is False


def test_duties_only_and_navigation_shell_are_rejected_with_specific_reasons() -> None:
    duties_only = "岗位职责：" + "负责核心软件研发、测试和维护，参与系统优化。" * 20
    navigation = "首页 招聘职位 全部职位 搜索 筛选 工作地点 申请职位 收藏 分享 下一页"

    duties_quality = assess_jd_quality(_job(jd_raw=duties_only))
    navigation_quality = assess_jd_quality(_job(jd_raw=navigation))

    assert duties_quality.reason_code == "duties_without_requirements"
    assert navigation_quality.reason_code == "navigation_or_list_shell"


def test_untitled_structured_jd_with_duties_and_requirements_is_complete() -> None:
    text = (
        "1. 负责机器人控制软件的模块设计、核心代码开发与持续维护，跟进线上问题并推动稳定性优化；"
        "2. 参与传感器、执行器和上层规划模块的接口设计，完成联调测试、性能分析及故障定位；"
        "3. 构建自动化测试与发布流程，沉淀可复用工具，支持产品在不同硬件平台上的部署；"
        "4. 与算法、硬件和产品团队协作，评审技术方案，推进关键功能按计划交付并持续改进；"
        "5. 熟悉 C++、Python、Linux 和常用数据结构，具备良好的编码规范与工程实践能力；"
        "6. 能够阅读英文技术资料，掌握多线程或网络编程，有机器人项目经验者优先，并具备清晰沟通能力。"
    )

    quality = assess_jd_quality(_job(title="机器人软件工程师", jd_raw=text))

    assert quality.complete is True
    assert is_jd_incomplete(_job(title="机器人软件工程师", jd_raw=text)) is False


def test_identical_repeated_jd_blocks_are_deduplicated_for_quality() -> None:
    block = (
        "岗位名称：Linux内核工程师 "
        "岗位职责：负责 Linux 内核模块开发、维护和性能优化。"
        "任职要求：熟悉 C++、Linux，具备系统软件开发经验。"
    )
    raw = "滴滴27届秋招-自动驾驶-" + block + block + "申请职位 分享 收藏"

    quality = assess_jd_quality(_job(title="Linux内核工程师", jd_raw=raw))

    assert quality.complete is True


def test_same_nested_job_identity_passes_but_different_job_blocks_are_rejected() -> None:
    same_job = (
        "职位名称：Linux内核工程师 岗位职责：负责内核开发和维护。"
        "任职要求：熟悉 C++、Linux。"
        "职位名称：Linux内核工程师 岗位职责：负责内核开发和维护。"
        "任职要求：熟悉 C++、Linux。"
    )
    different_jobs = (
        "职位名称：Linux内核工程师 岗位职责：负责内核开发和维护。"
        "任职要求：熟悉 C++、Linux。"
        "职位名称：视觉算法工程师 岗位职责：负责视觉算法研发。"
        "任职要求：熟悉 Python、视觉算法。"
    )

    assert assess_jd_quality(_job(jd_raw=same_job)).complete is True
    different_quality = assess_jd_quality(_job(jd_raw=different_jobs))
    assert different_quality.reason_code == "cross_job_content"


@pytest.mark.parametrize(
    "text",
    [
        "职位描述：负责 Linux 平台 C++ 软件模块设计、开发和自动化测试。"
        "任职要求：熟悉 C++、多线程、数据结构和软件工程实践，有完整项目经验。",
        "岗位职责：负责 C++ 服务开发和故障定位。任职要求：熟悉 Linux 和网络编程。",
        "任职要求：掌握 C++ 和多线程编程。岗位职责：负责机器人软件开发与联调。",
        "职位描述\n岗位职责：负责 C++ 服务开发和维护。\n任职要求：熟悉 Linux。",
    ],
)
def test_complete_short_jd_uses_section_evidence_instead_of_total_length(text: str) -> None:
    assert len(text) < 80
    assert assess_jd_quality(_job(jd_raw=text)).complete is True


@pytest.mark.parametrize(
    "text",
    [
        "Responsibilities: design and maintain C++ services, write unit and integration tests, "
        "investigate production issues, and collaborate with robotics engineers. "
        "Requirements: master's degree, strong C++ and Linux experience, familiarity with ROS, "
        "networking, algorithms, continuous integration, and clear technical communication.",
        "Responsibilities: Develop C++ services. Requirements: Proficient in Linux.",
        "Job Description\nResponsibilities\nBuild and test robot controllers.\n"
        "Qualifications\nExperience developing C++ software on Linux.",
        "Responsibilities: Optimize inference services. Requirements: Knowledge of CUDA.",
        "Responsibilities: Maintain software services. Qualifications: Bachelor's degree in CS.",
    ],
)
def test_english_jd_requires_duty_and_qualification_semantics_in_their_sections(text: str) -> None:
    assert assess_jd_quality(_job(jd_raw=text)).complete is True


@pytest.mark.parametrize(
    "text",
    [
        "岗位职责 任职要求：熟悉 C++，负责软件研发、测试和部署。",
        "岗位职责与任职要求：" + "负责算法研发、测试、部署和持续优化。" * 20,
        "岗位职责：负责开发具备高可用能力的软件系统。任职要求：",
        "岗位职责：负责软件开发和维护。任职要求：负责提升用户体验与系统能力。",
        "岗位职责：负责软件开发和维护。任职要求：参与开发具备高可用能力的系统。",
        "岗位职责：熟悉 C++，具备 Linux 开发经验。任职要求：掌握网络编程。",
        "岗位职责：任职要求：具备软件开发经验，熟悉 C++ 和 Linux。",
        "岗位职责：负责软件开发。任职要求：加分项：熟悉 C++ 和 Linux。",
        "岗位职责：负责软件开发。任职要求：工作地点：上海 收藏 分享 熟悉 C++。",
        "Responsibilities Requirements: Experience developing C++ services on Linux.",
        "Responsibilities: Develop C++ services with users. Requirements:",
        "Responsibilities: Maintain software services. Requirements: Improve user experience.",
        "Responsibilities: Experience developing C++ software. Requirements: Knowledge of Linux.",
        "Responsibilities: Develop C++ services. Requirements: Preferred qualifications: Linux experience.",
        "岗位职责：负责。任职要求：熟悉。",
        "Responsibilities: Develop. Requirements: Proficient in.",
    ],
)
def test_empty_sections_and_duties_disguised_as_requirements_are_incomplete(text: str) -> None:
    quality = assess_jd_quality(_job(jd_raw=text))
    assert quality.complete is False
    assert quality.reason_code in {
        "empty_detail_section", "duty_evidence_missing", "requirement_evidence_missing",
    }
    assert quality.reason


def test_describing_a_jd_is_not_evidence_of_one() -> None:
    text = "A complete job description with responsibilities and requirements."
    assert assess_jd_quality(text).complete is False


def test_repeated_nested_jd_sections_keep_original_audit_text() -> None:
    block = (
        "职位描述\n岗位职责：负责 Linux 内核模块开发和故障定位。\n"
        "任职要求：掌握 C++，具有 Linux 内核开发经验。\n"
    )
    job = _job(jd_raw=block + block + "申请职位 分享 收藏")
    before = dict(job)
    quality = assess_jd_quality(job)
    assert quality.complete is True
    assert quality.raw_length == len(job["jd_raw"])
    assert job == before


def test_application_quota_footer_does_not_make_identical_jds_conflict() -> None:
    block = (
        "岗位职责：参与 Agent 和 RAG 系统的自动化测试工具开发。\n"
        "任职资格：软件工程专业毕业，熟悉 Python，具备接口测试经验。 "
    )
    job = _job(jd_raw=block + block + "2027届正式批允许投递3次,请选择适合的职位进行投递 立即投递")
    assert assess_jd_quality(job).complete is True
    other = block.replace("Agent 和 RAG 系统", "Linux 内核模块")
    assert assess_jd_quality(block + other + "立即投递").reason_code == "cross_job_content"


_SHANCHUAN_SOFTWARE_TESTING_BLOCK = (
    "【岗位职责】\n"
    "1.负责机器人软件系统(包括嵌入式软件、服务端、APP)的需求分析,理解产品逻辑和实现原理,构造测试环境。\n"
    "2.负责机器人软件系统(包括嵌入式软件、服务端、APP)的测试用例设计和执行,包括功能测试、性能测试、接口测试、稳定性测试、专项测试等。\n"
    "3.根据测试计划执行测试任务,记录测试中所发现的问题,与开发人员进行沟通,协助修复问题,跟踪问题直至关闭。\n"
    "4.对测试数据、测试结果进行分析统计,推导测试结论,编写测试报告。\n"
    "5.针对工作流程中的问题,提出产品需求、测试用例、流程规范等方面的改进建议,持续提升测试质量和效率。\n"
    "6.针对工作流程中的经验教训,进行总结并形成文档,在团队内进行分享。\n"
    "7.按需配合公司其它部门的工作。\n\n"
    "【任职要求】\n"
    "1.本科及以上学历,2027届应届毕业生,计算机、电子信息、软件工程、机械工程相关专业优先。\n"
    "2.希望在测试领域深耕发展,学习过测试相关书籍课程,了解测试基础理论、测试用例设计方法。\n"
    "3.熟悉Android、iOS操作系统,熟悉ADB工具、抓包工具、数据库操作者优先考虑。\n"
    "4.具备自动化测试脚本开发经验者优先考虑。\n"
    "5.思维逻辑清晰,工作认真负责,良好的沟通能力和团队合作意识,抗压能力强。"
)


@pytest.mark.parametrize(
    "label",
    ["应届生杉尖计划-", "应届生杉尖计划—", "校招专项计划："],
)
def test_shanchuan_quota_label_is_removed_from_repeated_same_jd(label: str) -> None:
    footer = f"{label}2027届允许投递3次,请选择适合的职位进行投递 立即投递"
    assert assess_jd_quality(_SHANCHUAN_SOFTWARE_TESTING_BLOCK * 2 + footer).complete


def test_shanchuan_repeated_footer_fix_keeps_distinct_jobs_rejected() -> None:
    footer = "应届生杉尖计划-2027届允许投递3次,请选择适合的职位进行投递 立即投递"
    different = _SHANCHUAN_SOFTWARE_TESTING_BLOCK.replace("需求分析", "硬件需求分析", 1)
    quality = assess_jd_quality(_SHANCHUAN_SOFTWARE_TESTING_BLOCK + different + footer)
    assert quality.reason_code == "cross_job_content"


def test_single_heading_with_complete_unlabelled_requirements_is_valid() -> None:
    raw = (
        "【职位描述】\n"
        "1. 参与灵巧手电机控制算法开发，包括电流环、速度环、位置环与力矩控制。\n"
        "2. 参与驱动器调试、传感器标定、控制参数整定和运动性能优化。\n"
        "3. 参与精细操作场景中的控制效果验证，分析抖动、跟随误差和力控稳定性。\n"
        "4. 与硬件、嵌入式和测试团队协作，推动执行器控制链路稳定落地。\n"
        "1. 2027届硕士研究生及以上学历，控制、自动化或机器人工程专业。\n"
        "2. 熟悉电机控制基础，理解 FOC、PID、编码器和电流采样基本原理。\n"
        "3. 具备 C/C++ 开发能力，了解 MCU、驱动器及实时控制系统。\n"
        "4. 有机器人关节、无人车或硬件调试项目经验者优先。"
    )
    assert assess_jd_quality(raw).complete is True
    duties_only = raw.split("1. 2027届")[0] * 2
    assert assess_jd_quality(duties_only).complete is False
    assert assess_jd_quality(duties_only + "2027届应届生可投递。").complete is False


def test_exploration_is_a_real_duty_only_when_it_has_an_object() -> None:
    assert assess_jd_quality(
        "岗位职责：探索多模态大模型与推荐算法结合的技术。任职要求：熟悉 Python 和模型训练。"
    ).complete is True
    assert assess_jd_quality(
        "岗位职责：探索。任职要求：熟悉 Python 和模型训练。"
    ).complete is False


class _FakeMatchingClient:
    model = "test-matching-client"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, **_kwargs: Any) -> DeepSeekResponse:
        self.calls += 1
        return DeepSeekResponse(
            content=json.dumps(
                {
                    "matched_directions": ["cpp_software"],
                    "primary_match_direction": "cpp_software",
                    "score_breakdown": {
                        "core_direction": 25,
                        "required_skills": 25,
                        "project_evidence": 20,
                        "engineering_stack": 10,
                    },
                    "evidence_level": "partial",
                    "evidence": [
                        {
                            "jd_requirement": "C++ 软件开发",
                            "profile_evidence": "C++ 软件项目",
                            "relation": "direct",
                            "requirement_type": "core",
                        }
                    ],
                    "summary": "提前批岗位通过确定性规则并进入分析。",
                    "missing_core_requirements": [],
                    "advantages": [],
                    "gaps": [],
                },
                ensure_ascii=False,
            ),
            model=self.model,
            input_tokens=1,
            output_tokens=1,
        )


def test_confirmed_2027_early_batch_enters_scoring() -> None:
    client = _FakeMatchingClient()
    outcome = MatchingService(client).analyze(
        _job(title="27届提前批-C++应用开发工程师", batch="early"),
        _profile(),
    )

    assert outcome.decision.action is DecisionAction.ANALYZE
    assert outcome.result.analysis_status is AnalysisStatus.COMPLETE
    assert client.calls == 1
    assert any(
        item.signal == "early_batch_allowed" for item in outcome.result.screening_evidence
    )
