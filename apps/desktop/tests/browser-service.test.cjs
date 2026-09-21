const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
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
test('fixed isolated world pins full URL and trusted application IDs', async () => {
  const service = new BrowserService(adapter);
  const wc = contents(async (world, scripts) => { assert.equal(world, 1004); assert.equal(scripts[0].code, 'fixed-code'); return { evidence: true }; });
  const result = await service.observe(wc, 'operation-1', ['application-1']);
  assert.deepEqual(result.context.application_ids, ['application-1']);
  assert.equal(wc.listenerCount('did-start-navigation'), 0);
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
    assert.equal(wc.listenerCount('did-start-navigation'), 0);
    assert.equal(wc.listenerCount('did-navigate-in-page'), 0);
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
  assert.equal(wc.listenerCount('did-start-navigation'), 0);
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
  assert.equal(wc.listenerCount('did-start-navigation'),0);
});
