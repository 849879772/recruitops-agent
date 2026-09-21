const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs/promises');
const http = require('node:http');
const WebSocket = require('ws');
const { OwnedRuntime } = require('../dist/runtime-client');
const { DesktopBridge } = require('../dist/bridge-client');
const { normalizeObservation, normalizeFrameObservations } = require('../../../packages/desktop_browser/index.cjs');
const { ReviewObservationError, reviewDiagnosticSummary } = require('../dist/review-readiness');
const root = path.resolve(__dirname, '../../..');
const wait = async predicate => { for (let n = 0; n < 300; n++) { if (await predicate()) return; await new Promise(r => setTimeout(r, 30)); } throw new Error('fixture timeout'); };
function request(runtime, route, value) {
  return new Promise((resolve, reject) => {
    const body = value === undefined ? undefined : JSON.stringify(value);
    const req = http.request(runtime.origin + route, { method: body ? 'POST' : 'GET', headers: { Authorization: runtime.authorization(), ...(body ? { 'Content-Type':'application/json', 'Content-Length': Buffer.byteLength(body) } : {}) } }, res => {
      let result = ''; res.on('data', d => result += d); res.on('end', () => { try { assert.equal(res.statusCode,200,result); resolve(JSON.parse(result)); } catch(e) { reject(e); } });
    }); req.on('error', reject); req.end(body);
  });
}
test('REAL Python bridge + official consumer verify updates, replay, cancellation and ownership', { timeout: 45000 }, async () => {
  await fs.mkdir(path.join(root, '.desktop-runtime-tests'), { recursive: true });
  const directory = await fs.mkdtemp(path.join(root, '.desktop-runtime-tests/shell-bridge-'));
  const runtime = new OwnedRuntime(() => {}, { executable: path.join(root, '.venv-desktop-tests/Scripts/python.exe'),
    args: [path.join(__dirname,'python_bridge_fixture.py'), directory], cwd: root,
    env: { SystemRoot: process.env.SystemRoot, WINDIR:process.env.WINDIR, PYTHONUTF8:'1', PYTHONNOUSERSITE:'1', NO_PROXY:'localhost,127.0.0.1,::1', HOME:directory, USERPROFILE:directory }, expectedInstance:'python-bridge-fixture' });
  let calls = 0, cancelled = false, interrupted = false;
  const counts = new Map();
  const bridge = new DesktopBridge(runtime,'desktop-cross-language', async (url,id,ids,signal,_deadline,onStage) => {
    calls++;
    counts.set(id,(counts.get(id)||0)+1);
    if (id === 'observe-op') {
      onStage('EXTRACTING');
      onStage('WAITING_FOR_CONTENT');
      await new Promise(resolve => setTimeout(resolve, 100));
      assert.equal(signal.aborted, false, 'a readiness stage must not disconnect the bridge');
      onStage('VALIDATING');
    }
    if (id === 'unclear-op') {
      onStage('EXTRACTING');
      onStage('STATE_UNCLEAR');
      return { type:'result',operation_id:id,status:'STATE_UNCLEAR',error_code:'STATE_UNCLEAR',
        result:{evidence_only:true,database_updated:false,page_url:url,application_ids:ids,reason:'fixture_unsupported_page'} };
    }
    if (id === 'failed-op') throw new ReviewObservationError('browser_readiness_timeout',reviewDiagnosticSummary({result:{
      application_records:[],diagnostics:{iframeCount:2,frameCount:3,skippedFrameCount:2}}}));
    if (id.startsWith('consumer-')) {
      assert.deepEqual(ids,['consumer-app']);
      if (id === 'consumer-interrupted') return new Promise(resolve => signal.addEventListener('abort',()=>{ interrupted = true; resolve({}); }));
      const status = id === 'consumer-downgrade' ? 'applied' : 'written';
      const label = status === 'written' ? '笔试中' : '已投递';
      const observedUrl = id === 'consumer-redirect' ? url + '#/app/application_center' : url;
      const raw={protocolVersion:3,type:'extension.controlled_action_result',requestId:id,ok:true,
        data:{action:'observe_application_page',selectorKey:'application_page',
          page:{page_url:observedUrl,origin:'https://ats.example',path:new URL(observedUrl).pathname,title:'Fixture role',text:`Fixture Company Fixture role ${label}`},
          capturedAt:new Date().toISOString(),semanticNodes:[],applicationRecords:[{title:'Fixture role',status,label,context:`Fixture Company Fixture role ${label}`}],
          entries:[{status,label,context:`Fixture Company Fixture role ${label}`}],diagnostics:{frameScope:'top_only',iframeCount:0}}};
      const context={operation_id:id,page_url:observedUrl,application_ids:ids};
      if(id==='consumer-iframe') {
        const top=structuredClone(raw);top.data.applicationRecords=[];top.data.entries=[];top.data.page.text='';top.data.diagnostics.iframeCount=1;
        const childUrl='https://ats.example/embedded';raw.data.page.page_url=childUrl;raw.data.page.path='/embedded';
        raw.data.diagnostics.frameScope='single_frame';
        return normalizeFrameObservations([{frameId:0,frameUrl:observedUrl,raw:top},{frameId:2,frameUrl:childUrl,raw}],context);
      }
      return normalizeObservation(raw,context);
    }
    assert.deepEqual(ids,['fixture-app']);
    if (id === 'cancel-op') return new Promise(resolve => signal.addEventListener('abort',()=>{ cancelled = true; resolve({}); }));
    return { protocol_version:1,type:'result',operation_id:id,event_id:`fixture-result-${id}`,status:'SUCCEEDED',result:{evidence_only:true,database_updated:false,application_id:'fixture-app',application_ids:ids,page_url:'https://ats.example/applications',application_records:[{title:'Fixture role',status:'applied',evidence:'Fixture only'}]} };
  },()=>{});
  try {
    runtime.start(); await wait(()=> runtime.state.status === 'ready'); bridge.connect(); await wait(()=>bridge.status==='connected');
    for (const id of ['observe-op','review-op']) {
      await request(runtime,'/fixture/create',{device_id:'desktop-cross-language',operation_id:id,review:id==='review-op'});
      await wait(async()=> (await request(runtime,`/fixture/state/${id}`)).events.some(e=>e.event_type==='observation'));
      const before = await request(runtime,`/fixture/state/${id}`);
      assert.equal(before.result,null); // Browser evidence is NOT the backend business result.
      assert.equal(before.unacked,0);
      const evidence = before.events.filter(e=>e.event_type==='observation');
      assert.equal(evidence.length,1); assert.equal(evidence[0].payload.result.database_updated,false);
      if (id === 'observe-op') {
        assert.ok(before.events.some(e => e.payload.stage === 'WAITING_FOR_CONTENT' && e.status === 'EXTRACTING'));
        assert.equal(bridge.status,'connected');
      }
      await request(runtime,`/fixture/duplicate/${id}`,{});
      await new Promise(r=>setTimeout(r,150));
      const after = await request(runtime,`/fixture/state/${id}`);
      assert.equal(after.events.length,before.events.length); assert.equal(after.status,'VALIDATING');
    }
    assert.equal(calls,2);
    await request(runtime,'/fixture/create',{device_id:'another-device',operation_id:'foreign-op'});
    await new Promise(r=>setTimeout(r,150)); assert.equal(calls,2);
    assert.equal((await request(runtime,'/fixture/state/foreign-op')).unacked,1);
    await request(runtime,'/fixture/create',{device_id:'desktop-cross-language',operation_id:'cancel-op'});
    await wait(()=>calls===3); await request(runtime,'/fixture/cancel/cancel-op',{}); await wait(()=>cancelled);
    await wait(async()=> (await request(runtime,'/fixture/state/cancel-op')).unacked===0);
    assert.equal((await request(runtime,'/fixture/state/cancel-op')).status,'CANCELLED');
    await request(runtime,'/fixture/disconnect/desktop-cross-language',{});
    await wait(()=>bridge.status==='disconnected'); await wait(()=>bridge.status==='connected');
    await request(runtime,'/fixture/create',{device_id:'desktop-cross-language',operation_id:'after-reconnect'});
    await wait(async()=> (await request(runtime,'/fixture/state/after-reconnect')).status==='VALIDATING');
    assert.equal(calls,4);
    for (const [id,expected] of [['consumer-update','updated'],['consumer-redirect','unchanged'],['consumer-iframe','unchanged'],['consumer-unchanged','unchanged'],['consumer-downgrade','unchanged']]) {
      const response = await request(runtime,'/fixture/workflow',{device_id:'desktop-cross-language',operation_id:id});
      assert.equal(response.success,true,JSON.stringify(response));
      assert.equal(response.data.status,'SUCCEEDED');
      assert.equal(response.data.verification.status,expected);
      const readable = await request(runtime,`/fixture/readable/${id}`);
      assert.equal(readable.stage,'written');
      assert.equal(readable.operation.data.status,'SUCCEEDED');
      assert.equal(readable.operation.data.operation.result.verification.status,expected);
    }
    const baseline = await request(runtime,'/fixture/readable/consumer-update');
    const replay = await request(runtime,'/fixture/workflow',{device_id:'desktop-cross-language',operation_id:'consumer-update'});
    assert.equal(replay.success,true,JSON.stringify(replay));
    assert.equal((await request(runtime,'/fixture/readable/consumer-update')).audits,baseline.audits);
    assert.equal(counts.get('consumer-update'),1);
    const inflight = request(runtime,'/fixture/workflow',{device_id:'desktop-cross-language',operation_id:'consumer-interrupted',timeout_ms:1500});
    await wait(async()=> {try {return (await request(runtime,'/fixture/state/consumer-interrupted')).unacked===0;} catch{return false;}});
    await request(runtime,'/fixture/disconnect/desktop-cross-language',{}); await wait(()=>interrupted);
    await inflight;
    assert.equal((await request(runtime,'/fixture/state/consumer-interrupted')).status,'CANCELLED');
    await wait(()=>bridge.status==='connected');
    const fresh = await request(runtime,'/fixture/workflow',{device_id:'desktop-cross-language',operation_id:'consumer-resumed'});
    assert.equal(fresh.success,true); assert.equal(fresh.data.verification.status,'unchanged');
    assert.equal((await request(runtime,'/fixture/state/consumer-interrupted')).status,'CANCELLED');
    assert.equal(counts.get('consumer-interrupted'),1); assert.equal(counts.get('consumer-resumed'),1);
    for (const [id,status] of [['unclear-op','STATE_UNCLEAR'],['failed-op','FAILED']]) {
      await request(runtime,'/fixture/create',{device_id:'desktop-cross-language',operation_id:id});
      await wait(async()=> (await request(runtime,`/fixture/state/${id}`)).result !== null);
      const result=await request(runtime,`/fixture/state/${id}`);
      assert.equal(result.status,status);
      assert.equal(result.result.database_updated,false);
      if(id==='failed-op') {
        assert.equal(result.result.last_observation.pageState,'frame_scope_denied');
        assert.equal(result.result.last_observation.skippedFrameCount,2);
        assert.equal(result.result.last_observation.iframeCount,2);
      }
      assert.equal(bridge.status,'connected');
    }
    const url = runtime.origin.replace('http:','ws:')+'/browser-bridge';
    for (const headers of [
      {Authorization:'Bearer wrong',Origin:runtime.origin},
      {Authorization:runtime.authorization(),Origin:'https://remote.example'},
      {Authorization:runtime.authorization(),Origin:runtime.origin,Host:'localhost:'+new URL(runtime.origin).port}
    ]) {
      await new Promise((resolve,reject)=> {
        const socket = new WebSocket(url,{headers,handshakeTimeout:2000});
        socket.on('open',()=>{socket.terminate();reject(new Error('invalid boundary accepted'));});
        socket.on('unexpected-response',(_req,res)=>{assert.equal(res.statusCode,403);res.resume();socket.terminate();resolve();}); socket.on('error',()=>{});
      });
    }
    await new Promise((resolve,reject)=> {
      const socket=new WebSocket(url,{headers:{Authorization:runtime.authorization(),Origin:runtime.origin},handshakeTimeout:2000});
      socket.on('message',data=>{const challenge=JSON.parse(data);socket.send(JSON.stringify({protocol_version:1,type:'auth',device_id:'invalid-proof',challenge:challenge.challenge,signature:'0'.repeat(64)}));});
      socket.on('close',code=>{try {assert.equal(code,4401);resolve();} catch(e){reject(e);}});socket.on('error',reject);
    });
  } finally {
    bridge.stop(); await runtime.stop();
    await fs.rm(directory,{recursive:true,force:true});
  }
});
