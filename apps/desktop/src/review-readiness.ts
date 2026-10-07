type ReviewObservation = {
  status?: unknown;
  error_code?: unknown;
  result?: {
    page?: { page_url?: unknown; title?: unknown; text?: unknown };
    application_records?: unknown;
    semantic_nodes?: unknown;
    diagnostics?: unknown;
  };
};

export function isRedirectedHome(value: unknown, requestedUrl?: string): boolean {
  if (!requestedUrl || !value || typeof value !== 'object') return false;
  const result = (value as ReviewObservation).result;
  if (!result || !Array.isArray(result.application_records) || result.application_records.length) return false;
  if (typeof result.page?.page_url !== 'string') return false;
  try {
    const original = new URL(requestedUrl), current = new URL(result.page.page_url);
    const homePath = /(?:^\/$|\/(?:index|home)(?:\.html?)?\/?$)/i.test(current.pathname);
    const homeFragment = !current.hash || /^#(?:!?\/)?(?:home|index)?\/?$/i.test(current.hash);
    return original.origin === current.origin && original.href !== current.href && homePath && homeFragment
      && (original.pathname !== current.pathname || original.hash !== current.hash);
  } catch { return false; }
}

export function isRedirectedLogin(value: unknown, requestedUrl?: string): boolean {
  if (!isRedirectedHome(value, requestedUrl)) return false;
  const result = (value as ReviewObservation).result as Record<string, unknown>;
  const evidence = result.auth_evidence as Record<string, unknown> | undefined;
  if (!evidence || !['selector', 'visible_text', 'identity_gate', 'overlay'].includes(String(evidence.trigger))) return false;
  // A navigation link saying "login" is normal on public home pages. Require
  // the saved form/gate evidence, rather than inferring a lost login from it.
  return evidence.selector === "input[type='password']" || evidence.selector === "input[autocomplete='username']"
    || evidence.selector === "[data-recruitops-auth='login']"
    || typeof evidence.text === 'string' && /请先登录|登录后(?:查看|继续|访问)|必须登录|需要登录|身份(?:认证|验证)|please\s+(?:sign|log)\s+in|required\s+to\s+(?:sign|log)\s+in/i.test(evidence.text);
}

export type ReviewReadiness = 'records' | 'confirmed_empty' | 'login_required' | 'record_entry_required' | 'unparsed_page' | 'page_unavailable' | 'terminal' | 'pending';

const EMPTY_APPLICATION_TEXT = /(?:暂无|没有|未有|无)(?:相关)?(?:申请|投递|应聘|报名)(?:记录|信息|数据)|(?:(?:no|zero|0)\s+(?:applications?|submissions?)(?:\s+(?:records?|history|data))?)/i;
const EMPTY_STATE_CLASS = /(?:^|[-_])(empty|no[-_]?data|no[-_]?result)(?:$|[-_])/i;
const UNAVAILABLE_PAGE_TEXT = /页面(?:不存在|已失效|已删除)|网页(?:不存在|已失效)|(?:404\s*(?:[-:：]\s*)?(?:page\s*)?not\s+found)|(?:page\s+(?:not\s+found|does\s+not\s+exist|(?:is\s+)?no\s+longer\s+available))|(?:该|此)链接(?:已失效|不存在)/i;

function unavailablePage(result: NonNullable<ReviewObservation['result']>): boolean {
  const text = [result.page?.title, result.page?.text].filter((part): part is string => typeof part === 'string').join(' ').slice(0, 20000);
  return UNAVAILABLE_PAGE_TEXT.test(text)
    || (typeof result.page?.title === 'string' && /^\s*(?:404|error\s*404)\s*$/i.test(result.page.title));
}

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
  if (code === 'UNPARSED_APPLICATION_PAGE') return 'unparsed_page';
  if (code === 'APPLICATION_PAGE_UNAVAILABLE') return 'page_unavailable';
  if (['APPLICATION_RECORD_ENTRY_NOT_ENTERED', 'APPLICATION_RECORD_HOME_REDIRECT'].includes(code)) return 'record_entry_required';
  if (observation.status === 'FAILED' || ['CAPTCHA_REQUIRED', 'SOURCE_NOT_ALLOWED', 'FRAME_NOT_ALLOWED', 'FRAME_SCOPE_DENIED'].includes(code)) return 'terminal';
  const result = observation.result;
  if (!result || typeof result !== 'object' || Array.isArray(result)) return 'pending';
  if (Array.isArray(result.application_records) && result.application_records.length > 0) return 'records';
  if (unavailablePage(result)) return 'page_unavailable';
  if (reliableEmpty(result)) return 'confirmed_empty';
  return 'pending';
}

