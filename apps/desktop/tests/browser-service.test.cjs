const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const vm = require('node:vm');
const { BrowserService } = require('../dist/browser-service');
function contents(evaluate) {
  return Object.assign(new EventEmitter(), { id: 1, isDestroyed: () => false, isLoadingMainFrame: () => false,
    getURL: () => 'https://ats.example/jobs?binding=one', executeJavaScriptInIsolatedWorld: evaluate });
}
function navigableContents(url, evaluate) {
  let currentUrl = url;
  return Object.assign(new EventEmitter(), { id: Math.random(), isDestroyed: () => false, isLoadingMainFrame: () => false,
    getURL: () => currentUrl, setURL: value => { currentUrl = value; }, executeJavaScriptInIsolatedWorld: evaluate });
}
const adapter = { buildObservationScript: () => 'fixed-code', normalizeObservation: (raw, context) => ({ raw, context }) };
function assertObservationListenersReleased(wc) {
  // A single shared provenance watcher intentionally survives observations so
  // screenshots cannot reuse evidence after navigation, including same-URL loads.
  for (const event of ['did-start-navigation', 'did-navigate-in-page', 'render-process-gone']) {
    assert.equal(wc.listenerCount(event), 1, `${event}: only the shared provenance watcher remains`);
  }
  for (const event of ['will-navigate', 'will-redirect']) {
    assert.equal(wc.listenerCount(event), 0, `${event}: temporary observation listener released`);
  }
}
test('fixed isolated world pins full URL and trusted application IDs', async () => {
  const service = new BrowserService(adapter);
  const wc = contents(async (world, scripts) => { assert.equal(world, 1004); assert.equal(scripts[0].code, 'fixed-code'); return { evidence: true }; });
  const result = await service.observe(wc, 'operation-1', ['application-1']);
  assert.deepEqual(result.context.application_ids, ['application-1']);
  assertObservationListenersReleased(wc);
});
test('repeated observations reuse one provenance watcher without leaking temporary listeners', async () => {
  const service = new BrowserService(adapter);
  const wc = contents(async () => ({ evidence: true }));
  for (let index = 0; index < 20; index++) {
    await service.observe(wc, `repeat-observation-${index}`, ['application-1']);
    assertObservationListenersReleased(wc);
  }
});
test('navigation race, large payload and missing capture contract fail closed', async () => {
  const service = new BrowserService(adapter);
  const wc = contents(async () => { wc.emit('did-start-navigation'); return {}; });
  await assert.rejects(service.observe(wc, 'op'), /navigation_changed/);
  await assert.rejects(service.observe(contents(async () => 'a'.repeat(262145)), 'op'), /payload_limit/);
  await assert.rejects(service.observe(contents(async () => ({})), 'op', [], true), /capture_unavailable/);
});

for (const [name, targetUrl] of [
  ['hashchange', 'https://ats.example/applications#/applications'],
  ['Feishu history.replaceState', 'https://ats.example/applications#/app/application_center?tenant=anonymous']
]) {
  test(`re-observes after an owned same-document ${name} and waits for its URL to settle`, async () => {
    let calls = 0;
    const observedUrls = [];
    const record = { type: 'result', status: 'SUCCEEDED', result: { application_records: [{ application_id: 'app-1' }] } };
    const wc = navigableContents('https://ats.example/applications', async () => {
      calls++;
      if (calls === 1) {
        wc.emit('did-start-navigation', {}, targetUrl, true, true);
        wc.setURL(targetUrl);
        wc.emit('did-navigate-in-page', {}, targetUrl, true);
      }
      return record;
    });
    const service = new BrowserService({
      buildObservationScript: params => { observedUrls.push(params.page_url); return 'fixed-code'; },
      normalizeObservation: raw => raw
    }, { reviewPollIntervalMs: 4 });
    const operationId = `owned-${name.replace(/[^a-zA-Z0-9_.:-]/g, '-')}`;
    const outcome = await service.observeForReview(wc, operationId, ['app-1'], {
      deadline: Date.now() + 1000, ownedOrigin: 'https://ats.example'
    });
    assert.equal(outcome.review_readiness, 'records');
    assert.equal(calls, 2);
    assert.deepEqual(observedUrls, ['https://ats.example/applications', targetUrl]);
    assertObservationListenersReleased(wc);
  });
}

test('owned whole-document navigation discards an interrupted sample and observes the new document', async () => {
  let calls = 0;
  const target = 'https://ats.example/login';
  const wc = navigableContents('https://ats.example/applications', async () => {
    if (++calls === 1) {
      wc.emit('will-navigate', {}, target);
      wc.emit('did-start-navigation', {}, target, false, true);
      wc.setURL(target);
      throw new Error('Execution context was destroyed');
    }
    return { type: 'result', status: 'STATE_UNCLEAR', error_code: 'LOGIN_REQUIRED', result: {} };
  });
  const service = new BrowserService({ buildObservationScript: () => 'fixed-code', normalizeObservation: raw => raw });
  const result = await service.observeForReview(wc, 'document-login', ['app-1'],
    { deadline: Date.now() + 800, ownedOrigin: 'https://ats.example' });
  assert.equal(result.review_readiness, 'login_required');
  assert.equal(calls, 2);
  assert.equal(wc.listenerCount('will-navigate'), 0);
});

