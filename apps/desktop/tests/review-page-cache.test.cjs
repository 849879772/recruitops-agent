const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {ReviewPageCache} = require('../dist/review-page-cache');
const {ReviewNavigationPolicy} = require('../dist/review-readiness');
const {bindReviewObservation, captureReviewIdentity} = require('../dist/review-observation-binding');
const {BrowserService, reviewStabilityProof} = require('../dist/browser-service');
const url = 'https://ats.example.test/applications#/records';
let sequence = 0;
function fixture(cache = new ReviewPageCache(), session = {}) {
  let destroyed = false, loading = false, currentUrl = url, closed = 0;
  const wc = Object.assign(new EventEmitter(), {id: ++sequence, session, mainFrame: {frameToken: `frame-${sequence}`},
    getURL: () => currentUrl, setURL: value => currentUrl = value, setLoading: value => loading = value,
    isDestroyed: () => destroyed, isLoadingMainFrame: () => loading});
  const lease = {id: wc.id, purpose: 'review', close: () => {closed++; if (!destroyed) {destroyed = true; wc.emit('destroyed');}}};
  const policy = new ReviewNavigationPolicy(url);
  const scope = {url, applicationIds: ['app-1','app-2'], reviewTaskId: 'run-1', session, profileEpoch: cache.profileEpoch(session)};
  const observation = bindReviewObservation({type: 'result', operation_id: 'source-1', status: 'SUCCEEDED', review_readiness: 'records',
    result: {page_url: url, page: {page_url: url, title: '投递记录', text: '软件工程师 当前状态：已投递'},
      application_records: [{title: '软件工程师', status: 'applied'}], diagnostics: {readyState: 'complete'}}}, captureReviewIdentity(wc));
  return {cache, wc, lease, policy, scope, observation, closed: () => closed,
    retain: (id = 'source-1', value = observation, hidden = true) => cache.retain(id, lease, wc, policy, value, scope, hidden)};
}
test('hidden-page reuse is one-shot, same-session/run/URL and permits only an application-ID subset', () => {
  const f = fixture();
  assert.equal(f.retain(), true);
  const taken = f.cache.take('source-1', {...f.scope, applicationIds: ['app-2']});
  assert.equal(taken.wc, f.wc); assert.equal(taken.policy, f.policy);
  assert.equal(f.cache.size, 0); assert.equal(f.closed(), 0);
  assert.equal(f.cache.take('source-1', f.scope), undefined);
  taken.lease.close(); assert.equal(f.closed(), 1);
});
for (const [name, change] of [
  ['different run', scope => ({...scope, reviewTaskId: 'run-2'})],
  ['different session', scope => ({...scope, session: {}})],
  ['different query/account', scope => ({...scope, url: url.replace('#', '?tenant=other#')})],
  ['different route', scope => ({...scope, url: url.replace('records', 'profile')})],
  ['different application', scope => ({...scope, applicationIds: ['other-app']})],
  ['empty identity', scope => ({...scope, applicationIds: []})],
  ['different profile epoch', scope => ({...scope, profileEpoch: 1})],
]) test(`${name} invalidates the cached renderer and requires a cold read`, () => {
  const f = fixture(); assert.equal(f.retain(), true);
  assert.equal(f.cache.take('source-1', change(f.scope)), undefined);
  assert.equal(f.closed(), 1); assert.equal(f.cache.size, 0);
});
test('cached pages reject a busy or loading renderer and an unannounced changed frame token', () => {
  for (const kind of ['busy','loading','frame']) {
    const f = fixture(); assert.equal(f.retain(), true);
    if (kind === 'loading') f.wc.setLoading(true);
    if (kind === 'frame') f.wc.mainFrame.frameToken = 'replacement-document';
    assert.equal(f.cache.take('source-1', f.scope, () => kind !== 'busy'), undefined, kind);
    assert.equal(f.closed(), 1);
  }
});
test('same-URL reload, in-page navigation, cancellation, renderer failure and destruction release the lease', () => {
  for (const event of ['did-start-navigation','did-navigate-in-page','will-navigate','will-redirect','render-process-gone','destroyed','cancel']) {
    const f = fixture(); assert.equal(f.retain(), true);
    if (event === 'cancel') f.cache.invalidate('source-1'); else f.wc.emit(event, {}, url, false, true);
    assert.equal(f.cache.size, 0, event); assert.equal(f.closed(), 1, event);
    assert.equal(f.cache.take('source-1', f.scope), undefined);
  }
});
test('TTL is at most thirty seconds and capacity evicts rather than growing hidden tabs', async () => {
  let now = 0;
  const cache = new ReviewPageCache(60000, () => now);
  const pages = Array.from({length: 7}, () => fixture(cache));
  pages.forEach((f, i) => assert.equal(f.retain(`source-${i}`), true));
  assert.equal(cache.size, 6); assert.equal(pages[0].closed(), 1);
  now = 30000;
  assert.equal(cache.take('source-6', pages[6].scope), undefined);
  assert.equal(pages[6].closed(), 1);
  cache.invalidate(); assert.equal(cache.size, 0);
  const timed = fixture(new ReviewPageCache(15)); assert.equal(timed.retain(), true);
  await new Promise(resolve => setTimeout(resolve, 30)); assert.equal(timed.closed(), 1);
});
test('login changes invalidate the profile; harmless analytics and flush-equivalent no-events do not', () => {
  const f = fixture(); assert.equal(f.retain(), true);
  f.cache.cookieChanged(f.scope.session, '_ga'); assert.equal(f.cache.size, 1);
  f.cache.cookieChanged(f.scope.session, 'session_id'); assert.equal(f.cache.size, 0); assert.equal(f.closed(), 1);
  assert.equal(f.retain(), false, 'a cookie change during the source read also prevents retaining its old epoch');
  const g = fixture(); assert.equal(g.retain(), true);
  g.cache.cookieChanged(g.scope.session, '_ga', true); assert.equal(g.cache.size, 0, 'HTTP-only cookies are never excluded');
  const h = fixture(); assert.equal(h.retain(), true); h.cache.profileChanged(h.scope.session); assert.equal(h.closed(), 1);
});
test('after take, only related account cookies or manual profile changes abort the active reuse lease', () => {
  for (const kind of ['related','sso','manual','other-company','analytics','other-profile']) {
    const f = fixture(); assert.equal(f.retain(), true); const taken = f.cache.take('source-1', f.scope);
    const policy = kind === 'sso' ? new ReviewNavigationPolicy('https://career.huawei.com/applications') : taken.policy;
    const guard = f.cache.watchReusedProfile(f.scope.session, f.scope.url, policy);
    guard.signal.addEventListener('abort', () => taken.lease.close());
    if (kind === 'manual') f.cache.profileChanged(f.scope.session);
    else if (kind === 'other-profile') f.cache.cookieChanged({}, 'session_id', true, 'ats.example.test');
    else f.cache.cookieChanged(f.scope.session, kind === 'analytics' ? '_ga' : 'session_id', kind !== 'analytics',
      kind === 'sso' ? '.huawei.com' : kind === 'other-company' ? 'other.example.test' : 'ats.example.test');
    const invalid = ['related','sso','manual'].includes(kind);
    assert.equal(guard.signal.aborted, invalid, kind);
    if (invalid) {assert.throws(guard.check, /browser_account_changed/); assert.equal(f.closed(), 1);}
    else {guard.check(); taken.lease.close();}
    guard.dispose();
  }
});
test('related cookie changes during fresh DOM re-read discard the in-flight old-account evidence', async () => {
  const f = fixture(); assert.equal(f.retain(), true); const taken = f.cache.take('source-1', f.scope);
  const guard = f.cache.watchReusedProfile(f.scope.session, f.scope.url, taken.policy);
  guard.signal.addEventListener('abort', () => taken.lease.close());
  f.wc.executeJavaScriptInIsolatedWorld = () => new Promise(() => {});
  const service = new BrowserService({buildObservationScript: () => 'fixed-code', normalizeObservation: raw => raw});
  const pending = service.observeForReview(f.wc, 'fresh-operation', ['app-1'], {deadline: Date.now()+1000, signal: guard.signal});
  f.cache.cookieChanged(f.scope.session, 'account_id', true, 'ats.example.test');
  await assert.rejects(pending, /browser_cancelled/); assert.equal(f.closed(), 1);
  assert.throws(guard.check, /browser_account_changed/); guard.dispose();
});
test('unrelated foreground navigation does not interrupt active reuse; same-host navigation does', () => {
  const f = fixture(); assert.equal(f.retain(), true); const taken = f.cache.take('source-1', f.scope);
  const guard = f.cache.watchReusedProfile(f.scope.session, f.scope.url, taken.policy);
  f.cache.foregroundNavigated(f.scope.session, 'https://other.example.test/jobs'); guard.check();
  f.cache.foregroundNavigated(f.scope.session, 'https://ats.example.test/login');
  assert.throws(guard.check, /browser_account_changed/); guard.dispose(); taken.lease.close();
});
test('user/foreground pages, auth gates, failed/unreadable or unbound observations are never retained', () => {
  for (const kind of ['foreground','login','captcha','failed','unknown','loading','denied','unavailable','unbound','missing-run']) {
    const f = fixture();
    if (kind === 'foreground') {assert.equal(f.retain('source-1', f.observation, false), false); continue;}
    if (kind === 'missing-run') f.scope.reviewTaskId = '';
    else if (kind === 'unbound') f.observation = structuredClone(f.observation);
    else if (kind === 'login') f.observation.result.page.text = '请先登录';
    else if (kind === 'captcha') f.observation.error_code = 'CAPTCHA_REQUIRED';
    else if (kind === 'failed') f.observation.status = 'FAILED';
    else if (kind === 'unknown') f.observation.review_readiness = 'pending';
    else if (kind === 'loading') f.observation.result.diagnostics.readyState = 'loading';
    else if (kind === 'denied') Object.assign(f.observation.result.diagnostics, {skippedFrameCount: 1, scopeDeniedFrameCount: 1});
    else if (kind === 'unavailable') f.observation.result.diagnostics.unavailableFrameCount = 1;
    assert.equal(f.cache.retain('source-1', f.lease, f.wc, f.policy, f.observation, f.scope, true), false, kind);
    assert.equal(f.cache.size, 0);
  }
});

