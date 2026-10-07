const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const vm = require('node:vm');
const {BrowserService} = require('../dist/browser-service');
const {buildReviewRecordEntryScript, followReviewRecordEntry, reviewRecordEntryReason} = require('../dist/review-record-entry');
const {isRedirectedHome} = require('../dist/review-readiness');
const {reviewObservationBinding} = require('../dist/review-observation-binding');

const ORIGIN = 'https://ats.example';
const HOME = ORIGIN + '/index.html#/';
const RECORDS = ORIGIN + '/account.html#/myDeliver';

function element(label, options = {}) {
  const attributes = {...options.attributes, ...(options.href === undefined ? {} : {href: options.href})};
  return {
    tagName: options.tag || 'A', innerText: label, isConnected: true,
    disabled: !!options.disabled, hidden: !!options.hidden, inert: !!options.inert,
    parentElement: options.parent || null, styleFixture: options.style || {},
    getAttribute: name => attributes[name] ?? null,
    hasAttribute: name => attributes[name] !== undefined,
    closest: name => name === 'form' && options.form ? {} : null,
    getBoundingClientRect: () => ({top: 10, bottom: 40, left: 10, right: 150, width: 140, height: 30, ...options.rect}),
    getClientRects: () => [{}], click: () => options.click?.()
  };
}

function sandbox(elements, url = HOME, changed = () => {}) {
  const location = {href: url, origin: ORIGIN, assign: next => {location.href = next; changed(next);}};
  return {location, document: {baseURI: url, querySelectorAll: () => elements}, URL,
    innerHeight: 640, innerWidth: 900,
    getComputedStyle: owner => ({display: 'block', visibility: 'visible', opacity: '1', ...owner.styleFixture})};
}

function execute(elements, url = HOME) {
  const navigations = [], context = sandbox(elements, url, value => navigations.push(value));
  return {result: vm.runInNewContext(buildReviewRecordEntryScript(url, ORIGIN), context), navigations};
}

function observed(url, text, records = []) {
  return {status: 'SUCCEEDED', result: {page: {page_url: url, text}, application_records: records,
    semantic_nodes: [], diagnostics: {readyState: 'complete', frameScope: 'top_only', recordCount: records.length,
      recordBlockCount: records.length, iframeCount: 0}}};
}

test('only an explicit entry screen or genuine home redirect triggers follow-up', () => {
  assert.equal(reviewRecordEntryReason(observed(HOME, '首页 登录 注册'), RECORDS), 'application_record_home_redirect');
  assert.equal(reviewRecordEntryReason(observed(ORIGIN + '/personalCenter', '点击按钮查看应聘记录 应聘记录')),
    'application_record_entry_not_entered');
  assert.equal(reviewRecordEntryReason(observed(RECORDS, '应聘记录 登录')), undefined);
  assert.equal(reviewRecordEntryReason(observed(RECORDS, '暂无申请记录')), undefined);
  assert.equal(reviewRecordEntryReason(observed(HOME, '点击按钮查看应聘记录', [{}]), RECORDS), undefined);
  for (const error_code of ['LOGIN_REQUIRED', 'CAPTCHA_REQUIRED', 'FRAME_SCOPE_DENIED', 'APPLICATION_PAGE_UNAVAILABLE'])
    assert.equal(reviewRecordEntryReason({...observed(HOME, '点击按钮查看应聘记录'), error_code}, RECORDS), undefined);
});

test('hash home redirects are distinct from ordinary records routes and query changes', () => {
  assert.equal(isRedirectedHome(observed(HOME, ''), ORIGIN + '/index.html#/myDeliver'), true);
  assert.equal(isRedirectedHome(observed(ORIGIN + '/index.html#/myDeliver', ''), HOME), false);
  assert.equal(isRedirectedHome(observed(ORIGIN + '/index.html#/jobs', ''), RECORDS), false);
  assert.equal(isRedirectedHome(observed(HOME + '?nonce=two', ''), HOME + '?nonce=one'), false);
});

test('follows a visible same-origin literal records href, not a guessed route', () => {
  const {result, navigations} = execute([element('我的投递', {href: '/account.html#/myDeliver'})]);
  assert.equal(result.outcome, 'followed');
  assert.equal(result.label, '我的投递');
  assert.deepEqual(navigations, [RECORDS]);
});

