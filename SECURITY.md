# Security Policy

## Supported version

Security fixes are applied to the latest revision of the default branch.

## Reporting a vulnerability

Please open a private GitHub security advisory. Do not publish credentials,
mail content, browser storage, session cookies, resumes, or production database
extracts in a public issue.

## Local data boundaries

RecruitOps is local-first, but some optional integrations communicate with
third-party services:

- the configured LLM provider receives prompts required for enabled analysis;
- IMAP is used only when recruitment mail synchronization is enabled;
- public recruitment sites are accessed by the crawler;
- Codex and the Edge extension are optional execution surfaces.

Keep `.env`, `config/candidate_profile.yaml`, `.data/`, database dumps, cookies,
and browser profiles outside version control. Use a dedicated mailbox token or
application password instead of the account password whenever possible.

Write-capable tools are disabled by default. Review the configured allowlists
before setting `RECRUITOPS_WRITE_ENABLED=true`.

