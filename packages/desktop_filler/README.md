# Desktop Filler Adapter And Bundled Engine

This package contains only original adapter code. It does not redistribute the
user-selected extension, resume, PDF, browser storage, binding or keys. No license
file or package license field was found in the inspected local extension; do not
bundle its source without a separate grant.

## T09-T13 Delivery Contract

`loadBundledFiller()` is the default distributable entry point: an immutable,
hashed, independently authored engine embedded in this package. It reads no
extension directory or personal data. Package this module as a trusted resource;
do not accept source text or a bundle from renderer IPC. `loadLocalFiller` remains
an explicit trusted compatibility option, not a first-run requirement.

- `buildScanScript`, `buildFillScript`, `buildUndoScript` accept an optional third
  (undo: second) `FrameContext`. Main owns instance/tab/frame/document/profile
  version/URL, selects the actual execution frame, and permits subframes only
  after origin and sandbox checks. Without context, behavior remains top-only.
  Run every operation in the same dedicated isolated world of that frame.
- Main enumerates reachable owned frames, including nested frames. Report blocked,
  sandboxed, detached and unreachable frames through `aggregateFrameScans` rather
  than silently omitting them. It returns unique selection IDs and partial status.
  Keep selection-to-frame/scan/field mapping in main, never trust renderer routing.
  IDs are local to frames. Invalidate on reload, detach, navigation or profile edit.
  The adapter does not bypass sandbox/origin policies or itself enumerate frames.
- Native text, textarea, date/number/search inputs, checkbox, radio, select,
  multiselect and contenteditable use input/change events and read-back verification.
  Exact `data-resume-key` profile paths, unique field-name matches and a limited
  set of name/email/phone/gender/birth-date aliases are supported. No invented data.
  Custom ARIA search/select widgets are diagnosed as unsupported, not falsely filled.
- `profile.customAnswers` contains `{id,origin,pathname,label,value}`. Exact scope
  and label match is required. Duplicate answers produce `filler_answer_conflict`.
  Caller owns editing/deletion/persistence; change profileVersion and rescan after
  any edit. Preview contains sensitive values; diagnostics contain only IDs/codes.
- `buildPrepareScript(bundle,{scanId,sectionIds,confirmed:true},context)` is a write
  action. Only explicitly recognized `data-resume-section` roots with direct
  `data-resume-record` children and a direct `button[type=button][data-resume-add]`
  are supported. Section IDs select profile arrays; fields inside records use
  record-local keys. At most 10 records per section, 10 sections per request.
  MutationObserver checks additions, not sleep-based assumptions. A no-progress
  timeout is unknown and locks the world until page reload, preventing late duplicate
  additions. Rescan after preparing. Structural undo is NOT implemented.
- `buildUploadScript` validates a confirmed scan target and trusted attachment
  metadata, then returns an UploadTicket. It uploads nothing. Only resume/CV
  file inputs are candidates; photo/transcript/certificate inputs are excluded.
  Main selects one target, validates actual file bytes/type/size and stored ID,
  and never exposes arbitrary paths to the renderer/page. Max metadata size: 20MiB;
  accept and optional `data-max-bytes` constraints are checked before authorization.
- Immediately before file assignment, main evaluates `buildUploadTargetScript`
  in the SAME world via CDP `Runtime.evaluate` with `returnByValue:false`.
  It returns the original DOM node, once, within 10 seconds, after revalidation.
  Use its remote objectId with `DOM.setFileInputFiles`, not a newly queried selector.
  Revalidate frame/document generation at the host boundary and release object IDs.
  A successful ticket/claim is NOT evidence of upload or website acceptance.
- Poll `buildUploadStatusScript` only with its retained uploadId. `files` matching
  metadata proves selection only. Explicit `data-resume-upload-status` values
  loading/complete/failed support site-specific signals. No signal means parse
  unknown, not success; document replacement and a 60s timeout are also unknown.
  Host needs a trusted site adapter or user confirmation for unannotated sites.
  Upload invalidates the old preview; rescan only after confirmed parsing completion.
  Remote uploads cannot be undone locally.
- Built-in fill reports filled/failed/skipped IDs with matching counts and stable
  codes. Partial undo restores only unchanged original nodes and preserves later
  user edits, including radio-group changes. Legacy undo remains all-or-nothing.
  No SMS/OTP, password handling, save/submit clicks or automatic application submission.

Stable diagnostic codes include `filler_frame_{blocked,sandboxed,detached,unreachable}`,
`filler_frame_context_changed`, `filler_navigation_changed`, `filler_stale_scan`,
`filler_scan_expired`, `filler_field_changed_rescan`, `filler_operation_busy`,
`filler_fill_{complete,partial}`, `filler_undo_{complete,partial,document_changed}`,
`filler_nothing_to_undo`, `filler_prepare_timeout_unknown`,
`filler_attachment_{invalid,type_rejected,size_rejected,changed_rescan}`,
`filler_upload_{invalid,expired,pending,complete,rejected,timeout_unknown,dom_changed_unknown,selected_parse_unknown}`.
These are codes, not interpolated DOM/profile/exception text. Exceptions after
some writes do not prove zero writes. Host timeout must retain an unknown/running
state until execution ends or the document is destroyed; it must not retry blindly.