test('a unique readonly button is clickable outside forms while active navigation is ignored', () => {
  let clicks = 0;
  const {result, navigations} = execute([
    element('应聘记录', {href: '/personalCenter', attributes: {'aria-current': 'page'}}),
    element('应聘记录', {tag: 'BUTTON', click: () => clicks++})
  ], ORIGIN + '/personalCenter');
  assert.equal(result.outcome, 'followed');
  assert.equal(clicks, 1);
  assert.deepEqual(navigations, []);
});

test('delegated SPA records controls work, but arbitrary javascript hrefs cannot run', () => {
  for (const href of ['#', 'javascript:void(0);']) {
    let clicks = 0;
    const value = execute([element('应聘记录', {href, click: () => clicks++})]);
    assert.equal(value.result.outcome, 'followed');
    assert.equal(clicks, 1);
  }
  const value = execute([element('应聘记录', {href: 'javascript:withdrawApplication()'})]);
  assert.equal(value.result.outcome, 'not_found');
});

for (const label of ['立即投递', '投递简历', '撤回投递', '删除申请记录', '修改应聘记录', '绑定岗位', '登录', '注册']) {
  test('never clicks business mutation or authentication control: ' + label, () => {
    let clicks = 0;
    const value = execute([element(label, {tag: 'BUTTON', click: () => clicks++})]);
    assert.equal(value.result.outcome, 'not_found');
    assert.equal(clicks, 0);
    assert.deepEqual(value.navigations, []);
  });
}

for (const href of ['https://foreign.example/records', 'http://ats.example/records',
  'https://user:password@ats.example/records', '/records?cmd=delete', '/account#/withdraw']) {
  test('rejects a foreign, credential, downgrade or mutating records destination: ' + href, () => {
    const value = execute([element('应聘记录', {href})]);
    assert.equal(value.result.outcome, 'not_found');
    assert.deepEqual(value.navigations, []);
  });
}

for (const options of [{hidden: true}, {inert: true}, {disabled: true}, {form: true},
  {style: {opacity: '0'}}, {rect: {top: 900, bottom: 930}}, {attributes: {'aria-disabled': 'true'}}]) {
  test('hidden, disabled, offscreen or form-contained entry cannot be clicked: ' + JSON.stringify(options), () => {
    const value = execute([element('应聘记录', {href: '/records', ...options})]);
    assert.equal(value.result.outcome, 'not_found');
    assert.deepEqual(value.navigations, []);
  });
}

test('identical links deduplicate, but distinct destinations never pick an arbitrary candidate', () => {
  assert.equal(execute([element('应聘记录', {href: '/records'}), element('我的投递', {href: '/records'})]).result.outcome, 'followed');
  const ambiguous = execute([element('应聘记录', {href: '/records/a'}), element('我的投递', {href: '/records/b'})]);
  assert.equal(ambiguous.result.outcome, 'ambiguous');
  assert.deepEqual(ambiguous.navigations, []);
});

test('direct records destination has priority over a visible personal center', () => {
  const value = execute([element('个人中心', {href: '/center'}), element('应聘记录', {href: '/records'})]);
  assert.deepEqual(value.navigations, [ORIGIN + '/records']);
});

function contents(url, pages) {
  let current = url;
  const wc = Object.assign(new EventEmitter(), {id: Math.random(), isDestroyed: () => false,
    isLoadingMainFrame: () => false, getURL: () => current});
  wc.navigate = next => {current = next; wc.emit('did-start-navigation', {}, next, false, true);};
  wc.executeJavaScriptInIsolatedWorld = async (_world, scripts) => {
    if (scripts[0].code === 'observation') return pages(current).observation;
    return vm.runInNewContext(scripts[0].code, sandbox(pages(current).elements || [], current, wc.navigate));
  };
  return wc;
}
const adapter = {buildObservationScript: () => 'observation', normalizeObservation: value => value};
const fast = {reviewPollIntervalMs: 1, unparsedWindowMs: 0, unparsedStableMs: 0};

test('home-entry navigation recollects records and binds the final owned document', async () => {
  const wc = contents(HOME, url => url === HOME ? {observation: observed(url, '首页 登录 注册'),
    elements: [element('我的投递', {href: '/account.html#/myDeliver'})]} :
    {observation: observed(url, '我的投递 当前状态: 已投递', [{title: 'Software engineer'}])});
  const result = await new BrowserService(adapter, fast).observeForReview(wc, 'home-entry', ['app-1'],
    {deadline: Date.now() + 1000, requestedUrl: RECORDS});
  assert.equal(result.review_readiness, 'records');
  assert.equal(reviewObservationBinding(result).url, RECORDS);
  assert.deepEqual(result.result.record_entry_followup, {attempt_count: 1, outcome: 'entered', label: '我的投递'});
  assert.equal(result.result.navigation_diagnostics.reason, 'application_record_entry_followed');
});

