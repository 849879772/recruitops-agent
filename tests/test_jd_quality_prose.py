from __future__ import annotations

import pytest

from packages.matching.jd_quality import assess_jd_quality


@pytest.mark.parametrize(
    "intro",
    [
        "团队每周固定进行技术分享，氛围开放。",
        "团队同事会分享项目经验，帮助新人快速成长。",
        "分享 Python 技术实践，促进工程成长。",
        "首页推荐是业务的核心场景。",
        "系统根据异常类型返回错误信息。",
    ],
)
def test_prose_navigation_words_do_not_truncate_real_duties(intro: str) -> None:
    jd = (
        f"岗位职责\n{intro}\n"
        "构建并优化模型，精准识别服务质量波动并实现自动化处理方案。\n"
        "任职要求\n统招硕士及以上学历，熟悉 Python、PyTorch 和多模态模型。"
    )
    assert assess_jd_quality(jd).complete


def test_research_with_concrete_object_is_a_duty() -> None:
    jd = (
        "岗位职责\n团队由高校专家组成，在学术期刊发表多篇论文。\n"
        "围绕业务场景，研究大模型与推荐结合提升业务效果并落地。\n"
        "任职要求\n硕士及以上学历，具备 Python、C++ 编程基础，熟悉 PyTorch。"
    )
    assert assess_jd_quality(jd).complete


@pytest.mark.parametrize("text", ["研究生学历", "研究能力强", "研究院团队", "研究经验丰富"])
def test_research_background_is_not_a_work_duty(text: str) -> None:
    jd = f"岗位职责\n{text}\n任职要求\n熟悉 Python、C++。"
    assert assess_jd_quality(jd).reason_code == "duty_evidence_missing"


@pytest.mark.parametrize("footer", ["分享\n", "分享 | 收藏\n", "工作地点：上海\n", "申请职位\n"])
def test_actual_footer_still_blocks_unrelated_qualification(footer: str) -> None:
    jd = f"岗位职责\n负责 C++ 软件开发。\n任职要求\n{footer}熟悉 Python。"
    assert not assess_jd_quality(jd).complete


def test_nested_duties_and_related_experience_do_not_imply_multiple_jobs() -> None:
    jd = (
        "岗位职责\n参与智能座舱算法研发，具体工作内容包括但不限于：\n"
        "构建评测 Agent，设计用户行为仿真系统。\n"
        "任职要求\n熟悉 Python 和 PyTorch，具备模型微调经验。\n"
        "加分项\n有 AI 相关岗位实习经验。"
    )
    assert assess_jd_quality(jd).complete


def test_prose_requirement_word_does_not_create_a_second_complete_block() -> None:
    jd = (
        "岗位职责\n参与机器人测试工艺。我们不要求应届生一开始就懂量产。\n"
        "工作内容\n参与自动化测试开发并维护测试平台。\n"
        "任职要求\n熟悉 Python，具备机器人工程背景。"
    )
    assert assess_jd_quality(jd).complete


def test_ats_wrapper_preserves_nested_basic_qualifications() -> None:
    jd = (
        "岗位职责\n优化计算效率，支持模型研发。\n"
        "任职要求\n【主要职责】\n负责训练平台研发。\n"
        "【基本资格】\n熟悉 Linux，掌握 Python，具备项目经验。\n"
        "【期望资格】\n有大规模 GPU 集群经验。"
    )
    assert assess_jd_quality(jd).complete


def test_two_different_self_contained_jobs_are_still_rejected() -> None:
    jd = (
        "岗位职责\n负责 Python 数据平台开发。任职要求\n熟悉 SQL。\n"
        "岗位职责\n负责 C++ 内核维护。任职要求\n熟悉 Linux。"
    )
    assert assess_jd_quality(jd).reason_code == "cross_job_content"


@pytest.mark.parametrize("footer", [
    "2027届-秋招允许投递3次,请选择适合的职位进行投递 立即投递",
    "允许6个月内投递2个职位,请选择适合的职位进行投递 立即投递",
    "允许一年内投递5个职位,请选择适合的职位进行投递 立即投递",
])
def test_repeated_description_with_quota_is_one_job(footer: str) -> None:
    block = (
        "岗位职责：参与 Agent 研发，参与技术分享。\n"
        "任职要求：熟悉 Python，具备模型应用经验，善于分享。\n"
    )
    assert assess_jd_quality(block + block + footer).complete


