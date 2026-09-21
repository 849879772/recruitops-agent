"""Retired personal-document surfaces must not return through UI or tools."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.codex_runtime.instructions import with_response_language
from packages.tools.knowledge import KnowledgeSearchInput

ROOT = Path(__file__).resolve().parents[1]


def test_personal_tool_domain_is_rejected():
    with pytest.raises(ValidationError):
        KnowledgeSearchInput(query="my notes", domain="personal")
    assert KnowledgeSearchInput(query="ATS adapter", domain="crawler").query


def test_no_personal_document_instructions_or_ui():
    assert "domain='personal'" not in with_response_language({})["developerInstructions"]
    html = (ROOT / "apps/web/index.html").read_text(encoding="utf-8")
    for marker in ('data-view="knowledge"', "knowledge.js", "assistant-knowledge", "knowledge-preview"):
        assert marker not in html
    assert 'id="assistant-selected-job"' in html
    assert 'id="assistant-clear-job"' in html
    assert 'id="resume-upload"' in html


def test_personal_routes_are_removed_and_job_context_is_preserved():
    from apps.api.main import CodexTurnStartRequest, app

    assert not any(getattr(route, "path", "").startswith("/api/local-ui/knowledge") for route in app.routes)
    assert CodexTurnStartRequest(text="hello").prompt() == "hello"
    request = CodexTurnStartRequest(text="analyze", job_id="job-1")
    assert '"job_id": "job-1"' in request.prompt()
    assert "job_detail" in request.prompt()
    assert "knowledge" not in request.prompt()
    with pytest.raises(ValidationError):
        CodexTurnStartRequest(text="hello", knowledge_enabled=True)


def test_personal_implementation_is_not_shipped_and_stored_content_is_removed():
    for name in ("apps/api/knowledge.py", "apps/web/knowledge.js", "packages/personal_knowledge.py"):
        assert not (ROOT / name).exists()
    assert (ROOT / "migrations/022_personal_knowledge.sql").exists()
    removal = (ROOT / "migrations/024_remove_personal_knowledge.sql").read_text(encoding="utf-8")
    assert "DROP TABLE IF EXISTS personal_knowledge_chunks" in removal
    assert "DROP TABLE IF EXISTS personal_knowledge_documents" in removal
