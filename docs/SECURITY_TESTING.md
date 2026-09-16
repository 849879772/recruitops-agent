# Security Testing

This document describes the offline stage 8 security boundary tests in
`tests/test_security_boundaries.py`. They are deterministic and do not open a
browser, call a model, access the network, or require a new dependency.

## Scope

The tests cover four threat families:

| Threat | Boundary under test | Fail-closed result |
| --- | --- | --- |
| Expired login, CAPTCHA, or unclear browser state | Extension pause protocol and `build_browser_pause` | `paused`, `requiresUserAction=true`, no automatic resume |
| Prompt injection or malicious recruitment page | `assess_web_content`, MCP evidence boundaries, and Codex instructions | Page text is data only; suspicious content cannot authorize an Agent action |
| Privilege escalation to a write tool | MCP annotations, App Server request policy, and approval token binding | Unknown tools, shell/file writes, side effects, and unapproved operation changes are denied |
| Sensitive information leakage | Extension static audit and recursive payload redaction | Cookies, credentials, tokens, PII, and common key material are absent from boundary output |

The browser fixtures are checked from the repository files. The test suite does
not execute JavaScript or use a live browser, so a passing result is independent
of accounts, sessions, websites, and network availability.

## Security helper

`packages/security/boundaries.py` contains only local decisions:

- `build_browser_pause` emits the same pause shape as `extension/src/protocol.js`
  and rejects unsupported reasons.
- `assess_web_content` recognizes common English and Chinese instruction
  overrides, credential exfiltration requests, verification bypasses, and active
  HTML/script fragments. Clean content is still evidence, never an instruction.
  Oversized content is treated as unassessed and blocked from Agent action.
- `authorize_tool_call` and `require_read_only_tool` allow only the twelve current
  read-only MCP tools. The four approval-gated operation names are deliberately
  not capabilities of this helper.
- `redact_sensitive`, `sensitive_kinds`, and `require_redacted` recursively
  handle structured payloads without returning matched secret values. They cover
  common authorization headers, cookies, API keys, JWTs, passwords, OTPs,
  private keys, email addresses, phone numbers, and long national identifiers.

The helper is intentionally not wired into the existing business modules in
this subtask because those files are outside the authorized edit scope. An
adapter that forwards page text, tool traces, or errors outside the process
should apply the relevant helper at that boundary.

## Deterministic checks

The tests assert that:

- the extension has only `activeTab`, `scripting`, and `storage`, plus host
  permission for the local Agent API; it has no recruitment-site host
  permissions, cookie access, form values, or external network APIs;
- the actual twelve typed-tool responses remain explicitly read-only and carry no
  detected sensitive values;
- page-shaped instructions remain untrusted evidence and cannot authorize an
  MCP side effect or repository write;
- approval tokens cannot be consumed against a different canonical operation,
  and unconfirmed 2027 or incomplete JD previews are rejected;
- strict Pydantic tool inputs reject a mutating escape-hatch field;
- redaction is recursive and does not expose the original secret in an error or
  serialized payload.

## Run

From `RecruitOps-Agent`:

```powershell
python -m pytest tests/test_security_boundaries.py
python -m pytest tests/test_security_boundaries.py tests/test_typed_tools.py tests/test_mcp.py tests/test_codex_runtime.py tests/test_approval.py tests/test_approval_service.py tests/test_extension_manifest.py
```

No command in this evaluation installs packages, downloads fixtures, or makes
external requests.