test('rejects a committed foreign main-frame navigation but ignores child-frame navigation', async () => {
  const record = { type: 'result', status: 'SUCCEEDED', result: { application_records: [{ application_id: 'app-1' }] } };
  let calls = 0;
  let foreign;
  foreign = navigableContents('https://ats.example/applications', async () => {
    calls++;
    foreign.emit('did-start-navigation', {}, 'https://foreign.example/applications', false, true);
    foreign.setURL('https://foreign.example/applications');
    return record;
  });
  const service = new BrowserService({ buildObservationScript: () => 'fixed-code', normalizeObservation: raw => raw });
  await assert.rejects(service.observeForReview(foreign, 'foreign-main-frame', ['app-1'], {
    deadline: Date.now() + 200, ownedOrigin: 'https://ats.example'
  }), /browser_navigation_changed/);
  assert.equal(calls, 1);

  const attempted = navigableContents('https://ats.example/applications', async () => {
    attempted.emit('will-navigate', {}, 'https://foreign.example/applications');
    return record;
  });
  await assert.rejects(service.observeForReview(attempted, 'foreign-navigation-attempt', ['app-1'], {
    deadline: Date.now() + 200, ownedOrigin: 'https://ats.example'
  }), /browser_navigation_changed/);
  assert.equal(attempted.listenerCount('will-navigate'), 0);

  const child = navigableContents('https://ats.example/applications', async () => {
    child.emit('did-start-navigation', {}, 'https://foreign-frame.example/embed', false, false);
    return record;
  });
  const outcome = await service.observeForReview(child, 'foreign-child-frame', ['app-1'], {
    deadline: Date.now() + 200, ownedOrigin: 'https://ats.example'
  });
  assert.equal(outcome.review_readiness, 'records');
});

test('owned navigation uses the remaining deadline for a slow replacement document', async () => {
  let calls = 0, loadingUntil = 0;
  const target = 'https://ats.example/login';
  const wc = navigableContents('https://ats.example/applications', async () => {
    if (++calls === 1) {
      loadingUntil = Date.now() + 2100;
      wc.emit('did-start-navigation', {}, target, false, true);
      wc.setURL(target);
      throw new Error('Execution context was destroyed');
    }
    return { error_code: 'LOGIN_REQUIRED', result: {} };
  });
  wc.isLoadingMainFrame = () => Date.now() < loadingUntil;
  const service = new BrowserService({ buildObservationScript: () => 'fixed-code', normalizeObservation: raw => raw });
  const result = await service.observeForReview(wc, 'slow-document-login', ['app-1'],
    { deadline: Date.now() + 3500, ownedOrigin: 'https://ats.example' });
  assert.equal(result.review_readiness, 'login_required');
  assert.equal(calls, 2);
});

test('owned navigation cannot reset the deadline or return evidence from the old document', async () => {
  let calls = 0, loading = false;
  const target = 'https://ats.example/login';
  const wc = navigableContents('https://ats.example/applications', async () => {
    calls++;
    loading = true;
    wc.emit('did-start-navigation', {}, target, false, true);
    wc.setURL(target);
    return {result: {application_records: [{title: 'stale record'}]}};
  });
  wc.isLoadingMainFrame = () => loading;
  const service = new BrowserService({ buildObservationScript: () => 'fixed-code', normalizeObservation: raw => raw });
  await assert.rejects(service.observeForReview(wc, 'navigation-deadline', ['app-1'],
    { deadline: Date.now() + 100, ownedOrigin: 'https://ats.example' }), /browser_readiness_timeout/);
  assert.equal(calls, 1);
  assertObservationListenersReleased(wc);
});

test('child collection is isolated, same-origin scoped, and re-sampled after navigation', async()=>{
  const makeFrame=(url,id)=>({url,origin:new URL(url).origin,frameTreeNodeId:id,frameToken:'token-'+id,
    detached:false,isDestroyed:()=>false});
  const top=makeFrame('https://ats.example/applications',1),child=makeFrame('https://ats.example/records',2);
  const foreign=makeFrame('https://foreign.example/records',3);
  top.framesInSubtree=[top,child,foreign];
  const wc=navigableContents(top.url,async()=>({top:true}));wc.mainFrame=top;
  const service=new BrowserService({
    buildObservationScript:()=> 'top-code',buildFrameObservationScript:params=>params.page_url,
    normalizeObservation:()=>{throw Error('must aggregate');},
    normalizeFrameObservations:(samples,context,skipped)=>{
      assert.equal(skipped,1);assert.equal(samples.length,2);
      assert.equal(samples[1].frameId,2);assert.equal(samples[1].frameUrl,'https://ats.example/current-records');
      assert.equal(context.page_url,top.url);
      return {result:{application_records:[{title:'Fresh record'}]}};
    }
  },{reviewPollIntervalMs:2});
  let calls=0;
  service.frameExecutor={execute:async(target,frame,code)=>{
    assert.equal(target,wc);assert.equal(frame,child);assert.equal(code,child.url);
    if(++calls===1){wc.emit('did-start-navigation',{},child.url,false,false);child.url='https://ats.example/current-records';}
    return {child:true};
  }};
  const result=await service.observeForReview(wc,'child-race',['app-1'],{deadline:Date.now()+300});
  assert.equal(result.review_readiness,'records');assert.equal(calls,2);
  assertObservationListenersReleased(wc);
});

