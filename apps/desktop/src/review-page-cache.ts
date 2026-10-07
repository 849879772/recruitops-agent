import type { Session, WebContents } from 'electron';
import type { BackgroundPageLease } from './browser-contract';
import { matchesReviewIdentity, reviewObservationBinding } from './review-observation-binding';
import { reviewDiagnosticSummary } from './review-readiness';
import type { ReviewNavigationPolicy } from './review-readiness';
import { reviewStabilityProof } from './browser-service';
import type { ReviewStabilityProof } from './browser-service';
import { canCaptureReview } from './review-vision';

export const REVIEW_PAGE_REUSE_TTL_MS = 30000;
export const REVIEW_PAGE_REUSE_CAPACITY = 6;

export interface ReviewReuseContext {
  reviewTaskId?: string;
  reuseObservationOperationId?: string;
  // Set only by the authenticated main-process bridge, never renderer IPC.
  retainForVisionReuse?: boolean;
}
type Scope = { url: string; applicationIds: string[]; reviewTaskId: string; session: Session; profileEpoch: number };
type CachedPage = Scope & { operationId: string; lease: BackgroundPageLease; wc: WebContents;
  policy: ReviewNavigationPolicy; binding: NonNullable<ReturnType<typeof reviewObservationBinding>>;
  expiresAt: number; timer: NodeJS.Timeout; invalidate: () => void; stableProof?: ReviewStabilityProof };
type ProfileWatch = {hosts: string[]; controller: AbortController};

