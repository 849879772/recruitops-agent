import type { WebContents } from 'electron';
import { createHash } from 'node:crypto';
import { classifyReviewReadiness, isRedirectedHome, isRedirectedLogin, reviewDiagnosticSummary, ReviewObservationError,
  reviewDiagnosticUrl, reviewNavigationDiagnostics } from './review-readiness';
import type { ReviewReadiness, ReviewDiagnosticCallback, ReviewDiagnosticSummary, ReviewNavigationPolicy, ReviewNavigationDiagnostics } from './review-readiness';
import { FillerFrameExecutor } from './filler-frame-executor';
import { captureReviewIdentity, bindReviewObservation, inheritReviewObservation, matchesReviewIdentity, reviewObservationBinding } from './review-observation-binding';
import { followReviewRecordEntry, reviewRecordEntryReason } from './review-record-entry';
import type { ReviewRecordEntryAttempt, ReviewRecordEntryReason } from './review-record-entry';
import { expandReviewRecordDetails, needsReviewRecordDetails } from './review-record-details';
import type { ReviewRecordDetailsFollowup } from './review-record-details';

type FrameSample = { frameId: number; frameUrl: string; raw?: unknown; unavailable?: boolean };

export interface BrowserAdapter {
  buildObservationScript(params: { operation_id: string; page_url: string }): string;
  normalizeObservation(raw: unknown, context: { operation_id: string; page_url: string; application_ids: string[] }): unknown;
  buildFrameObservationScript?(params: { operation_id: string; page_url: string }): string;
  normalizeFrameObservations?(samples: FrameSample[], context: { operation_id: string; page_url: string; application_ids: string[] }, skippedFrameCount: number): unknown;
  buildManualCaptureScript?(params: { operation_id: string; page_url: string }): string;
  normalizeManualCapture?(raw: unknown, context: { operation_id: string; page_url: string }): unknown;
}

export interface ReviewObserveOptions {
  deadline: number;
  signal?: AbortSignal;
  ownedOrigin?: string;
  requestedUrl?: string;
  onStage?: (stage: 'EXTRACTING' | 'WAITING_FOR_CONTENT' | 'VALIDATING' | 'WAITING_FOR_LOGIN' | 'STATE_UNCLEAR') => void;
  onDiagnostic?: ReviewDiagnosticCallback;
  stableReadableProof?: ReviewStabilityProof;
}

export type ReviewStabilityProof = { fingerprint: string; binding: ReturnType<typeof captureReviewIdentity> };
const stabilityProofs = new WeakMap<object, ReviewStabilityProof>();
const issuedStabilityProofs = new WeakSet<ReviewStabilityProof>();
export function reviewStabilityProof(observation: unknown): ReviewStabilityProof | undefined {
  return observation && typeof observation === 'object' ? stabilityProofs.get(observation) : undefined;
}
function surfaceFingerprint(observation: unknown, summary: ReviewDiagnosticSummary): string {
  const result = (observation as {result?: Record<string, unknown>})?.result;
  const page = result?.page as {page_url?: string; title?: string; text?: string} | undefined;
  return createHash('sha256').update(JSON.stringify({page: page && {page_url: page.page_url, title: page.title, text: page.text},
    nodes: result?.semantic_nodes, frames: (result?.diagnostics as {frames?: unknown[]})?.frames, summary})).digest('hex');
}

interface ObservationOptions {
  deadline?: number;
  signal?: AbortSignal;
  ownedOrigin?: string;
  allowSameOriginNavigation?: boolean;
}

class OwnedNavigation extends Error {
  constructor(readonly targetUrl: string) { super('browser_owned_navigation'); }
}

class ObservationTimeout extends Error {
  constructor(readonly deadlineLimited: boolean) { super('browser_observation_timeout'); }
}

function httpOrigin(value: string): string | undefined {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.origin : undefined;
  } catch { return undefined; }
}

// Only inspect embedding elements in the owned document. This never executes
// inside, or reads content from, a foreign or opaque frame.
const visibleRestrictedFramesScript = `(() => {
  return Array.from(document.querySelectorAll('iframe, frame')).slice(0, 32).filter(element => {
    const rect = element.getBoundingClientRect();
    if (rect.width <= 16 || rect.height <= 16 || !element.getClientRects().length) return false;
    for (let owner = element; owner; owner = owner.parentElement) {
      const style = getComputedStyle(owner);
      if (owner.hidden || owner.inert || owner.getAttribute('aria-hidden') === 'true'
          || style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) return false;
    }
    if (element.hasAttribute('sandbox') && !element.sandbox.contains('allow-same-origin')) return true;
    const source = element.getAttribute('src');
    if (!source || source === 'about:blank') return false;
    try {
      const target = new URL(source, document.baseURI);
      return !['http:', 'https:'].includes(target.protocol) || target.origin !== location.origin;
    } catch { return false; }
  }).length;
})()`;

