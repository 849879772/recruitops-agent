export interface ObservationParams {
  operation_id: string;
  page_url: string;
}

export interface ObservationContext extends ObservationParams {
  application_ids: string[];
}

export interface ApplicationRecordEvidence {
  frameId?: number;
  frameUrl?: string;
  title?: string;
  status?: string;
  label?: string;
  context?: string;
  evidence?: string;
  confidence?: number;
  applied_at?: string;
  evidence_source?: string;
  raw_status_labels?: string[];
  signals?: Partial<Record<"unmapped_status" | "has_date" | "has_operation" |
    "has_volunteer_index" | "has_explicit_status" | "has_active_step" |
    "conflicting_statuses", boolean>>;
}

export interface PageEvidence {
  page_url: string;
  origin: string;
  path: string;
  title?: string;
  text?: string;
  links?: string[];
  networkRequests?: Array<{method?: string; url?: string; status_code?: number | null; resource_type?: string}>;
  capturedAt?: string;
}

export interface EvidenceResult {
  evidence_only: true;
  database_updated: false;
  page_url: string;
  requires_user_action?: true;
  page?: PageEvidence;
  captured_at?: string;
  application_records?: ApplicationRecordEvidence[];
  entries?: ApplicationRecordEvidence[];
  semantic_nodes?: Array<Record<string, unknown>>;
  diagnostics?: {frameScope: "top_only" | "single_frame" | "authorized_frames"; iframeCount?: number; [key: string]: unknown};
}

export interface EvidenceEnvelope<T extends EvidenceResult> {
  protocol_version: 1;
  type: "result";
  operation_id: string;
  event_id: string;
  status: "SUCCEEDED" | "STATE_UNCLEAR" | "FAILED";
  error_code?: string;
  result: T;
}

export type ObservationEnvelope = EvidenceEnvelope<EvidenceResult & (
  {application_id: string; application_ids: [string, ...string[]]} |
  {application_ids: []}
)>;

export type ManualCaptureEnvelope = EvidenceEnvelope<EvidenceResult & {
  kind: "manual_capture";
  requires_user_confirmation: true;
  draft?: {url: string; title: string; page_text: string};
}>;

export interface FormFillCapability {
  readonly supported: true;
  readonly code: "LIMITED_NATIVE_V1";
  readonly enabled_by_default: false;
  readonly preview: true;
  readonly fill: true;
  readonly file_selection: "user_only";
  readonly final_submission: "user_only";
  readonly reason: string;
}

export interface FormFieldMapping {
  field_id: string;
  target: {id: string; name?: never} | {name: string; id?: never};
  value: string;
}

export interface FormPreviewParams extends ObservationParams {
  fields: FormFieldMapping[];
}

export interface FormFillParams extends FormPreviewParams {
  preview_id: string;
  writes_enabled: true;
  user_confirmed: true;
}

export type FormFieldStatus = "READY" | "NOT_FOUND" | "AMBIGUOUS" | "UNSUPPORTED" |
  "SENSITIVE" | "NOT_EDITABLE" | "OPTION_NOT_FOUND" | "VALUE_TOO_LONG" |
  "DUPLICATE_TARGET" | "NOT_ATTEMPTED" | "FILLED" | "UNCHANGED" | "FAILED";

export type FormResultCode = "FRAME_NOT_ALLOWED" | "SOURCE_NOT_ALLOWED" | "STATE_UNCLEAR" |
  "CAPTCHA_REQUIRED" | "LOGIN_REQUIRED" | "PREVIEW_STALE" | "FIELDS_NOT_READY" |
  "PREVIEW_READY" | "DOM_CHANGED" | "VALUE_REJECTED" | "FILL_COMPLETE";

export interface FormResult {
  protocol_version: 1;
  operation_id: string;
  preview_id: string;
  page_url: string;
  database_updated: false;
  submission_attempted: false;
  frame_scope: "top_only";
  iframe_count: number;
  code: FormResultCode;
  fields: Array<{field_id: string; status: FormFieldStatus; match_count: number}>;
}

export interface FormPreviewResult extends FormResult {
  kind: "form_preview";
  status: "READY" | "UNSUPPORTED" | "BLOCKED";
}

export interface FormFillResult extends FormResult {
  kind: "form_fill";
  status: "FILLED" | "UNCHANGED" | "PARTIAL" | "UNSUPPORTED" | "BLOCKED";
}

export function buildObservationScript(params: ObservationParams): string;
export function buildFrameObservationScript(params: ObservationParams): string;
export function normalizeFrameObservations(samples: Array<{frameId: number; frameUrl: string; raw?: unknown; unavailable?: boolean}>, context: ObservationContext, skippedFrameCount?: number): ObservationEnvelope;
export function normalizeObservation(raw: unknown, context: ObservationContext): ObservationEnvelope;
export function buildManualCaptureScript(params: ObservationParams): string;
export function normalizeManualCapture(raw: unknown, context: ObservationParams): ManualCaptureEnvelope;
export function getFormFillCapability(): Readonly<FormFillCapability>;
/** Read-only field preview; execute in the same isolated world as the later fill. */
export function buildFormPreviewScript(params: FormPreviewParams): string;
export function normalizeFormPreview(raw: unknown, params: FormPreviewParams): FormPreviewResult;
/** Requires an unexpired single-use preview receipt and explicit trusted user opt-in. */
export function buildFormFillScript(params: FormFillParams): string;
export function normalizeFormFill(raw: unknown, params: FormFillParams): FormFillResult;
export interface DesktopBrowserAdapter {
  buildObservationScript: typeof buildObservationScript;
  buildFrameObservationScript: typeof buildFrameObservationScript;
  normalizeFrameObservations: typeof normalizeFrameObservations;
  normalizeObservation: typeof normalizeObservation;
  buildManualCaptureScript: typeof buildManualCaptureScript;
  normalizeManualCapture: typeof normalizeManualCapture;
  getFormFillCapability: typeof getFormFillCapability;
  buildFormPreviewScript: typeof buildFormPreviewScript;
  buildFormFillScript: typeof buildFormFillScript;
  normalizeFormPreview: typeof normalizeFormPreview;
  normalizeFormFill: typeof normalizeFormFill;
}
export function createObservationAdapter(resourceDirectory?: string): Readonly<DesktopBrowserAdapter>;
export function loadObservationResources(directory?: string): Record<string, string>;
export function packageObservationResources(destination: string): void;
export const RESOURCE_HASHES: Readonly<Record<string, string>>;
