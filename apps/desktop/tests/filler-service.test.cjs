const {test}=require('node:test');
const assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const {FillerService}=require('../dist/filler-service');
const desktopAdapter=require('../../../packages/desktop_filler/index.cjs');

function setup(timeout=100) {
  const frame={processId:7,routingId:9,url:'https://example.com/form',detached:false,frames:[],async executeJavaScript(){throw new Error('top frame uses isolated world');}};
  const scans=[];
  const wc=Object.assign(new EventEmitter(),{id:1,url:frame.url,mainFrame:frame,isDestroyed:()=>false,isLoadingMainFrame:()=>false,
    getURL(){return this.url;},async executeJavaScriptInIsolatedWorld(world,[{code}]){
      assert.equal(world,1005);
      if(code==='scan') return {ok:true,totalFields:1,emptyFields:1,scanId:'fixture-scan',issuedAt:1,sourceHash:'x',blockedCount:0,
        topFrameOnly:false,route:null,attachments:[],repeaters:[],diagnostics:[],matches:[{fieldId:'name',label:'Name',value:'Synthetic'}]};
      if(code==='fill') return {ok:true,filled:1,failed:[],skipped:[],code:'filler_fill_complete',results:[{fieldId:'name',ok:true,reason:'',status:'filled'}]};
      return {ok:true,restored:1,failed:[],code:'filler_undo_complete',structureRestored:false,remoteUploadsReverted:false};
    }});
  frame.framesInSubtree=[frame];
  const adapter={loadDesktopFiller:()=>({}),buildCancelScript:()=> 'cancel',buildScanScript:(_bundle,profile,route)=>{scans.push({profile:structuredClone(profile),route});return 'scan';},buildFillScript:()=> 'fill',buildUndoScript:()=> 'undo',
    mergeApplicationContexts:desktopAdapter.mergeApplicationContexts,
    aggregateFrameScans(outcomes){const hit=outcomes[0],selectionId=JSON.stringify(['instance',String(wc.id),'7:9',hit.route.documentId,hit.scan.scanId,'name']);return {ok:true,partial:false,failures:[],fields:[{route:hit.route,scanId:hit.scan.scanId,fieldId:'name',selectionId,match:hit.scan.matches[0]}]};}};
  const executor={execute:(page,frame,code)=>page.executeJavaScriptInIsolatedWorld(1005,[{code}]),assignFile:async()=>{throw new Error('unexpected upload');}};
  const service=new FillerService(adapter,()=>{},timeout,executor);service.attach(wc);service.setProfile({basic:{name:'Synthetic'}},1);
  return {wc,service,scans,adapter,executor};
}
async function scanned(service,wc){await service.scan(wc,'a'.repeat(32));const snapshot=service.snapshot(wc);return {scanId:snapshot.scanId,fieldId:snapshot.fields[0].fieldId};}

test('registration context reads in the isolated world without a profile and rejects a navigation race',async()=>{
  const {wc,service,adapter}=setup();service.setProfile(undefined);
  adapter.buildApplicationContextScript=(_bundle,route)=>{assert.equal(route.href,wc.url);return 'registration';};
  wc.executeJavaScriptInIsolatedWorld=async(world,[{code}])=>{
    assert.equal(world,1005);assert.equal(code,'registration');
    return {company:'Synthetic Robotics',titles:['Engineer'],records:[{title:'Engineer',date:'',sourceStatus:''}],url:wc.url};
  };
  assert.equal((await service.readApplicationContext(wc,'a'.repeat(32))).titles[0],'Engineer');
  assert.equal(service.snapshot(wc).profileReady,false);
  assert.equal(service.snapshot(wc).busy,false);
  wc.executeJavaScriptInIsolatedWorld=async()=>{
    wc.emit('did-start-navigation',{},wc.url,false,true);
    return {company:'Stale',titles:['Engineer'],url:wc.url};
  };
  await assert.rejects(service.readApplicationContext(wc,'a'.repeat(32)),/已变化/);
});