function readableFixture() {
  const f = fixture(); let text = '投递记录 软件工程师 投递简历 2026-10-06', calls = 0;
  f.wc.executeJavaScriptInIsolatedWorld = async () => {calls++; return text;};
  const service = new BrowserService({buildObservationScript: () => 'fixed-program', normalizeObservation: (raw, context) => ({
    type: 'result', operation_id: context.operation_id, status: 'SUCCEEDED', result: {page_url: context.page_url,
      application_ids: context.application_ids, page: {page_url: context.page_url, title: '投递记录', text: raw},
      application_records: [], semantic_nodes: [], diagnostics: {readyState: 'complete'}}})},
    {reviewPollIntervalMs: 2, unparsedWindowMs: 45, unparsedStableMs: 12});
  const read = (id, proof, deadlineMs = 500) => service.observeForReview(f.wc, id, ['app-1'], {
    deadline: Date.now()+deadlineMs, requestedUrl: url, stableReadableProof: proof});
  return {...f, read, setText: value => text = value, calls: () => calls};
}
test('stable-page proof avoids duplicate waiting but obtains a new DOM observation and operation ID', async () => {
  const f = readableFixture(); const source = await f.read('source-operation');
  assert.equal(source.review_readiness, 'unparsed_page'); assert.ok(source.result.last_observation.sampling.count >= 5);
  const before = f.calls(); const fresh = await f.read('vision-operation', reviewStabilityProof(source));
  assert.equal(f.calls()-before, 1); assert.equal(fresh.operation_id, 'vision-operation');
  assert.equal(fresh.result.page.text, source.result.page.text); assert.equal(fresh.result.last_observation.sampling.count, 1);
});
test('changed text waits again, forged proof cannot skip readiness, and same-URL reload invalidates proof', async () => {
  for (const kind of ['changed','forged','reload']) {
    const f = readableFixture(); const source = await f.read('source-operation'); let proof = reviewStabilityProof(source);
    if (kind === 'changed') f.setText('投递记录 软件工程师 当前进度：分配简历');
    if (kind === 'forged') proof = {...proof};
    if (kind === 'reload') f.wc.emit('did-start-navigation', {}, url, false, true);
    const before = f.calls(); const fresh = await f.read('fresh-operation', proof);
    assert.ok(f.calls()-before >= 5, kind); assert.ok(fresh.result.last_observation.sampling.elapsedMs >= 45, kind);
  }
});