test('cross-origin navigation diagnostics preserve the actual trigger and redact target secrets',async()=>{
  const initial='https://ats.example/applications?token=initial-secret';
  const attempted='https://login.example/sso/authorize?code=auth-secret&email=person@example.test';
  const wc=navigableContents(initial,async()=>{
    wc.emit('will-redirect',{},attempted,false,true);
    return {result:{application_records:[{title:'must not be returned'}]}};
  });
  const service=new BrowserService({buildObservationScript:()=>'',normalizeObservation:value=>value});
  await assert.rejects(service.observeForReview(wc,'blocked-sso',['app-1'],
    {deadline:Date.now()+200,requestedUrl:initial}),error=>{
    assert.equal(error.message,'browser_navigation_changed');
    assert.deepEqual(error.navigation,{
      requestedUrl:'https://ats.example/applications',finalUrl:'https://ats.example/applications',
      attemptedUrl:'https://login.example/sso/authorize',reason:'will_redirect_cross_origin',sameOrigin:false,ssoCandidate:true,
      restriction:'unapproved_origin',phase:'observation'
    });
    assert.ok(!JSON.stringify(error.navigation).includes('secret'));
    assert.ok(!JSON.stringify(error.navigation).includes('person@'));
    return true;
  });
});

test('scope evidence uses visible embedding elements without reading foreign frame contents',async()=>{
  const origin='https://ats.example',url=origin+'/applications';
  const element=(source,options={})=>({
    ...options, parentElement:options.parentElement||null,
    getBoundingClientRect:()=>({width:options.width??900,height:options.height??500}),
    getClientRects:()=>[{}],getAttribute:name=>name==='src'?source:name==='aria-hidden'?options.ariaHidden:null,
    hasAttribute:name=>name==='sandbox'&&!!options.sandbox,
    sandbox:{contains:()=>false},
    get contentDocument(){throw Error('foreign content must never be read');},
    get contentWindow(){throw Error('foreign window must never be read');}
  });
  const frames=[
    element('https://foreign.example/applications'),
    element('/applications',{sandbox:true}),
    element('https://foreign.example/helper',{hidden:true}),
    element('https://foreign.example/pixel',{width:1,height:1}),
    element('https://foreign.example/helper',{opacity:'0'}),
    element('https://foreign.example/helper',{ariaHidden:'true'}),
    element('https://foreign.example/helper',{parentElement:{hidden:true,parentElement:null,getAttribute:()=>null}}),
    element('about:blank'),element(null),element('/helper')
  ];
  const top={url,origin,frameTreeNodeId:1,frameToken:'top',detached:false,isDestroyed:()=>false};
  const foreign={url:'https://foreign.example/applications',origin:'https://foreign.example',
    frameTreeNodeId:2,frameToken:'foreign',detached:false,isDestroyed:()=>false};
  top.framesInSubtree=[top,foreign,{...foreign,frameTreeNodeId:3,frameToken:'opaque',url:'about:blank',origin:'null'}];
  let calls=0;
  const wc=navigableContents(url,async(world,scripts)=>{
    assert.equal(world,1004);calls++;
    if(scripts[0].code==='top-code')return {top:true};
    return vm.runInNewContext(scripts[0].code,{
      document:{querySelectorAll:()=>frames,baseURI:url},location:{origin},URL,
      getComputedStyle:owner=>({display:'block',visibility:'visible',opacity:owner.opacity??'1'})
    });
  });
  wc.mainFrame=top;
  const service=new BrowserService({buildObservationScript:()=> 'top-code',
    buildFrameObservationScript:()=>{throw Error('must not execute in a restricted frame');},
    normalizeObservation:()=>{throw Error('must aggregate');},
    normalizeFrameObservations:(samples,_context,skipped)=>{
      assert.deepEqual(samples,[{frameId:0,frameUrl:url,raw:{top:true}}]);
      assert.equal(skipped,2);
      return {result:{diagnostics:{skippedFrameCount:skipped},application_records:[]}};
    }
  });
  const result=await service.observe(wc,'frame-scope-evidence');
  assert.equal(calls,2);
  assert.equal(result.result.diagnostics.scopeDeniedFrameCount,2);
});