function normalizedUrl(value: string): string | undefined {
  try {
    const url = new URL(value);
    // Keep query and fragment identity: different tenant/account parameters must
    // not accidentally share a page, even when the server accepts a weaker hint.
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}

// No text, pixels, credentials or persistent cache. Ownership transfers once to
// a new review operation; navigation invalidates even a reload to the same URL.
export class ReviewPageCache {
  private pages = new Map<string, CachedPage>();
  private profileEpochs = new WeakMap<Session, number>();
  private profileWatches = new WeakMap<Session, Set<ProfileWatch>>();
  constructor(private readonly ttlMs = REVIEW_PAGE_REUSE_TTL_MS, private readonly now = Date.now) {}
  get size() { return this.pages.size; }
  profileEpoch(session: Session) { return this.profileEpochs.get(session) || 0; }
  watchReusedProfile(session: Session, pageUrl: string, policy: ReviewNavigationPolicy) {
    const watches = this.profileWatches.get(session) || new Set<ProfileWatch>();
    this.profileWatches.set(session, watches);
    const hosts = [new URL(pageUrl).hostname, ...['https://mozi-login.alibaba-inc.com', 'https://uniportal.huawei.com']
      .filter(origin => policy.isAuthUrl(origin)).map(origin => new URL(origin).hostname)];
    const watch = {hosts, controller: new AbortController()}; watches.add(watch);
    return {signal: watch.controller.signal, check: () => {
      if (watch.controller.signal.aborted) throw new Error('browser_account_changed');
    }, dispose: () => {watches.delete(watch);}};
  }
  retain(operationId: string, lease: BackgroundPageLease, wc: WebContents, policy: ReviewNavigationPolicy,
    observation: unknown, scope: Scope, hidden: boolean): boolean {
    const value = observation as {status?: string; error_code?: unknown; review_readiness?: string;
      result?: {requires_user_action?: unknown; pause?: unknown; page_url?: unknown}} | undefined;
    const summary = reviewDiagnosticSummary(observation);
    const binding = reviewObservationBinding(observation);
    const url = normalizedUrl(scope.url);
    if (!hidden || lease.purpose !== 'review' || !scope.reviewTaskId || !url || !scope.applicationIds.length
        || wc.session !== scope.session || scope.profileEpoch !== this.profileEpoch(scope.session)
        || !binding || !matchesReviewIdentity(wc, binding)
        || !canCaptureReview(observation) || value?.status !== 'SUCCEEDED' || value.error_code || value.result?.requires_user_action || value.result?.pause
        || !['records', 'unparsed_page', 'confirmed_empty'].includes(value.review_readiness || '')
        || summary.readyState !== 'complete' || summary.loadingVisible || !summary.visibleTextLength
        || summary.unavailableFrameCount || summary.scopeDeniedFrameCount || policy.denied || policy.awaitingAuthentication
        || normalizedUrl(binding.url) !== normalizedUrl(String(value.result?.page_url || ''))) return false;
    this.invalidate(operationId);
    while (this.pages.size >= REVIEW_PAGE_REUSE_CAPACITY) this.evictOldest();
    const invalidate = () => this.invalidate(operationId);
    const timer = setTimeout(invalidate, Math.max(1, Math.min(this.ttlMs, REVIEW_PAGE_REUSE_TTL_MS)));
    timer.unref();
    const page: CachedPage = {...scope, url, applicationIds: [...scope.applicationIds], operationId, lease, wc,
      policy, binding, timer, invalidate, expiresAt: this.now() + Math.min(this.ttlMs, REVIEW_PAGE_REUSE_TTL_MS),
      stableProof: reviewStabilityProof(observation)};
    this.pages.set(operationId, page);
    for (const event of ['did-start-navigation', 'did-navigate-in-page', 'will-navigate', 'will-redirect',
      'render-process-gone', 'destroyed']) wc.on(event as any, invalidate);
    return true;
  }
  take(operationId: string, scope: Scope, available: (wc: WebContents) => boolean = () => true): {lease: BackgroundPageLease; wc: WebContents;
    policy: ReviewNavigationPolicy; stableProof?: ReviewStabilityProof} | undefined {
    const page = this.pages.get(operationId);
    if (!page) return undefined;
    this.remove(page);
    if (this.now() >= page.expiresAt || page.session !== scope.session || page.reviewTaskId !== scope.reviewTaskId
        || page.profileEpoch !== scope.profileEpoch || scope.profileEpoch !== this.profileEpoch(scope.session)
        || page.url !== normalizedUrl(scope.url) || !scope.applicationIds.length
        || scope.applicationIds.some(id => !page.applicationIds.includes(id))
        || !matchesReviewIdentity(page.wc, page.binding) || !available(page.wc) || page.policy.denied || page.policy.awaitingAuthentication) {
      page.lease.close(); return undefined;
    }
    return {lease: page.lease, wc: page.wc, policy: page.policy, stableProof: page.stableProof};
  }
  private remove(page: CachedPage) {
    this.pages.delete(page.operationId); clearTimeout(page.timer);
    for (const event of ['did-start-navigation', 'did-navigate-in-page', 'will-navigate', 'will-redirect',
      'render-process-gone', 'destroyed']) page.wc.removeListener(event as any, page.invalidate);
  }
  invalidate(operationId?: string) {
    for (const page of [...this.pages.values()]) {
      if (operationId !== undefined && page.operationId !== operationId) continue;
      this.remove(page); page.lease.close();
    }
  }
  evictOldest() { const first = this.pages.keys().next().value; if (first !== undefined) this.invalidate(first); }
  profileChanged(session: Session) {
    this.profileEpochs.set(session, this.profileEpoch(session) + 1);
    for (const page of [...this.pages.values()]) if (page.session === session) this.invalidate(page.operationId);
    for (const watch of this.profileWatches.get(session) || []) watch.controller.abort();
  }
  foregroundNavigated(session: Session, pageUrl: string) {
    this.profileEpochs.set(session, this.profileEpoch(session) + 1);
    for (const page of [...this.pages.values()]) if (page.session === session) this.invalidate(page.operationId);
    let host: string;
    try {host = new URL(pageUrl).hostname;} catch {return;}
    for (const watch of this.profileWatches.get(session) || []) if (watch.hosts.includes(host)) watch.controller.abort();
  }
  cookieChanged(session: Session, name: string, httpOnly = false, cookieDomain?: string) {
    // Ignore only explicit, well-known analytics counters. Unknown or account
    // cookies are conservative cache invalidations; flushing alone emits no change.
    if (!httpOnly && /^(?:_ga(?:_[A-Za-z0-9]+)?|_gid|_gat(?:_[A-Za-z0-9_]+)?|_gcl_au|__utm[a-z]|Hm_lvt_[a-f0-9]+|Hm_lpvt_[a-f0-9]+|HMACCOUNT)$/i.test(name)) return;
    this.profileEpochs.set(session, this.profileEpoch(session) + 1);
    for (const page of [...this.pages.values()]) if (page.session === session) this.invalidate(page.operationId);
    const domain = cookieDomain?.replace(/^\./, '').toLowerCase();
    for (const watch of this.profileWatches.get(session) || []) {
      if (!domain || watch.hosts.some(host => host === domain || host.endsWith('.' + domain))) watch.controller.abort();
    }
  }
}