export interface ReviewDiagnosticSummary {
  pageState: 'blank' | 'records' | 'unrecognized_content' | 'page_unavailable' | 'frame_scope_denied' | 'frame_unavailable' | 'not_observed';
  visibleTextLength: number;
  recordCount: number;
  iframeCount: number;
  frameCount: number;
  skippedFrameCount: number;
  scopeDeniedFrameCount: number;
  unavailableFrameCount: number;
  recordBlockCount: number;
  semanticNodeCount: number;
  readyState: 'loading' | 'interactive' | 'complete' | 'unknown';
  loadingVisible: boolean;
  page?: { url?: string; title: string; textSnippet: string };
  sampling?: { count: number; elapsedMs: number; stableMs: number;
    first: Pick<ReviewDiagnosticSummary, 'pageState' | 'readyState' | 'visibleTextLength' | 'recordCount' | 'loadingVisible' | 'page'> };
}

const count = (value: unknown) => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? Math.min(value, 1000000) : 0;

// Diagnostics retain public UI vocabulary, never arbitrary account/profile text.
// This deliberately omits unlabelled names and opaque secrets as well as common PII.
const DIAGNOSTIC_WORDS = /\b(?:404\s+(?:page\s+)?not\s+found|page\s+not\s+found|loading|please\s+wait|applications?|submissions?|records?|recruitment|careers?|status|sign\s+in|log\s+in|login|account|home|error|unavailable|empty|no\s+data)\b|页面不存在|页面已失效|网页不存在|链接已失效|正在加载|加载中|暂无(?:申请|投递|应聘)?记录|我的(?:申请|投递)|投递记录|应聘记录|申请记录|当前(?:进度|状态)|校园招聘|招聘|职位|岗位|笔试|面试|已投递|待处理|首页|登录|注册|验证码|密码|姓名/gi;

export function reviewDiagnosticText(value: unknown, limit = 480): string {
  if (typeof value !== 'string') return '';
  const text = value.slice(0, 20000);
  let output = '', cursor = 0;
  for (const match of text.matchAll(DIAGNOSTIC_WORDS)) {
    if (text.slice(cursor, match.index).trim()) output += '[redacted] ';
    output += match[0] + ' ';
    cursor = (match.index ?? 0) + match[0].length;
    if (output.length >= limit) break;
  }
  if (text.slice(cursor).trim()) output += '[redacted]';
  return output.trim().slice(0, limit);
}

export function reviewDiagnosticUrl(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol)) return undefined;
    const route = (path: string) => path.split('/').map(segment => {
      const safe = /^(?:(?:app|application|applications|application_center|status|record|records|recruit|recruitment|campus|candidate|candidatehome|user|center|account|accounts|auth|sso|saml|oauth|oauth2|authorize|callback|redirect|redirected|login|signin|sign|in|home|index|position|positions|job|jobs|portal|error|404|html|htm|aspx|php|pb|www|[._-]))+$/i;
      return !segment || safe.test(segment) ? segment : '[redacted]';
    }).join('/');
    const fragment = url.hash.slice(1).split('?', 1)[0];
    return (url.origin + route(url.pathname) + (/^(?:\/|!\/)/.test(fragment) ? '#' + route(fragment) : '')).slice(0, 1024);
  } catch { return undefined; }
}

export interface ReviewNavigationDiagnostics {
  requestedUrl?: string;
  finalUrl?: string;
  attemptedUrl?: string;
  reason: string;
  sameOrigin: boolean;
  ssoCandidate: boolean;
  phase?: 'initial_load' | 'observation' | 'navigation_recovery' | 'vision_capture';
  restriction?: 'https_downgrade' | 'unapproved_origin' | 'credential_url' | 'invalid_url';
  reobservationCount?: number;
  authNavigation?: { provider: 'alibaba' | 'huawei'; hops: number; returnedToRecruitment: boolean };
  authWait?: { elapsedMs: number; budgetMs: number; progressCount: number;
    outcome: 'returned' | 'timeout' | 'cancelled' | 'navigation_denied' };
}

