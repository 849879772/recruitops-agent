from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_dockerfile_contains_the_pinned_codex_runtime_assets() -> None:
    dockerfile = _read("Dockerfile")

    assert "FROM node:22-bookworm-slim AS codex-cli" in dockerfile
    assert "COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node" in dockerfile
    assert "@openai/codex@0.149.0" in dockerfile
    assert "node -e 'const p=require" in dockerfile
    assert 'p.version !== "0.149.0"' in dockerfile
    assert "COPY config ./config" in dockerfile
    assert "COPY AGENTS.md ./AGENTS.md" in dockerfile
    assert "COPY .agents ./.agents" in dockerfile
    assert "CODEX_HOME=/app/.data/codex-home" in dockerfile
    assert "PATH=/opt/codex-cli/node_modules/.bin:${PATH}" in dockerfile
    assert "/app/.data/codex-cli" not in dockerfile
    assert "latest" not in dockerfile.casefold()


def test_compose_keeps_postgres_loopback_only_and_reads_codex_provider_from_env() -> None:
    compose_text = _read("docker-compose.yml")
    compose = yaml.safe_load(compose_text)
    services = compose["services"]
    postgres = services["postgres"]
    api = services["api"]
    environment = api["environment"]

    assert postgres["image"] == "pgvector/pgvector:pg16"
    assert postgres["ports"] == ["127.0.0.1:5433:5432"]
    assert "recruitops-postgres:/var/lib/postgresql/data" in postgres["volumes"]
    assert api["ports"] == ["127.0.0.1:${RECRUITOPS_API_PORT:-8010}:8010"]
    assert environment["RECRUITOPS_CODEX_CLI_VERSION"] == "0.149.0"
    assert environment["RECRUITOPS_CODEX_COMMAND"] == (
        '["/opt/codex-cli/node_modules/.bin/codex","app-server"]'
    )
    assert environment["CODEX_HOME"] == "/app/.data/codex-home"
    assert environment["RECRUITOPS_CODEX_MODEL_PROVIDER_ID"] == (
        "${RECRUITOPS_CODEX_MODEL_PROVIDER_ID:-deepseek}"
    )
    assert environment["RECRUITOPS_CODEX_MODEL_BASE_URL"] == (
        "${RECRUITOPS_CODEX_MODEL_BASE_URL:-https://api.deepseek.com}"
    )
    assert environment["RECRUITOPS_CODEX_MODEL_API_KEY_ENV"] == (
        "${RECRUITOPS_CODEX_MODEL_API_KEY_ENV:-RECRUITOPS_LLM_API_KEY}"
    )
    assert environment["RECRUITOPS_LLM_API_KEY"] == "${RECRUITOPS_LLM_API_KEY:-}"
    assert "latest" not in compose_text.casefold()
    assert "0.149.0" in compose_text
    assert "sk-" not in compose_text


def test_dockerignore_and_readme_keep_credentials_out_of_the_delivery() -> None:
    dockerignore = _read(".dockerignore")
    readme = _read("README.md")

    assert ".env" in dockerignore
    assert ".env.*" in dockerignore
    assert "secrets" in dockerignore
    assert "@openai/codex@0.149.0" in readme
    assert "/opt/codex-cli/node_modules/.bin/codex" in readme
    assert "AGENTS.md" in readme
    assert ".agents/skills/" in readme
    assert "RECRUITOPS_CODEX_RUNTIME_ENABLED=true" in readme
    assert "RECRUITOPS_LLM_API_KEY" in readme
    assert "verify_codex_app_server.py" in readme
    assert re.search(r"\b(?:sk|rk|pk)-[A-Za-z0-9]{16,}\b", readme) is None
    assert "BEGIN PRIVATE KEY" not in readme
