from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_codex_project_instructions_define_evidence_and_execution_policy() -> None:
    content = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "Write-capable behavior must remain disabled by default" in content
    assert "Never commit resumes, mail bodies, credentials, cookies" in content
    assert "Preserve idempotency, evidence, and checkpoint behavior" in content


def test_codex_project_instructions_define_only_local_task_ids() -> None:
    from apps.api.automation import CodexAutomationExecutor
    from packages.automation import ClaimedAutomation
    from datetime import datetime, timezone

    for task_id in (
        "daily_recruitment_intelligence",
        "crawler_health",
        "application_progress",
        "recruitment_mailbox",
    ):
        task = ClaimedAutomation(
            execution_id="fixture-run", schedule_id="fixture-schedule", task_id=task_id,
            task_label=task_id, target_kind="all", target_id=None, target_label=None,
            scheduled_for=datetime(2026, 9, 18, tzinfo=timezone.utc),
        )
        prompt = CodexAutomationExecutor._prompt(task)
        assert prompt
        if task_id == "application_progress":
            assert "all_non_terminal=true" in prompt
            assert "run_id" in prompt and "scope_complete=true" in prompt
            assert "不得再减 excluded_terminal" in prompt
            assert "excluded_mail_only" in prompt and "不猜测链接" in prompt


def test_runtime_review_instructions_are_idempotent_on_resume() -> None:
    from packages.codex_runtime.instructions import (
        USER_FACING_TASK_OUTPUT_INSTRUCTIONS,
        with_response_language,
    )

    original = {"developerInstructions": "Caller policy"}
    first = with_response_language(original)
    resumed = with_response_language(first)
    assert resumed == first
    assert original == {"developerInstructions": "Caller policy"}
    text = resumed["developerInstructions"]
    assert "默认全程使用简体中文" in text
    assert "all_non_terminal=true" in text and "内部保存的 run_id" in text
    assert "不得再次减去" in text
    assert text.count(USER_FACING_TASK_OUTPUT_INSTRUCTIONS) == 1
    for phrase in ("不默认展示", "公司发现", "岗位抓取", "任务轨迹"):
        assert phrase in text


def test_recruitops_skills_cover_the_product_capabilities() -> None:
    skills = ROOT / ".agents" / "skills"
    expected = {
        "application-status",
        "job-intelligence",
        "crawler-operations",
        "recruitment-mail",
        "schedule-management",
    }

    assert {path.name for path in skills.iterdir() if path.is_dir()} >= expected
    for name in expected:
        content = (skills / name / "SKILL.md").read_text(encoding="utf-8")
        assert content.startswith("---\nname:")
        assert "description:" in content


def test_review_diagnostics_are_precise_and_preserved_on_resume() -> None:
    from packages.codex_runtime.instructions import APPLICATION_REVIEW_DIAGNOSTICS_INSTRUCTIONS, with_response_language

    first = with_response_language({})
    assert with_response_language(first) == first
    assert first["developerInstructions"].count(APPLICATION_REVIEW_DIAGNOSTICS_INSTRUCTIONS) == 1
    for phrase in ("唯一岗位身份绑定", "不可信数据", "不能把岗位名称不一致说成链接失效",
                   "不能把模型不可用或判读超时说成官网故障", "保留数据库原阶段"):
        assert phrase in APPLICATION_REVIEW_DIAGNOSTICS_INSTRUCTIONS


def test_review_presentation_retains_stages_without_claiming_verification() -> None:
    from packages.codex_runtime.instructions import APPLICATION_REVIEW_DIAGNOSTICS_INSTRUCTIONS

    instructions = APPLICATION_REVIEW_DIAGNOSTICS_INSTRUCTIONS
    for field in ("retained_count", "unchanged_or_retained_count", "retained_by_stage",
                  "saved_stage", "attention_required_count", "summary.reason_breakdown"):
        assert field in instructions
    assert "投递动作加日期（含分行显示）可核验为已投递" in instructions
    assert "证据不足时保留record_present_status_unknown和unresolved，不虚报官网核验成功" in instructions
    assert "未发现新进展，保留原阶段" in instructions
    assert "原本笔试、面试、Offer等阶段必须保留" in instructions
    assert "真实登录/验证码、加载超时和执行失败仍独立提示" in instructions
    assert "不要把所有原因统计堆进主结论" in instructions
    skill = (ROOT / ".agents" / "skills" / "application-status" / "SKILL.md").read_text(encoding="utf-8")
    assert "They remain internally `unresolved`, never `verified`" in skill
    assert "retained_by_stage" in skill and "attention_required_count" in skill


