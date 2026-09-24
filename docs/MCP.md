# MCP Server

RecruitOps Agent uses MCP protocol version `25`. The full diagnostic catalog
contains 47 typed tools, including 28 read-only tools, while normal Codex
sessions receive a compact 39-tool Agent profile that omits low-level audit
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
`application_capture`, `daily_recruitment_sync_status`, `background_task_status`,
`application_review_status`, `recruitment_mail_run_status`, and
`recruitment_mail_binding_candidates`.

## Action Tools

`recruitment_mail_process`, `recruitment_mail_sync`, `application_status_update`,
`schedule_manage`, `application_edit`,
`observe_application_status_page`,
`batch_observe_application_status`, `verify_application_status_evidence`,
`cancel_browser_operation`, `operation_run`, `daily_recruitment_sync`,
`offerbiu_source_refresh`,
`automation_schedule`, `automation_schedule_disable`, `application_review_control`,
`daily_recruitment_sync_control`, `recruitment_mail_run_start`,
`recruitment_mail_run_control`, and `recruitment_mail_binding_propose`.

## Durable tasks and human mail association

Use `background_task_status` to discover active/recoverable tasks when a response
was lost or a conversation was restarted. Internal IDs remain in tool results;
the user need not copy them. Multiple recoverable candidates require selection,
not a guess. Status calls never start work. The local UI displays only active
tasks at `/api/local-ui/tasks/progress`, with separate crawl, review and mail
counters. Completed, failed and paused history is not displayed as running.

For full application review, pass `background=false` to
`batch_observe_application_status`. Keep the assistant turn open and continue
bounded waves with the same saved run ID while `continuation_required=true`.
Only the full recruitment crawl uses a background receipt followed by ending the reply.
An ordinary wave boundary is `awaiting_continuation`, not a user pause or a crash;
the active card is retained only for a bounded continuation window. Pause/cancel requests
are cooperative: retain the lease until in-flight operations reach a safe exit.
Resume preserves completed work and scope; it does not undo previous writes.
Old daily tasks that lack the new control receipt cannot be forcibly cancelled
through the UI. New tasks expose their supported actions explicitly.

`recruitment_mail_run_start` is the preferred assistant workflow for processing
mail and awaiting its result in the current turn. Start/resume/status wait up to
`wait_ms=20000` per call, below the MCP timeout. Continue read-only status waits
for that same run while `continuation_required=true`, then report its actual outcome;
do not tell the user to ask again later. A progress-only inquiry uses `wait_ms=0`.
It records sync and per-message progress, freezes message
IDs/content digests, and supports pause/cancel/resume. The older synchronous
`recruitment_mail_process` remains available for bounded diagnostic callers.
The local UI reads cached mail (`refresh=false`); explicit sync and startup sync
remain responsible for mailbox updates.

When association is ambiguous, search candidate applications and propose an
exact single-mail binding, correction or unbinding. A pending proposal is not a
write authorization: the user must approve the displayed email and application
in the UI. No approval tool is exposed to the model. Approved execution checks
mail digest, binding revision and target identity again, and stores an audit.
Binding provides identity evidence only; sender, event/time and forward-stage
rules still apply. Informational mail/company-level todos need not be linked.
Legacy placeholder confidence values are not presented as percentages.

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
