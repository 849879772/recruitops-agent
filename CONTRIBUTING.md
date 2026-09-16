# Contributing

## Development setup

1. Install Python 3.11+, Docker Desktop, and Playwright Chromium.
2. Create a virtual environment and install `.[dev]`.
3. Copy `.env.example` to `.env` and keep write-capable features disabled.
4. Run `python -m pytest -c pytest-public.ini` before opening a pull request.

## Pull requests

- Keep changes focused and use Conventional Commit-style messages.
- Add deterministic tests for parsers, matching rules, database behavior, and
  MCP tool contracts.
- Do not commit live recruitment data, resumes, mail bodies, credentials,
  browser state, or generated databases.
- Include a screenshot for user-interface changes, with personal data removed.

Live-site tests can be unstable. Prefer saved HTML fixtures for regression
coverage and identify real network checks explicitly in the pull request.

The public CI profile excludes tests that require a private company catalog,
historical snapshots, logged-in browser state, or a machine-specific virtual
environment. Those test sources remain visible so maintainers can run them
with the required local fixtures.
