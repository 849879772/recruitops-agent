# Architecture Baseline

## Runtime Boundary

RecruitOps Agent is an independent local application. Its runtime source of
truth is the Agent configuration and Agent-owned PostgreSQL database. The old
autumn recruitment project is not on the normal crawler, API, Agent, or MCP
execution path.

```text
authorized OC snapshot + OC-backed Agent company catalog
                             |
       OC discovery -> company reconciliation (read-only)
                             |
       independent recruitment_core crawler registry
                             |
 acceptance -> incremental analysis -> safe offline reconciliation
                             |
       Agent-owned PostgreSQL / pgvector + task run stages
                             |
 FastAPI BFF -> Codex App Server -> typed RecruitOps MCP tools
                             |
       local workbench, MCP read surface, approvals, and audits

Optional one-time boundary:
old project config.yaml + SQLite/JSON --read-only migration--> Agent PostgreSQL
                                                              + companies.yaml
```

OC is the sole active company-discovery source. Tencent Docs and the migrated
legacy company list are not consulted by the daily runtime. Existing crawler
adapters remain reusable, but only for companies proven by the current OC
snapshot.

`packages/recruitment_core` contains the standalone crawler adapters. It must
not import legacy `crawlers`, `job_filters`, `job_cohorts`, or `job_details`, and
it must not rely on a fixed legacy source path. `packages/pipeline/daily.py`
selects connected rows from `config/companies.yaml`, runs the core, rejects
incomplete or unconfirmed results, and commits accepted Agent records through
bounded main-thread checkpoints. Company crawlers and JD hydration run in
killable subprocesses; model matching uses controlled concurrency, persists
every configured batch, and resumes unchanged successful analyses by versioned
fingerprints.

Only jobs with `cohort=2027`, `cohort_status=confirmed`, and a complete JD are
eligible for analysis. The deterministic matcher performs local screening.

The conversational runtime uses Codex App Server as its only model and tool-call
harness. DeepSeek is configured as a Responses-compatible provider. Codex owns
threads, turns, context continuation, tool choice, streaming events, retries,
and cancellation; FastAPI only adapts the App Server protocol for the local UI.
RecruitOps capabilities are exposed as typed MCP tools with Pydantic inputs and
structured evidence. The harness auto-accepts approval requests only for this
trusted local MCP server. Shell commands, arbitrary file writes, unknown MCP
servers, and implicit business writes are denied. High-risk business changes
still require the RecruitOps approval, validation, idempotency, and audit layer.

The complete daily flow is one high-level Harness operation. Codex starts
`daily_recruitment_sync` and receives a run ID immediately. Deterministic Python
services execute discovery, reconciliation, crawl, offline reconciliation and
reporting; `daily_recruitment_sync_status` reads persisted phase progress and
the in-process terminal result. The model does not plan one call per company.

## Persistence

- PostgreSQL is the runtime database for companies, jobs, analyses,
  applications, schedules, mail, task state, approvals, and audit records.
- Conversation history is keyed by an explicit `thread_id`: repeated questions
  append to the active thread, while only an explicit new-conversation action
  creates another sidebar item. Messages use a per-thread sequence number so
  equal timestamps cannot reorder a dialogue. Diagnostic API calls may disable
  history persistence explicitly.
- pgvector is available for approved Crawler/ATS knowledge. Structured
  candidate profile data is read from Agent config, not candidate-document RAG.
- Migrations are ordered, checksummed, and recorded in `schema_migrations`.
- Daily recruitment writes are limited to Agent-owned PostgreSQL. Approval-gated
  business writes remain disabled by default and are separate from the legacy
  source boundary.

## One-Time Migration

`scripts/import_legacy_snapshot.py` is the supported importer. It opens the old
SQLite database with `mode=ro`, fingerprints the three legacy input files
before/after reading, and writes only Agent-owned PostgreSQL snapshot tables and
the Agent `companies.yaml`. The legacy tree is never initialized, updated, or
scheduled as a recurring source.

## Integration Order

1. Apply Agent PostgreSQL migrations.
2. Optionally run the legacy importer once, in dry-run then apply mode.
3. Verify `config/companies.yaml` and the Agent database.
4. Run the independent crawler core or the daily task.
5. Expose persisted records through typed MCP tools and the Codex App Server
   thread/turn runtime; FastAPI remains a protocol BFF and local UI host.
6. Use real-site and browser/mail evidence to complete the still-pending
   acceptance work; fixture results do not prove production coverage.