function delay(ms: number, signal?: AbortSignal) {
  if (signal?.aborted) return Promise.reject(new Error('browser_cancelled'));
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(finish, ms);
    const abort = () => finish(new Error('browser_cancelled'));
    function finish(error?: Error) {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      error ? reject(error) : resolve();
    }
    signal?.addEventListener('abort', abort, { once: true });
  });
}

// Passive SSO only: no script execution or form interaction on authentication
// origins. Recovery time is bounded by the operation's remaining budget; only
// observable navigation/loading progress is recorded without extending the cap.
export async function waitForReviewAuthentication(wc: WebContents, policy: ReviewNavigationPolicy,
  deadline: number, signal?: AbortSignal, maxWaitMs = 15000) {
  const started = Date.now();
  const until = Math.min(deadline, started + Math.max(1, Math.min(maxWaitMs, 15000)));
  let progressCount = 0;
  let outcome: NonNullable<ReviewNavigationDiagnostics['authWait']>['outcome'] = 'timeout';
  let ownedSince: number | undefined;
  try {
    if (signal?.aborted || wc.isDestroyed()) throw new Error('browser_cancelled');
    let lastUrl = wc.getURL(), lastLoading = wc.isLoadingMainFrame();
    while (Date.now() < until) {
      if (signal?.aborted || wc.isDestroyed()) throw new Error('browser_cancelled');
      if (policy.denied) throw new Error('browser_navigation_changed');
      const current = wc.getURL();
      const loading = wc.isLoadingMainFrame();
      if (current !== lastUrl || loading !== lastLoading) {
        progressCount++;
        lastUrl = current; lastLoading = loading;
      }
      if (httpOrigin(current) === policy.origin && !policy.awaitingAuthentication && !loading) {
        ownedSince ??= Date.now();
        if (Date.now() - ownedSince >= 100) { outcome = 'returned'; return; }
      } else {
        ownedSince = undefined;
        if (httpOrigin(current) !== policy.origin && !policy.isAuthUrl(current)) throw new Error('browser_navigation_changed');
      }
      // An IdP can be DOM-stable while a script/token exchange is still pending.
      // Give only approved SSO pages the full bounded passive-return window.
      await delay(Math.max(1, Math.min(25, until - Date.now())), signal);
    }
    if (policy.denied) throw new Error('browser_navigation_changed');
    throw new Error(policy.awaitingAuthentication || policy.isAuthUrl(wc.getURL()) ? 'authentication_recovery_timeout' : 'browser_readiness_timeout');
  } catch (error) {
    if ((error as Error).message === 'browser_cancelled') outcome = 'cancelled';
    else if ((error as Error).message === 'browser_navigation_changed') outcome = 'navigation_denied';
    throw error;
  } finally {
    policy.recordAuthenticationWait({elapsedMs: Math.max(0, Date.now() - started),
      budgetMs: Math.max(0, until - started), progressCount, outcome});
  }
}

async function waitForOwnedUrlStable(wc: WebContents, origin: string, targetUrl: string, deadline: number, signal?: AbortSignal) {
  const until = deadline;
  let expectedUrl = targetUrl;
  let currentUrl = wc.getURL();
  let stableSince = Date.now();
  let unownedNavigation = false;
  const noteNavigation = (url: string, inPlace: boolean, isMainFrame: boolean) => {
    if (isMainFrame === false) return;
    if (httpOrigin(url) !== origin) { unownedNavigation = true; return; }
    expectedUrl = url;
    stableSince = Date.now();
  };
  const started = (_event: unknown, url: string, inPlace: boolean, isMainFrame: boolean) => noteNavigation(url, inPlace, isMainFrame);
  const navigatedInPage = (_event: unknown, url: string, isMainFrame: boolean) => noteNavigation(url, true, isMainFrame);
  const rendererGone = () => { unownedNavigation = true; };
  wc.on('did-start-navigation', started);
  wc.on('did-navigate-in-page', navigatedInPage);
  wc.on('render-process-gone', rendererGone);
  try {
    while (Date.now() < until) {
      if (signal?.aborted) throw new Error('browser_cancelled');
      if (wc.isDestroyed()) throw new Error('browser_cancelled');
      if (unownedNavigation) throw new Error('browser_navigation_changed');
      const nextUrl = wc.getURL();
      if (httpOrigin(nextUrl) !== origin) throw new Error('browser_navigation_changed');
      if (nextUrl !== currentUrl) { currentUrl = nextUrl; stableSince = Date.now(); }
      if (currentUrl === expectedUrl && !wc.isLoadingMainFrame() && Date.now() - stableSince >= 100) return;
      await delay(Math.min(25, until - Date.now()), signal);
    }
    throw new Error('browser_readiness_timeout');
  } finally {
    wc.removeListener('did-start-navigation', started);
    wc.removeListener('did-navigate-in-page', navigatedInPage);
    wc.removeListener('render-process-gone', rendererGone);
  }
}