function scanOnlyService({scanResult,executeError}={}) {
  const url='https://careers.example.test/form';
  const frame={processId:7,routingId:9,url,detached:false,frames:[]};
  const wc=Object.assign(new EventEmitter(),{id:1,url,mainFrame:frame,isDestroyed:()=>false,isLoadingMainFrame:()=>false,
    getURL(){return this.url;}});
  frame.framesInSubtree=[frame];
  const executor={execute:async()=>{if(executeError)throw executeError;return typeof scanResult==='function'?scanResult():scanResult;},assignFile:async()=>{throw new Error('unexpected upload');}};
  const service=new FillerService(desktopAdapter,()=>{},100,executor);
  service.attach(wc);service.setProfile({basic:{fullName:'Synthetic Candidate'}},1);
  return {wc,service,frame,executor,setScanResult(value){scanResult=value;}};
}

function scanOutcome(route,{matches=[],candidates=[],totalFields=matches.length+candidates.length,blockedCount=0}={}) {
  return {ok:true,route,scanId:'123e4567-e89b-42d3-a456-426614174000',totalFields,emptyFields:totalFields,
    matches,candidates,blockedCount,topFrameOnly:false,attachments:[],repeaters:[],diagnostics:[]};
}

function serviceRoute(frame,wc) {
  return {instanceId:'a'.repeat(32),tabId:String(wc.id),frameId:`${frame.processId}:${frame.routingId}`,
    documentId:`0:${frame.url}`,profileVersion:'1',href:frame.url,allowSubframe:false};
}

test('attachment assignment uses the claimed target executor and the live binding guard',async()=>{
  const {wc,service,adapter,executor}=setup();
  const original=wc.executeJavaScriptInIsolatedWorld;
  wc.executeJavaScriptInIsolatedWorld=async(world,scripts)=>{
    if(scripts[0].code==='ticket')return {uploadId:'ticket-one',selector:'#must-not-requery'};
    if(scripts[0].code==='status')return {ok:true,code:'filler_upload_complete'};
    const result=await original(world,scripts);
    if(scripts[0].code==='scan')result.attachments=[{fieldId:'resume',label:'Resume',accept:'.pdf',multiple:false}];
    return result;
  };
  adapter.buildUploadScript=()=> 'ticket';
  adapter.buildUploadTargetScript=(_bundle,id)=>{assert.equal(id,'ticket-one');return 'exact-target';};
  adapter.buildUploadStatusScript=()=> 'status';
  let assignments=0,guard;
  executor.assignFile=async(page,frame,code,file,valid)=>{
    assert.equal(page,wc);assert.equal(frame,wc.mainFrame);assert.equal(code,'exact-target');
    assert.equal(file,'synthetic-private.pdf');assert.equal(valid(),true);assignments++;guard=valid;
  };
  await service.scan(wc,'a'.repeat(32));const plan=service.snapshot(wc);
  const store={snapshot:()=>({attachment:{id:'one',name:'Synthetic.pdf',type:'pdf',size:16}}),
    withAttachment:fn=>fn('synthetic-private.pdf')};
  await service.uploadAttachment(wc,plan.scanId,plan.attachmentTargets[0].fieldId,store);
  assert.equal(assignments,1);
  wc.emit('did-start-navigation',{},wc.url,false,true);
  assert.equal(guard(),false);
});

test('stop requests cooperative cancellation without reload or early unlocking and keeps undo',async()=>{
  const {wc,service}=setup(),plan=await scanned(service,wc);
  let finish,cancelled=0;
  wc.reload=()=>assert.fail('stop must not reload');
  wc.executeJavaScriptInIsolatedWorld=async(_world,[{code}])=>{
    if(code==='fill')return new Promise(resolve=>{finish=resolve;});
    if(code==='cancel'){cancelled++;return {ok:true};}
    return {ok:true,restored:1,failed:[]};
  };
  const filling=service.fill(wc,plan.scanId,[plan.fieldId]);
  await service.stop(wc);
  assert.equal(cancelled,1);
  assert.equal(service.snapshot(wc).busy,true);
  assert.equal(service.snapshot(wc).busyOperation.cancelRequested,true);
  await service.stop(wc);assert.equal(cancelled,1);
  await assert.rejects(service.scan(wc,'a'.repeat(32)),/尚未结束/);
  finish({ok:true,results:[{fieldId:'name',ok:false,status:'skipped',reason:'filler_cancelled'}]});
  await filling;
  assert.equal(service.snapshot(wc).busy,false);
  assert.equal(service.snapshot(wc).undoReady,true);
  assert.match(service.snapshot(wc).message,/填写已停止/);
  await service.undo(wc);
});

