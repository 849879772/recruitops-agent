# Recruitment Capture Workflow

## Active Discovery Source

OfferBiu is the only active company-discovery source. Daily discovery must use
`offerbiu_source_refresh`; it must not read OC snapshots, open GiveMeOC, or invoke
retired OC candidate and browser-capture operations. Removing an active source
does not delete jobs, analyses, applications, or historical provenance already
stored in PostgreSQL.

For a bounded new-company workflow, a complete `offerbiu_source_refresh` returns
at most twenty readable, distinct unregistered companies in `pending_entries`.
Pass up to ten of those `record_id` values directly to
`daily_recruitment_sync(source_record_ids=[...])`. The runtime creates a
run-scoped company configuration and executes the same formal title-first crawl,
JD capture, deduplicated persistence, optional scoring, and reporting path. Do
not query the complete company catalog or use web search as an intermediate
identity bridge.

For a normal all-company run, invoke `daily_recruitment_sync(mode="full")` once
without `company_ids` or `source_record_ids`. The operation refreshes the complete
OfferBiu snapshot, builds a run-scoped catalog from every currently crawlable BIU
company, deduplicates aliases and multiple entry URLs, then merges nonduplicate
legacy configured companies. This includes previously complete, partial, failed,
and pending BIU companies so each run can detect new titles and missing titles.
The bounded `pending_entries` response is for manual scoped runs only and must not
limit an unscoped full run. Invalid destinations and application forms remain
outside the run and are reconsidered by the next complete source refresh.
Use unscoped `mode="crawl_only"` for the same discovery and company capture
without scoring. Do not manually call source refresh first for either full mode.

## Card Summary And Drawer Detail

Some official campus pages render a shortened responsibility summary inside each
job card and expose the complete responsibilities and requirements in a drawer or
dialog. Treat the visible card as list evidence only. Configure a `dialog`
`detail_interaction` whose trigger is inside the identity-matched card and whose
container is the unique visible drawer/dialog. Accept the captured JD only when
the requested title identifies exactly one card, the opened container repeats the
same title, loading is terminal, no expansion controls remain, and the persisted
receipt hash matches the captured text. Do not certify the page-wide DOM or a card
summary as the official complete JD.

Job cards may arrive after the document-ready event. Poll for the exact
identity-bound trigger within a bounded deadline instead of scanning once or
using a fixed sleep. Mark the resolved detail container in the returned DOM and
parse only that scope; titles still visible behind a drawer are not competing
detail identities. Serialize requests per host when a slow site becomes unstable
under concurrent browser sessions.

## Same-URL Recruitment Scope Switching

Some official sites open social recruitment by default and switch to campus
recruitment without changing the URL. Select and verify the campus control before
collecting list snapshots. If the scope changes, exclude network observations
captured before the click; run any blank search only after campus selection.

Carry the required entry clicks into detail hydration. A list page's generic site
heading is not job identity evidence: an inconclusive or mismatched plain-HTTP
list shell must fall through to the bounded renderer. Bind the requested title to
one job card or official native ID, and retain a terminal text-bound receipt.
Jobs visible only in the social scope must not enter the campus result.
Keep the complete raw campus list for pagination auditing, then apply title-only
screening before JD hydration. Titles explicitly containing internship markers
are recorded as filtered observations and must not be fetched, imported as
eligible jobs, or scored.

## Latest OfferBiu Cohort Policy

The user now explicitly assigns all OfferBiu-sourced companies/jobs to 2027,
overriding the earlier warning and other-year restriction below for this source.
Use `apply_offerbiu_cohort`, retaining original evidence separately and recording
`offerbiu_user_policy` as provenance. Do not claim official confirmation from this
assignment. Other sources retain their cohort rules. Replay schema v3 prevents
accidental reuse of an earlier-policy output directory. No production migration
or live retest is implied by this implementation change.

## Live Pilot Admission Warning

Follow-up fix: the default title-first hydrator now sends the explicit
`detail_capture_policy=title_first_v2` marker. The detail helper allows unknown
or unconfirmed cohort metadata under this policy without modifying evidence.
Explicit other-year or conflicting evidence still blocks, and legacy callers
retain their previous gate. Capture fidelity checks are unchanged. Use a new
pilot output directory for retesting; old frozen results remain historical.

