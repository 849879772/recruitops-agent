export type ResumeProfile = { [key: string]: unknown };
export interface LocalFillerBundle {
  readonly source: string;
  readonly hash: string;
  readonly files: readonly { readonly name: string; readonly bytes: number; readonly sha256: string }[];
}
export interface ConfirmedFill {
  scanId: string;
  fieldIds: string[];
  confirmed: true;
}
/** MAIN-owned identity, never accepted verbatim from a renderer. frameId identifies
 * the actual selected WebFrameMain, documentId changes on reload/detach/navigation.
 * Host separately enforces allowed origins/sandbox restrictions before execution. */
export interface FrameContext {
  instanceId: string;
  tabId: string;
  frameId: string;
  documentId: string;
  profileVersion: string;
  href: string;
  allowSubframe?: boolean;
}
export interface RepeaterPreview {
  sectionId: string;
  count: number;
  desired: number;
  supported: boolean;
  reason: string;
}
export interface AttachmentTarget { fieldId: string; label: string; accept: string; multiple: boolean; }
export interface CustomAnswer { id: string; origin: string; pathname: string; label: string; value: string | boolean | string[]; }
export type FrameScanOutcome = { route: FrameContext; status: 'scanned'; scan: ScanResult }
  | { route: FrameContext; status: 'blocked' | 'sandboxed' | 'detached' | 'unreachable' };
export interface AggregateScanResult {
  ok: boolean; partial: boolean;
  fields: { route: FrameContext; scanId: string; fieldId: string; selectionId: string; match: FillerMatch }[];
  candidates: { route: FrameContext; scanId: string; fieldId: string; selectionId: string; candidate: FillerCandidate }[];
  failures: { route: FrameContext; code: string }[];
}
/** Caller enumerates owned frames and supplies explicit failures, never omits a blocked frame.
 * Keep selectionId -> (route,scanId,fieldId) mapping in MAIN; renderer selects IDs only. */
export function aggregateFrameScans(outcomes: FrameScanOutcome[]): AggregateScanResult;
export interface FillerMatch {
  fieldId: string;
  key?: string;
  label?: string;
  section?: string;
  controlKind?: string;
  value: unknown;
  confidence?: number | string;
  recordIndex?: number;
}
export interface FillerCandidate {
  fieldId: string;
  label: string;
  controlKind: string;
  reason: string;
  customAnswerSupported: boolean;
}
export interface ScanResult {
  ok: true;
  totalFields: number;
  emptyFields: number;
  matches: FillerMatch[];
  /** Visible non-sensitive controls without an automatic answer; never contains current values. */
  candidates?: FillerCandidate[];
  scanId: string;
  issuedAt: number;
  sourceHash: string;
  blockedCount: number;
  topFrameOnly: boolean;
  route: FrameContext | null;
  attachments: AttachmentTarget[];
  repeaters: RepeaterPreview[];
  diagnostics: { fieldId: string; reason: string }[];
}
export interface FillResult {
  ok: boolean;
  filled: number;
  failed: string[];
  skipped: string[];
  code: 'filler_fill_complete' | 'filler_fill_partial' | 'filler_fill_cancelled';
  results: { fieldId: string; ok: boolean; reason: string; status: 'filled' | 'failed' | 'skipped' }[];
}
export interface UndoResult {
  ok: boolean; restored: number; failed: string[];
  code: 'filler_undo_complete' | 'filler_undo_partial';
  structureRestored: false; remoteUploadsReverted: false;
}
export interface CancelResult {
  ok: true;
  requested: boolean;
  code: 'filler_cancel_requested' | 'filler_cancel_queued' | 'filler_operation_not_running';
}
export const SOURCE_FILES: readonly string[];
/** Explicitly selected trusted directory. Reads ONLY core/repeater/content; no profile auto-load. */
export function loadLocalFiller(folder: string): LocalFillerBundle;
/** Self-authored resource embedded in this package. No filesystem/profile import. */
export function loadBundledFiller(): LocalFillerBundle;
/** Packaged browser-extension DOM engine; never imports profile/background/SMS files. */
export function loadDesktopFiller(): LocalFillerBundle;
export interface ApplicationContext { company: string; titles: string[]; url: string;
  records: {title: string; date: string; sourceStatus: string}[]; }
