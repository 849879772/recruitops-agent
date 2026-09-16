"""Build a small, explicit-allowlist distribution without any local owner state."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]

COMPOSE = '''name: recruitops-share
services:
  postgres:
    image: pgvector/pgvector:pg16
    restart: unless-stopped
    environment:
      POSTGRES_DB: recruitops
      POSTGRES_USER: recruitops
      POSTGRES_PASSWORD: recruitops
    volumes:
      - postgres-data:/var/lib/postgresql/data
    healthcheck:
      test: [CMD-SHELL, "pg_isready -U recruitops -d recruitops"]
      interval: 5s
      timeout: 5s
      retries: 20
  api:
    build: .
    restart: unless-stopped
    environment:
      RECRUITOPS_AGENT_ROOT: /app
      RECRUITOPS_DATABASE_URL: postgresql+psycopg://recruitops:recruitops@postgres:5432/recruitops
      RECRUITOPS_API_HOST: 0.0.0.0
      RECRUITOPS_WRITE_ENABLED: 'true'
      RECRUITOPS_AUTOMATION_ENABLED: 'true'
      RECRUITOPS_LLM_ENABLED: 'false'
      RECRUITOPS_CODEX_RUNTIME_ENABLED: 'false'
      RECRUITOPS_MAIL_ENABLED: 'false'
      RECRUITOPS_BROWSER_EXECUTABLE_PATH: /usr/bin/chromium
      RECRUITOPS_BROWSER_CHANNEL: chromium
    ports:
      - '127.0.0.1:${RECRUITOPS_API_PORT:-8012}:8010'
    volumes:
      - agent-state:/app/.data
      - ./config:/app/config:ro
      - ./seed:/app/seed:ro
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: [CMD, python, -c, "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/ready', timeout=3)"]
      interval: 5s
      timeout: 5s
      retries: 30
volumes:
  postgres-data:
  agent-state:
'''

START = '''$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
function Run-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker failed; see output above." }
}
try {
    Run-Docker info --format '{{.ServerVersion}}'
    $images = & docker compose images -q api
    if (-not $images) { Run-Docker compose build api }
    Run-Docker compose up -d --wait --wait-timeout 180
    Run-Docker compose exec -T api python scripts/share_catalog.py import --directory /app/seed --if-empty
    $port = 8012
    $line = Get-Content -LiteralPath .env | Where-Object { $_ -match '^RECRUITOPS_API_PORT=(\\d+)$' } | Select-Object -First 1
    if ($line -match '=(\\d+)$') { $port = [int]$Matches[1] }
    Start-Process "http://127.0.0.1:$port/"
} catch { Write-Host $_ -ForegroundColor Red; Read-Host 'Press Enter to close'; exit 1 }
'''


def build(seed, destination):
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("apps", "packages", "migrations", ".agents", "extension", "evals"):
        shutil.copytree(ROOT / name, destination / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    for name in ("Dockerfile", "pyproject.toml", ".dockerignore", "AGENTS.md"):
        shutil.copy2(ROOT / name, destination / name)
    (destination / "scripts").mkdir()
    for name in ("__init__.py", "apply_migrations.py", "run_mcp_server.py", "run_local_task.py", "share_catalog.py"):
        shutil.copy2(ROOT / "scripts" / name, destination / "scripts" / name)
    (destination / "docs").mkdir()
    for name in ("SHARE_DEPLOYMENT.md", "EXTENSION.md", "ARTIFACT_VERSIONS.json"):
        shutil.copy2(ROOT / "docs" / name, destination / "docs" / name)
    shutil.copy2(ROOT / "docs/SHARE_DEPLOYMENT.md", destination / "部署说明.md")
    shutil.copy2(ROOT / "docs/SHARE_DEPLOYMENT.md", destination / "README.md")
    (destination / "config").mkdir()
    shutil.copy2(ROOT / "config/companies.yaml", destination / "config/companies.yaml")
    (destination / "config/candidate_profile.yaml").write_text(json.dumps({"profile": {
        "degree": None, "job_type": "校招", "direction": "", "skills": [], "matching": {}}}, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "config/rag_sources.yaml").write_text("version: 1\nsources: []\n", encoding="utf-8")
    (destination / "docker-compose.yml").write_text(COMPOSE, encoding="utf-8")
    (destination / ".env").write_text("RECRUITOPS_API_PORT=8012\n", encoding="utf-8")
    (destination / "Start.ps1").write_text(START, encoding="utf-8-sig")
    (destination / "Start.cmd").write_text('@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start.ps1"\r\n', encoding="ascii")
    shutil.copytree(seed, destination / "seed")
    files = {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(destination.rglob("*")) if p.is_file()}
    (destination / "PACKAGE_MANIFEST.json").write_text(json.dumps(files, indent=2), encoding="utf-8")
    archive = destination.with_suffix(".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                output.write(path, f"{destination.name}/{path.relative_to(destination).as_posix()}")
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{checksum}  {archive.name}\n", encoding="ascii")
    print(json.dumps({"archive": str(archive), "bytes": archive.stat().st_size, "sha256": checksum}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    build(args.seed, args.destination)