def test_review_identity_confirmation_uses_queue_without_assistant_approval() -> None:
    from packages.codex_runtime.instructions import APPLICATION_REVIEW_DIAGNOSTICS_INSTRUCTIONS

    instructions = APPLICATION_REVIEW_DIAGNOSTICS_INSTRUCTIONS
    assert "identity_confirmation_items是未解决记录的子集，不额外计数" in instructions
    assert "model_identity_mismatch也可进入待核对" in instructions
    assert "只有列表实际提供可选候选时才提示确认" in instructions
    assert "重新读取只刷新页面证据，不直接更新投递阶段" in instructions
    assert "待核对列表" in instructions and "不重复弹窗" in instructions
    assert "不先问是否列出候选" in instructions
    assert "不要求用户输入确认文字或内部ID" in instructions
    assert "只有用户实际点击确认" in instructions and "不得代用户批准" in instructions
    assert "确认绑定不直接更新阶段，仍需新页面证据" in instructions


def test_mail_only_progress_policy_is_shared_on_new_and_resumed_turns() -> None:
    from packages.codex_runtime.instructions import APPLICATION_PROGRESS_CHANNEL_INSTRUCTIONS, with_response_language

    first = with_response_language({})
    resumed = with_response_language(first)
    assert resumed == first
    assert resumed["developerInstructions"].count(APPLICATION_PROGRESS_CHANNEL_INSTRUCTIONS) == 1
    for phrase in ("record_url", "仅邮件更新", "官网复核必须跳过", "excluded_mail_only",
                   "不擅自启动邮件处理", "唯一投递关联", "不清空阶段", "用户补充并保存"):
        assert phrase in APPLICATION_PROGRESS_CHANNEL_INSTRUCTIONS
    for name in ("application-status", "recruitment-mail"):
        skill = (ROOT / ".agents" / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
        assert "mail-only" in skill and "record_url" in skill


def test_background_crawl_authorization_is_scoped_and_preserved_on_resume():
    from packages.codex_runtime.instructions import BACKGROUND_RECRUITMENT_INSTRUCTIONS, with_response_language
    from packages.tools.typed import CapabilitiesInput, describe_capabilities

    params = with_response_language(with_response_language({}))
    assert params["developerInstructions"].count(BACKGROUND_RECRUITMENT_INSTRUCTIONS) == 1
    for phrase in ("无需再次确认", "先创建定时任务", "write_enabled", "配置权限不构成启动",
                   "daily_recruitment_sync_status", "不包含投递岗位"):
        assert phrase in BACKGROUND_RECRUITMENT_INSTRUCTIONS
    assert "run_id 仅在内部保留" in BACKGROUND_RECRUITMENT_INSTRUCTIONS
    assert "立即告知真实 run_id" not in BACKGROUND_RECRUITMENT_INSTRUCTIONS
    response = describe_capabilities(CapabilitiesInput())
    assert any("后台执行全量抓取" in value for value in response.data.capabilities)


def test_crawl_reporting_distinguishes_persistence_and_bounded_samples():
    from packages.codex_runtime.instructions import BACKGROUND_RECRUITMENT_INSTRUCTIONS, with_response_language

    text = with_response_language(with_response_language({}))["developerInstructions"]
    assert text.count(BACKGROUND_RECRUITMENT_INSTRUCTIONS) == 1
    for phrase in ("company_coverage", "不代表公司来源入口未保存", "pending_entries 是有界样本",
                   "必须披露内部失败", "不代表已保存完整 JD"):
        assert phrase in text


def test_reviews_and_mail_remain_in_current_turn_while_crawl_stays_background():
    from packages.codex_runtime.instructions import with_response_language
    instructions = with_response_language({})["developerInstructions"]
    assert "all_non_terminal=true,background=false" in instructions
    assert "all_non_terminal=true,background=true" not in instructions
    assert "continuation_required=true" in instructions
    assert "wait_ms=20000" in instructions
    assert "不另问是否继续" in instructions
    assert "completed_count" in instructions and "已完成14、待完成72" in instructions
    assert "用户要求后台运行时可结束当前回复" in instructions
    assert "暂停或取消须由用户明确要求" in instructions
    assert "不得要求用户复制内部编号" in instructions


def test_default_job_counts_share_homepage_scope_without_rescreening_history():
    from packages.codex_runtime.instructions import JOB_READ_SCOPE_INSTRUCTIONS, with_response_language

    first = with_response_language({})
    resumed = with_response_language(first)
    assert resumed == first
    instructions = resumed["developerInstructions"]
    assert instructions.count(JOB_READ_SCOPE_INSTRUCTIONS) == 1
    for phrase in ("cohort=2027", "cohort_status='confirmed'", "batches=['formal','early']",
                   "返回的 total", "只影响后续爬取筛选", "用户明确查询其他范围", "展示历史岗位不等于推荐"):
        assert phrase in JOB_READ_SCOPE_INSTRUCTIONS
