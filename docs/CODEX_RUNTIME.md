# Codex CLI Runtime

This project keeps the Codex CLI independent from the Python runtime. The
supported package is pinned to `@openai/codex@0.149.0`, and the default npm
prefix is `.data/codex-cli` inside this repository. The prefix is local state;
it is not a Python dependency and is not installed globally.

## Install And Check

Run these commands from the repository root:

```powershell
python scripts/bootstrap_codex_cli.py --check
python scripts/bootstrap_codex_cli.py --install
python scripts/bootstrap_codex_cli.py --check
```

`--install` uses npm with the exact package spec and captures npm output. The
scripts report only structured status, paths, return codes, and the expected or
observed version; they do not print subprocess output, environment variables,
API keys, or other credentials. A different local prefix can be supplied when
needed:

```powershell
python scripts/bootstrap_codex_cli.py --install --install-dir .data\codex-cli
```

On Windows the launcher is resolved as:

```text
.data\codex-cli\node_modules\.bin\codex.cmd
```

On Unix-like systems the corresponding launcher is `node_modules/.bin/codex`.
Pass the actual launcher path explicitly to the acceptance script:

```powershell
python scripts/verify_codex_app_server.py `
  --command .data\codex-cli\node_modules\.bin\codex.cmd
```

The verifier runs both `--version` and `app-server --help`, applies a timeout
to each child process, checks the exact `0.149.0` version, and prints one JSON
health result. It does not search `PATH` or select another Codex executable.

## Windows Desktop Boundary

The packaged desktop Codex application and its executable or launcher under
`%LOCALAPPDATA%\Microsoft\WindowsApps` are not a stable child-process
dependency. WindowsApps entries can be package launchers, aliases, or internal
desktop-app files with different lifecycle and command-line behavior. Do not
use a desktop-app executable for the RecruitOps child process. Use the pinned
npm CLI launcher under `.data\codex-cli\node_modules\.bin\codex.cmd` and pass
that explicit path to the verifier and later runtime supervisor.

## Official App Server Contract

The official Codex App Server is a child process connected over **stdio**. Its
protocol is line-delimited JSON (**JSONL**): each request, response, or
notification occupies one JSON object per line on stdin/stdout. It is not an
HTTP endpoint, and stderr is for diagnostics rather than protocol messages.
The runtime must preserve the stdio pipes, avoid mixing logs into stdout, and
apply process/request timeouts.

## RecruitOps BFF And MCP Boundary

The RecruitOps MCP protocol version 15 is fixed. The canonical 34-tool full
catalog is defined by `MCP_TOOL_NAMES` in `packages/mcp/server.py`; its read-only
subset contains 25 tools, and the default 25-tool model surface is
`MCP_AGENT_TOOL_NAMES`. The read-only and
side-effect subsets are `MCP_READ_ONLY_TOOL_NAMES` and
`MCP_ACTION_TOOL_NAMES`. The documentation and tests consume that source
rather than maintaining a second hand-written tool registry.

FastAPI is a local BFF for the App Server's JSON-RPC thread/turn methods. Its
`/api/codex/*` routes expose health, thread lifecycle, turn start/interruption,
and SSE event projections; they do not implement a second model loop. Business
operations are selected through the typed MCP tools. Passive browser evidence
continues to use `POST /api/browser/observations`, and generic approval remains
available for high-risk or business write operations.

The stdio MCP server inherits only the environment variable names listed in
`mcp_servers.recruitops.env_vars`. Database, crawler, embedding, and mail values
therefore come from the App Server process environment and are never rendered
into `config.toml`. The server is configured with `required = true`, so a broken
database or MCP startup fails visibly instead of silently producing a chat-only
Agent. App Server JSONL uses a 4 MiB reader limit because the complete schemas
for the typed tools exceed Python's default 64 KiB line limit.

The Edge browser bridge is a separate persistent WebSocket at
`/browser-bridge`. The extension authenticates with a server challenge and
HMAC proof, then receives server-pushed typed operations and returns ACK,
progress, and sanitized evidence. It is not an HTTP claim/poll queue. Login,
CAPTCHA, unclear state, destructive changes, and missing evidence remain
fail-closed and require user action or the existing approval flow.

When a custom model provider is used, the provider must satisfy the Codex
**Responses-only** provider contract. In practice, the configured endpoint and
authentication must support the OpenAI Responses API request/response and
streaming/tool-call shapes expected by Codex. A Chat Completions-only endpoint
or an Anthropic Messages-only endpoint is not a valid substitute for this
provider contract; adapt it behind a Responses-compatible provider first.

On 2026-08-22 this project probed `https://api.deepseek.com/responses` with the
Agent-only local credential. A minimal response, streamed response events, and
a function call all succeeded for `deepseek-v4-flash`. The probe output kept
the credential private. DeepSeek's public guide still emphasizes Chat
Completions and Anthropic formats, so this capability must remain covered by a
live acceptance test whenever Codex or the DeepSeek model version changes.

## Acceptance Evidence

An installation is accepted only when the bootstrap check reports the exact
package name/version and an existing platform launcher, followed by a healthy
verifier result with successful `--version` and `app-server --help` checks. A
passing package-manifest check alone is insufficient: the launcher must also
be executable, and the App Server command surface must respond before it is
used as a subprocess dependency.

The Docker acceptance on 2026-08-22 additionally verified a healthy API and
PostgreSQL container, MCP protocol version 15 with the compact Agent profile, and a fresh
DeepSeek turn that called `recruitops.capabilities` and consumed its structured
result. This does not claim that real Edge login state, external sites, mail, or
approval-gated writes have passed their separate acceptance checks.

## Repeatable Live Harness Check

After rebuilding or changing Codex, DeepSeek, MCP schemas, or context settings, run:

```powershell
python scripts/verify_harness_live.py --live --api-base-url http://127.0.0.1:8012
```

The default check is read-only. It verifies runtime readiness, MCP protocol `15`, the
typed write-tool annotation boundary, one real `capabilities` Tool Call, and same-thread
context continuity using a random marker. It never executes a business write.

Cancellation can be exercised explicitly:

```powershell
python scripts/verify_harness_live.py --live --exercise-cancel
```

That option starts a read-only configured crawler request and immediately sends
`turn/interrupt`. A successful interrupt request proves that the Harness accepted the
cancellation command; terminal tool cancellation must still be confirmed from App Server
events. True auto-compaction beyond the configured 96,000-token threshold and business
writes remain separate, deliberately expensive tests that require an isolated database.
