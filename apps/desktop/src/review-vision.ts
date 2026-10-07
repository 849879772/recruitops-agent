import type { WebContents } from 'electron';
import { captureReviewIdentity, matchesReviewIdentity, reviewObservationBinding } from './review-observation-binding';

export function canCaptureReview(observation: any): boolean {
  const data = observation?.result;
  if (observation?.status !== 'SUCCEEDED' || !data || data.requires_user_action || data.pause) return false;
  const diagnostics = data.diagnostics || {};
  if (diagnostics.scopeDeniedFrameCount || diagnostics.unavailableFrameCount) return false;
  const text = `${data.page?.title || ''} ${data.page?.text || ''}`;
  return !!data.page?.text?.trim() && !/captcha|验证码|人机验证|请先登录|请登录|登录失效|重新登录|login required|session expired|sign in|log in/i.test(text);
}

// Fixed, isolated-world program: only scroll and reversible privacy masks.
// No click, form submission, foreign-frame access or arbitrary caller script.
function captureSurface(action: string, y = 0): any {
  const stateKey = '__recruitopsReviewCapture';
  const globals = globalThis as any;
  if (action === 'restore') {
    const previous = globals[stateKey];
    if (previous) {
      previous.masks.forEach((e: HTMLElement) => e.remove());
      if (previous.target?.isConnected) previous.target.scrollTo({left: previous.targetX, top: previous.targetY, behavior: 'instant'});
      window.scrollTo({left: previous.x, top: previous.y, behavior: 'instant'});
    }
    delete globals[stateKey]; return true;
  }
  if (action === 'prepare') {
    if (globals[stateKey]) throw new Error('capture_already_active');
    const state: any = {x: scrollX, y: scrollY, masks: [], target: null};
    globals[stateKey] = state;
    window.scrollTo({left: 0, top: 0, behavior: 'instant'});
    // Only a visible, record-like list is eligible. Navigation, account panels,
    // forms and offscreen/hidden scrollers must never be explored for evidence.
    let bestArea = 0;
    for (const e of Array.from(document.querySelectorAll<HTMLElement>('body *')).slice(0, 5000)) {
      if (e.closest('nav,aside,form,[role="navigation"],[role="menu"],[role="dialog"],[hidden],[inert],[aria-hidden="true"],input,textarea,select,[contenteditable="true"]') ||
          /sidebar|side-bar|navigation|nav-menu|toolbar|modal/i.test(`${e.id} ${e.className}`)) continue;
      const style = getComputedStyle(e), r = e.getBoundingClientRect();
      if (!/auto|scroll/.test(style.overflowY) || style.display === 'none' || style.visibility !== 'visible' ||
          e.scrollHeight <= e.clientHeight + 40 || e.clientHeight < 150 || e.clientHeight > innerHeight || r.width < Math.max(200, innerWidth * .3)) continue;
      let top = Math.max(0, r.top), bottom = Math.min(innerHeight, r.bottom);
      let left = Math.max(0, r.left), right = Math.min(innerWidth, r.right);
      for (let parent = e.parentElement; parent && bottom > top && right > left; parent = parent.parentElement) {
        const parentStyle = getComputedStyle(parent), bounds = parent.getBoundingClientRect();
        if (parentStyle.visibility !== 'visible' || parentStyle.display === 'none' || parentStyle.opacity === '0') { bottom = top; break; }
        if (/hidden|clip|auto|scroll/.test(parentStyle.overflowY)) { top = Math.max(top, bounds.top); bottom = Math.min(bottom, bounds.bottom); }
        if (/hidden|clip|auto|scroll/.test(parentStyle.overflowX)) { left = Math.max(left, bounds.left); right = Math.min(right, bounds.right); }
      }
      if (bottom - top < e.clientHeight * .8 || right - left < r.width * .8 || style.opacity === '0') continue;
      const text = (e.innerText || '').slice(0, 40000);
      if (!/投递|申请|应聘|简历|笔试|面试|offer|applied|application|interview|assessment|resume/i.test(text) ||
          (text.match(/工程师|开发|岗位|职位|engineer|developer|position|job/gi) || []).length < 2) continue;
      const area = (bottom - top) * (right - left);
      if (area > bestArea) { bestArea = area; state.target = e; }
    }
    if (state.target) { state.targetX = state.target.scrollLeft; state.targetY = state.target.scrollTop; }
  }
  const state = globals[stateKey];
  if (!state) throw new Error('capture_not_prepared');
  state.masks.forEach((e: HTMLElement) => e.remove()); state.masks = [];
  if (state.target && !state.target.isConnected) throw new Error('vision_surface_unavailable');
  if (action !== 'sample') {
    if (state.target) state.target.scrollTo({left: state.targetX, top: y, behavior: 'instant'});
    else window.scrollTo({left: 0, top: y, behavior: 'instant'});
  }
  const cover = (r: DOMRect) => {
    const left = Math.max(0, r.left), top = Math.max(0, r.top);
    const right = Math.min(innerWidth, r.right), bottom = Math.min(innerHeight, r.bottom);
    if (right <= left || bottom <= top) return;
    const mask = document.createElement('div');
    mask.style.cssText = `position:absolute!important;left:${left + scrollX}px!important;top:${top + scrollY}px!important;width:${right - left}px!important;height:${bottom - top}px!important;background:#eceff4!important;z-index:2147483647!important;pointer-events:none!important;`;
    document.documentElement.appendChild(mask); state.masks.push(mask);
  };
  document.querySelectorAll('input,textarea,[contenteditable="true"],iframe,frame,[autocomplete],[class*="avatar"],[class*="user-info"],[class*="userInfo"]').forEach(e => cover(e.getBoundingClientRect()));
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let item: Node | null; let count = 0;
  while ((item = walker.nextNode()) && count++ < 10000) {
    if (!item.parentElement || /^(SCRIPT|STYLE|NOSCRIPT)$/.test(item.parentElement.tagName)) continue;
    if (/[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\d)1[3-9]\d{9}(?!\d)|\b\d{17}[\dXx]\b/.test(item.textContent || '')) {
      const range = document.createRange(); range.selectNodeContents(item);
      Array.from(range.getClientRects()).forEach(cover);
    }
  }
  const height = Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0);
  const target = state.target, bounds = target?.getBoundingClientRect();
  const containerTop = bounds ? Math.max(0, bounds.top + target.clientTop) : 0;
  const containerBottom = bounds ? Math.min(innerHeight, bounds.top + target.clientTop + target.clientHeight) : 0;
  return {height, viewport: innerHeight, width: innerWidth, y: scrollY, truncatedPrivacyScan: count >= 10000,
    scrollSurface: target ? 'application_container' : 'document',
    targetHeight: target ? target.scrollHeight : height,
    targetViewport: target ? containerBottom - containerTop : innerHeight,
    targetY: target ? target.scrollTop + Math.max(0, containerTop - bounds.top - target.clientTop) : scrollY,
    containerY: target ? target.scrollTop : null, containerTop: target ? containerTop : null,
    containerBottom: target ? containerBottom : null};
}

