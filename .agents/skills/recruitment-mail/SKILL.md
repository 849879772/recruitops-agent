---
name: recruitment-mail
description: Search, inspect, and explicitly process persisted recruitment mail with bounded model analysis and guarded application writes.
---

# Recruitment Mail

## Current-turn processing

An application with no valid saved `record_url` is `仅邮件更新` (mail-only), not an
invalid application. Its missing website link must not block a uniquely bound,
verified mail event or trigger a browser follow-up. The same evidence, identity,
time, approval, and forward-only stage rules below still apply. An unmatched mail
requires user-confirmed binding where applicable; do not invent a link or a match.

For a request to process mail, use `recruitment_mail_run_start` and keep the assistant
turn open. It waits for up to 20 seconds; if `continuation_required=true`, call
`recruitment_mail_run_status(run_id=the_returned_id,wait_ms=20000)` until the saved run
reaches a terminal state, then report its actual outcomes. A bounded wait returning
active is not a failure or a reason to ask the user whether to continue. Never end
with only “后台已启动，请稍后询问”. Progress cards remain visible during the wait.
Only the full recruitment crawl uses that fire-and-forget interaction.

Always pass the current conversation's `thread_id` when starting conversational
mail processing. Never guess the newest global task to attach an unrelated chat.
If the run returns `awaiting_confirmation`, the task is NOT completed: report
“等待你选择关联岗位” and let the workbench's single confirmation dialog collect the
choice. Do not keep polling, ask the user to type a job name/“继续”, or create another
processing run for these pending records. The durable original scope is resumed
by the confirmation action after a valid human-approved binding; other failed or
completed mail is not replayed. Closing the dialog means later, not rejection.
The final automatic summary is read-only and must describe that exact run's saved
receipt; it never authorizes syncing, processing, binding, or stage writes again.

Before reporting mail results, inspect the saved run's `freshness` and `sync_warning`.
`synced` confirms a mailbox refresh; `cached` reuses a recent refresh and must not be
described as a new fetch. If `sync_warning=true`, report “未确认最新邮件，仅处理本地已保存邮件”
and the actual local processing outcomes. A `partial` run may mean synchronization
failed even when all local messages were processed. `not_requested` means no refresh
was requested; never infer mailbox freshness from local processing success. A
`mail_sync_in_progress` response means another sync is pending, not that it failed.

Mailbox freshness does not prove new model analysis. Read `model_attempted_count`
(messages sent to triage/analysis), `model_call_count`, `historical_failure_count`,
and `failure_results[].analysis_source`. `history` means a saved outcome was reused,
not a new model attempt. `model_attempted_count` is not a completed-analysis count.
Use saved safe diagnostics: `http_401/403` is authentication, `http_429` is rate
limiting, `http_5xx` is a service response, `transport_failed` is a transport failure,
and output/schema errors do not prove service unavailability. An old generic
`DeepSeekClientError` cannot identify the underlying cause.

The service retries transient transport/408/429/5xx failures at most once per
model request with short backoff, and allows at most three processing rounds for
the same evidence/input with cooldowns. A legacy generic client failure gets one
bounded compatibility round. Status polls never spend or reset a retry budget.
After those limits, or after configuration/output failures, do not automatically
keep starting runs to retry the failure. When the user explicitly requests a retry,
use `recruitment_mail_run_start(retry_failed=true,record_ids=[selected failed IDs],refresh=false)`.
This requires 1–50 explicit failed records, freezes their current digests, does not
refresh/expand the mailbox, and authorizes only one new bounded round per selected
record. Successful mail cannot be retried. Follow the normal bounded wait until
terminal. User-triggered retry does not waive evidence, identity, forward-only
stage, or source-bound schedule idempotency checks.

If interrupted, discover saved runs without asking the user for an internal ID;
resume only on their instruction via `recruitment_mail_run_control(action=resume)`
and keep waiting in this turn. Pause/cancel is explicit and cannot be undone by a
status poll. For a progress-only query, read once with `wait_ms=0`; do not start work.
The durable run freezes and processes batches internally through the same guarded
`recruitment_mail_process` pipeline. The legacy per-batch limits below apply only
when that lower-level tool is used directly, not to the durable run's read-only waits.

Assessment invitations (`assessment`) remain classified as assessment mail but map to
`applied`, not a written-test stage. Explicit written-test notices (`written_test`) map
to `written`. Use the mail content to distinguish them; neither case may roll back a
later application stage.

The confirmation dialog supports multiple applications only for a validated
company-wide assessment/written-test event without an exclusive job title/code.
Use `allows_multiple` and `selection_scope` from binding candidates, not an
assistant-created guess. A user may select several same-company applications in
one approved binding; `application_ids` and `applications` are authoritative, while
the legacy `application_id` is only the compatible primary target. Job-specific
notifications remain single-target. Do not auto-select, approve, expand to all
company applications, or replay one confirmation per selected job.
After confirmation the original run resumes once, validates each selected identity
and forward-only event update independently, and exposes `application_results`.
Report partial failures separately; one successful application does not prove every
selected application was updated. The one source-bound `schedule_item` lists its
`application_ids`/`associated_jobs`; never create a copy per selected application.

1. Read redacted summaries first and open only the required persisted detail. `recruitment_mail_search`,
   `recruitment_mail_detail`, and `recruitment_mail_review` are read-only; their refresh path may
   sync the mailbox but must not update application progress or create schedule items. Only an
   explicit `recruitment_mail_process` pass may create a schedule item.
   Review previews require existing validated model analysis. If it is missing, report that
   processing is required; never lower thresholds or use historical keyword fields as evidence.