The engine is integrated with the desktop main process, encrypted instance storage,
the three-tab side panel, controlled attachment transport, application registration
and normalized progress evidence. Generic unannotated repeaters and custom widgets
remain site-dependent. Authorized real-site coverage and clean-machine acceptance
are release gates, not properties inferred from anonymous fixtures. No original
unlicensed source was copied; see `NOTICE`.

## Contract

1. The packaged workflow loads `loadBundledFiller()` from sealed application
   resources and requires no extension directory. The optional compatibility path
   may explicitly select an absolute local extension directory.
   `loadLocalFiller(folder)` reads only `core.js`, `repeater-engine.js`, `content.js`
   in that order. Regular-file, ancestor symlink, size, identity and UTF-8 checks
   precede producing an immutable source snapshot and per-file/aggregate SHA256.
   Hashes pin the chosen snapshot, not its author or trustworthiness.
2. Import raw exported resume JSON through `parseProfileJson(text)`. The original
   export uses `basic.fullName` and other raw resume sections, not a wrapper or
   the matching CandidateProfile schema. Profiles remain caller-owned memory.
3. Optional explicitly trusted JS compatibility accepts only one
   `globalThis.DEFAULT_RESUME = <data literal>` assignment (actual original name),
   or `globalThis.LOCAL_RESUME_DATA`. Comments and trailing commas are allowed;
   computed expressions, calls, methods and async code are rejected before VM
   execution. The VM has no supplied Node APIs, disables string/WASM codegen and
   uses a 100ms limit. This is not a general-purpose JS sandbox. Prefer JSON.
4. Without a FrameContext, run `buildScanScript(bundle, profile)` in a dedicated isolated world in the
   selected owned top frame (Halley's world 1005). Preserve the same world for
   fill/undo; no Node/IPC or arbitrary script API may be exposed to the page.
   Scan adds engine markers/highlights but does not fill values or prepare/add
   repeatable experiences. Store the returned scan plan in trusted main memory.
   After SPA URL/document changes, a new scan resets the private world state and
   engine listener/undo history. Old fill/undo cannot cross that boundary.
5. Renderer may select only `fieldIds` from that preview. After explicit owner
   confirmation, call `buildFillScript(bundle, {scanId,fieldIds,confirmed:true})`.
   Values are taken from the world-retained plan, never a renderer-supplied value.
   A scan is one-use and expires after 120 seconds. Preview invalidation on
   tab/URL/navigation-generation change remains the main-process owner's duty.
6. `buildUndoScript(bundle)` is an explicit same-document operation. The original
   engine returns `restored`, not `undone`. Replaced nodes, semantic changes,
   subsequent user edits, or navigation block undo rather than targeting a new
   field with the same label.

The only Chrome shim is runtime onMessage listener registration/removal plus
no-op storage.session.set. Only SCAN/FILL/UNDO are dispatched. Never load
background, sidepanel, agent-binding, default PDF or profile scripts implicitly.
Password/OTP/captcha/hidden/file/submit/button fields and hidden/inert controls
are excluded. Every fill rechecks original node identity, marker, type, label
attributes, current value, document and URL before calling the original engine.
Fill results exclude the original `expected`/`actual` and raw error strings.
Do not log source strings, profiles, scan matches or generated execution scripts.

## Bounds And Limitations

- A 60-second deadline stops starting subsequent fields, not an already-running
  asynchronous field operation. A main-process timeout does not cancel the
  original engine. Preserve this distinction in UI errors.
- Legacy source defaults to top-frame-only; explicit frame context is supported.
  New preparation/upload contracts are built-in-only. No SMS, nativeMessaging,
  automatic submission or browser-profile import is implemented. Persistent data,
  UI and permissions are owned by the desktop host, not this pure engine package.
- The local extension is explicitly trusted executable code. An isolated world
  separates JS globals, not the DOM/network; a hash is not a malware check.
- Actual source test is an opt-in offline DOM fixture, not Electron or real-site
  acceptance. Complex widgets and dynamic asynchronous page mutation still need
  dedicated site fixtures/acceptance.

## Tests

Run `node --test tests/desktop_filler/*.test.cjs`. The adapter suite uses synthetic
code/DOM objects; contracts test frame aggregation and input rejection. The
bundled-engine suite uses the repository's Electron/Playwright dependencies, a
hidden sandboxed window, fresh temporary userData and two ephemeral loopback
servers. It exercises real Chromium DOM in isolated world 1005 for top-level
operations. Child-frame fixture calls use their renderer worlds, not the future
host CDP routing implementation. Same/cross-origin nested frames, duplicate local
IDs, detach/reload, native controls, events, selective/partial undo, user edits,
three-record preparation, upload constraints/claim/unknown outcomes and timeout
reentry guards are covered. No downloads or real instances/profiles are needed.
This is anonymous fixture evidence, not packaged or real-site acceptance.

`tests/desktop_filler/local-source.test.cjs` is skipped by default. Explicitly
set `RECRUITOPS_TEST_LOCAL_FILLER_DIR` to a user-selected trusted directory to
load the three actual scripts and its jsdom dev dependency. It never imports
resume-data.js. Actual original engine + synthetic name/form scan, selected fill
and undo passed (one test). No original source or personal fixtures are stored
in this repository.
