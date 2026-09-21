const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { BrowserService } = require('../dist/browser-service');
const { classifyReviewReadiness, isRedirectedLogin, reviewDiagnosticSummary, ReviewObservationError } = require('../dist/review-readiness');
const { normalizeFrameObservations } = require('../../../packages/desktop_browser/index.cjs');

function contents(evaluate) {
  return Object.assign(new EventEmitter(), { id: Math.random(), isDestroyed: () => false, isLoadingMainFrame: () => false,
    getURL: () => 'https://ats.example/applications', executeJavaScriptInIsolatedWorld: evaluate });
}

const shell = { type: 'result', status: 'SUCCEEDED', result: { page: { text: 'Welcome to applications' }, application_records: [],
  semantic_nodes: [], diagnostics: { recordBlockCount: 0, recordCount: 0, iframeCount: 0, frameScope: 'top_only' } } };
const record = { type: 'result', status: 'SUCCEEDED', result: { application_records: [{ application_id: 'app-1', status: 'reviewing' }] } };
const empty = { type: 'result', status: 'SUCCEEDED', result: { application_records: [],
  semantic_nodes: [{ role: 'status', text: '暂无申请记录', classTokens: [] }],
  diagnostics: { recordBlockCount: 0, recordCount: 0, iframeCount: 0, frameScope: 'top_only' } } };
const adapter = { buildObservationScript: () => 'fixed-observation', normalizeObservation: raw => raw };

test('readiness requires records, login, or a credible empty-state marker', () => {
  assert.equal(classifyReviewReadiness(shell), 'pending');
  assert.equal(classifyReviewReadiness(record), 'records');
  assert.equal(classifyReviewReadiness(empty), 'confirmed_empty');
  assert.equal(classifyReviewReadiness({ ...empty, result: { ...empty.result,
    semantic_nodes: [{ role: 'status', text: 'No application records', classTokens: [] }] } }), 'confirmed_empty');
  assert.equal(classifyReviewReadiness({ ...empty, result: { ...empty.result,
    diagnostics: { ...empty.result.diagnostics, iframeCount: 1 } } }), 'pending');
  assert.equal(classifyReviewReadiness({ status: 'STATE_UNCLEAR', error_code: 'LOGIN_REQUIRED', result: {} }), 'login_required');
  assert.equal(classifyReviewReadiness({ status: 'STATE_UNCLEAR', error_code: 'FRAME_EVIDENCE_UNAVAILABLE', result: {} }), 'pending');
});

test('rechecks delayed DOM until a structured application record appears', async () => {
  let calls = 0;
  const service = new BrowserService(adapter, { reviewPollIntervalMs: 4, observationTimeoutMs: 30 });
  const wc = contents(async () => { calls++; return calls < 3 ? shell : record; });
  const stages = [];
  const outcome = await service.observeForReview(wc, 'delayed-dom', ['app-1'], {
    deadline: Date.now() + 250, onStage: stage => stages.push(stage)
  });
  assert.equal(outcome.review_readiness, 'records');
  assert.ok(calls >= 3);
  assert.ok(stages.includes('WAITING_FOR_CONTENT'));
  assert.ok(stages.includes('VALIDATING'));
});

test('arbitrary nonempty shell text times out instead of becoming success', async () => {
  const service = new BrowserService(adapter, { reviewPollIntervalMs: 4, observationTimeoutMs: 30 });
  const wc = contents(async () => shell);
  await assert.rejects(service.observeForReview(wc, 'shell-only', ['app-1'], { deadline: Date.now() + 35 }), /browser_readiness_timeout/);
});

test('a timed-out isolated script is not started again', async () => {
  let calls = 0;
  const service = new BrowserService(adapter, { reviewPollIntervalMs: 4, observationTimeoutMs: 12 });
  const wc = contents(() => { calls++; return new Promise(() => {}); });
  await assert.rejects(service.observeForReview(wc, 'slow-script', ['app-1'], { deadline: Date.now() + 100 }), /browser_observation_timeout/);
  assert.equal(calls, 1);
});

test('credible empty state and login are terminal readiness results', async () => {
  const service = new BrowserService(adapter, { reviewPollIntervalMs: 4, observationTimeoutMs: 30, emptyConfirmationMs: 15 });
  const emptyResult = await service.observeForReview(contents(async () => empty), 'empty-state', ['app-1'], { deadline: Date.now() + 100 });
  assert.equal(emptyResult.review_readiness, 'confirmed_empty');
  const login = { type: 'result', status: 'STATE_UNCLEAR', error_code: 'LOGIN_REQUIRED', result: { requires_user_action: true } };
  const loginResult = await service.observeForReview(contents(async () => login), 'login-state', ['app-1'], { deadline: Date.now() + 100 });
  assert.equal(loginResult.review_readiness, 'login_required');
});

