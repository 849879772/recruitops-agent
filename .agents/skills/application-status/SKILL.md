---
name: application-status
description: Verify and update recorded applications from page evidence through the connected desktop or Edge browser bridge.
---

# Application Status

## Batch Review

For "复核官网投递状态" or all current applications, call
`batch_observe_application_status(all_non_terminal=true)` directly. The service selects
all saved applications except rejected/withdrawn; do not query or infer IDs first.
Omit `timeout_ms`, or set it to at most `120000`. The complete review uses bounded waves,
not a longer single call. Continue using only the returned `run_id` while `remaining_count`
is positive; do not start a new full review after a timeout. A completed checkpoint is not
the same as successful verification: report updated/unchanged separately from errors.
`processed_count` counts attempted unique records, including retryable failures; use
`completed_count` for completed records and `remaining_count` for those still needing work.
Never subtract attempted records from the scope to invent a remaining count.
Use the individual workflow below only for a specific application or justified follow-up.

## Individual Review

0. If the requested status change comes from a persisted recruitment email, stop this page workflow
   and follow the `recruitment-mail` skill. Authenticated, uniquely bound explicit mail evidence goes
   directly to `application_status_update`; the absence of a matching status on the recruitment page
   does not veto a later email event.
1. Query applications using company, job title, job ID, and aliases until exactly one record is
   identified. Do not assume a missing first search means no application exists.
2. Check `edge_connection_status` before starting browser work.
3. Call `observe_application_status_page` with the unique application identifier and
   `include_vision=false`. Interpret the sanitized page text, semantic nodes, ARIA state, layout,
   and visual emphasis. Do not use the legacy one-shot parser.
   First inspect `application_records`. If exactly one target card has only its submission
   and the saved stage is `applied`, submit that card's verbatim evidence to the verifier
   as `applied`, even when its status label is empty. This is a read-only unchanged
   confirmation, not a low-confidence write. An empty canonical status does NOT prove
   absence of status: inspect label, raw_status_labels, context and signals first.
   For `status_unmapped`, interpret the returned target-card text directly and submit its
   actual status quotation to the verifier. Do not classify unknown text as unchanged.
   Do not request another page or screenshot when the returned DOM already contains the text.
   A clearly bound explicit page outcome is sufficient; a matching email is not mandatory.
   Never require the entire page to be free
   of terminal labels: another role or volunteer's termination belongs to that other card.
4. If the individual DOM-only observation has no bindable structured status evidence but the
   visible screenshot plausibly contains status text, call `observe_application_status_page` again
   with a new idempotency key, `include_vision=true`, and
   `vision_fallback_reason=no_structured_evidence_visible_status_likely`. Send the screenshot only
   to the configured DeepSeek vision model when this fallback is necessary; do not send screenshots
   for blank pages, timeouts, login walls, CAPTCHA, or ambiguous targets, and never solve a CAPTCHA.
   The structured vision result does not replace source binding or the status verifier.
   For visual evidence, quote the target job title together with its visible status from
   `vision.text`; do not invent DOM text or raise confidence above `vision.confidence`.
5. Call `verify_application_status_evidence` with the canonical status, observed label, a short
   verbatim fragment from that observation, and calibrated confidence. The verifier, not the
   model, owns application binding, URL checks, forward-only transitions, and database writes.
6. Follow browser events through a terminal state. A verified page that still shows the stored
   stage is `unchanged`, not `STATE_UNCLEAR`; use that state only after the optional vision fallback
   still leaves the page, status meaning, or evidence binding ambiguous.
7. Treat `COMMAND_INVALID` or `ACTION_NOT_ALLOWED` before navigation as an Edge extension protocol
   mismatch. Ask the user to reload the unpacked extension and verify version `0.3.22`; do not blame
   login state or the recruitment page. Treat `LOGIN_REQUIRED` and `CAPTCHA_REQUIRED` as genuine
   page pauses only when the browser result explicitly reports them. Treat a visible phone/email
   identity-verification wall as the same user-facing `需要登录或验证` category.
   A login/register shell with no application cards is `需要登录或验证` (including H3C),
   not `无法确认`. When one exact target card shows only its submission and no newer
   status, retain an existing `applied` stage as `状态未变化`; another job card's
   termination is not evidence against this target. Never infer this from an empty
   record list or roll back a later stage or mail rejection.
8. Write all narration and summaries in Chinese, including intermediate updates.
   Never explain a past result as a rendering failure unless its persisted observation proves it.
   On verification_internal_error or retryable=false, stop that evidence attempt; changing
   quotation length or switching channels does not repair an internal tool error.
   Explain failures using only this turn's returned reason codes and observations.
   Do not reuse earlier confidence/conflict explanations when the current tool returned
   missing records or an unavailable page. Never claim rate limiting without evidence.
   A requested retry may use one bounded follow-up observation; if no new evidence appears,
   stop and report the actual limitation instead of repeating the same calls.
   Return the old status, observed status, confidence, evidence summary, write result, and audit ID when available.
   Keep internal enum keys unchanged, but present batch categories to the user only as `已更新`,
   `状态未变化`, `需要登录或验证`, `无法确认`, and `执行失败`. For `无法确认`, state clearly that
   the saved database stage was retained and no write occurred.
   When an authenticated ATS shell loads its page-not-found state without requesting application
   records, say `官方投递记录页已不可用，数据库保留原状态`; do not call it a login failure or a
   verified unchanged result.
9. Treat "更新/检查/同步 + 公司或岗位" as this workflow whenever a matching application exists,
   even if the user does not repeat the words "投递状态". A daily or timed version of the same
   request maps to the local `application_progress` automation task.