The September 9 title-first pilot identified an admission mismatch: the lower
detail helper rejects unknown cohort metadata before networking, even after
title-first queue selection. Report this as `cohort_ineligible`, not website
capture failure. Do not remove or forge evidence to make diagnostic cases pass.
Keep pre-fetch blocks separate from failed requests and verified bodies; align
source admission and detail policy before expanding the pilot. Evidence:
[Live Pilot](TITLE_FIRST_LIVE_PILOT_20260909.md).

## Latest Implementation Direction

The user-approved title-first incremental design is implemented and locally verified under
[Title-First Capture Plan](TITLE_FIRST_CAPTURE_PLAN_20260909.md). It replaces
routine stored-JD refresh and complex catalog identity reconciliation for existing
same-company titles in the default daily/scheduled path. Preserve all failed company/job records,
separate availability from capture status, and score new successful jobs only
after collection ends. This is not a deployed behavior claim; the sections below
document the preceding pipeline. Broader regression and full frozen-data replay
are recorded in [Title-First Acceptance](TITLE_FIRST_ACCEPTANCE_20260909.md).
The replay keeps immutable inputs, pairs each chosen JD with its own receipt,
checks the body hash and terminal loading evidence, and uses the formal profile
loader. Historical missing-title plans remain blocked from production execution.

The latest revision retries a stored missing/failed detail when that exact company
and title reappears in the current eligible list. A shared
`stored_detail_retry_required` check distinguishes empty/explicitly unfinished
capture from nonempty legacy text without metadata. Keep old IDs and completed
scores; failed repair must not erase prior content. Do not scan absent old jobs
or loop-retry a title within a run. Successful existing details still skip refresh.

The current official-content capture policy is documented in
[Official Capture And Source Visibility](RECRUITMENT_CAPTURE_CONTRACT_20260908.md).

Persist source records before fetching. Keep original and corrected recruitment links even when
the entry is a form, article, unsupported platform or failed request. An attempt updates the
source history independently of job ingestion.

Determine the recruitment cohort before detail hydration or model work. Traverse lists with
explicit pagination/loading termination evidence and retain virtual-list windows. Capture the
job's official API/detail/expanded content with verified identity and a text-bound receipt.
Never use description length or responsibilities/requirements headings as a scoring gate.

Apply existing scope and direction filters independently. Admit eligible rows independently
from other rows' failures. Do not run full scoring as part of source-quality evaluation.

Validate changes with frozen tests, then bounded anonymous live samples. Report company-wide
coverage separately from successful individual details. Keep the current verification delta in
`outputs/company_integration_status.md` and record production migration/deployment separately.

## Efficiency Verification

See [OfferBiu Efficiency Audit](OFFERBIU_EFFICIENCY_AUDIT_20260909.md) for measured
startup costs and duplicate-request evidence. Keep official request identity
separate from source-company provenance. Preserve full API fields and capture
receipts at first retrieval before reusing a list JD; do not certify legacy text
from length or normalized similarity. Benchmark successful unique details per
minute, requests, memory and failures before claiming an end-to-end speedup.
The run-scoped `ReusingDetailHydrator` is integrated into the daily pipeline and
the OfferBiu hydration script. Its controlled ByteDance/Feishu routes require a
matching tenant, native post ID, title and complete official API receipt. Each
source record and checkpoint remains independent; request reuse is not employer
merging. Concurrent duplicate callers share one request, and unsuccessful or
unverified responses are not stored in the cache. The bounded cache is cleared at
run end, so it does not hide official changes between scheduled runs.

ByteDance list captures now preserve full official fields and construct receipts
from observed API response context. Hotjob's list-side detail fetch opts into full
text and identity-bound receipts; its legacy helper contract remains unchanged.
Only a verified text-bound receipt permits skipping a subsequent detail fetch.
One complete detail does not establish complete company pagination.

See [Efficiency Acceptance](OFFERBIU_EFFICIENCY_ACCEPTANCE_20260909.md) for the exact
offline and anonymous live verification scope. Split HTTP/browser scheduling,
persistent transport reuse and wider platform receipt coverage remain pending.
Existing running captures and production deployment are not changed by editing
these modules; deployment and any checkpoint-preserving restart are separate steps.