test('entry button with no resulting records stops without repeated clicks', async () => {
  let clicks = 0;
  const url = ORIGIN + '/personalCenter';
  const wc = contents(url, () => ({observation: observed(url, '点击按钮查看应聘记录 应聘记录'),
    elements: [element('应聘记录', {tag: 'BUTTON', click: () => clicks++})]}));
  const result = await new BrowserService(adapter, fast).observeForReview(wc, 'entry-no-effect', ['app-1'],
    {deadline: Date.now() + 1000, requestedUrl: url});
  assert.equal(clicks, 1);
  assert.equal(result.error_code, 'APPLICATION_RECORD_ENTRY_NOT_ENTERED');
  assert.equal(result.review_readiness, 'record_entry_required');
  assert.equal(result.result.database_updated, false);
});

test('personal-center then records navigation is bounded to two visible owned entries', async () => {
  const center = ORIGIN + '/personalCenter';
  let clicks = 0, changed = false;
  const wc = contents(HOME, url => url === HOME ? {observation: observed(HOME, '首页'),
    elements: [element('个人中心', {href: '/personalCenter'})]} :
    {observation: observed(center, '点击按钮查看应聘记录 应聘记录 ' + (changed ? '待加载' : '')),
      elements: [element('应聘记录', {tag: 'BUTTON', click: () => {clicks++; changed = !changed;}})]});
  const result = await new BrowserService(adapter, fast).observeForReview(wc, 'entry-cap', ['app-1'],
    {deadline: Date.now() + 1000, requestedUrl: RECORDS});
  assert.equal(result.error_code, 'APPLICATION_RECORD_ENTRY_NOT_ENTERED');
  assert.equal(result.result.record_entry_followup.attempt_count, 2);
  assert.equal(clicks, 1);
});

test('a home with only login links reports home return, not a fabricated login wall', async () => {
  const wc = contents(HOME, () => ({observation: observed(HOME, '首页 登录 注册'),
    elements: [element('登录', {href: '/login'})]}));
  const result = await new BrowserService(adapter, fast).observeForReview(wc, 'home-no-entry', ['app-1'],
    {deadline: Date.now() + 500, requestedUrl: RECORDS});
  assert.equal(result.error_code, 'APPLICATION_RECORD_HOME_REDIRECT');
  assert.equal(result.review_readiness, 'record_entry_required');
  assert.equal(wc.getURL(), HOME);
  assert.equal(result.result.requires_user_action, undefined);
});

test('after entry navigation, unparsed real records content can still take ordinary screenshot fallback', async () => {
  const target = ORIGIN + '/records';
  const wc = contents(HOME, url => url === HOME ? {observation: observed(url, '首页'),
    elements: [element('应聘记录', {href: '/records'})]} : {observation: observed(target, 'Software engineer 投递简历 2026-09-30')});
  const result = await new BrowserService(adapter, fast).observeForReview(wc, 'entry-unparsed', ['app-1'],
    {deadline: Date.now() + 1000, requestedUrl: RECORDS});
  assert.equal(result.review_readiness, 'unparsed_page');
  assert.equal(result.status, 'SUCCEEDED');
  assert.equal(result.error_code, undefined);
  assert.equal(result.result.extraction_reason, 'unparsed_page');
});

test('record-entry script honors cancellation and the remaining total deadline', async () => {
  let calls = 0;
  const wc = {getURL: () => HOME, isDestroyed: () => false,
    executeJavaScriptInIsolatedWorld: () => {calls++; return new Promise(() => {});}};
  const controller = new AbortController(); controller.abort();
  await assert.rejects(followReviewRecordEntry(wc, ORIGIN, Date.now() + 500, controller.signal), /browser_cancelled/);
  assert.equal(calls, 0);
  const start = Date.now();
  await assert.rejects(followReviewRecordEntry(wc, ORIGIN, Date.now() + 30), /browser_observation_timeout/);
  assert.ok(Date.now() - start < 250);
  assert.equal(calls, 1);
});
