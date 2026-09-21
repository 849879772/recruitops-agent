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


def test_runtime_review_instructions_are_idempotent_on_resume() -> None:
    from packages.codex_runtime.instructions import with_response_language

    original = {"developerInstructions": "Caller policy"}
    first = with_response_language(original)
    resumed = with_response_language(first)
    assert resumed == first
    assert original == {"developerInstructions": "Caller policy"}
    text = resumed["developerInstructions"]
    assert "默认全程使用简体中文" in text
    assert "all_non_terminal=true" in text and "只传 run_id" in text
    assert "不得再次减去" in text


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


def test_background_crawl_authorization_is_scoped_and_preserved_on_resume():
    from packages.codex_runtime.instructions import BACKGROUND_RECRUITMENT_INSTRUCTIONS, with_response_language
    from packages.tools.typed import CapabilitiesInput, describe_capabilities

    params = with_response_language(with_response_language({}))
    assert params["developerInstructions"].count(BACKGROUND_RECRUITMENT_INSTRUCTIONS) == 1
    for phrase in ("无需再次确认", "先创建定时任务", "write_enabled", "配置权限不构成启动",
                   "daily_recruitment_sync_status", "不包含投递岗位"):
        assert phrase in BACKGROUND_RECRUITMENT_INSTRUCTIONS
    response = describe_capabilities(CapabilitiesInput())
    assert any("后台执行全量抓取" in value for value in response.data.capabilities)


def test_crawl_reporting_distinguishes_persistence_and_bounded_samples():
    from packages.codex_runtime.instructions import BACKGROUND_RECRUITMENT_INSTRUCTIONS, with_response_language

    text = with_response_language(with_response_language({}))["developerInstructions"]
    assert text.count(BACKGROUND_RECRUITMENT_INSTRUCTIONS) == 1
    for phrase in ("company_coverage", "不代表公司来源入口未保存", "pending_entries 是有界样本",
                   "必须披露内部失败", "不代表已保存完整 JD"):
        assert phrase in text