export async function captureReviewImages(wc: WebContents, expectedUrl: string, deadline: number, signal?: AbortSignal) {
  const identity = captureReviewIdentity(wc);
  const check = () => {
    if (signal?.aborted || wc.isDestroyed()) throw new Error('browser_cancelled');
    if (Date.now() >= deadline) throw new Error('browser_observation_timeout');
    if (wc.getURL() !== expectedUrl || !matchesReviewIdentity(wc, identity)) throw new Error('browser_navigation_changed');
  };
  const withinBudget = <T>(pending: Promise<T>) => new Promise<T>((resolve, reject) => {
    const finish = (error: Error | null, value?: T) => {
      clearTimeout(timer); signal?.removeEventListener('abort', cancel);
      error ? reject(error) : resolve(value as T);
    };
    const cancel = () => finish(new Error('browser_cancelled'));
    const timer = setTimeout(() => finish(new Error('browser_observation_timeout')), Math.max(1, deadline - Date.now()));
    signal?.addEventListener('abort', cancel, {once: true});
    pending.then(value => finish(null, value), error => finish(error));
    if (signal?.aborted) cancel();
  });
  const execute = async (action: string, y = 0) => {
    check();
    const result = await withinBudget(wc.executeJavaScriptInIsolatedWorld(1005,
      [{code: `(${captureSurface.toString()})(${JSON.stringify(action)},${y})`}]));
    check();
    return result;
  };
  check();
  const images: string[] = [];
  const offsets: any[] = [];
  let capturedBottom = 0, height = 0, targetHeight = 0, scrollSurface = 'document', targetTopReached = false;
  try {
    let surface = await execute('prepare');
    for (let i = 0; i < 4; i++) {
      check();
      surface = await execute('scroll', i === 0 ? 0 : Math.max(0, capturedBottom - 80));
      await new Promise(resolve => setTimeout(resolve, 100));
      surface = await execute('sample'); // Actual settled offsets for this bitmap, with fresh privacy masks.
      const targetViewport = surface.targetViewport ?? surface.viewport, targetY = surface.targetY ?? surface.y;
      if (surface.truncatedPrivacyScan || surface.viewport < 100 || surface.width < 100 || targetViewport < 100 ||
          surface.viewport > 4096 || surface.width > 4096) throw new Error('vision_surface_unavailable');
      check();
      const frame = await withinBudget(wc.capturePage());
      check();
      if (frame.isEmpty()) throw new Error('vision_surface_unavailable');
      const size = frame.getSize?.();
      if (size && (size.width > 8192 || size.height > 8192)) throw new Error('vision_image_too_large');
      const bytes = frame.toJPEG(82);
      if (bytes.length > 6 * 1024 * 1024) throw new Error('vision_image_too_large');
      images.push('data:image/jpeg;base64,' + bytes.toString('base64'));
      height = surface.height; targetHeight = surface.targetHeight ?? height;
      scrollSurface = surface.scrollSurface || 'document';
      capturedBottom = targetY + targetViewport;
      if (i === 0) targetTopReached = targetY === 0;
      offsets.push({document_y: surface.y, document_height: height, viewport_height: surface.viewport,
        container_y: surface.containerY ?? null, container_height: scrollSurface === 'application_container' ? targetHeight : null,
        container_viewport: scrollSurface === 'application_container' ? targetViewport : null,
        container_top: surface.containerTop ?? null, container_bottom: surface.containerBottom ?? null,
        target_visible_start: targetY, target_visible_bottom: capturedBottom});
      if (capturedBottom >= targetHeight) break;
    }
    return {images, coverage: {segments: images.length, truncated: !targetTopReached || capturedBottom < targetHeight,
      captured_bottom: capturedBottom, document_height: height, pagination_followed: false,
      scroll_surface: scrollSurface, target_height: targetHeight, target_top_reached: targetTopReached,
      target_bottom_reached: capturedBottom >= targetHeight, per_segment_offsets: offsets,
      coverage_basis: 'bounded_viewports', cards_complete: false}};
  } finally {
    // Cleanup may outlive cancellation/deadline, but must not touch a new document.
    if (!wc.isDestroyed() && wc.getURL() === expectedUrl && matchesReviewIdentity(wc, identity)) {
      let cleanupTimer: ReturnType<typeof setTimeout>;
      await Promise.race([wc.executeJavaScriptInIsolatedWorld(1005,
        [{code: `(${captureSurface.toString()})(${JSON.stringify('restore')},0)`}]),
      new Promise(resolve => { cleanupTimer = setTimeout(resolve, 1000); })]).catch(() => {}).finally(() => clearTimeout(cleanupTimer));
    }
  }
}

