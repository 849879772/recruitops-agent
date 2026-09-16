"""Keep operator documentation aligned with the current local contracts."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC_PATHS = (
    ROOT / "README.md",
    ROOT / "docs" / "BASELINE.md",
    ROOT / "docs" / "INSTALLATION.md",
    ROOT / "docs" / "CODEX_RUNTIME.md",
    ROOT / "PROJECT_PLAN.md",
    ROOT / "AGENTS.md",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _all_docs() -> str:
    return "\n".join(_read(path) for path in DOC_PATHS)


def _mcp_tuple_names(constant: str) -> tuple[str, ...]:
    source = _read(ROOT / "packages" / "mcp" / "server.py")
    match = re.search(
        rf"{re.escape(constant)}: tuple\[str, \.\.\.\] = \((?P<body>.*?)\n\)",
        source,
        flags=re.DOTALL,
    )
    assert match is not None
    return tuple(re.findall(r'"([a-z_]+)"', match.group("body")))


def _mcp_tool_names() -> tuple[str, ...]:
    return _mcp_tuple_names("MCP_TOOL_NAMES")


def _api_routes() -> set[str]:
    source = _read(ROOT / "apps" / "api" / "main.py")
    return set(
        re.findall(
            r"@app\.(?:get|post|put|patch|delete)\(\s*[\"']([^\"']+)",
            source,
        )
    )


def test_registered_mcp_tools_match_the_current_server_contract() -> None:
    actual = _mcp_tool_names()
    read_only = _mcp_tuple_names("MCP_READ_ONLY_TOOL_NAMES")
    actions = _mcp_tuple_names("MCP_ACTION_TOOL_NAMES")
    agent = _mcp_tuple_names("MCP_AGENT_TOOL_NAMES")
    server = _read(ROOT / "packages" / "mcp" / "server.py")

    assert re.search(r'MCP_TOOL_PROTOCOL_VERSION\s*=\s*"22"', server)
    assert len(actual) == 37
    assert len(set(actual)) == 37
    assert len(read_only) == 24
    assert len(actions) == 13
    assert len(agent) == 29
    assert {
        "public_recruitment_entry_discovery",
        "public_recruitment_entry_validate",
        "offerbiu_source_refresh",
    } <= set(agent)
    assert set(actual) == set(read_only) | set(actions)
    assert set(read_only).isdisjoint(actions)

    for path in (ROOT / "README.md", ROOT / "PROJECT_PLAN.md"):
        text = _read(path)
        assert "MCP" in text and "22" in text
        assert all(f"`{name}`" in text for name in actual), path


def test_codex_bff_passive_evidence_and_approval_routes_are_current() -> None:
    routes = _api_routes()
    current_routes = {
        "/api/codex/health",
        "/api/codex/traces",
        "/api/codex/threads",
        "/api/codex/threads/{thread_id}",
        "/api/codex/threads/{thread_id}/resume",
        "/api/codex/threads/{thread_id}/turns",
        "/api/codex/threads/{thread_id}/turns/stream",
        "/api/codex/threads/{thread_id}/interrupt",
        "/api/codex/threads/{thread_id}/events",
        "/api/browser/observations",
        "/api/browser/application-captures",
        "/api/approvals",
        "/api/approvals/{token_id}/approve",
        "/api/approvals/{token_id}/reject",
        "/api/approvals/{token_id}/execute",
    }
    assert current_routes <= routes

    legacy_routes = {
        "/api/agent/tasks",
        "/api/assistant",
        "/api/browser/actions",
        "/api/browser/actions/claim",
        "/api/browser/actions/poll",
        "/api/browser/application-status/reviews",
        "/api/browser/application-status/observations",
    }
    api_source = _read(ROOT / "apps" / "api" / "main.py")
    docs = _all_docs()
    for route in legacy_routes:
        assert route not in api_source
        assert route not in docs

    installation = _read(ROOT / "docs" / "INSTALLATION.md")
    runtime = _read(ROOT / "docs" / "CODEX_RUNTIME.md")
    assert "@openai/codex@0.149.0" in runtime
    assert "stdio" in runtime and "JSONL" in runtime
    assert "MCP protocol version 15" in runtime
    assert "/browser-bridge" in installation
    assert "WebSocket" in installation
    assert "/api/browser/observations" in installation
    assert "/api/approvals" in installation


def test_token_write_switch_and_compose_are_documented_consistently() -> None:
    api_source = _read(ROOT / "apps" / "api" / "main.py")
    extension_source = _read(ROOT / "extension" / "src" / "background.js")
    compose = _read(ROOT / "docker-compose.yml")
    docs = _all_docs()

    assert 'scheme.casefold() != "bearer"' in api_source
    assert "headers.Authorization = `Bearer ${settings.apiToken}`" in extension_source
    assert "RECRUITOPS_WRITE_ENABLED" in api_source
    assert "RECRUITOPS_WRITE_ENABLED: ${RECRUITOPS_WRITE_ENABLED:-false}" in compose
    import yaml

    environment = yaml.safe_load(compose)["services"]["api"]["environment"]
    assert environment["RECRUITOPS_CHECKPOINT_MODE"] == "${RECRUITOPS_CHECKPOINT_MODE:-both}"
    assert re.search(r"\bread_only:\s*true\b", compose)
    assert "RECRUITOPS_API_TOKEN" in docs
    assert "Authorization: Bearer <token>" in docs
    assert "RECRUITOPS_WRITE_ENABLED=true" in docs
    assert "RECRUITOPS_WRITE_ENABLED" in docs and "false" in docs


def test_documented_commands_use_existing_scripts_and_compose_services() -> None:
    compose = _read(ROOT / "docker-compose.yml")
    installation_doc = _read(ROOT / "docs" / "INSTALLATION.md")
    readme = _read(ROOT / "README.md")

    assert re.search(r"(?ms)^services:\s+  postgres:\s+.*?^  api:", compose)
    assert '"127.0.0.1:${RECRUITOPS_API_PORT:-8010}:8010"' in compose
    assert "http://127.0.0.1:8010/ready" in compose
    for script in (
        "scripts/ingest_rag_sources.py",
        "scripts/sync_sqlite_readonly.py",
        "scripts/check_migrations.py",
    ):
        assert (ROOT / script.replace("/", "\\")).exists(), script
    assert "docker compose up -d --build" in installation_doc
    assert "docker compose run --rm api python scripts/sync_sqlite_readonly.py" in installation_doc
    assert "docker compose exec -T postgres" in installation_doc
    assert "uvicorn apps.api.main:app --reload --port 8010" in readme


def test_documentation_keeps_real_browser_e2e_pending_and_contains_no_secret() -> None:
    docs = _all_docs()
    assert re.search(r"真实\s*Edge.{0,100}(?:仍需|仍待|未在|未完成)", docs, re.DOTALL)
    positive_words = re.compile(r"(?:已完成|已通过|通过验收|已验收)")
    for line in docs.splitlines():
        if "真实" in line and "Edge" in line and positive_words.search(line):
            assert re.search(r"不代表|未|仍|待", line), line

    secret_patterns = (
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\b(?:sk|rk|pk)-[A-Za-z0-9]{16,}\b",
        r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b",
        r"(?:api[_-]?key|secret|password|passwd|token)\s*[:=]\s*[\"']?[A-Za-z0-9+/=_-]{20,}",
    )
    for pattern in secret_patterns:
        assert re.search(pattern, docs, re.IGNORECASE) is None
