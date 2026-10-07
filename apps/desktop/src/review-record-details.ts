import type { WebContents } from 'electron';
import { captureReviewIdentity, matchesReviewIdentity } from './review-observation-binding';

export interface ReviewRecordDetailsFollowup {
  outcome: 'expanded' | 'not_found' | 'blocked' | 'limit_reached';
  expanded_count: number;
  reason?: 'record_context_not_proven' | 'card_boundary_not_proven' | 'unsafe_or_ambiguous_control' | 'candidate_limit';
}

export function needsReviewRecordDetails(value: unknown): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const observation = value as {status?: string; error_code?: string; result?: {
    page?: {text?: string}; requires_user_action?: boolean; pause?: unknown;
    diagnostics?: {scopeDeniedFrameCount?: number; unavailableFrameCount?: number}}};
  const result = observation.result;
  return observation.status === 'SUCCEEDED' && !observation.error_code && !!result
    && !result.requires_user_action && !result.pause && !result.diagnostics?.scopeDeniedFrameCount
    && !result.diagnostics?.unavailableFrameCount && typeof result.page?.text === 'string'
    && /展开详情|查看详情/.test(result.page.text.slice(0, 40000));
}

// Fixed top-document script. An application card needs its own literal delivery
// context and date, plus one explicit role heading. A date on a public job card
// is not enough. Never navigate to a job description or click business actions.
export function buildReviewRecordDetailsScript(expectedUrl: string, ownedOrigin: string, deadline: number): string {
  return `(() => {
    const pinned = () => location.href === ${JSON.stringify(expectedUrl)} && location.origin === ${JSON.stringify(ownedOrigin)}
      && Date.now() < ${deadline};
    if (!pinned()) return {outcome: 'blocked', expanded_count: 0};
    const visible = element => {
      const rect = element.getBoundingClientRect();
      if (!element.isConnected || rect.width <= 0 || rect.height <= 0 || rect.bottom <= 0 || rect.right <= 0
          || rect.top >= innerHeight || rect.left >= innerWidth || !element.getClientRects().length) return false;
      for (let owner = element; owner; owner = owner.parentElement) {
        const style = getComputedStyle(owner);
        if (owner.hidden || owner.inert || owner.getAttribute('aria-hidden') === 'true'
            || style.display === 'none' || style.visibility !== 'visible' || Number(style.opacity) === 0) return false;
      }
      return true;
    };
    const excluded = 'form,nav,aside,[role="navigation"],[role="menu"],[role="dialog"],[hidden],[inert],[aria-hidden="true"]';
    const titleSelector = 'h1,h2,h3,h4,h5,[data-job-title],[data-recruitops-job-title],[class*="job-title"],[class*="jobTitle"],[class*="position-title"],[class*="positionName"],[class~="title"]';
    // A navigation link labelled "我的投递" is not a records-page heading.
    const recordHeading = /^(?:历史投递记录|我的投递(?:记录)?|投递记录|投递历史|我的应聘(?:记录)?|应聘记录|应聘历史|我的申请(?:记录)?|申请记录|申请历史)(?:[（(]\\d+[）)]|\\d+条)?$/;
    const recordHeadings = Array.from(document.querySelectorAll('h1,h2,h3,header,[role="heading"],[class*="page-title"],[class*="pageTitle"],[class*="record-title"],[class~="title"]')).slice(0, 100);
    const hasRecordHeading = () => recordHeadings.some(heading => visible(heading) && !heading.closest(excluded)
        && !heading.querySelector('nav,[role="navigation"],[role="menu"]')
        && recordHeading.test(String(heading.innerText || '').replace(/\\s+/g, '').trim()));
    if (!hasRecordHeading())
      return {outcome: 'blocked', expanded_count: 0, reason: 'record_context_not_proven'};
    const labelOf = element => String(element.innerText || element.getAttribute('aria-label') || '').replace(/\\s+/g, '').trim();
    const isExpander = element => /^(?:展开详情|查看详情)$/.test(labelOf(element));
    const controls = Array.from(document.querySelectorAll('button,a,summary,[role="button"],[tabindex],[onclick]')).slice(0, 300);
    const date = /(?:19|20)\\d{2}[-/.年](?:0?[1-9]|1[0-2])[-/.月](?:0?[1-9]|[12]\\d|3[01])(?:日)?(?!\\d)/;
    const delivery = /投递(?:成功|日期|时间|记录|岗位)|申请(?:成功|日期|时间|记录)|应聘(?:日期|时间|记录)|已投递|已申请/;
    const datedSubmission = /(?:投递简历|提交简历)\\s*[:：]?\\s*(?:19|20)\\d{2}[-/.年](?:0?[1-9]|1[0-2])[-/.月](?:0?[1-9]|[12]\\d|3[01])(?:日)?(?!\\d)/;
    const isCard = element => /^(?:ARTICLE|LI|TR)$/.test(element.tagName)
      || element.hasAttribute('data-application-id') || element.hasAttribute('data-recruitops-application')
      || String(element.className || '').split(/\\s+/).some(name =>
        /^(?:(?:application|deliver|delivery|apply|record|job|position)[-_](?:card|item|row)|(?:application|deliver|delivery|apply|record|job|position)(?:Card|Item|Row)|(?:el-)?card)$/i.test(name));
    const findCard = control => {
      let card = control.parentElement;
      for (let depth = 0; card && card !== document.body && depth < 7; depth++, card = card.parentElement) {
        const text = String(card.innerText || '');
        if (text.length > 2500) break;
        if (!isCard(card)) continue;
        // An inner card cannot borrow its neighbour's date or delivery label
        // from the outer records list when its own evidence is incomplete.
        if (card.closest(excluded) || !date.test(text) || !delivery.test(text) && !datedSubmission.test(text)) return undefined;
        const titles = Array.from(card.querySelectorAll(titleSelector)).filter(title => {
          const text = String(title.innerText || '').trim().replace(/^投递岗位\\s*[:：]\\s*/, '');
          return visible(title) && !title.closest(excluded) && text.length >= 3 && text.length <= 150
            && /工程师|开发|研究员|设计师|产品经理|engineer|developer/i.test(text)
            && !/投递|撤回|更改|修改|提交|绑定|招聘职位/.test(text);
        });
        const independentTitles = titles.filter(title => !titles.some(child => child !== title && title.contains(child)));
        if (independentTitles.length !== 1) return undefined;
        return card;
      }
    };
    const candidates = [], blockedCards = new Set();
    let unsafe = false;
    const safeControl = control => {
      if (!isExpander(control) || !visible(control) || control.closest(excluded) || control.disabled
          || control.getAttribute('aria-disabled') === 'true' || control.getAttribute('aria-expanded') === 'true') return false;
      const href = String(control.getAttribute('href') || '').trim();
      return !(href && href !== '#' && !/^javascript:\\s*void\\s*\\(\\s*0\\s*\\)\\s*;?$/i.test(href)
          || control.hasAttribute('download') || ['_blank','_parent','_top'].includes(control.getAttribute('target') || '')
          || /location|window\\.open|submit|withdraw|cancel|delete|edit|changeJob/i.test(control.getAttribute('onclick') || ''));
    };
    const localPanel = (control, card) => {
      const controlsId = control.getAttribute('aria-controls');
      const panel = controlsId ? document.getElementById(controlsId) : undefined;
      const nativeDetails = control.tagName === 'SUMMARY' && control.parentElement?.tagName === 'DETAILS'
        && !control.parentElement.open && card.contains(control.parentElement);
      return !(controlsId && (!panel || !card.contains(panel)) || labelOf(control) === '查看详情' && !nativeDetails && !panel);
    };
    const uniqueControl = (control, card) => {
      const sameCard = Array.from(card.querySelectorAll('button,a,summary,[role="button"],[tabindex],[onclick]'))
        .filter(other => isExpander(other) && visible(other));
      return sameCard.length === 1 && sameCard[0] === control;
    };
    for (const control of controls) {
      if (!isExpander(control) || !visible(control)) continue;
      if (!safeControl(control)) {unsafe = true; continue;}
      const card = findCard(control);
      if (!card) continue;
      if (!localPanel(control, card)) {unsafe = true; continue;}
      if (!uniqueControl(control, card)) {blockedCards.add(card); unsafe = true; continue;}
      candidates.push({control, card});
    }
    let expanded = 0;
    const eligible = candidates.filter(candidate => !blockedCards.has(candidate.card));
    for (const {control, card} of eligible.slice(0, 3)) {
      if (!pinned() || !hasRecordHeading() || !safeControl(control) || !card.isConnected || findCard(control) !== card
          || !localPanel(control, card) || !uniqueControl(control, card))
        return {outcome: 'blocked', expanded_count: expanded};
      control.click();
      expanded++;
    }
    return expanded ? {outcome: eligible.length > 3 ? 'limit_reached' : 'expanded', expanded_count: expanded,
      ...(eligible.length > 3 ? {reason: 'candidate_limit'} : {})}
      : {outcome: 'not_found', expanded_count: 0,
        reason: unsafe ? 'unsafe_or_ambiguous_control' : 'card_boundary_not_proven'};
  })()`;
}