test('timeout holds operation lock until actual settle; partial write retains safe undo',async()=>{
  const {wc,service}=setup(15),plan=await scanned(service,wc);
  let finish;wc.executeJavaScriptInIsolatedWorld=()=>new Promise(resolve=>{finish=resolve;});
  await assert.rejects(service.fill(wc,plan.scanId,[plan.fieldId]),/超时/);
  assert.equal(service.snapshot(wc).busy,true);assert.equal(service.snapshot(wc).undoReady,true);
  await assert.rejects(service.scan(wc,'a'.repeat(32)),/尚未结束/);
  await assert.rejects(service.undo(wc),/尚未结束/);
  finish({ok:true,filled:1,failed:[],skipped:[],results:[{fieldId:'name',status:'filled'}]});await new Promise(resolve=>setImmediate(resolve));
  assert.equal(service.snapshot(wc).busy,false);assert.match(service.snapshot(wc).message,/部分变化/);
  wc.executeJavaScriptInIsolatedWorld=async()=>({ok:true,restored:1,failed:[]});
  await service.undo(wc);assert.equal(service.snapshot(wc).undoReady,false);
});

test('same URL navigation invalidates scan and foreign page cannot fill',async()=>{
  const {wc,service}=setup();let plan=await scanned(service,wc);
  wc.emit('did-start-navigation',{},wc.url,false,true);
  await assert.rejects(service.fill(wc,plan.scanId,[plan.fieldId]),/预览已失效/);
  assert.equal(service.snapshot(wc).scanId,'');
  plan=await scanned(service,wc);
  await assert.rejects(service.fill({...wc,id:2},plan.scanId,[plan.fieldId]),/预览已失效/);
});

test('frame navigation invalidates the plan without unlocking still-running sibling work',async()=>{
  const {wc,service}=setup(),plan=await scanned(service,wc);
  let finish;
  wc.executeJavaScriptInIsolatedWorld=()=>new Promise(resolve=>{finish=resolve;});
  const filling=service.fill(wc,plan.scanId,[plan.fieldId]);
  wc.emit('did-frame-navigate');
  assert.equal(service.snapshot(wc).busy,true);
  await assert.rejects(service.scan(wc,'a'.repeat(32)),/尚未结束/);
  finish({ok:true,results:[]});
  await assert.rejects(filling,/页面或资料已变化/);
  assert.equal(service.snapshot(wc).busy,false);
  assert.equal(service.snapshot(wc).undoReady,false);
});

test('global custom answers are expanded to each scanned page without mutating stored scope',async()=>{
  const {wc,service,scans}=setup();
  const global={id:'global-one',origin:'*',pathname:'*',label:'Why this role?',value:'Synthetic global answer'};
  const scoped={id:'site-one',origin:'https://other.example/form',pathname:'/form',label:'Why this role?',value:'Synthetic site answer'};
  const profile={basic:{name:'Synthetic'},customAnswers:[global,scoped]};
  service.setProfile(profile,2);
  await service.scan(wc,'a'.repeat(32));
  assert.deepEqual(scans.at(-1).profile.customAnswers,[
    {...global,origin:'https://example.com',pathname:'/form'},scoped
  ]);
  assert.deepEqual(profile.customAnswers,[global,scoped]);
});

test('fill snapshots preserve bounded engine failure codes for renderer feedback',async()=>{
  const {wc,service}=setup(),plan=await scanned(service,wc);
  wc.executeJavaScriptInIsolatedWorld=async()=>({ok:true,results:[{fieldId:'name',ok:false,status:'failed',reason:'filler_option_missing'}]});
  await service.fill(wc,plan.scanId,[plan.fieldId]);
  assert.deepEqual(service.snapshot(wc).results,[{fieldId:plan.fieldId,ok:false,reason:'filler_option_missing',status:'failed'}]);
});

test('profile mode/version switch invalidates the old fill candidate set',async()=>{
  const {wc,service}=setup(),plan=await scanned(service,wc);
  service.setProfile({basic:{name:'Different synthetic profile'}},2);
  await assert.rejects(service.fill(wc,plan.scanId,[plan.fieldId]),/预览已失效/);
  assert.equal(service.snapshot(wc).scanId,'');
});