// This class is main-process only. A caller supplies an owned WebContents, never code.
export class BrowserService {
  private busy = new Set<number>();
  isBusy(wc: WebContents) { return this.busy.has(wc.id); }
  private frameExecutor = new FillerFrameExecutor();
  constructor(private adapter: BrowserAdapter, private options: {
    observationTimeoutMs?: number; reviewPollIntervalMs?: number; emptyConfirmationMs?: number; frameConfirmationMs?: number;
    unparsedWindowMs?: number; unparsedStableMs?: number;
  } = {}) {}
  private async collectFrames(wc: WebContents, operationId: string, url: string, code: string, signal?: AbortSignal) {
    const top = wc.mainFrame;
    const subtree = top.framesInSubtree;
    if (subtree.length > 32) throw new Error('browser_payload_limit');
    const origin = httpOrigin(url);
    const frames = subtree.filter(frame => !frame.detached && !frame.isDestroyed()
      && httpOrigin(frame.url) === origin && frame.origin === origin);
    const snapshots = frames.map(frame => ({ frame, url: frame.url, token: frame.frameToken }));
    const samples: FrameSample[] = [];
    let payloadBytes = 0;
    for (const {frame, url: frameUrl} of snapshots) {
      if (signal?.aborted || wc.isDestroyed()) throw new Error('browser_cancelled');
      const source = {frameId: frame === top ? 0 : frame.frameTreeNodeId, frameUrl};
      try {
        const raw = frame === top ? await wc.executeJavaScriptInIsolatedWorld(1004, [{ code }])
          : await this.frameExecutor.execute(wc, frame, this.adapter.buildFrameObservationScript!({ operation_id: operationId, page_url: frameUrl }));
        samples.push({...source, raw});
      } catch (error) {
        if (frame === top) throw error;
        if (/document_changed|frame_detached|context_destroyed/.test((error as Error).message)) throw new Error('browser_frame_changed');
        samples.push({...source, unavailable: true});
      }
      if (signal?.aborted || wc.isDestroyed()) throw new Error('browser_cancelled');
      payloadBytes += Buffer.byteLength(JSON.stringify(samples[samples.length - 1]), 'utf8');
      if (payloadBytes > 262144) throw new Error('browser_payload_limit');
    }
    const skipped = subtree.length - frames.length;
    const restricted = skipped ? await wc.executeJavaScriptInIsolatedWorld(1004, [{code: visibleRestrictedFramesScript}]) : 0;
    const scopeDeniedFrameCount = Number.isSafeInteger(restricted) && restricted > 0 ? Math.min(restricted, skipped) : 0;
    const current = wc.mainFrame.framesInSubtree;
    if (wc.mainFrame !== top || current.length !== subtree.length || snapshots.some(({frame, url: frameUrl, token}) =>
      frame.isDestroyed() || frame.detached || !current.includes(frame) || frame.url !== frameUrl || frame.frameToken !== token)) {
      throw new Error('browser_frame_changed');
    }
    return {samples, skipped, scopeDeniedFrameCount};
  }
  async observe(wc: WebContents, operationId: string, applicationIds: string[] = [], capture = false, options: ObservationOptions = {}) {
    if (options.signal?.aborted) throw new Error('browser_cancelled');
    if (wc.isDestroyed() || this.busy.has(wc.id)) throw new Error('browser_unavailable_or_busy');
    if (!/^[a-zA-Z0-9_.:-]{1,128}$/.test(operationId) || applicationIds.length > 100 ||
        applicationIds.some(id => !/^[a-zA-Z0-9_.:-]{1,128}$/.test(id))) throw new Error('browser_invalid_binding');
    if (capture && (!this.adapter.buildManualCaptureScript || !this.adapter.normalizeManualCapture)) throw new Error('manual_capture_unavailable');
    const captureIdentity = captureReviewIdentity(wc);
    const url = captureIdentity.url;
    const ownedOrigin = options.ownedOrigin ?? httpOrigin(url);
    if (!ownedOrigin || httpOrigin(url) !== ownedOrigin) throw new Error('browser_navigation_changed');
    this.busy.add(wc.id);
    let mainDocumentChanged = false;
    let childDocumentChanged = false;
    let ownedInPlaceUrl: string | undefined;
    const started = (_event: unknown, navigationUrl: string, inPlace: boolean, isMainFrame: boolean) => {
      if (isMainFrame === false) { childDocumentChanged = true; return; }
      if ((inPlace || options.allowSameOriginNavigation) && httpOrigin(navigationUrl) === ownedOrigin) ownedInPlaceUrl = navigationUrl;
      else mainDocumentChanged = true;
    };
    const navigatedInPage = (_event: unknown, navigationUrl: string, isMainFrame: boolean) => {
      if (isMainFrame === false) { childDocumentChanged = true; return; }
      if (httpOrigin(navigationUrl) === ownedOrigin) ownedInPlaceUrl = navigationUrl;
      else mainDocumentChanged = true;
    };
    const attemptedNavigation = (_event: unknown, navigationUrl: string, _inPlace?: boolean, isMainFrame?: boolean) => {
      if (isMainFrame === false) return;
      if (options.allowSameOriginNavigation && httpOrigin(navigationUrl) === ownedOrigin) ownedInPlaceUrl = navigationUrl;
      else mainDocumentChanged = true;
    };
    const rendererGone = () => { mainDocumentChanged = true; };
    wc.on('did-start-navigation', started);
    wc.on('did-navigate-in-page', navigatedInPage);
    wc.on('will-navigate', attemptedNavigation);
    wc.on('will-redirect', attemptedNavigation);
    wc.on('render-process-gone', rendererGone);
    let timer: NodeJS.Timeout | undefined;
    let onAbort: (() => void) | undefined;
    const collectionController = new AbortController();
    const collectionSignal = options.signal ? AbortSignal.any([options.signal, collectionController.signal]) : collectionController.signal;
    try {
      if (wc.isLoadingMainFrame()) throw new Error('browser_page_loading');
      const params = { operation_id: operationId, page_url: url };
      const code = capture ? this.adapter.buildManualCaptureScript!(params) : this.adapter.buildObservationScript(params);
      const remaining = options.deadline === undefined ? Infinity : options.deadline - Date.now();
      if (remaining <= 0) throw new ObservationTimeout(true);
      const scriptTimeoutMs = this.options.observationTimeoutMs ?? 12000;
      const timeoutMs = Math.max(1, Math.min(scriptTimeoutMs, remaining));
      const aborted = options.signal ? new Promise<never>((_, reject) => {
        onAbort = () => reject(new Error('browser_cancelled'));
        options.signal!.addEventListener('abort', onAbort, { once: true });
      }) : new Promise<never>(() => {});
      let raw: unknown;
      const multiFrame = !capture && !!wc.mainFrame && !!this.adapter.buildFrameObservationScript && !!this.adapter.normalizeFrameObservations;
      try {
        raw = await Promise.race([
          multiFrame ? this.collectFrames(wc, operationId, url, code, collectionSignal) : wc.executeJavaScriptInIsolatedWorld(1004, [{ code }]),
          new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new ObservationTimeout(remaining < scriptTimeoutMs)), timeoutMs); }),
          aborted
        ]);
      } catch (error) {
        if (options.signal?.aborted) throw new Error('browser_cancelled');
        if (wc.isDestroyed() || mainDocumentChanged) throw new Error('browser_navigation_changed');
        if (ownedInPlaceUrl) throw new OwnedNavigation(ownedInPlaceUrl);
        throw error;
      }
      if (wc.isDestroyed() || mainDocumentChanged || httpOrigin(wc.getURL()) !== ownedOrigin) throw new Error('browser_navigation_changed');
      if (ownedInPlaceUrl) throw new OwnedNavigation(ownedInPlaceUrl);
      if (multiFrame && childDocumentChanged) throw new Error('browser_frame_changed');
      if (wc.getURL() !== url) throw new Error('browser_navigation_changed');
      if (Buffer.byteLength(JSON.stringify(raw) || '', 'utf8') > 262144) throw new Error('browser_payload_limit');
      if (multiFrame) {
        const collected = raw as {samples: FrameSample[]; skipped: number; scopeDeniedFrameCount: number};
        const observation = this.adapter.normalizeFrameObservations!(collected.samples, { ...params, application_ids: applicationIds }, collected.skipped);
        if (observation && typeof observation === 'object' && !Array.isArray(observation)) {
          const captured = observation as {result?: {diagnostics?: Record<string, unknown>}};
          if (captured.result) return bindReviewObservation({...observation, result: {...captured.result,
            diagnostics: {...captured.result.diagnostics, scopeDeniedFrameCount: collected.scopeDeniedFrameCount}}}, captureIdentity);
        }
        return bindReviewObservation(observation, captureIdentity);
      }
      return bindReviewObservation(capture ? this.adapter.normalizeManualCapture!(raw, params) :
        this.adapter.normalizeObservation(raw, { ...params, application_ids: applicationIds }), captureIdentity);
    } finally {
      collectionController.abort();
      if (onAbort) options.signal?.removeEventListener('abort', onAbort);
      clearTimeout(timer); wc.removeListener('did-start-navigation', started); wc.removeListener('did-navigate-in-page', navigatedInPage);
      wc.removeListener('will-navigate', attemptedNavigation); wc.removeListener('will-redirect', attemptedNavigation);
      wc.removeListener('render-process-gone', rendererGone);
      this.busy.delete(wc.id);
    }
  }

  async observeForReview(wc: WebContents, operationId: string, applicationIds: string[], options: ReviewObserveOptions) {
    let lastObservation: ReviewDiagnosticSummary = {...reviewDiagnosticSummary(), page: {
      url: reviewDiagnosticUrl(wc.getURL()), title: '', textSnippet: ''}};
    let reportedWaiting = false;
    let emptySince: number | undefined;
    let deniedSince: number | undefined, deniedKey: string | undefined, deniedSamples = 0;
    let pendingSamples = 0;
    let lastPendingObservation: ReviewDiagnosticSummary | undefined;
    const samplingStarted = Date.now();
    let firstObservation: ReviewDiagnosticSummary | undefined;
    let sampleCount = 0, stableSamples = 0, stableSince: number | undefined, stableKey: string | undefined;
    let attemptedUrl: string | undefined, navigationReason: string | undefined;
    let entryReason: ReviewRecordEntryReason | undefined;
    let entrySeen = false;
    let entryAttempts = 0;
    let entryAttempt: ReviewRecordEntryAttempt | undefined;
    const entrySamples = new Set<string>();
    let detailsAttempted = false;
    let detailsFollowup: ReviewRecordDetailsFollowup | undefined;
    let phase: ReviewNavigationDiagnostics['phase'] = 'observation';
    const requestedUrl = options.requestedUrl || wc.getURL();
    let lastUrl = wc.getURL();
    const currentUrl = () => { if (!wc.isDestroyed()) lastUrl = wc.getURL(); return lastUrl; };
    const navigation = () => navigationReason || requestedUrl !== currentUrl()
      ? {...reviewNavigationDiagnostics(requestedUrl, currentUrl(), attemptedUrl, navigationReason || 'same_origin_navigation'), phase} : undefined;
    const ownedOrigin = options.ownedOrigin ?? httpOrigin(wc.getURL());
    if (!ownedOrigin || httpOrigin(wc.getURL()) !== ownedOrigin) throw new ReviewObservationError('browser_navigation_changed', lastObservation,
      {...reviewNavigationDiagnostics(requestedUrl, wc.getURL(), undefined, 'initial_origin_mismatch'), phase});
    let mainDocumentChanged = false;
    const noteNavigation = (url: string, event: string) => {
      if (mainDocumentChanged) return;
      emptySince = deniedSince = stableSince = undefined;
      attemptedUrl = url;
      const sameOrigin = httpOrigin(url) === ownedOrigin;
      navigationReason = event + (sameOrigin ? '_same_origin' : '_cross_origin');
      if (!sameOrigin) mainDocumentChanged = true;
    };
    const started = (_event: unknown, navigationUrl: string, inPlace: boolean, isMainFrame: boolean) => {
      if (isMainFrame === false) return;
      noteNavigation(navigationUrl, 'did_start_navigation');
    };
    const navigatedInPage = (_event: unknown, navigationUrl: string, isMainFrame: boolean) => {
      if (isMainFrame !== false) noteNavigation(navigationUrl, 'did_navigate_in_page');
    };
    const attemptedNavigation = (_event: unknown, navigationUrl: string, _inPlace?: boolean, isMainFrame?: boolean) => {
      if (isMainFrame === false) return;
      noteNavigation(navigationUrl, 'will_navigate');
    };
    const redirected = (_event: unknown, navigationUrl: string, _inPlace?: boolean, isMainFrame?: boolean) => {
      if (isMainFrame !== false) noteNavigation(navigationUrl, 'will_redirect');
    };
    const rendererGone = () => { mainDocumentChanged = true; navigationReason = 'renderer_gone'; };
    wc.on('did-start-navigation', started);
    wc.on('did-navigate-in-page', navigatedInPage);
    wc.on('will-navigate', attemptedNavigation);
    wc.on('will-redirect', redirected);
    wc.on('render-process-gone', rendererGone);
    try {
      options.onStage?.('EXTRACTING');
      options.onDiagnostic?.(lastObservation, navigation());
      while (Date.now() < options.deadline) {
        if (options.signal?.aborted) throw new Error('browser_cancelled');
        if (mainDocumentChanged || wc.isDestroyed() || httpOrigin(wc.getURL()) !== ownedOrigin) throw new Error('browser_navigation_changed');
        let observation: unknown;
        try {
          observation = await this.observe(wc, operationId, applicationIds, false,
            { ...options, ownedOrigin, allowSameOriginNavigation: true });
        } catch (error) {
          emptySince = deniedSince = stableSince = undefined;
          if (error instanceof OwnedNavigation) {
            phase = 'navigation_recovery';
            await waitForOwnedUrlStable(wc, ownedOrigin, error.targetUrl, options.deadline, options.signal);
            phase = 'observation';
            continue;
          }
          // The final sample can have only milliseconds left after readiness
          // polling. That clipped total budget is not proof of a stalled script;
          // an independent script limit, or no prior readable sample, still is.
          if (error instanceof ObservationTimeout && error.deadlineLimited && lastPendingObservation) {
            lastObservation = lastPendingObservation;
            throw new Error('browser_readiness_timeout');
          }
          const code = (error as Error).message;
          if (code !== 'browser_page_loading' && code !== 'browser_frame_changed') throw error;
          if (!reportedWaiting) { reportedWaiting = true; options.onStage?.('WAITING_FOR_CONTENT'); }
          const remaining = options.deadline - Date.now();
          if (remaining > 0) await delay(Math.min(this.options.reviewPollIntervalMs ?? 250, remaining), options.signal);
          continue;
        }
        if (mainDocumentChanged) throw new Error('browser_navigation_changed');
        const summary = reviewDiagnosticSummary(observation);
        firstObservation ??= summary;
        sampleCount++;
        const capturedResult = (observation as {result?: Record<string, unknown>})?.result;
        // Compare bounded in-memory content, not the redacted snippet (which can
        // hide real DOM changes). Only the first/last safe summaries are retained.
        const sampledPage = capturedResult?.page as {page_url?: string; title?: string; text?: string} | undefined;
        const sampledDiagnostics = capturedResult?.diagnostics as {frames?: unknown[]; successfulFrameCount?: number} | undefined;
        const key = surfaceFingerprint(observation, summary);
        if (summary.readyState !== 'complete' || summary.loadingVisible || !summary.visibleTextLength) {
          stableSince = undefined; stableSamples = 0;
        } else {
          if (key !== stableKey || stableSince === undefined) { stableSince = Date.now(); stableSamples = 0; }
          stableSamples++;
        }
        stableKey = key;
        lastObservation = {...summary, page: summary.page || {url: reviewDiagnosticUrl(wc.getURL()), title: '', textSnippet: ''},
          sampling: {count: sampleCount, elapsedMs: Date.now() - samplingStarted,
            stableMs: stableSince === undefined ? 0 : Date.now() - stableSince,
            first: {pageState: firstObservation.pageState, readyState: firstObservation.readyState,
              visibleTextLength: firstObservation.visibleTextLength, recordCount: firstObservation.recordCount,
              loadingVisible: firstObservation.loadingVisible, page: firstObservation.page}}};
        options.onDiagnostic?.(lastObservation, navigation());
        if (isRedirectedLogin(observation, options.requestedUrl)) {
          const captured = observation as { result: Record<string, unknown> };
          observation = inheritReviewObservation(observation, { ...captured, status: 'STATE_UNCLEAR', error_code: 'LOGIN_REQUIRED',
              result: { ...captured.result, requires_user_action: true, pause: { reason: 'login_required' } } });
        } else if (isRedirectedHome(observation, options.requestedUrl)) {
          navigationReason = 'returned_to_home_without_application_records';
        }
        let readiness: ReviewReadiness = classifyReviewReadiness(observation);
        if (!detailsAttempted && (readiness === 'records' || readiness === 'pending')
            && summary.readyState === 'complete' && !summary.loadingVisible && needsReviewRecordDetails(observation)
            && options.deadline - Date.now() > 100) {
          detailsAttempted = true;
          detailsFollowup = await expandReviewRecordDetails(wc, ownedOrigin, options.deadline, options.signal);
          if (detailsFollowup.expanded_count) {
            // Discard collapsed-card evidence, then re-read the actual expanded
            // DOM before any model/screenshot sees the observation.
            emptySince = deniedSince = stableSince = undefined;
            stableSamples = 0;
            options.onStage?.('WAITING_FOR_CONTENT');
            await delay(Math.min(100, Math.max(0, options.deadline - Date.now())), options.signal);
            continue;
          }
        }
        const entry = reviewRecordEntryReason(observation, options.requestedUrl);
        if (!entry && entryReason) {
          entryReason = undefined;
          if (entryAttempt?.outcome === 'followed') navigationReason = 'application_record_entry_followed';
        }
        if (readiness === 'pending' && entry) {
          entrySeen = true;
          entryReason = entry;
          navigationReason = entry;
          const entryKey = currentUrl() + ':' + key;
          if (entryAttempts < 2 && stableSamples >= 2 && !summary.loadingVisible
              && summary.readyState === 'complete' && !entrySamples.has(entryKey)
              && options.deadline - Date.now() > 100) {
            entrySamples.add(entryKey);
            entryAttempts++;
            entryAttempt = await followReviewRecordEntry(wc, ownedOrigin, options.deadline, options.signal);
            if (entryAttempt.outcome === 'followed') {
              emptySince = deniedSince = stableSince = undefined;
              stableSamples = 0;
              options.onStage?.('WAITING_FOR_CONTENT');
              if (wc.isLoadingMainFrame() || currentUrl() !== sampledPage?.page_url) {
                phase = 'navigation_recovery';
                await waitForOwnedUrlStable(wc, ownedOrigin, currentUrl(), options.deadline, options.signal);
                phase = 'observation';
              } else await delay(Math.min(100, options.deadline - Date.now()), options.signal);
              continue;
            }
          }
        }
        const reuseProof = options.stableReadableProof;
        const sameStableSurface = !!reuseProof && issuedStabilityProofs.has(reuseProof)
          && matchesReviewIdentity(wc, reuseProof.binding) && reuseProof.fingerprint === key
          && summary.readyState === 'complete' && !summary.loadingVisible && !!summary.visibleTextLength;
        let provenStableSurface = false;
        if ((readiness === 'pending' && !entryReason && sameStableSurface) || (readiness === 'pending' && stableSince !== undefined && stableSamples >= 5
            && Date.now() - samplingStarted >= (this.options.unparsedWindowMs ?? 12000)
            && Date.now() - stableSince >= (this.options.unparsedStableMs ?? 4000))) {
          const captured = observation as {result: Record<string, unknown>};
          readiness = entryReason ? 'record_entry_required' : 'unparsed_page';
          provenStableSurface = readiness === 'unparsed_page';
          observation = inheritReviewObservation(observation, {...captured, status: entryReason ? 'STATE_UNCLEAR' : 'SUCCEEDED',
            error_code: entryReason?.toUpperCase(),
            result: {...captured.result, evidence_only: true, database_updated: false,
              extraction_reason: entryReason || 'unparsed_page', last_observation: lastObservation}});
        }
        if (readiness === 'page_unavailable') {
          const captured = observation as { result: Record<string, unknown> };
          observation = inheritReviewObservation(observation, {...captured, status: 'STATE_UNCLEAR', error_code: 'APPLICATION_PAGE_UNAVAILABLE',
            result: {...captured.result, evidence_only: true, database_updated: false, last_observation: lastObservation}});
        }
        if (readiness === 'pending' && lastObservation.pageState === 'frame_scope_denied'
            && lastObservation.readyState === 'complete' && !lastObservation.loadingVisible
            && !lastObservation.unavailableFrameCount) {
          const key = JSON.stringify(summary);
          if (key !== deniedKey || deniedSince === undefined) { deniedKey = key; deniedSince = Date.now(); deniedSamples = 0; }
          deniedSamples++;
          // A blank readable surface plus a stable denied frame can end early.
          // Nonempty/loading pages keep polling so a helper iframe cannot hide delayed cards.
          if (deniedSamples >= 3
              && Date.now() - samplingStarted >= (this.options.unparsedWindowMs ?? 12000)
              && Date.now() - deniedSince >= (this.options.frameConfirmationMs ?? 2500)) {
            throw new Error('FRAME_SCOPE_DENIED');
          }
        } else { deniedSince = deniedKey = undefined; deniedSamples = 0; }
        // An initial empty-state placeholder can precede the asynchronous list.
        if (readiness === 'confirmed_empty') {
          emptySince ??= Date.now();
          if (Date.now() - emptySince < (this.options.emptyConfirmationMs ?? 750)) readiness = 'pending';
        } else emptySince = undefined;
        if (readiness !== 'pending') {
          if (readiness === 'login_required') options.onStage?.('WAITING_FOR_LOGIN');
          else if (readiness === 'terminal' || readiness === 'page_unavailable' || readiness === 'unparsed_page'
              || readiness === 'record_entry_required') options.onStage?.('STATE_UNCLEAR');
          else options.onStage?.('VALIDATING');
          if (observation && typeof observation === 'object' && !Array.isArray(observation)) {
            const captured = observation as {result?: Record<string, unknown>};
            if (entrySeen && readiness === 'records') navigationReason = 'application_record_entry_followed';
            const completed = inheritReviewObservation(observation, { ...observation, review_readiness: readiness,
              ...(captured.result && (navigation() || entrySeen || detailsFollowup) ? {result: {...captured.result,
                ...(detailsFollowup ? {record_details_followup: detailsFollowup} : {}),
                ...(entrySeen ? {record_entry_followup: {attempt_count: entryAttempts,
                  outcome: readiness === 'records' ? 'entered' : entryAttempt?.outcome || 'not_found',
                  ...(entryAttempt?.label ? {label: entryAttempt.label} : {})}} : {}),
                ...(navigation() ? {navigation_diagnostics: navigation()} : {})}} : {}) });
            // This capability is process-local and cannot be fabricated by a
            // page, protocol response or model. Only a completed stable wait
            // (or the same still-owned document) can skip the duplicate wait.
            const binding = reviewObservationBinding(completed);
            if (provenStableSurface && binding) {
              const proof = {fingerprint: key, binding};
              issuedStabilityProofs.add(proof); stabilityProofs.set(completed, proof);
            }
            return completed;
          }
          return observation;
        }
        const successfulFrameCount = sampledDiagnostics?.successfulFrameCount;
        if (typeof sampledPage?.text === 'string' || (typeof successfulFrameCount === 'number'
          && Number.isSafeInteger(successfulFrameCount) && successfulFrameCount > 0)) lastPendingObservation = lastObservation;
        if (!reportedWaiting) { reportedWaiting = true; options.onStage?.('WAITING_FOR_CONTENT'); }
        const remaining = options.deadline - Date.now();
        const interval = Math.min((this.options.reviewPollIntervalMs ?? 250) * 2 ** Math.min(pendingSamples++, 2), 1000);
        if (remaining > 0) await delay(Math.min(interval, remaining), options.signal);
      }
      const stableDenied = deniedSince !== undefined && deniedSamples >= 3
        && Date.now() - deniedSince >= (this.options.frameConfirmationMs ?? 2500);
      throw new Error(options.signal?.aborted ? 'browser_cancelled' : stableDenied ? 'FRAME_SCOPE_DENIED' : 'browser_readiness_timeout');
    } catch (error) {
      throw new ReviewObservationError((error as Error).message, lastObservation, navigation());
    } finally {
      wc.removeListener('did-start-navigation', started);
      wc.removeListener('did-navigate-in-page', navigatedInPage);
      wc.removeListener('will-navigate', attemptedNavigation);
      wc.removeListener('will-redirect', redirected);
      wc.removeListener('render-process-gone', rendererGone);
    }
  }
}