def test_observed_transsion_structured_activity_items_are_duty_evidence() -> None:
    jd = (
        "岗位职责\n"
        "店铺运营：协助 Amazon / Mercado Libre / Flipkart / Shopee / TikTok Shop 等海外平台店铺日常运营（产品上架 / 详情页 / 关键词 / 评价管理）\n"
        "数据监控：建立周度销售 / 流量 / 转化 / 广告 ROI 看板，输出运营复盘\n"
        "用户与评价运营：监控产品评分、差评回复、退货原因分析，输出产品改进建议\n"
        "选品调研：用 AI 工具挖掘新品机会、监控趋势品、分析竞品定价与销售\n"
        "AI 提效：用 AI 工具批量生成 listing 文案、客服回复、广告关键词素材、视觉素材、AIGC 视频。\n"
        "任职要求\n"
        "熟练使用至少 3 个主流大模型。\n"
        "能用 AI 做信息检索、长文摘要、报告生成。\n"
        "熟悉 Prompt Engineering 基本原则。"
    )
    assert assess_jd_quality(jd).complete


def test_existing_duty_semantics_run_before_structured_fallback() -> None:
    jd = (
        "岗位职责\n开发 C++ 系统。测试 ROS 节点。\n"
        "任职要求\n本科及以上学历，熟悉 C++、ROS。"
    )
    assert assess_jd_quality(jd).complete


def test_observed_haid_activity_noun_list_is_not_only_a_technical_stack() -> None:
    jd = "岗位职责\n水质监测、PCR监测、微生物监测、病毒检测\n任职要求\n本科及以上学历\n水产相关专业"
    assert assess_jd_quality(jd).complete


def test_structured_major_and_english_certificate_are_two_requirement_dimensions() -> None:
    jd = (
        "岗位职责\n负责自动化控制系统设计、开发和测试。\n"
        "任职要求\n自动化、电气工程、控制工程等相关专业。\n英语CET-6"
    )
    assert assess_jd_quality(jd).complete


@pytest.mark.parametrize("requirement", ["专业能力；具备经验", "专业素养；具备经验", "专业技能；具备经验"])
def test_generic_professional_words_do_not_count_as_a_major(requirement: str) -> None:
    jd = f"岗位职责\n负责产品开发和测试。\n任职要求\n{requirement}"
    assert assess_jd_quality(jd).complete is False


@pytest.mark.parametrize(
    "jd",
    [
        "岗位职责\n薄膜工艺控制\n任职要求\n化工相关专业",
        "岗位职责\n过程管控&客诉处理\n任职要求\n工科类专业",
        "岗位职责\n算法工程师、测试工程师、开发工程师\n任职要求\n本科及以上学历",
        "岗位职责\nC++、Python、Linux\n任职要求\n本科及以上学历",
        "岗位职责\n负责产品颜色开发工作\n任职要求\n轻化工程、高分子材料等相关专业",
        "岗位职责\n投产期间负责原辅材料要料；投产后负责日常计划下发\n"
        "任职要求\n高分子、化学、工科类专业",
    ],
)
def test_structured_fallback_rejects_sparse_or_non_activity_lists(jd: str) -> None:
    assert assess_jd_quality(jd).complete is False


def test_officially_reversed_sections_are_not_silently_repaired() -> None:
    jd = (
        "岗位职责\n"
        "1. 统招本科及以上学历，计算机类、电子类、通信类相关专业优先\n"
        "2. 熟悉软件项目管理流程，有软件产品规划相关经历或证书者优先\n"
        "3. 具备较强的产品思维和数据分析能力\n"
        "任职要求\n"
        "1. 负责产品需求管理，收集、分析并梳理业务需求，输出需求文档\n"
        "2. 参与产品规划与迭代，跟踪上线效果并持续优化\n"
        "3. 对项目交付负责，管理进度、风险和质量"
    )
    quality = assess_jd_quality(jd)
    assert quality.complete is False
    assert quality.reason_code in {"duty_evidence_missing", "requirement_evidence_missing"}
