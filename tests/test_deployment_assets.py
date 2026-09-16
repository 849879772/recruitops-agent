from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_dockerfile_builds_api_image_with_healthcheck_and_apps() -> None:
    dockerfile = _read("Dockerfile")

    assert "FROM python:3.11-slim" in dockerfile
    assert "COPY apps ./apps" in dockerfile
    assert "COPY packages ./packages" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
    assert "COPY scripts ./scripts" in dockerfile
    assert "python scripts/apply_migrations.py" in dockerfile
    assert "exec uvicorn apps.api.main:app" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "USER app" in dockerfile


def test_compose_keeps_postgres_volume_and_adds_healthy_api_dependency() -> None:
    compose = yaml.safe_load(_read("docker-compose.yml"))
    services = compose["services"]
    postgres = services["postgres"]
    api = services["api"]

    assert postgres["image"] == "pgvector/pgvector:pg16"
    assert postgres["restart"] == "unless-stopped"
    assert postgres["environment"]["POSTGRES_DB"] == "recruitops"
    assert postgres["environment"]["POSTGRES_USER"] == "recruitops"
    assert postgres["ports"] == ["127.0.0.1:5433:5432"]
    assert "recruitops-postgres:/var/lib/postgresql/data" in postgres["volumes"]
    assert api["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert api["restart"] == "unless-stopped"
    assert "python scripts/apply_migrations.py" in api["command"][-1]
    assert api["ports"] == ["127.0.0.1:${RECRUITOPS_API_PORT:-8010}:8010"]
    assert (
        api["environment"]["RECRUITOPS_WRITE_ENABLED"]
        == "${RECRUITOPS_WRITE_ENABLED:-false}"
    )
    assert api["healthcheck"]["test"][0] == "CMD"
    assert "/ready" in api["healthcheck"]["test"][-1]
    assert "recruitops-agent-state:/app/.data" in api["volumes"]
    assert "frontend" not in services


def test_dockerignore_excludes_sources_and_credentials() -> None:
    dockerignore = _read(".dockerignore")

    assert ".env" in dockerignore
    assert ".env.*" in dockerignore
    assert "data" in dockerignore
    assert "*.db" in dockerignore
    assert ".git" in dockerignore
    assert "secrets" in dockerignore


def test_ci_runs_required_checks_without_secret_values() -> None:
    ci = _read(".github/workflows/ci.yml")

    assert "pip install -e \".[dev]\"" in ci
    assert "python -m pytest" in ci
    assert "python -m compileall -q apps packages evals scripts" in ci
    assert "python -m evals.mvp_runner" in ci
    assert "python scripts/check_migrations.py" in ci
    assert "sk-" not in ci
    assert "BEGIN " + "PRIVATE KEY" not in ci


def test_migration_static_check_passes() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_migrations.py"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "migration static check passed" in result.stdout


def test_operations_docs_cover_required_topics_without_credentials() -> None:
    installation = _read("docs/INSTALLATION.md")
    troubleshooting = _read("docs/TROUBLESHOOTING.md")
    documentation = f"{installation}\n{troubleshooting}"

    for term in (
        "docker compose up -d --build",
        "RECRUITOPS_SOURCE_ROOT",
        "mode=ro",
        "sync_sqlite_readonly.py",
        "schema_migrations",
        "write_audits",
        "CREATE EXTENSION IF NOT EXISTS vector",
        "常见故障",
    ):
        assert term in documentation

    assert "sk-" not in documentation
    assert "BEGIN " + "PRIVATE KEY" not in documentation
    assert "FEISHU_WEBHOOK" + "=http" not in documentation
