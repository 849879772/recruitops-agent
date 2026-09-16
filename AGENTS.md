# Repository Guidelines

## Project layout

- `apps/`: FastAPI endpoints and the local web workbench.
- `packages/`: domain, storage, crawling, matching, mail, scheduling, RAG,
  MCP, and Codex runtime modules.
- `migrations/`: ordered PostgreSQL migrations.
- `.agents/skills/`: domain instructions loaded by the agent runtime.
- `extension/`: optional Edge/Chromium browser bridge.
- `evals/`: deterministic evaluation fixtures and runners.
- `tests/`: unit, contract, and regression tests.

## Development commands

```bash
pip install -e ".[dev]"
playwright install chromium
python -m pytest -c pytest-public.ini
python -m compileall apps packages evals scripts
python scripts/check_migrations.py
docker compose config
```

## Engineering rules

- Keep business data in structured storage; use RAG for unstructured personal
  knowledge, not as a replacement for exact job or application queries.
- Preserve idempotency, evidence, and checkpoint behavior for write-capable
  tools and long-running tasks.
- Add deterministic fixtures for crawler/parser changes whenever possible.
- Keep live-site checks separate from the public CI profile.
- Never commit resumes, mail bodies, credentials, cookies, browser profiles,
  databases, backups, or generated local configuration.
- Write-capable behavior must remain disabled by default.