test('helper iframes do not end readiness polling before delayed records appear', async () => {
  let calls = 0;
  const framed = { ...shell, status: 'STATE_UNCLEAR', error_code: 'FRAME_EVIDENCE_UNAVAILABLE',
    result: { ...shell.result, diagnostics: { ...shell.result.diagnostics, iframeCount: 2 } } };
  const service = new BrowserService(adapter, { reviewPollIntervalMs: 4 });
  const output = await service.observeForReview(contents(async () => ++calls < 4 ? framed : record),
    'delayed-framed-dom', ['app-1'], { deadline: Date.now() + 250 });
  assert.equal(output.review_readiness, 'records');
  assert.equal(calls, 4);
  await assert.rejects(service.observeForReview(contents(async () => framed), 'frames-not-proof', ['app-1'],
    { deadline: Date.now() + 45 }), /browser_readiness_timeout/);
});

test('temporary empty placeholder waits for the list instead of declaring no applications', async () => {
  let calls = 0;
  const service = new BrowserService(adapter, { reviewPollIntervalMs: 4, emptyConfirmationMs: 30 });
  const output = await service.observeForReview(contents(async () => ++calls < 3 ? empty : record),
    'placeholder-empty', ['app-1'], { deadline: Date.now() + 200 });
  assert.equal(output.review_readiness, 'records');
  assert.equal(calls, 3);
});

test('a redirected home needs visible login evidence and no records to mean login required', async () => {
  const home = { ...shell, result: { ...shell.result, page: { page_url: 'https://ats.example/pb/index.html' },
    diagnostics: { ...shell.result.diagnostics, loginPromptVisible: true } } };
  assert.equal(isRedirectedLogin(home, 'https://ats.example/pb/account.html'), true);
  assert.equal(isRedirectedLogin(home, 'https://ats.example/pb/index.html'), false);
  assert.equal(isRedirectedLogin(home, 'https://foreign.example/account.html'), false);
  assert.equal(isRedirectedLogin({ ...home, result: { ...home.result, application_records: [{}] } },
    'https://ats.example/pb/account.html'), false);
  assert.equal(isRedirectedLogin({ ...home, result: { ...home.result, diagnostics: {} } },
    'https://ats.example/pb/account.html'), false);
  const service = new BrowserService(adapter);
  const output = await service.observeForReview(contents(async () => home), 'home-login', ['app-1'],
    { deadline: Date.now() + 100, requestedUrl: 'https://ats.example/pb/account.html' });
  assert.equal(output.error_code, 'LOGIN_REQUIRED');
  assert.equal(output.review_readiness, 'login_required');
  assert.equal(output.result.pause.reason, 'login_required');
});

test('cancellation interrupts an in-flight isolated observation and removes listeners', async () => {
  const service = new BrowserService(adapter);
  const wc = contents(() => new Promise(() => {}));
  const controller = new AbortController();
  const pending = service.observeForReview(wc, 'cancel-observation', ['app-1'], {
    deadline: Date.now() + 1000, signal: controller.signal
  });
  setTimeout(() => controller.abort(), 5);
  await assert.rejects(pending, /browser_cancelled/);
  assert.equal(wc.listenerCount('did-start-navigation'), 0);
  assert.equal(wc.listenerCount('render-process-gone'), 0);
});

test('readiness timeout keeps only the last bounded diagnostic summary', async () => {
  const service = new BrowserService(adapter, {reviewPollIntervalMs: 2});
  const observation = {...shell, result: {...shell.result, page: {text:'do-not-persist@example.test'},
    diagnostics: {...shell.result.diagnostics, iframeCount:2, frameCount:3, skippedFrameCount:1}}};
  await assert.rejects(service.observeForReview(contents(async()=>observation),'diagnostic-timeout',['app-1'],
    {deadline:Date.now()+35}), error=>{
    assert.ok(error instanceof ReviewObservationError);
    assert.equal(error.message,'browser_readiness_timeout');
    assert.equal(error.lastObservation.pageState,'frame_scope_denied');
    assert.equal(error.lastObservation.iframeCount,2);
    assert.equal(error.lastObservation.skippedFrameCount,1);
    assert.ok(!JSON.stringify(error.lastObservation).includes('do-not-persist'));
    return true;
  });
  assert.equal(reviewDiagnosticSummary({...shell,result:{...shell.result,page:{text:''}}}).pageState,'blank');
  assert.equal(reviewDiagnosticSummary(shell).pageState,'shell');
});

