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

For terminal downloads, set the local proxy in the same PowerShell session:

```powershell
$env:HTTP_PROXY="http://127.0.0.1:10808"
$env:HTTPS_PROXY="http://127.0.0.1:10808"
```

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

## Desktop deployment rules

- Routine Python, API, and web business-code fixes must use source-only
  incremental deployment to the formal desktop installation. Replace only
  changed application files, including new allowlisted business modules, and
  update the runtime integrity manifest and application provenance together.
  Use `scripts/desktop/promote_sources.py` to preview the change set first;
  perform deployment only with its explicit `--apply` option and a fresh,
  narrowly scoped `--backup` directory.
- Use full desktop packaging for Electron or desktop-shell changes, runtime or
  dependency updates, database migration changes requiring full validation, or
  preparation of a distributable release. If incremental compatibility cannot
  be established, stop and explain why the full workflow is required.
- Before replacement, confirm that the formal application and its managed
  processes have exited normally. Do not interrupt active business tasks or
  force-kill them for deployment; request a normal exit when needed.
- Preserve the entire formal `.data` directory, including databases, browser
  sessions, configuration, task checkpoints, and user records. Incremental
  deployment must not recreate or migrate user data as a side effect.
- Back up only the files being replaced and the integrity/provenance manifests;
  record newly added paths for rollback. Do not copy the whole installation or
  database for an ordinary source-only fix. Validate exact paths, reject
  traversal and reparse points, and keep backups outside user data.
- Verify the prior installation, planned file hashes, replacement integrity,
  and rollback behavior. Run proportionate regression tests, then restart the
  formal application and check startup and relevant read-only endpoints.
  Do not launch or replay a crawl, mail-processing run, or application-status
  update merely as a deployment check unless the user explicitly requests it.
- Report the deployed changes, verification result, and any remaining blocker;
  never treat a successful build alone as a successful formal deployment.