test('partial failure remains explicit and offers guarded undo',async()=>{
  const {wc,service}=setup(),plan=await scanned(service,wc);
  wc.executeJavaScriptInIsolatedWorld=async()=>{throw new Error('filler_field_changed_rescan');};
  await assert.rejects(service.fill(wc,plan.scanId,[plan.fieldId]));
  assert.equal(service.snapshot(wc).undoReady,true);assert.match(service.snapshot(wc).message,/部分修改/);
});

test('successful zero-match scans retain state and expose only safe unmapped candidates',async()=>{
  const harness=scanOnlyService(),route=serviceRoute(harness.frame,harness.wc);
  const candidates=[
    {fieldId:'rf-site',label:'面试站点 interviewSite',controlKind:'select',reason:'filler_answer_missing',customAnswerSupported:true},
    {fieldId:'rf-code',label:'推荐码 recommendationCode',controlKind:'text',reason:'filler_answer_missing',customAnswerSupported:true},
    {fieldId:'rf-photo',label:'照片 photo',controlKind:'file',reason:'filler_attachment_unsupported',customAnswerSupported:false},
  ];
  harness.setScanResult(scanOutcome(route,{candidates,totalFields:3}));
  await harness.service.scan(harness.wc,'a'.repeat(32));
  const snapshot=harness.service.snapshot(harness.wc);
  assert.equal(snapshot.scanState,'complete');
  assert.ok(snapshot.scanId);
  assert.deepEqual(snapshot.scanSummary,{framesScanned:1,framesFailed:0,controlsSeen:3,matched:0,needsAnswer:2,unsupported:1,attachments:0,filtered:0});
  assert.deepEqual(snapshot.fields.map(item=>[item.label,item.fillable,item.blocked,item.value]),[
    ['面试站点 interviewSite',false,false,''],['推荐码 recommendationCode',false,false,''],['照片 photo',false,true,''],
  ]);
  assert.match(snapshot.message,/没有唯一资料答案/);
  await assert.rejects(harness.service.fill(harness.wc,snapshot.scanId,[snapshot.fields[0].fieldId]),/字段身份校验失败/);
});

test('frame execution failure is reported as a failed scan, not an unscanned empty page',async()=>{
  const {wc,service}=scanOnlyService({executeError:new Error('filler_executor_context_unproven')});
  await service.scan(wc,'a'.repeat(32));
  const snapshot=service.snapshot(wc);
  assert.equal(snapshot.scanState,'failed');
  assert.equal(snapshot.scanId,'');
  assert.equal(snapshot.scanSummary.framesScanned,0);
  assert.equal(snapshot.scanSummary.framesFailed,1);
  assert.match(snapshot.message,/扫描失败.*未能读取任何页面框架/);
  assert.equal(snapshot.diagnostics[0].code,'filler_frame_unreachable');
});

test('cross-origin form frame requires an explicit origin grant and clears the blocked hint after rescan',async()=>{
  const {wc,service,frame,executor}=scanOnlyService();
  const embedded={processId:8,routingId:10,url:'https://forms.example.test/apply',detached:false,frames:[]};
  frame.framesInSubtree=[frame,embedded];
  executor.execute=async(_page,target)=>scanOutcome({...serviceRoute(target,wc),allowSubframe:target!==frame});
  await service.scan(wc,'a'.repeat(32));
  assert.deepEqual(service.snapshot(wc).blockedFrameOrigins,['https://forms.example.test']);
  assert.equal(service.snapshot(wc).scanSummary.framesFailed,1);
  await service.scan(wc,'a'.repeat(32),['https://forms.example.test']);
  assert.deepEqual(service.snapshot(wc).blockedFrameOrigins,[]);
  assert.equal(service.snapshot(wc).scanSummary.framesScanned,2);
});

test('scan-in-progress is visible even before the first frame finishes',async()=>{
  let finish;
  const harness=scanOnlyService({scanResult:()=>new Promise(resolve=>{finish=resolve;})});
  const route=serviceRoute(harness.frame,harness.wc);
  const running=harness.service.scan(harness.wc,'a'.repeat(32));
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(harness.service.snapshot(harness.wc).scanState,'scanning');
  finish(scanOutcome(route));
  await running;
  assert.equal(harness.service.snapshot(harness.wc).scanState,'complete');
});
