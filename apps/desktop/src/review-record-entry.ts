import type { WebContents } from 'electron';
import { isRedirectedHome } from './review-readiness';

export type ReviewRecordEntryReason = 'application_record_entry_not_entered' | 'application_record_home_redirect';
export type ReviewRecordEntryAttempt = { outcome: 'followed' | 'not_found' | 'ambiguous' | 'blocked'; label?: string };

// A records link in ordinary navigation is not by itself proof that a records
// page has not loaded. Follow only an explicit entry screen or a saved-record
// URL returning to the public home page, never a login/verification wall.
export function reviewRecordEntryReason(value: unknown, requestedUrl?: string): ReviewRecordEntryReason | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const observation = value as {status?: string; error_code?: string; result?: {
    application_records?: unknown[]; page?: {text?: string}; requires_user_action?: boolean}};
  if (observation.status === 'FAILED' || ['LOGIN_REQUIRED', 'CAPTCHA_REQUIRED', 'SOURCE_NOT_ALLOWED',
      'FRAME_NOT_ALLOWED', 'FRAME_SCOPE_DENIED', 'APPLICATION_PAGE_UNAVAILABLE'].includes(observation.error_code || '')
      || observation.result?.requires_user_action) return undefined;
  if (!Array.isArray(observation.result?.application_records) || observation.result.application_records.length) return undefined;
  if (isRedirectedHome(value, requestedUrl)) return 'application_record_home_redirect';
  const text = observation.result.page?.text;
  return typeof text === 'string' && /点击(?:按钮|这里)?(?:查看|进入)(?:我的)?(?:应聘|投递|申请)记录/.test(text.slice(0, 20000))
    ? 'application_record_entry_not_entered' : undefined;
}

// This fixed script sees only the owned top document. It does not inspect
// frames, guess routes, follow login links, submit forms or alter business data.
export function buildReviewRecordEntryScript(expectedUrl: string, ownedOrigin: string): string {
  return `(() => {
    if (location.href !== ${JSON.stringify(expectedUrl)} || location.origin !== ${JSON.stringify(ownedOrigin)})
      return {outcome: 'blocked'};
    const records = /^(?:(?:我的)?(?:应聘|投递|申请)记录|我的(?:应聘|投递|申请)|(?:查看|进入)(?:我的)?(?:应聘|投递|申请)记录)$/;
    const mutation = /(?:^|[\\/_.?&=:#-])(?:withdraw|cancel|delete|remove|submit|update|edit|save|bind|apply)(?:$|[\\/_.?&=:#-])/i;
    const visible = element => {
      const rect = element.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0 || rect.bottom <= 0 || rect.right <= 0
          || rect.top >= innerHeight || rect.left >= innerWidth || !element.getClientRects().length) return false;
      for (let owner = element; owner; owner = owner.parentElement) {
        const style = getComputedStyle(owner);
        if (owner.hidden || owner.inert || owner.getAttribute('aria-hidden') === 'true'
            || style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) return false;
      }
      return true;
    };
    const candidates = [];
    for (const element of Array.from(document.querySelectorAll('a,button,[role="button"],[role="link"],[tabindex],[onclick]')).slice(0, 150)) {
      if (!visible(element) || element.closest('form') || element.disabled
          || element.getAttribute('aria-disabled') === 'true' || element.getAttribute('aria-current') === 'page') continue;
      const label = String(element.innerText || element.getAttribute('aria-label') || '').replace(/\\s+/g, '').trim();
      const priority = records.test(label) ? 0 : label === '个人中心' ? 1 : -1;
      if (priority < 0 || /投递简历|申请职位|撤回|撤销|取消|删除|绑定|确认|修改|更新|提交|登录|注册|验证码/.test(label)) continue;
      const href = element.getAttribute('href');
      let target;
      if (href && /^javascript:/i.test(href.trim()) && !/^javascript:\\s*(?:void\\s*\\(\\s*0\\s*\\)|;)\\s*;?$/i.test(href.trim())) continue;
      if (href && href !== '#' && !/^javascript:/i.test(href.trim())) {
        try {
          target = new URL(href, document.baseURI);
          if (!['http:', 'https:'].includes(target.protocol) || target.origin !== location.origin || target.username || target.password
              || mutation.test(decodeURIComponent(target.pathname + target.search + target.hash)) || target.href === location.href) continue;
        } catch { continue; }
      }
      candidates.push({element, label, priority, target, key: target ? target.href : label + ':' + candidates.length});
    }
    if (!candidates.length) return {outcome: 'not_found'};
    const priority = Math.min(...candidates.map(candidate => candidate.priority));
    const selected = candidates.filter(candidate => candidate.priority === priority);
    const targets = new Set(selected.map(candidate => candidate.key));
    if (targets.size !== 1) return {outcome: 'ambiguous'};
    const candidate = selected[0];
    if (!candidate.element.isConnected || !visible(candidate.element) || location.href !== ${JSON.stringify(expectedUrl)})
      return {outcome: 'blocked'};
    if (candidate.target) location.assign(candidate.target.href);
    else candidate.element.click();
    return {outcome: 'followed', label: candidate.label};
  })()`;
}

export async function followReviewRecordEntry(wc: WebContents, ownedOrigin: string, deadline: number,
  signal?: AbortSignal): Promise<ReviewRecordEntryAttempt> {
  if (signal?.aborted || wc.isDestroyed()) throw new Error('browser_cancelled');
  const expectedUrl = wc.getURL();
  try {
    const url = new URL(expectedUrl);
    if (!['http:', 'https:'].includes(url.protocol) || url.origin !== ownedOrigin || url.username || url.password)
      throw new Error('browser_navigation_changed');
  } catch { throw new Error('browser_navigation_changed'); }
  const remaining = deadline - Date.now();
  if (remaining <= 0) return {outcome: 'blocked'};
  let timer: NodeJS.Timeout | undefined;
  let abort: (() => void) | undefined;
  try {
    const value = await Promise.race([
      wc.executeJavaScriptInIsolatedWorld(1004, [{code: buildReviewRecordEntryScript(expectedUrl, ownedOrigin)}]),
      new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('browser_observation_timeout')), Math.min(1500, remaining)); }),
      signal ? new Promise<never>((_, reject) => {
        abort = () => reject(new Error('browser_cancelled'));
        signal.addEventListener('abort', abort, {once: true});
      }) : new Promise<never>(() => {})
    ]);
    if (signal?.aborted || wc.isDestroyed()) throw new Error('browser_cancelled');
    if (new URL(wc.getURL()).origin !== ownedOrigin) throw new Error('browser_navigation_changed');
    if (!value || !['followed', 'not_found', 'ambiguous', 'blocked'].includes(value.outcome)) return {outcome: 'not_found'};
    return {outcome: value.outcome, ...(typeof value.label === 'string' && value.label.length <= 16 ? {label: value.label} : {})};
  } finally {
    clearTimeout(timer);
    if (abort) signal?.removeEventListener('abort', abort);
  }
}
