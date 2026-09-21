# MCP Server

RecruitOps Agent uses MCP protocol version `24`. The full diagnostic catalog
contains 38 typed tools, including 24 read-only tools, while normal Codex
sessions receive a compact 30-tool Agent profile that omits low-level audit
and administration primitives.
`packages/mcp/server.py` is the only tool registry; documentation and tests
must not maintain a competing list. The server uses Agent-owned PostgreSQL and
configuration. It never writes the old autumn recruitment project.

`knowledge_search` only supports approved crawler/candidate evidence. Personal document upload and retrieval are not available.

Validate the contract without a database connection:

```powershell
python scripts/run_mcp_server.py --check
```

Inspect or run the complete diagnostic catalog explicitly:

```powershell
python scripts/run_mcp_server.py --check --profile full
python scripts/run_mcp_server.py --profile full
```

Start the stdio server after PostgreSQL and migrations are ready:

```powershell
python scripts/run_mcp_server.py
```

The default `agent` profile preserves the two evidence-bearing application
status tools (`observe_application_status_page` and
`verify_application_status_evidence`) and the shared `application_status_update`
entry point for persisted mail/page evidence. It hides internal preview, cancellation and generic
operation tools that otherwise compete during routine tool selection.

Mail search, detail, and review first refresh the local cache with a bounded
TTL/singleflight hook. `recruitment_mail_sync` only synchronizes messages: it
does not update applications. Freshness failures are returned explicitly so
cached results are never represented as freshly synchronized mail. Updating
progress is a separate action, not a side effect of reading a message.
`recruitment_mail_processing_status` reads persisted processing state without
syncing, model calls, or writes, and reports proposed, verified, written,
unchanged, unresolved, and failed outcomes separately. The mutating
`recruitment_mail_process` tool is used once for an explicit processing request;
it refreshes mail once, skips already processed or terminal records, and returns
the service's actual bounded per-record results.

## Read-Only Tools

`capabilities`, `today_schedule`, `search_jobs`, `job_detail`,
`company_coverage`, `application_query`, `application_status_review`,
`browser_observation`, `crawler_acceptance`, `recruitment_mail_search`,
`recruitment_mail_detail`, `recruitment_mail_review`,
`recruitment_mail_processing_status`, `schedule_window`,
`edge_connection_status`, `browser_operation_status`, `knowledge_search`,
`configured_crawler_run`, `public_recruitment_entry_discovery`,
`public_recruitment_entry_validate`,
`automation_plan`, `automation_schedule_list`,
`application_capture`, and `daily_recruitment_sync_status`.

## Action Tools

`recruitment_mail_process`, `recruitment_mail_sync`, `application_status_update`,
`schedule_manage`,
`observe_application_status_page`,
`batch_observe_application_status`, `verify_application_status_evidence`,
`cancel_browser_operation`, `operation_run`, `daily_recruitment_sync`,
`offerbiu_source_refresh`,
`automation_schedule`, and `automation_schedule_disable`.

`schedule_manage` creates or updates a local todo/calendar item without changing application
progress or entering the application approval flow. Create requires a stable `request_key` and
rejects same-key payload changes; update uses `event_id` and can use `expected_updated_at` for
optimistic concurrency. `company_name` is required, `job_title` may be empty, and an explicit
application binding must match its company/job (an empty job is filled from that application).
Company-level items may remain unbound. No-date items are `unspecified`; adding a date produces an
`appointment` unless explicitly a deadline, and no duration is inferred.

`daily_recruitment_sync` is the preferred whole-batch operation. It delegates
to deterministic Python stages for trusted-source discovery, company
reconciliation, configured-company crawling, safe offline reconciliation, and
reporting. The model starts or schedules the operation but does not improvise
hundreds of per-company calls.
The start call returns a run ID immediately. The model uses
`daily_recruitment_sync_status` to inspect progress or the terminal result.
The daily operation accepts `full`, `crawl_only`, `score_only`, and `resume`.
For a full company queue, omit both company ID lists; the ten-ID bound applies
only to explicit pilots. Both `full` and `crawl_only` refresh sources internally.
`full` scores when model configuration enables it; `crawl_only` makes no scoring
calls. Resume restores the persisted original scope and completed work and rejects
missing recovery evidence. It must not turn a bounded pilot into an all-company run.
`offerbiu_source_refresh` registers only usable BIU company entries from a
fully validated 2027 autumn-recruitment snapshot; partial captures do not write.

The retired OC discovery tools are not registered in either MCP profile. BIU is
the active company-discovery source; historical job provenance remains intact.

High-risk business writes remain approval-gated. The explicit source-refresh tool
can persist validated source records; it is not a read-only tool. It does not
rewrite the saved company configuration. Login, CAPTCHA, unclear browser evidence,
and destructive actions fail closed.