## Persisted Detail Refresh

The daily pipeline may reuse a stored official detail only when its receipt,
text hash, job identity, source URL and observed capture time remain valid. The
default refresh interval is 24 hours, configurable on the pipeline constructor.
This is cache freshness, not a recruitment deadline or a job's online/offline
status. Expiry schedules a detail check only when a later crawl encounters it.

Record `capture_evidence.captured_at` at an actual successful response capture.
Do not replace it during list rediscovery, receipt validation, cache reuse, or
unrelated row writes. Missing or future capture times require a refresh; an
old `updated_at` cannot manufacture a capture time. A complete but stale input
receipt must be removed from the isolated fetch input to prevent the hydrator
from returning the same stored body without making a request. Preserve the
original receipt separately. Failed refreshes keep the old JD, score and capture
timestamp, and are reported as failed rather than successful freshness checks.

## Offline Screening Before Reconciliation

Use `scripts/screen_offerbiu_capture.py screen` on the frozen capture and the
existing candidate profile. Use verified successful `candidate_jd_raw` plus its
bound capture evidence when available; preserve original source rows. Scope,
direction and capture deferrals remain separate from definite exclusions.
Only `passed-only.jsonl` enters the script's `reconcile` command, against a
PostgreSQL READ ONLY catalog export. Neither command calls a model or writes
production data.

Reconcile native identities within controlled tenants, canonical detail URLs
and company aliases, never titles alone. Distinguish exactly reproducible legacy
internal hashes from genuine official IDs; do not discard arbitrary conflicting
IDs. Report source-row counts and distinct work groups separately, retaining all
source provenance. A successful finite existing score is retained regardless of
provider version; changed text is a separate reconciliation result, not automatic
permission to rescore. This offline policy does not itself change every runtime
matching-service version/fingerprint reuse rule.
# Entry admission boundary

- Reject missing entries, forms, articles, login/personal-center pages, and other unusable destinations before source/catalog persistence. A later full source discovery evaluates the feed again, so a corrected upstream URL can enter normally.
- Preserve a company failure only after a crawlable recruitment entry was admitted and the list crawl failed or remained incomplete.
- Preserve a job failure when a relevant title was observed from an admitted company but its official detail capture failed. Store the title, company, attempted official URL, and failure reason; do not score it until capture succeeds.
# BIU discovery and resumable daily execution

- An unscoped daily operation owns the source refresh internally. Its refresh is
  accepted only when every public API page is present and the source totals are
  stable. WeChat pages, forms, login pages, missing URLs, and other unusable
  entries are excluded before registration.
- Use `daily_recruitment_sync(mode="crawl_only")` for crawling without model
  calls, `score_only` for scoring persisted eligible JDs, and `resume` with the
  interrupted run ID after a process restart. `full` performs crawl followed by
  scoring.
- Scoring checkpoints are persisted after every bounded wave. Empty or invalid
  model output receives rate-limited retries; a scoring failure never returns to
  JD hydration.
- API startup changes stale `running` task leases to `stopped` with a
  `recoverable:` stage prefix and `process_interrupted` error code.

## Full-Run Repair Acceptance (2026-09-13)

- Scope list totals to the active company and campus list. Application quotas,
  graduation years, navigation badges, and unrelated detail text are not list totals.
- Extract real list items structurally before applying business title keywords.
  Raw list IDs/counts serve coverage checks only; rejected titles and their details
  do not enter the business job catalog. Do not compare filtered counts with an
  unfiltered official total.
- Traverse category tabs, pagination, load-more controls, and virtual-list windows
  within a bounded budget. Preserve the union of observed identities. A stopped
  loader, exhausted budget, missing selector, or blocked request is not proof of
  full coverage. Store the actual failure stage and terminal evidence.
- A resume must restore the original frozen company configuration and completed
  progress. Reject missing or incompatible checkpoints rather than silently
  rediscovering companies. A scoring-stage resume must not open websites.
- Use deterministic fixtures, previous successful controls, and public-site holdout
  samples. No production scoring is required for crawler regression. Check the
  production job, analysis and application digests before/after deployment.
- Separate a finished task from fully captured sources. Report company list
  completeness, detail completeness and pending scores independently.