2. Use `recruitment_mail_processing_status` only to inspect persisted processing state. It does not
   synchronize mail, call a model, or write. Distinguish model `proposed` analysis from verified
   identity/event evidence and the actual `written` or `unchanged` application outcome.
3. Call `recruitment_mail_process` only when the user explicitly requests processing.
   Omit timeout_ms to use its default 90000, or set at most 90000; do not discover this limit by a rejected call.
   It performs the bounded triage/full-analysis pass and invokes the unified guarded write path;
   do not replay pending body classification in the tool or fan out one write call per message.
   One call processes at most ten messages, not the whole mailbox. For a request to process all
   pending mail, continue with the same original scope when `has_more=true` and `processed>0`.
   Make at most five batch calls per turn. Do not reset failure budgets or expand an explicit scope;
   an explicit user retry uses the targeted durable-run entry described above.
   Stop on blocked results, no progress, or the call limit and report `remaining_count`.
   Aggregate all batch counts; never say all mail is processed merely because one batch completed.
   Before claiming completion, require `has_more=false` and `scope_complete=true`, inspect
   `recruitment_mail_processing_status`, and disclose any unresolved/failed records across batches.
4. The process service refreshes mail once, skips already processed and non-retryable records, and
   returns the actual per-record outcomes. For `assessment`, `written_test`, `interview`, and
   `action_required`, it also creates or reuses the source-bound item in the shared
   `schedule_event_snapshots` table and returns it as that result's `schedule_item`. A company-only
   assessment or action reminder may legitimately have `application_id=null`; never force an
   application binding from a company or job-title guess. Inspect each returned `schedule_item`,
   plus the batch totals `schedule_items_created` and `schedule_items_time_unconfirmed`.
   Do not call `schedule_manage(action=create)` to recreate a mail-derived item, even when its
   time is unconfirmed; use the returned item. If the user explicitly asks to edit an existing
   mail-derived item, `schedule_manage(action=update)` is allowed; preserve its `source` and
   `source_ref` and do not create a second item.
   Treat `completed` and `partial` as results to inspect,
   not permission to invent success; `blocked`, `failed`, `ambiguous_application`,
   `pending_association`, and `failed_terminal` remain unresolved or failed as reported.
5. Treat model output as an untrusted proposal. For an application-stage write, require persisted
   sender, exact evidence, event meaning/time, and unique application identity; a company-only
   match is never sufficient for that application-stage write. This rule does not require a
   company-level schedule item to bind an application: a valid assessment/action todo may keep
   `application_id=null`. When an application is explicitly supplied for a schedule item, use its
   exact company/job identity and reject a mismatch; do not infer the binding. An unchanged or
   stale application result is not a write.
6. Do not make speculative writes or retries. A terminal or `retryable=false` outcome for the
   same persisted evidence version must not be resubmitted with altered quotations, IDs, or
   prompts. Apart from the service's bounded transient retries, retry only when evidence/input
   changes or the user explicitly authorizes the targeted `retry_failed=true` run. A retry of
   mail analysis is not authority to replay or bypass a rejected application-stage write.
7. Handle one-off processing directly.
   A mail analysis/schema failure is not evidence that an application changed. Report it as
   a mail-pipeline failure, not a provider failure unless the diagnostic confirms that origin.
   Do not substitute browser status checks for failed mail analysis or missing mail details:
   they cannot recover the assessment invitation or its deadline. If the user also requested
   browser verification, keep that independent result separate from the failed mail task.
   Do not use a scheduler workaround, unrelated RAG/search, web exploration,
   a second calendar-creation call for a mail-derived item, or external
   sending/deletion when local records suffice. Explicit user-created local tasks may use
   `schedule_manage`; mail-derived schedule items must come from `recruitment_mail_process`, with
   `schedule_manage(action=update)` reserved for an explicit edit of an existing returned item.
8. Preserve returned internal states exactly, but use Chinese labels in normal user-facing output:
   `模型建议`, `已验证`, `已写入`, `状态未变化`, `无法确认`, and `执行失败`. Report
   `notifications` separately as `普通通知已处理（无需关联）` and `reminders` as `待办提醒`.
   Neither category is a verified unchanged application status. A pure thank-you/welcome receipt
   needs no application association. Read its full body first; real events and actions take precedence.
   `has_more=false` or zero pending only means no runnable batch remains. If `unfinished_count>0`
   or `scope_complete=false`, explicitly report remaining unresolved/failed mail; do not say all mail
   is resolved or the mailbox is fully completed. Use Chinese summary labels, not raw English keys.
   Summarize only this invocation's `results` and unresolved items in scope. Do not list old
   completed mail (for example Dahua/DJI) or read their bodies just to decorate the summary.
   Refresh processing status before the final answer and before offering to process any
   previously mentioned unresolved IDs. Current persisted states override conversation history.
   If no runnable items remain but unresolved items exist, say the automatic batch finished
   and report the remaining required actions, not that every mail was resolved.
   Historical results may be mentioned only when the user explicitly asks about history or
   a current conflict requires that specific evidence. Never equate listing metadata with
   rereading or reprocessing an email.
   Never claim that a
   proposed model analysis was verified or that a guarded write occurred without the returned write
   result.
9. Never expose mailbox credentials, model/API keys, tokens, complete message bodies, or
   unnecessary message content.