export async function expandReviewRecordDetails(wc: WebContents, ownedOrigin: string, deadline: number,
  signal?: AbortSignal): Promise<ReviewRecordDetailsFollowup> {
  if (signal?.aborted || wc.isDestroyed()) throw new Error('browser_cancelled');
  const identity = captureReviewIdentity(wc), expectedUrl = wc.getURL();
  const url = new URL(expectedUrl);
  if (!['http:', 'https:'].includes(url.protocol) || url.origin !== ownedOrigin || url.username || url.password)
    throw new Error('browser_navigation_changed');
  const remaining = deadline - Date.now();
  if (remaining <= 0) return {outcome: 'blocked', expanded_count: 0};
  let timer: NodeJS.Timeout | undefined, abort: (() => void) | undefined;
  try {
    const value = await Promise.race([
      wc.executeJavaScriptInIsolatedWorld(1004, [{code: buildReviewRecordDetailsScript(expectedUrl, ownedOrigin, Math.min(deadline, Date.now() + 1500))}]),
      new Promise<never>((_, reject) => {timer = setTimeout(() => reject(new Error('browser_observation_timeout')), Math.min(1500, remaining));}),
      signal ? new Promise<never>((_, reject) => {
        abort = () => reject(new Error('browser_cancelled'));
        signal.addEventListener('abort', abort, {once: true});
      }) : new Promise<never>(() => {})
    ]);
    if (signal?.aborted || wc.isDestroyed()) throw new Error('browser_cancelled');
    if (!matchesReviewIdentity(wc, identity)) throw new Error('browser_navigation_changed');
    if (!value || !['expanded', 'not_found', 'blocked', 'limit_reached'].includes(value.outcome)
        || !Number.isInteger(value.expanded_count) || value.expanded_count < 0 || value.expanded_count > 3)
      return {outcome: 'blocked', expanded_count: 0};
    return {outcome: value.outcome, expanded_count: value.expanded_count,
      ...(['record_context_not_proven', 'card_boundary_not_proven', 'unsafe_or_ambiguous_control', 'candidate_limit'].includes(value.reason)
        ? {reason: value.reason} : {})};
  } finally {
    clearTimeout(timer);
    if (abort) signal?.removeEventListener('abort', abort);
  }
}
