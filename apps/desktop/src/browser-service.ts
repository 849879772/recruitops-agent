import type { WebContents } from 'electron';
import { classifyReviewReadiness, isRedirectedLogin, reviewDiagnosticSummary, ReviewObservationError } from './review-readiness';
import type { ReviewReadiness } from './review-readiness';
import { FillerFrameExecutor } from './filler-frame-executor';

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

function httpOrigin(value: string): string | undefined {
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.origin : undefined;
  } catch { return undefined; }
}

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
  private frameExecutor = new FillerFrameExecutor();
  constructor(private adapter: BrowserAdapter, private options: {
    observationTimeoutMs?: number; reviewPollIntervalMs?: number; emptyConfirmationMs?: number;
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
    const current = wc.mainFrame.framesInSubtree;
    if (wc.mainFrame !== top || current.length !== subtree.length || snapshots.some(({frame, url: frameUrl, token}) =>
      frame.isDestroyed() || frame.detached || !current.includes(frame) || frame.url !== frameUrl || frame.frameToken !== token)) {
      throw new Error('browser_frame_changed');
    }
    return {samples, skipped: subtree.length - frames.length};
  }
  async observe(wc: WebContents, operationId: string, applicationIds: string[] = [], capture = false, options: ObservationOptions = {}) {
    if (options.signal?.aborted) throw new Error('browser_cancelled');
    if (wc.isDestroyed() || this.busy.has(wc.id)) throw new Error('browser_unavailable_or_busy');
    if (!/^[a-zA-Z0-9_.:-]{1,128}$/.test(operationId) || applicationIds.length > 100 ||
        applicationIds.some(id => !/^[a-zA-Z0-9_.:-]{1,128}$/.test(id))) throw new Error('browser_invalid_binding');
    if (capture && (!this.adapter.buildManualCaptureScript || !this.adapter.normalizeManualCapture)) throw new Error('manual_capture_unavailable');
    const url = wc.getURL();
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
      if (remaining <= 0) throw new Error('browser_observation_timeout');
      const timeoutMs = Math.max(1, Math.min(this.options.observationTimeoutMs ?? 12000, remaining));
      const aborted = options.signal ? new Promise<never>((_, reject) => {
        onAbort = () => reject(new Error('browser_cancelled'));
        options.signal!.addEventListener('abort', onAbort, { once: true });
      }) : new Promise<never>(() => {});
      let raw: unknown;
      const multiFrame = !capture && !!wc.mainFrame && !!this.adapter.buildFrameObservationScript && !!this.adapter.normalizeFrameObservations;
      try {
        raw = await Promise.race([
          multiFrame ? this.collectFrames(wc, operationId, url, code, collectionSignal) : wc.executeJavaScriptInIsolatedWorld(1004, [{ code }]),
          new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('browser_observation_timeout')), timeoutMs); }),
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
        const collected = raw as {samples: FrameSample[]; skipped: number};
        return this.adapter.normalizeFrameObservations!(collected.samples, { ...params, application_ids: applicationIds }, collected.skipped);
      }
      return capture ? this.adapter.normalizeManualCapture!(raw, params) :
        this.adapter.normalizeObservation(raw, { ...params, application_ids: applicationIds });
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
    let lastObservation = reviewDiagnosticSummary();
    let reportedWaiting = false;
    let emptySince: number | undefined;
    let pendingSamples = 0;
    const ownedOrigin = options.ownedOrigin ?? httpOrigin(wc.getURL());
    if (!ownedOrigin || httpOrigin(wc.getURL()) !== ownedOrigin) throw new Error('browser_navigation_changed');
    let mainDocumentChanged = false;
    const started = (_event: unknown, navigationUrl: string, inPlace: boolean, isMainFrame: boolean) => {
      if (isMainFrame === false) return;
      emptySince = undefined;
      if (httpOrigin(navigationUrl) !== ownedOrigin) mainDocumentChanged = true;
    };
    const navigatedInPage = (_event: unknown, navigationUrl: string, isMainFrame: boolean) => {
      if (isMainFrame !== false && httpOrigin(navigationUrl) !== ownedOrigin) mainDocumentChanged = true;
    };
    const attemptedNavigation = (_event: unknown, navigationUrl: string, _inPlace?: boolean, isMainFrame?: boolean) => {
      if (isMainFrame === false) return;
      emptySince = undefined;
      if (httpOrigin(navigationUrl) !== ownedOrigin) mainDocumentChanged = true;
    };
    const rendererGone = () => { mainDocumentChanged = true; };
    wc.on('did-start-navigation', started);
    wc.on('did-navigate-in-page', navigatedInPage);
    wc.on('will-navigate', attemptedNavigation);
    wc.on('will-redirect', attemptedNavigation);
    wc.on('render-process-gone', rendererGone);
    try {
      options.onStage?.('EXTRACTING');
      while (Date.now() < options.deadline) {
        if (options.signal?.aborted) throw new Error('browser_cancelled');
        if (mainDocumentChanged || wc.isDestroyed() || httpOrigin(wc.getURL()) !== ownedOrigin) throw new Error('browser_navigation_changed');
        let observation: unknown;
        try {
          observation = await this.observe(wc, operationId, applicationIds, false,
            { ...options, ownedOrigin, allowSameOriginNavigation: true });
        } catch (error) {
          emptySince = undefined;
          if (error instanceof OwnedNavigation) {
            await waitForOwnedUrlStable(wc, ownedOrigin, error.targetUrl, options.deadline, options.signal);
            continue;
          }
          const code = (error as Error).message;
          if (code !== 'browser_page_loading' && code !== 'browser_frame_changed') throw error;
          if (!reportedWaiting) { reportedWaiting = true; options.onStage?.('WAITING_FOR_CONTENT'); }
          const remaining = options.deadline - Date.now();
          if (remaining > 0) await delay(Math.min(this.options.reviewPollIntervalMs ?? 250, remaining), options.signal);
          continue;
        }
        if (mainDocumentChanged) throw new Error('browser_navigation_changed');
        lastObservation = reviewDiagnosticSummary(observation);
        if (isRedirectedLogin(observation, options.requestedUrl)) {
          const captured = observation as { result: Record<string, unknown> };
          observation = { ...captured, status: 'STATE_UNCLEAR', error_code: 'LOGIN_REQUIRED',
            result: { ...captured.result, requires_user_action: true, pause: { reason: 'login_required' } } };
        }
        let readiness: ReviewReadiness = classifyReviewReadiness(observation);
        // An initial empty-state placeholder can precede the asynchronous list.
        if (readiness === 'confirmed_empty') {
          emptySince ??= Date.now();
          if (Date.now() - emptySince < (this.options.emptyConfirmationMs ?? 750)) readiness = 'pending';
        } else emptySince = undefined;
        if (readiness !== 'pending') {
          if (readiness === 'login_required') options.onStage?.('WAITING_FOR_LOGIN');
          else if (readiness === 'terminal') options.onStage?.('STATE_UNCLEAR');
          else options.onStage?.('VALIDATING');
          if (observation && typeof observation === 'object' && !Array.isArray(observation)) {
            return { ...observation, review_readiness: readiness };
          }
          return observation;
        }
        if (!reportedWaiting) { reportedWaiting = true; options.onStage?.('WAITING_FOR_CONTENT'); }
        const remaining = options.deadline - Date.now();
        const interval = Math.min((this.options.reviewPollIntervalMs ?? 250) * 2 ** Math.min(pendingSamples++, 2), 1000);
        if (remaining > 0) await delay(Math.min(interval, remaining), options.signal);
      }
      throw new Error(options.signal?.aborted ? 'browser_cancelled' : 'browser_readiness_timeout');
    } catch (error) {
      throw new ReviewObservationError((error as Error).message, lastObservation);
    } finally {
      wc.removeListener('did-start-navigation', started);
      wc.removeListener('did-navigate-in-page', navigatedInPage);
      wc.removeListener('will-navigate', attemptedNavigation);
      wc.removeListener('will-redirect', attemptedNavigation);
      wc.removeListener('render-process-gone', rendererGone);
    }
  }
}