export async function attachReviewVision(wc: WebContents, observation: any, operationId: string,
  requestedUrl: string, origin: string, authorization: string, deadline: number, signal?: AbortSignal,
  transport: typeof fetch = fetch, onRequestStart?: () => void) {
  if (!canCaptureReview(observation)) return observation;
  const binding = reviewObservationBinding(observation);
  if (!binding || !matchesReviewIdentity(wc, binding)) throw new Error('browser_navigation_changed');
  const expectedUrl = binding.url;
  const check = () => {
    if (signal?.aborted || wc.isDestroyed()) throw new Error('browser_cancelled');
    if (!matchesReviewIdentity(wc, binding)) throw new Error('browser_navigation_changed');
    if (Date.now() >= deadline) throw new Error('browser_observation_timeout');
  };
  try {
    check();
    const captured = await captureReviewImages(wc, expectedUrl, deadline, signal);
    check(); // Includes the awaited restoration; no upload after a late navigation/cancel.
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new Error('browser_observation_timeout');
    onRequestStart?.();
    const response = await transport(origin + '/api/browser/vision', {method: 'POST', redirect: 'error',
      headers: {'Content-Type': 'application/json', Authorization: authorization},
      // Backend queue+provider is bounded at 65s. Never give it less than its
      // contract merely because the old synchronous provider cap was 45s; the
      // parent review deadline and cancellation remain the absolute limits.
      signal: AbortSignal.any([...(signal ? [signal] : []), AbortSignal.timeout(Math.min(67000, remaining))]),
      body: JSON.stringify({operation_id: operationId, page_url: requestedUrl, image_data_urls: captured.images})});
    const reading = await response.json() as any;
    if (!response.ok) throw new Error(typeof reading?.detail?.code === 'string' ? reading.detail.code : 'vision_service_unavailable');
    check();
    if (typeof reading.text !== 'string' || !Number.isFinite(reading.confidence) || reading.confidence < 0 || reading.confidence > 1 ||
        typeof reading.model !== 'string' || !/^[a-f0-9]{64}$/.test(reading.image_sha256)) throw new Error('vision_response_invalid');
    return {...observation, result: {...observation.result, vision: reading, vision_capture: captured.coverage}};
  } catch (error) {
    if (signal?.aborted || wc.isDestroyed()) throw new Error('browser_cancelled');
    if (!matchesReviewIdentity(wc, binding)) throw new Error('browser_navigation_changed');
    const code = error instanceof Error ? error.message : '';
    if (code === 'browser_navigation_changed') throw error;
    return {...observation, result: {...observation.result,
      vision_error: /^(?:vision_[a-z_]+|transport_failed|http_\d{3}|response_[a-z_]+|visual_evidence_missing|browser_observation_timeout)$/.test(code) ? code : 'vision_request_failed'}};
  }
}
