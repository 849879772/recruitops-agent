type ReviewObservation = {
  status?: unknown;
  error_code?: unknown;
  result?: {
    page?: { page_url?: unknown; text?: unknown };
    application_records?: unknown;
    semantic_nodes?: unknown;
    diagnostics?: unknown;
  };
};

export function isRedirectedLogin(value: unknown, requestedUrl?: string): boolean {
  if (!requestedUrl || !value || typeof value !== 'object') return false;
  const result = (value as ReviewObservation).result;
  if (!result || !Array.isArray(result.application_records) || result.application_records.length) return false;
  const diagnostics = result.diagnostics as Record<string, unknown> | undefined;
  if (diagnostics?.loginPromptVisible !== true || typeof result.page?.page_url !== 'string') return false;
  try {
    const original = new URL(requestedUrl), current = new URL(result.page.page_url);
    return original.origin === current.origin && original.pathname !== current.pathname
      && /(?:^\/$|\/(?:index|home)(?:\.html?)?\/?$)/i.test(current.pathname);
  } catch { return false; }
}

export type ReviewReadiness = 'records' | 'confirmed_empty' | 'login_required' | 'terminal' | 'pending';

const EMPTY_APPLICATION_TEXT = /(?:暂无|没有|未有|无)(?:相关)?(?:申请|投递|应聘|报名)(?:记录|信息|数据)|(?:(?:no|zero|0)\s+(?:applications?|submissions?)(?:\s+(?:records?|history|data))?)/i;
const EMPTY_STATE_CLASS = /(?:^|[-_])(empty|no[-_]?data|no[-_]?result)(?:$|[-_])/i;

function reliableEmpty(result: NonNullable<ReviewObservation['result']>): boolean {
  const diagnostics = result.diagnostics as Record<string, unknown> | undefined;
  const records = result.application_records;
  const nodes = result.semantic_nodes;
  if (!Array.isArray(records) || records.length !== 0 || !Array.isArray(nodes) || !diagnostics
      || diagnostics.recordBlockCount !== 0 || diagnostics.recordCount !== 0
      || diagnostics.iframeCount !== 0 || !(diagnostics.frameScope === 'top_only'
        || diagnostics.frameScope === 'authorized_frames' && diagnostics.frameCount === 1
          && diagnostics.skippedFrameCount === 0 && diagnostics.unavailableFrameCount === 0)) return false;
  return nodes.some((value) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    const node = value as Record<string, unknown>;
    const text = [node.text, node.ariaLabel].filter((part): part is string => typeof part === 'string').join(' ').trim();
    const role = typeof node.role === 'string' ? node.role.toLowerCase() : '';
    const tokens = Array.isArray(node.classTokens) ? node.classTokens.filter((part): part is string => typeof part === 'string') : [];
    return EMPTY_APPLICATION_TEXT.test(text)
      && (role === 'status' || role === 'alert' || tokens.some(token => EMPTY_STATE_CLASS.test(token)));
  });
}

export function classifyReviewReadiness(value: unknown): ReviewReadiness {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return 'pending';
  const observation = value as ReviewObservation;
  const code = typeof observation.error_code === 'string' ? observation.error_code : '';
  if (code === 'LOGIN_REQUIRED') return 'login_required';
  if (observation.status === 'FAILED' || ['CAPTCHA_REQUIRED', 'SOURCE_NOT_ALLOWED', 'FRAME_NOT_ALLOWED'].includes(code)) return 'terminal';
  const result = observation.result;
  if (!result || typeof result !== 'object' || Array.isArray(result)) return 'pending';
  if (Array.isArray(result.application_records) && result.application_records.length > 0) return 'records';
  if (reliableEmpty(result)) return 'confirmed_empty';
  return 'pending';
}

export interface ReviewDiagnosticSummary {
  pageState: 'blank' | 'shell' | 'records' | 'frame_scope_denied' | 'frame_unavailable' | 'not_observed';
  visibleTextLength: number;
  recordCount: number;
  iframeCount: number;
  frameCount: number;
  skippedFrameCount: number;
  unavailableFrameCount: number;
}

const count = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? Math.min(value, 1000000) : 0;

export function reviewDiagnosticSummary(value?: unknown): ReviewDiagnosticSummary {
  const result = value && typeof value === 'object' ? (value as ReviewObservation).result : undefined;
  const diagnostics = result?.diagnostics as Record<string, unknown> | undefined;
  const frameTextLength = Array.isArray(diagnostics?.frames)
    ? diagnostics.frames.reduce((total, frame) => total + count(frame?.visibleTextLength), 0) : 0;
  const visibleTextLength = Math.max(Math.min(frameTextLength, 1000000), count(diagnostics?.visibleTextLength)
    || (typeof result?.page?.text === 'string' ? Math.min(result.page.text.length, 20000) : 0));
  const recordCount = Array.isArray(result?.application_records) ? result.application_records.length : 0;
  const skippedFrameCount = count(diagnostics?.skippedFrameCount);
  const unavailableFrameCount = count(diagnostics?.unavailableFrameCount);
  return {pageState: !result ? 'not_observed' : recordCount ? 'records' : skippedFrameCount ? 'frame_scope_denied'
    : unavailableFrameCount ? 'frame_unavailable' : visibleTextLength ? 'shell' : 'blank',
    visibleTextLength, recordCount, iframeCount: count(diagnostics?.iframeCount), frameCount: count(diagnostics?.frameCount),
    skippedFrameCount, unavailableFrameCount};
}

export class ReviewObservationError extends Error {
  constructor(message: string, readonly lastObservation: ReviewDiagnosticSummary) { super(message); }
}
