from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_codex_project_instructions_define_evidence_and_execution_policy() -> None:
    content = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "PostgreSQL and typed MCP tools as the source of truth" in content
    assert "explicitly authorized" in content
    assert "low-risk" in content
    assert "non-destructive" in content
    assert "execute it without asking for a second confirmation" in content
    assert "high-risk" in content
    assert "typed approval flow" in content
    assert "human approval" in content
    assert "Never claim that an operation started" in content
    assert "STATE_UNCLEAR" in content
    assert "Cookie" in content
    assert "This product runs locally" in content
    assert "每天凌晨三点更新新华三岗位" in content
    assert "application_progress" in content
    assert "do not route it to a crawler" in content
    assert "list_all=true" in content


def test_codex_project_instructions_define_only_local_task_ids() -> None:
    content = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    for task_id in (
        "daily_recruitment_intelligence",
        "crawler_health",
        "application_progress",
        "recruitment_mailbox",
    ):
        assert task_id in content
    assert "legacy cloud task identifiers" in content


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