// Exact, reviewed origin pairs only. Auth origins are navigation destinations,
// never evidence scopes; do not generalize this to a registrable-domain allowlist.
const OFFICIAL_AUTH = new Map([
  ['https://campus-talent.alibaba.com', {origin: 'https://mozi-login.alibaba-inc.com', provider: 'alibaba' as const}],
  ['https://career.huawei.com', {origin: 'https://uniportal.huawei.com', provider: 'huawei' as const}],
]);

export class ReviewNavigationPolicy {
  readonly origin: string;
  private auth: {origin: string; provider: 'alibaba' | 'huawei'} | undefined;
  private lastTarget: string;
  private attempted?: string;
  private reason?: string;
  private hops = 0;
  private reobservations = 0;
  private authWait?: ReviewNavigationDiagnostics['authWait'];
  authVisited = false;
  denied = false;
  constructor(readonly requestedUrl: string) {
    this.origin = new URL(requestedUrl).origin;
    this.auth = OFFICIAL_AUTH.get(this.origin);
    this.lastTarget = requestedUrl;
  }
  isAuthUrl(value: string): boolean {
    try { const url = new URL(value); return !!this.auth && url.origin === this.auth.origin
      && url.protocol === 'https:' && !url.username && !url.password; } catch { return false; }
  }
  get awaitingAuthentication(): boolean { return this.authVisited && this.isAuthUrl(this.lastTarget); }
  recordAuthenticationWait(value: NonNullable<ReviewNavigationDiagnostics['authWait']>): void { this.authWait = value; }
  claimObservationRecovery(): boolean {
    if (this.denied || !this.authVisited) return false;
    if (this.reobservations >= 1) { this.reason = 'official_sso_reobservation_limit'; return false; }
    this.reobservations++;
    return true;
  }
  note(value: string, event: string): boolean {
    if (this.denied) return false;
    // Electron emits multiple navigation events for one destination. Redirect
    // events still count: an IdP can redirect to exactly the same URL in a loop.
    if (event !== 'will_redirect' && value === this.lastTarget && (this.reason || value === this.requestedUrl)) return true;
    let sameOrigin = false;
    try { const url = new URL(value); sameOrigin = ['http:', 'https:'].includes(url.protocol)
      && url.origin === this.origin && !url.username && !url.password; } catch { /* denied below */ }
    const auth = this.isAuthUrl(value);
    if (!sameOrigin && !auth) this.denied = true;
    if (auth) this.authVisited = true;
    if (this.authVisited && (value !== this.lastTarget || event === 'will_redirect') && ++this.hops > 8) this.denied = true;
    this.lastTarget = value;
    this.attempted = value;
    this.reason = this.denied && this.hops > 8 ? 'official_sso_hop_limit'
      : event + (sameOrigin ? '_same_origin' : auth ? '_official_sso' : '_cross_origin');
    return !this.denied;
  }
  diagnostic(finalUrl: string): ReviewNavigationDiagnostics | undefined {
    if (!this.reason) return undefined;
    const result = reviewNavigationDiagnostics(this.requestedUrl, finalUrl, this.attempted, this.reason);
    if (this.reobservations) result.reobservationCount = this.reobservations;
    if (this.authWait) result.authWait = this.authWait;
    if (this.authVisited && this.auth) result.authNavigation = {provider: this.auth.provider, hops: this.hops,
      returnedToRecruitment: (() => { try { return !this.awaitingAuthentication && new URL(finalUrl).origin === this.origin; } catch { return false; } })()};
    return result;
  }
}