const frameContext={operation_id:'frame-review',page_url:'https://ats.example/applications',application_ids:['app-1']};
function frameRaw(url, records=[], pause) {
  if(pause)return {protocolVersion:3,type:'extension.pause_state',requestId:'frame-review',ok:false,pause:{reason:pause}};
  const page=new URL(url);page.search='';
  return {protocolVersion:3,type:'extension.controlled_action_result',requestId:'frame-review',ok:true,data:{
    action:'observe_application_page',selectorKey:'application_page',
    page:{page_url:page.href,origin:page.origin,path:page.pathname,text:records.length?'Application evidence':'',title:'Fixture'},
    applicationRecords:records,semanticNodes:[],entries:[],capturedAt:'2026-09-20T00:00:00Z',
    diagnostics:{frameScope:'single_frame',iframeCount:0,recordCount:records.length,recordBlockCount:records.length,visibleTextLength:0}
  }};
}
const frameSample=(frameId,url,records,pause)=>({frameId,frameUrl:url,raw:frameRaw(url,records,pause)});

test('frame aggregation preserves top binding, host provenance and record-over-helper priority',()=>{
  const root=frameSample(0,frameContext.page_url);
  const childUrl='https://ats.example/records?token=secret';
  const combined=normalizeFrameObservations([root,
    frameSample(7,'https://ats.example/helper',[],'login_required'),
    frameSample(9,childUrl,[{title:'Platform Engineer',status:'written',label:'Written test',evidence:'Written test'}])],frameContext);
  assert.equal(combined.status,'SUCCEEDED');
  assert.equal(combined.result.page.page_url,frameContext.page_url);
  assert.deepEqual(combined.result.application_ids,['app-1']);
  assert.equal(combined.result.application_records[0].frameId,9);
  assert.equal(combined.result.application_records[0].frameUrl,'https://ats.example/records');
  assert.ok(!JSON.stringify(combined).includes('secret'));
  assert.equal(combined.result.database_updated,false);
  for(const pause of ['login_required','captcha_required','state_unclear']) {
    const blocked=normalizeFrameObservations([frameSample(0,frameContext.page_url,[],pause),
      frameSample(9,childUrl,[{title:'Platform Engineer'}])],frameContext);
    assert.equal(blocked.error_code,pause.toUpperCase());
    assert.equal(blocked.result.application_records,undefined);
  }
  const childAuth=normalizeFrameObservations([root,
    frameSample(7,'https://ats.example/login',[],'login_required'),
    frameSample(8,'https://ats.example/challenge',[],'captcha_required')],frameContext);
  assert.equal(childAuth.error_code,'CAPTCHA_REQUIRED');
});

test('frame aggregation rejects foreign scopes, forged source fields and duplicate identities',()=>{
  const root=frameSample(0,frameContext.page_url);
  assert.throws(()=>normalizeFrameObservations([root,frameSample(1,'https://foreign.example/records')],frameContext),/origin scope/);
  assert.throws(()=>normalizeFrameObservations([root,root],frameContext),/frame identity/);
  assert.throws(()=>normalizeFrameObservations([frameSample(1,frameContext.page_url)],frameContext),/top frame/);
  const forged=frameSample(2,'https://ats.example/records',[{title:'Engineer',frameUrl:frameContext.page_url}]);
  assert.throws(()=>normalizeFrameObservations([root,forged],frameContext),/evidence fields/);
  const wrongPage=frameSample(2,'https://ats.example/records');wrongPage.raw.data.page.page_url=frameContext.page_url;
  assert.throws(()=>normalizeFrameObservations([root,wrongPage],frameContext),/trusted current URL/);
});

test('readiness diagnostics distinguish readable child shell from a blank top document',()=>{
  const root=frameSample(0,frameContext.page_url);
  const child=frameSample(2,'https://ats.example/records');
  child.raw.data.page.text='Loading private account details';
  child.raw.data.diagnostics.visibleTextLength=child.raw.data.page.text.length;
  const observation=normalizeFrameObservations([root,child],frameContext);
  const summary=reviewDiagnosticSummary(observation);
  assert.equal(classifyReviewReadiness(observation),'pending');
  assert.equal(summary.pageState,'shell');
  assert.equal(summary.visibleTextLength,child.raw.data.page.text.length);
  assert.ok(!JSON.stringify(summary).includes('private'));
});