/** Read-only original-plugin discovery; no profile, saved applications or status writes. */
export function buildApplicationContextScript(bundle: LocalFillerBundle, context?: FrameContext): string;
/** Deduplicates trusted per-frame discovery results; only top/same-origin or explicitly allowed origins contribute. */
export function mergeApplicationContexts(samples: {frameId:number; frameUrl:string; result:ApplicationContext}[], pageUrl:string, allowedFrameOrigins?:string[]): ApplicationContext;
/** Raw resume object JSON, not the matching CandidateProfile projection. */
export function parseProfileJson(text: string): ResumeProfile;
/** Data-literal export only: globalThis.DEFAULT_RESUME or globalThis.LOCAL_RESUME_DATA. */
export function parseLegacyProfile(text: string): ResumeProfile;
/** Resolves ScanResult; adds DOM markers/highlights, never fills values. Never log the script. */
export function buildScanScript(bundle: LocalFillerBundle, profile: ResumeProfile, context?: FrameContext): string;
/** Resolves FillResult. Same world/top frame, 120s preview TTL, 60s operation deadline.
 * Yields between fields and checks its operation token; completed writes stay in this document
 * and remain eligible for same-document undo. Cannot hard-cancel an already-running page handler. */
export function buildFillScript(bundle: LocalFillerBundle, request: ConfirmedFill, context?: FrameContext): string;
export function buildUndoScript(bundle: LocalFillerBundle, context?: FrameContext): string;
/** Builds a cooperative cancel request for the latest fill/prepare script built for this bundle/route.
 * Run in the same document/world. `requested:false` means already settled/no operation;
 * `filler_cancel_queued` means the matching page script has not started. These are acknowledgements,
 * not completion: keep host busy until the original operation promise settles. A stop arriving before
 * its fill/prepare script is built must also be remembered by the host. No refresh or hard kill occurs. */
export function buildCancelScript(bundle: LocalFillerBundle, context?: FrameContext): string;
export interface ConfirmedPrepare { scanId: string; sectionIds: string[]; confirmed: true; }
export interface PrepareResult {
  ok: boolean;
  code: 'filler_prepare_complete' | 'filler_prepare_partial' | 'filler_prepare_cancelled';
  results: { sectionId: string; added: number; count: number; desired: number; code: string }[];
  requiresRescan: true; structureUndoSupported: false;
}
/** Only uniquely recognized sections/add buttons. No submit/save clicks. Timeout is unknown and locks
 * the page world until reload; cancellation preserves any row the site already added. */
export function buildPrepareScript(bundle: LocalFillerBundle, request: ConfirmedPrepare, context?: FrameContext): string;
/** Opaque host store ID, never a local path. Host validates extension/MIME/content consistency. */
export interface AttachmentMetadata { id: string; name: string; size: number; mime: string; }
export interface ConfirmedUpload { scanId: string; fieldId: string; attachment: AttachmentMetadata; confirmed: true; }
export interface UploadTicket {
  ok: true; uploadId: string; fieldId: string; attachmentId: string; selector: string;
  route: FrameContext | null; expiresInMs: 10000; requiresRescan: true; remoteUndoSupported: false;
}
/** Resolves UploadTicket, does NOT read/upload a file. Main retains ticket and revalidates
 * exact element/document immediately before controlled file assignment. Single use, <=10s.
 * A selector alone is NOT authorization to use a replacement element. */
export function buildUploadScript(bundle: LocalFillerBundle, request: ConfirmedUpload, context?: FrameContext): string;
export interface UploadStatus {
  ok: boolean;
  code: 'filler_upload_pending' | 'filler_upload_complete' | 'filler_upload_rejected'
    | 'filler_upload_dom_changed_unknown' | 'filler_upload_selected_parse_unknown' | 'filler_upload_timeout_unknown';
  requiresRescan: true; remoteUndoSupported: false;
}
/** No sleep or guessed parse success. Pending requires bounded host polling; selected/unknown
 * requires user/site-specific completion verification before a fresh preview. */
export function buildUploadStatusScript(bundle: LocalFillerBundle, uploadId: string, context?: FrameContext): string;
/** Returns an actual HTMLInputElement, NOT serializable JSON. Host must evaluate in the
 * SAME isolated world via CDP with returnByValue:false, retain its remote objectId,
 * and use that exact object in DOM.setFileInputFiles; never query the returned selector
 * again. Claims ticket once, <=10s, checking identity/visibility/current value. */
export function buildUploadTargetScript(bundle: LocalFillerBundle, uploadId: string, context?: FrameContext): string;
