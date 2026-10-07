const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { BrowserService } = require('../dist/browser-service');
const { classifyReviewReadiness, isRedirectedHome, isRedirectedLogin, reviewDiagnosticSummary, ReviewObservationError } = require('../dist/review-readiness');
const { normalizeFrameObservations } = require('../../../packages/desktop_browser/index.cjs');
const {reviewObservationBinding}=require('../dist/review-observation-binding');

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

test('total readiness deadline truncation preserves the prior pending evidence, not a script fault', async () => {
  let calls=0;
  const service=new BrowserService(adapter,{reviewPollIntervalMs:1,observationTimeoutMs:1000});
  const wc=contents(()=>++calls===1?Promise.resolve(shell):new Promise(()=>{}));
  await assert.rejects(service.observeForReview(wc,'clipped-sample',['app-1'],{deadline:Date.now()+60}),error=>{
    assert.equal(error.message,'browser_readiness_timeout');
    assert.equal(error.lastObservation.pageState,'unrecognized_content');
    assert.equal(error.lastObservation.sampling.count,1);
    return true;
  });
  assert.equal(calls,2,'the truncated final sample must not be restarted');
});

test('independent script timeout after pending evidence remains an observation failure', async () => {
  let calls=0;
  const service=new BrowserService(adapter,{reviewPollIntervalMs:1,observationTimeoutMs:12});
  const wc=contents(()=>++calls===1?Promise.resolve(shell):new Promise(()=>{}));
  await assert.rejects(service.observeForReview(wc,'stalled-sample',['app-1'],{deadline:Date.now()+200}),error=>{
    assert.equal(error.message,'browser_observation_timeout');
    assert.equal(error.lastObservation.pageState,'unrecognized_content');
    assert.equal(error.lastObservation.sampling.count,1);
    return true;
  });
  assert.equal(calls,2);
});

test('total deadline without a valid pending sample cannot claim a readiness diagnosis', async () => {
  for(const invalidFirst of [false,true]){
    let calls=0;
    const service=new BrowserService(adapter,{reviewPollIntervalMs:1,observationTimeoutMs:1000});
    const wc=contents(()=>{calls++;return invalidFirst&&calls===1?Promise.resolve(undefined):new Promise(()=>{});});
    await assert.rejects(service.observeForReview(wc,'no-readable-sample',['app-1'],{deadline:Date.now()+45}),error=>{
      assert.equal(error.message,'browser_observation_timeout');
      assert.equal(error.lastObservation.pageState,'not_observed');
      return true;
    });
    assert.equal(calls,invalidFirst?2:1);
  }
});

test('deadline truncation retains the last readable pending summary after an invalid sample', async () => {
  let calls=0;
  const service=new BrowserService(adapter,{reviewPollIntervalMs:1,observationTimeoutMs:1000});
  const wc=contents(()=>{calls++;return calls===1?Promise.resolve(shell):calls===2?Promise.resolve(undefined):new Promise(()=>{});});
  await assert.rejects(service.observeForReview(wc,'pending-then-invalid',['app-1'],{deadline:Date.now()+60}),error=>{
    assert.equal(error.message,'browser_readiness_timeout');
    assert.equal(error.lastObservation.pageState,'unrecognized_content');
    assert.equal(error.lastObservation.sampling.count,1);
    assert.equal(error.lastObservation.page.textSnippet,reviewDiagnosticSummary(shell).page.textSnippet);
    return true;
  });
  assert.equal(calls,3);
});