export function reviewNavigationDiagnostics(requestedUrl: string, finalUrl: string, attemptedUrl?: string,
  reason = 'current_origin_changed'): ReviewNavigationDiagnostics {
  let sameOrigin = false, ssoCandidate = false;
  let restriction: ReviewNavigationDiagnostics['restriction'];
  try {
    const target = new URL(attemptedUrl || finalUrl);
    const original = new URL(requestedUrl);
    sameOrigin = target.origin === original.origin;
    ssoCandidate = /(?:^|[./_-])(?:sso|saml|oauth2?|authorize|login|signin|accounts|passport|uniportal)(?:$|[./_-])/i.test(target.hostname + target.pathname);
    if (target.username || target.password) restriction = 'credential_url';
    else if (original.protocol === 'https:' && target.protocol === 'http:') restriction = 'https_downgrade';
    else if (!['http:', 'https:'].includes(target.protocol)) restriction = 'invalid_url';
    else if (!sameOrigin && OFFICIAL_AUTH.get(original.origin)?.origin !== target.origin) restriction = 'unapproved_origin';
  } catch { restriction = 'invalid_url'; }
  return { requestedUrl: reviewDiagnosticUrl(requestedUrl), finalUrl: reviewDiagnosticUrl(finalUrl),
    attemptedUrl: reviewDiagnosticUrl(attemptedUrl), reason, sameOrigin, ssoCandidate,
    ...(restriction ? {restriction} : {}) };
}

export type ReviewDiagnosticCallback = (summary: ReviewDiagnosticSummary, navigation?: ReviewNavigationDiagnostics) => void;

export function reviewDiagnosticSummary(value?: unknown): ReviewDiagnosticSummary {
  const result = value && typeof value === 'object' ? (value as ReviewObservation).result : undefined;
  const diagnostics = result?.diagnostics as Record<string, unknown> | undefined;
  const frameTextLength = Array.isArray(diagnostics?.frames)
    ? diagnostics.frames.reduce((total, frame) => total + count(frame?.visibleTextLength), 0) : 0;
  const visibleTextLength = Math.max(Math.min(frameTextLength, 1000000), count(diagnostics?.visibleTextLength)
    || (typeof result?.page?.text === 'string' ? Math.min(result.page.text.length, 20000) : 0));
  const recordCount = Array.isArray(result?.application_records) ? result.application_records.length : 0;
  const recordBlockCount = count(diagnostics?.recordBlockCount);
  const semanticNodeCount = count(diagnostics?.semanticNodeCount) || (Array.isArray(result?.semantic_nodes) ? result.semantic_nodes.length : 0);
  const skippedFrameCount = count(diagnostics?.skippedFrameCount);
  const scopeDeniedFrameCount = Math.min(skippedFrameCount, count(diagnostics?.scopeDeniedFrameCount));
  const unavailableFrameCount = count(diagnostics?.unavailableFrameCount);
  const readableFrames = Array.isArray(diagnostics?.frames) ? diagnostics.frames.filter(frame => count(frame?.visibleTextLength)) : [];
  const frameLoading = readableFrames.some(frame => frame.loadingVisible === true);
  const framePending = readableFrames.some(frame => ['loading', 'interactive'].includes(frame.readyState));
  const pageUrl = reviewDiagnosticUrl(result?.page?.page_url);
  return {pageState: !result ? 'not_observed' : recordCount ? 'records' : unavailablePage(result) ? 'page_unavailable'
    : recordBlockCount || visibleTextLength ? 'unrecognized_content'
    : scopeDeniedFrameCount ? 'frame_scope_denied' : unavailableFrameCount ? 'frame_unavailable' : 'blank',
    visibleTextLength, recordCount, iframeCount: count(diagnostics?.iframeCount), frameCount: count(diagnostics?.frameCount),
    skippedFrameCount, scopeDeniedFrameCount, unavailableFrameCount, recordBlockCount, semanticNodeCount,
    readyState: framePending ? 'loading' : ['loading', 'interactive', 'complete'].includes(String(diagnostics?.readyState)) ? diagnostics?.readyState as 'loading' | 'interactive' | 'complete' : 'unknown',
    loadingVisible: frameLoading || /loading|please\s+wait|加载中|正在加载/i.test(typeof result?.page?.text === 'string' ? result.page.text.slice(0, 20000) : ''),
    ...(result?.page ? {page: {...(pageUrl ? {url: pageUrl} : {}),
      title: reviewDiagnosticText(result.page.title, 160), textSnippet: reviewDiagnosticText(result.page.text)}} : {})};
}

export class ReviewObservationError extends Error {
  constructor(message: string, readonly lastObservation: ReviewDiagnosticSummary,
    readonly navigation?: ReviewNavigationDiagnostics) { super(message); }
}
