---
name: schedule-management
description: Inspect recruitment schedules, detect conflicts, and manage local interview or test events.
---

# Schedule Management

1. Query the requested date or range and identify timezone, application, source, and conflicts.
2. Prefer evidence from associated recruitment mail or explicit user input.
3. Create or update events idempotently. Do not duplicate the same test or interview.
   For an explicit local todo or calendar mutation, use `schedule_manage`: create requires a
   stable `request_key`, and reusing that key with a different normalized payload is a conflict.
   Update uses `event_id`; pass `expected_updated_at` when available to avoid overwriting a
   concurrent edit. This local write does not change application progress or require the
   application approval flow.
4. Do not call `schedule_manage(action=create)` to recreate items produced by
   `recruitment_mail_process`. If a user explicitly asks to edit an existing returned item,
   `schedule_manage(action=update)` is allowed and must preserve its source identity.
   Mail-derived `assessment`, `written_test`, `interview`, and `action_required` records already
   return their source-bound `schedule_item`; company-only items may intentionally have no
   `application_id`.
5. Keep schedule identity explicit. `company_name` is required; `job_title` may be empty for a
   company-wide task. When `application_id` is supplied, the company must match that application
   and a non-empty job must match it too; an empty job is completed from the selected application.
   Resolve the application first rather than guessing a binding.
6. Keep time semantics factual: an item without `event_date` has `time_kind=unspecified`; adding
   a date canonicalizes it to `appointment` unless it is explicitly a deadline. A date without a
   clock remains date-only, and no duration or appointment end time may be invented.
7. Ask only when time, target application, or requested mutation is ambiguous.
8. Report conflicts and persisted changes explicitly.
9. For a recurring request to "update/check" a company or job that already exists in the
   application database, resolve the application first and map the request to the local
   `application_progress` task. This is an application status review, not a crawler refresh.
10. Use only the four local task identifiers documented in `AGENTS.md`. Never mention cloud
   scheduling, GitHub Actions, or legacy task aliases as current capabilities.
11. A direct recurring-task request is already authorization for the reversible local schedule
   write. Call `automation_schedule` and verify `active=true`; do not stop at `automation_plan`.
   Bind an application progress schedule to the resolved `application_id`. Use list/disable tools
   for status or cancellation requests.