test('an enumerated frame without any successful page is not readable pending evidence', async () => {
  let calls=0;
  const service=new BrowserService(adapter,{reviewPollIntervalMs:1,observationTimeoutMs:1000});
  const noPage={status:'STATE_UNCLEAR',error_code:'STATE_UNCLEAR',result:{pause:{reason:'state_unclear'},
    diagnostics:{frameCount:1,successfulFrameCount:0,readyState:'complete'}}};
  const wc=contents(()=>++calls===1?Promise.resolve(noPage):new Promise(()=>{}));
  await assert.rejects(service.observeForReview(wc,'frame-without-page',['app-1'],{deadline:Date.now()+45}),error=>{
    assert.equal(error.message,'browser_observation_timeout');
    assert.equal(error.lastObservation.frameCount,1);
    assert.equal(error.lastObservation.visibleTextLength,0);
    return true;
  });
  assert.equal(calls,2);
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

test('a redirected home needs saved form evidence, not a navigation login link', async () => {
  const home = { ...shell, result: { ...shell.result, page: { page_url: 'https://ats.example/pb/index.html' },
    diagnostics: { ...shell.result.diagnostics, loginPromptVisible: true } } };
  assert.equal(isRedirectedHome(home, 'https://ats.example/pb/account.html'), true);
  assert.equal(isRedirectedLogin(home, 'https://ats.example/pb/account.html'), false);
  const gate = {...home, result: {...home.result,
    auth_evidence: {trigger:'selector',selector:"input[type='password']",text:'登录'}}};
  assert.equal(isRedirectedLogin(gate, 'https://ats.example/pb/account.html'), true);
  assert.equal(isRedirectedLogin(home, 'https://ats.example/pb/index.html'), false);
  assert.equal(isRedirectedLogin(home, 'https://foreign.example/account.html'), false);
  assert.equal(isRedirectedLogin({ ...home, result: { ...home.result, application_records: [{}] } },
    'https://ats.example/pb/account.html'), false);
  assert.equal(isRedirectedLogin({ ...home, result: { ...home.result, diagnostics: {} } },
    'https://ats.example/pb/account.html'), false);
  const service = new BrowserService(adapter);
  const output = await service.observeForReview(contents(async () => gate), 'home-login', ['app-1'],
    { deadline: Date.now() + 100, requestedUrl: 'https://ats.example/pb/account.html' });
  assert.equal(output.error_code, 'LOGIN_REQUIRED');
  assert.equal(output.review_readiness, 'login_required');
  assert.equal(output.result.pause.reason, 'login_required');
});

test('public home with login navigation records a home redirect without claiming login failure', async () => {
  const home = {...shell,result:{...shell.result,
    page:{page_url:'https://ats.example/pb/index.html',text:'校园招聘 首页 登录 注册'},
    diagnostics:{...shell.result.diagnostics,readyState:'complete',loginPromptVisible:true}}};
  const wc = contents(async()=>home);wc.getURL=()=>home.result.page.page_url;
  const service = new BrowserService(adapter,{reviewPollIntervalMs:1,unparsedWindowMs:0,unparsedStableMs:0});
  const output = await service.observeForReview(wc,'public-home',['app-1'],
    {deadline:Date.now()+100,requestedUrl:'https://ats.example/pb/account.html'});
  assert.equal(output.review_readiness,'record_entry_required');
  assert.equal(output.error_code,'APPLICATION_RECORD_HOME_REDIRECT');
  assert.equal(output.result.navigation_diagnostics.reason,'application_record_home_redirect');
  assert.equal(output.result.database_updated,false);
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
  // One lifetime provenance listener remains; per-observation waiters are gone.
  assert.equal(wc.listenerCount('did-start-navigation'), 1);
  assert.equal(wc.listenerCount('render-process-gone'), 1);
});

test('readiness timeout keeps only the last bounded diagnostic summary', async () => {
  const service = new BrowserService(adapter, {reviewPollIntervalMs: 2});
  const observation = {...shell, result: {...shell.result, page: {text:'do-not-persist@example.test'},
    diagnostics: {...shell.result.diagnostics, iframeCount:2, frameCount:3, skippedFrameCount:1}}};
  await assert.rejects(service.observeForReview(contents(async()=>observation),'diagnostic-timeout',['app-1'],
    {deadline:Date.now()+35}), error=>{
    assert.ok(error instanceof ReviewObservationError);
    assert.equal(error.message,'browser_readiness_timeout');
    assert.equal(error.lastObservation.pageState,'unrecognized_content');
    assert.equal(error.lastObservation.iframeCount,2);
    assert.equal(error.lastObservation.skippedFrameCount,1);
    assert.ok(!JSON.stringify(error.lastObservation).includes('do-not-persist'));
    return true;
  });
  assert.equal(reviewDiagnosticSummary({...shell,result:{...shell.result,page:{text:''}}}).pageState,'blank');
  assert.equal(reviewDiagnosticSummary(shell).pageState,'unrecognized_content');
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
  assert.equal(summary.pageState,'unrecognized_content');
  assert.equal(summary.visibleTextLength,child.raw.data.page.text.length);
  assert.ok(!JSON.stringify(summary).includes('private'));
});

test('stable denied frames return a scope error instead of a retryable timeout', async()=>{
  let calls=0;
  const denied={...shell,result:{...shell.result,page:{page_url:'https://ats.example/applications',text:''},
    diagnostics:{...shell.result.diagnostics,readyState:'complete',iframeCount:1,frameCount:2,skippedFrameCount:1,scopeDeniedFrameCount:1}}};
  const service=new BrowserService(adapter,{reviewPollIntervalMs:2,frameConfirmationMs:20,unparsedWindowMs:35});
  const started=Date.now();
  await assert.rejects(service.observeForReview(contents(async()=>{calls++;return denied;}),
    'denied-scope',['app-1'],{deadline:Date.now()+500}),error=>{
    assert.equal(error.message,'FRAME_SCOPE_DENIED');
    assert.equal(error.lastObservation.pageState,'frame_scope_denied');
    assert.equal(error.lastObservation.recordCount,0);
    return true;
  });
  assert.ok(calls>=3);
  assert.ok(Date.now()-started<400);
});

test('an unrelated denied helper frame does not interrupt delayed top-level cards', async()=>{
  let calls=0;
  const framed={...shell,result:{...shell.result,diagnostics:{...shell.result.diagnostics,
    readyState:'complete',iframeCount:1,frameCount:2,skippedFrameCount:1}}};
  const service=new BrowserService(adapter,{reviewPollIntervalMs:2,frameConfirmationMs:10});
  const result=await service.observeForReview(contents(async()=>++calls<8?framed:record),'helper-denied',
    ['app-1'],{deadline:Date.now()+300});
  assert.equal(calls,8);
  assert.equal(result.review_readiness,'records');
});

test('visible page-unavailable evidence terminates readiness without a shell timeout', async()=>{
  let calls=0;
  const unavailable={...shell,result:{...shell.result,page:{page_url:'https://ats.example/applications',
    title:'404 Page not found',text:'页面不存在'}}};
  assert.equal(classifyReviewReadiness(unavailable),'page_unavailable');
  const output=await new BrowserService(adapter).observeForReview(contents(async()=>{calls++;return unavailable;}),
    'unavailable-page',['app-1'],{deadline:Date.now()+100});
  assert.equal(calls,1);
  assert.equal(output.error_code,'APPLICATION_PAGE_UNAVAILABLE');
  assert.equal(output.result.database_updated,false);
  assert.equal(output.result.last_observation.pageState,'page_unavailable');
});

test('timeout retains bounded UI snippets without account identity or credentials',async()=>{
  const secret='person@example.test 13912345678 330102199001011234 姓名 张三 password=hunter2 OTP=654321 token=opaque-secret';
  const observation={...shell,result:{...shell.result,
    page:{page_url:'https://user:pass@ats.example/applications?token=secret#/app/application_center?code=private',
      title:'Applications 姓名 张三',text:('Loading applications '+secret+' ').repeat(500)},
    diagnostics:{...shell.result.diagnostics,recordBlockCount:2}}};
  await assert.rejects(new BrowserService(adapter,{reviewPollIntervalMs:2}).observeForReview(
    contents(async()=>observation),'private-shell',['app-1'],{deadline:Date.now()+35}),error=>{
    assert.equal(error.message,'browser_readiness_timeout');
    const diagnostic=error.lastObservation;
    assert.equal(diagnostic.pageState,'unrecognized_content');
    assert.equal(diagnostic.page.url,'https://ats.example/applications#/app/application_center');
    assert.ok(diagnostic.page.textSnippet.includes('Loading applications'));
    assert.ok(diagnostic.page.textSnippet.length<=480);
    assert.ok(diagnostic.page.title.length<=160);
    for(const text of ['person@','13912345678','330102199001011234','张三','hunter2','654321','opaque-secret','token=','user:pass']) {
      assert.ok(!JSON.stringify(diagnostic).includes(text),text);
    }
    return true;
  });
});

const readable = {...shell, result: {...shell.result, diagnostics: {...shell.result.diagnostics, readyState:'complete'}}};
const samplingOptions = {reviewPollIntervalMs:2, unparsedWindowMs:70, unparsedStableMs:25};
test('stable readable pages with skipped or unavailable helper frames are unparsed, never scope denied',async()=>{
  for(const diagnostics of [
    {iframeCount:1,frameCount:2,skippedFrameCount:1,scopeDeniedFrameCount:0},
    {iframeCount:1,frameCount:2,skippedFrameCount:1,scopeDeniedFrameCount:1},
    {iframeCount:1,frameCount:2,unavailableFrameCount:1},
    {iframeCount:1,frameCount:2,skippedFrameCount:0}
  ]) {
    const observation={...readable,result:{...readable.result,diagnostics:{...readable.result.diagnostics,...diagnostics}}};
    const result=await new BrowserService(adapter,samplingOptions).observeForReview(contents(async()=>observation),
      'readable-helper',['app-1'],{deadline:Date.now()+250});
    assert.equal(result.error_code,undefined);
    assert.equal(result.result.extraction_reason,'unparsed_page');
    assert.equal(result.review_readiness,'unparsed_page');
    assert.equal(result.result.database_updated,false);
    assert.equal(result.result.last_observation.pageState,'unrecognized_content');
    assert.ok(result.result.last_observation.sampling.elapsedMs>=70);
  }
});

test('authorized child loading and content changes are included in readiness sampling',async()=>{
  const root=frameSample(0,frameContext.page_url);
  root.raw.data.page.text='Applications';
  root.raw.data.diagnostics.readyState='complete';
  const makeChild=(text,readyState='complete')=>{
    const child=frameSample(2,'https://ats.example/records');
    Object.assign(child.raw.data.diagnostics,{readyState,visibleTextLength:text.length});
    child.raw.data.page.text=text;
    return normalizeFrameObservations([root,child],frameContext);
  };
  const loading=makeChild('Loading applications');
  assert.equal(reviewDiagnosticSummary(loading).loadingVisible,true);
  assert.equal(reviewDiagnosticSummary(makeChild('Records','interactive')).readyState,'loading');
  assert.notEqual(makeChild('Records 1').result.diagnostics.frames[1].contentFingerprint,
    makeChild('Records 2').result.diagnostics.frames[1].contentFingerprint);
  await assert.rejects(new BrowserService(adapter,samplingOptions).observeForReview(
    contents(async()=>loading),'child-loading',['app-1'],{deadline:Date.now()+110}),/browser_readiness_timeout/);
  const started=Date.now();
  const result=await new BrowserService(adapter,{...samplingOptions,unparsedStableMs:60}).observeForReview(
    contents(async()=>Date.now()-started>135?record:makeChild('Records '+Math.floor((Date.now()-started)/25))),
    'changing-child',['app-1'],{deadline:Date.now()+300});
  assert.equal(result.review_readiness,'records');
});

test('a skipped frame without visible restriction evidence cannot turn blank or loading waits into scope denial',async()=>{
  for(const text of ['', 'Loading applications']) {
    const observation={...readable,result:{...readable.result,page:{text},diagnostics:{...readable.result.diagnostics,
      iframeCount:1,frameCount:2,skippedFrameCount:1,scopeDeniedFrameCount:0}}};
    await assert.rejects(new BrowserService(adapter,{...samplingOptions,frameConfirmationMs:10}).observeForReview(
      contents(async()=>observation),'helper-no-proof',['app-1'],{deadline:Date.now()+110}),error=>{
      assert.equal(error.message,'browser_readiness_timeout');
      assert.equal(error.lastObservation.pageState,text?'unrecognized_content':'blank');
      return true;
    });
  }
});

test('loading, records and login take precedence over visible restricted helper frames',async()=>{
  const diagnostics={...readable.result.diagnostics,iframeCount:1,frameCount:2,skippedFrameCount:1,scopeDeniedFrameCount:1};
  const loading={...readable,result:{...readable.result,page:{text:'Loading applications'},diagnostics}};
  await assert.rejects(new BrowserService(adapter,{...samplingOptions,frameConfirmationMs:10}).observeForReview(
    contents(async()=>loading),'loading-restricted-helper',['app-1'],{deadline:Date.now()+110}),/browser_readiness_timeout/);
  for(const observation of [
    {...record,result:{...record.result,diagnostics}},
    {status:'STATE_UNCLEAR',error_code:'LOGIN_REQUIRED',result:{application_records:[],diagnostics}}
  ]) {
    const result=await new BrowserService(adapter,samplingOptions).observeForReview(contents(async()=>observation),
      'terminal-with-helper',['app-1'],{deadline:Date.now()+110});
    assert.equal(result.review_readiness,observation.error_code?'login_required':'records');
  }
});

test('stable readable no-card page is unparsed, preserving bounded first/last sampling evidence',async()=>{
  const result=await new BrowserService(adapter,samplingOptions).observeForReview(contents(async()=>({...readable,
    result:{...readable.result,page:{...readable.result.page,capturedAt:new Date().toISOString()}}})),
    'unparsed',['app-1'],{deadline:Date.now()+250});
  assert.equal(result.error_code,undefined);
  assert.equal(result.status,'SUCCEEDED');
  assert.equal(result.result.extraction_reason,'unparsed_page');
  assert.equal(result.result.evidence_only,true);
  assert.ok(reviewObservationBinding(result),'unparsed result must retain its main-process-only capture binding');
  assert.equal(result.review_readiness,'unparsed_page');
  assert.equal(result.result.database_updated,false);
  assert.deepEqual(result.result.semantic_nodes,[]);
  const {sampling,pageState}=result.result.last_observation;
  assert.equal(pageState,'unrecognized_content');
  assert.equal(sampling.first.pageState,'unrecognized_content');
  assert.ok(sampling.count>=5);
  assert.ok(sampling.elapsedMs>=70);
  assert.ok(sampling.stableMs>=25);
});

test('minimum sampling window allows late cards despite stable initial readable text',async()=>{
  const started=Date.now();
  const result=await new BrowserService(adapter,{...samplingOptions,unparsedWindowMs:150}).observeForReview(
    contents(async()=>Date.now()-started<105?readable:record),'late-readable',['app-1'],{deadline:Date.now()+300});
  assert.equal(result.review_readiness,'records');
});

test('blank top plus denied helper gets the full minimum window for delayed cards',async()=>{
  const started=Date.now();
  const framed={...readable,result:{...readable.result,page:{text:''},diagnostics:{...readable.result.diagnostics,
    iframeCount:1,frameCount:2,skippedFrameCount:1}}};
  const result=await new BrowserService(adapter,{...samplingOptions,frameConfirmationMs:15,unparsedWindowMs:150}).observeForReview(
    contents(async()=>Date.now()-started<105?framed:record),'late-blank-frame',['app-1'],{deadline:Date.now()+300});
  assert.equal(result.review_readiness,'records');
});

test('changes hidden by diagnostic redaction still reset the stable-content clock',async()=>{
  const started=Date.now();
  const result=await new BrowserService(adapter,{...samplingOptions,unparsedStableMs:60}).observeForReview(
    contents(async()=>{
      const elapsed=Date.now()-started;
      if(elapsed>135)return record;
      return {...readable,result:{...readable.result,page:{text:'Applications '+Math.floor(elapsed/25)}}};
    }),'changing-readable',['app-1'],{deadline:Date.now()+300});
  assert.equal(result.review_readiness,'records');
});

test('visible loading and blank complete pages remain genuine readiness waits',async()=>{
  for(const text of ['Loading applications','']) {
    const loading={...readable,result:{...readable.result,page:{text}}};
    await assert.rejects(new BrowserService(adapter,samplingOptions).observeForReview(contents(async()=>loading),
      'still-loading',['app-1'],{deadline:Date.now()+110}),error=>{
      assert.equal(error.message,'browser_readiness_timeout');
      assert.ok(error.lastObservation.sampling.count>=5);
      assert.equal(error.lastObservation.sampling.stableMs,0);
      return true;
    });
  }
});
