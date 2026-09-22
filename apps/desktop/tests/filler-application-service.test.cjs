const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const os=require('node:os');
const path=require('node:path');
const {FillerApplicationService,companyNameFromPageTitle}=require('../dist/filler-application-service');
const instanceId='a'.repeat(32);
const input={company:'Synthetic Robotics',title:'Platform Engineer',record_url:'https://ats.example/applications'};

function fixture(options={}) {
  let saved;
  const calls=[],refresh=[];
  const current={instanceId,tabId:7,generation:1,url:input.record_url};
  let connection={instanceId,origin:'http://127.0.0.1:19876',token:'fixture-token'};
  const store=options.store || {async load(){return saved && structuredClone(saved);},async save(value){saved=structuredClone(value);}};
  let handler=options.handler || (async()=>new Response(JSON.stringify({ok:true,application_id:'fixture',current_stage:'written'})));
  const make=()=>new FillerApplicationService(instanceId,()=>connection,()=>current,store,
    event=>refresh.push(event),async(url,init)=>{calls.push({url,init});return handler(url,init);},30,
    {'Synthetic Humanoid':'Synthetic Robotics'});
  return {make,service:make(),current,calls,refresh,store,
    connect(value){connection=value;}, handler(value){handler=value;}};
}

test('page title company draft only strips a recognized recruitment suffix',()=>{
  assert.equal(companyNameFromPageTitle('匿名公司校园招聘'),'匿名公司');
  assert.equal(companyNameFromPageTitle('匿名公司 - 社会招聘官网'),'匿名公司');
  assert.equal(companyNameFromPageTitle('校园招聘官网'),'');
  assert.equal(companyNameFromPageTitle('应届生招聘'),'');
  assert.equal(companyNameFromPageTitle('我的投递'),'');
  assert.equal(companyNameFromPageTitle(undefined),'');
});

test('explicit foreground confirmation, detail URL rejection and unknown URL confirmation',async()=>{
  const f=fixture();
  await assert.rejects(f.service.register(f.current,input,false),/confirmation_required/);
  await assert.rejects(f.service.register({...f.current,tabId:9},input,true),/foreground_changed/);
  await assert.rejects(f.service.register({...f.current,generation:0},input,true),/foreground_changed/);
  for(const record_url of ['https://ats.example/jobs/1','https://ats.example/#/job/1','https://ats.example/%6aob/1']) {
    await assert.rejects(f.service.register(f.current,{...input,record_url,progress_url_confirmed:true},true),/job_detail/);
  }
  await assert.rejects(f.service.register(f.current,{...input,record_url:'https://ats.example/me'},true),/confirmation_required/);
  const result=await f.service.register(f.current,{...input,record_url:'https://ats.example/me',progress_url_confirmed:true},true);
  assert.equal(result.queued,false);
  assert.equal(f.calls.length,1);
  assert.equal(f.calls[0].init.headers['X-RecruitOps-Instance-Id'],instanceId);
  assert.deepEqual(f.refresh,[{applications:true,counts:true,preservePosition:true}]);
});

test('registration posts distinct same-page jobs directly and replays stable job identity',async()=>{
  const f=fixture({handler:async(_url,init)=>{
    const body=JSON.parse(init.body);
    return new Response(JSON.stringify({ok:true,application_id:body.job_id,current_stage:'applied'}));
  }});
  const firstInput={...input,job_id:'job-1001'};
  const secondInput={...input,job_id:'job-1002'};
  const first=await f.service.register(f.current,firstInput,true);
  const second=await f.service.register(f.current,secondInput,true);
  const replay=await f.service.register(f.current,firstInput,true);

  assert.notEqual(first.id,second.id);
  assert.equal(replay.id,first.id);
  assert.deepEqual(f.calls.map(call=>call.init.method),['POST','POST','POST']);
  assert.ok(f.calls.every(call=>call.url.endsWith('/application')));
  assert.deepEqual(f.calls.map(call=>JSON.parse(call.init.body).job_id),
    ['job-1001','job-1002','job-1001']);
  assert.ok(f.calls.every(call=>call.init.headers['X-RecruitOps-Instance-Id']===instanceId));
});

test('Feishu application-list routes allow different roles but never authorize job detail URLs',async()=>{
  const f=fixture();
  for(const record_url of ['https://tenant.jobs.feishu.cn/704852/position/application',
    'https://tenant.jobs.feishu.cn/704852/position/application/','https://ats.example/#/position/application']) {
    for(const title of ['Client Engineer','Server Engineer']) {
      assert.equal((await f.service.register(f.current,{...input,title,record_url},true)).queued,false);
    }
  }
  assert.equal(f.calls.length,6);
  assert.ok(f.calls.every(call=>call.init.method==='POST'));
  for(const record_url of ['https://tenant.jobs.feishu.cn/704852/position/application/123',
    'https://ats.example/jobs/123#/position/application','https://ats.example/position/applications-extra']) {
    await assert.rejects(f.service.register(f.current,{...input,record_url,progress_url_confirmed:true},true),/job_detail/);
  }
});

test('batch registration writes each selected role directly and keeps queued failures explicit',async()=>{
  const f=fixture({handler:async(_url,init)=>JSON.parse(init.body).title==='Second'
    ?new Response('{}',{status:503}):new Response('{"ok":true,"application_id":"fixture"}')});
  const results=await f.service.registerBatch({...f.current},['First','Second','Third'].map(title=>({...input,title})),true);
  assert.deepEqual(results.map(item=>item.status),['saved','queued','saved']);
  assert.deepEqual(results.map(item=>item.applicationId),['fixture',undefined,'fixture']);
  assert.equal(results[1].error,'http_503');
  assert.deepEqual(f.calls.map(call=>[call.init.method,JSON.parse(call.init.body).title]),[['POST','First'],['POST','Second'],['POST','Third']]);
  assert.deepEqual((await f.service.pending()).map(item=>item.registration.title),['Second']);
});

test('one page-bound observation can sync confirmed statuses for several saved applications',async()=>{
  const ids=['application-one','application-two'];
  const f=fixture({handler:async(url,init)=>{
    assert.ok(url.endsWith('/sync-local-observations'));
    const body=JSON.parse(init.body);
    assert.deepEqual(body.application_ids,ids);
    assert.equal(body.page_url,input.record_url);
    return new Response(JSON.stringify({results:ids.map(application_id=>({application_id,success:true}))}));
  }});
  const results=await f.service.syncObservations(f.current,ids,{protocol_version:1});
  assert.deepEqual(results.map(item=>item.success),[true,true]);
  assert.equal(f.refresh.length,1);
  await assert.rejects(f.service.syncObservations(f.current,[ids[0],ids[0]],{}),/observation_required/);
});

test('batch validates all selected roles before writes and rejects empty, duplicate or unconfirmed selection',async()=>{
  const f=fixture();
  for(const values of [[],[input,input],[input,{...input,title:''}],Array.from({length:51},(_,i)=>({...input,title:String(i)}))])
    await assert.rejects(f.service.registerBatch({...f.current},values,true));
  await assert.rejects(f.service.registerBatch({...f.current},[input],false),/confirmation/);
  assert.equal(f.calls.length,0);assert.equal((await f.service.pending()).length,0);
});

test('batch stops further writes after navigation and never reports unsent records as saved',async()=>{
  const f=fixture({handler:async()=>{f.current.generation++;return new Response('{"ok":true,"application_id":"fixture"}');}});
  const results=await f.service.registerBatch({...f.current},['First','Second','Third'].map(title=>({...input,title})),true);
  assert.deepEqual(results.map(item=>item.status),['saved','failed','failed']);
  assert.equal(results[1].error,'foreground_changed');assert.equal(f.calls.length,1);
});

test('mapped company and per-job exact candidates never automatically bind similar jobs',async()=>{
  const f=fixture({handler:async()=>new Response(JSON.stringify({items:[
    {id:'target',company:input.company,title:input.title},
    {id:'other-company',company:'Other',title:input.title},
    {id:'other-role',company:input.company,title:'Data Engineer'},
  ]}))});
  const candidates=await f.service.candidates(f.current,[{company:'Synthetic Humanoid',title:input.title},
    {company:input.company,title:'Data Engineer'}]);
  assert.deepEqual(candidates.map(c=>c.existing.map(r=>r.id)),[['target'],['other-role']]);
  assert.equal(f.calls[0].init.method,'GET');
  assert.equal((await f.service.pending()).length,0);
});

test('navigation while loading candidates invalidates response',async()=>{
  const f=fixture({handler:async()=>{f.current.generation++;return new Response('{"items":[]}');}});
  await assert.rejects(f.service.candidates({...f.current},[input]),/foreground_changed/);
});

test('durable registration survives restart and response loss, retry has stable identity',async(t)=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'filler-queue-'));
  t.after(()=>fs.rm(root,{recursive:true,force:true}));
  const file=path.join(root,'queue.json');
  const store={async load(){try{return JSON.parse(await fs.readFile(file,'utf8'));}catch(e){if(e.code==='ENOENT')return;throw e;}},
    async save(value){await fs.writeFile(file+'.tmp',JSON.stringify(value));await fs.rename(file+'.tmp',file);}};
  const written=new Map();
  let lose=true;
  const f=fixture({store,handler:async(_url,init)=>{
    const body=JSON.parse(init.body),key=body.company+'\n'+body.title;
    written.set(key,{...body,stage:'written'});
    if(lose){lose=false;throw new Error('network secret must not be retained');}
    return new Response(JSON.stringify({ok:true,application_id:'same',current_stage:'written'}));
  }});
  const first=await f.service.register(f.current,input,true);
  assert.equal(first.queued,true);
  const restarted=f.make();
  const [pending]=await restarted.pending();
  assert.equal(pending.id,first.id);
  assert.equal(pending.attempts,1);
  assert.equal(pending.error,'transport_or_storage_failed');
  const result=await restarted.retry([pending.id]);
  assert.equal(result[0].queued,false);
  assert.equal(written.size,1);
  assert.deepEqual(JSON.parse(f.calls[0].init.body),JSON.parse(f.calls[1].init.body));
  assert.deepEqual(await restarted.pending(),[]);
  assert.equal((await fs.readFile(file,'utf8')).includes('fixture-token'),false);
});

test('offline queue is explicit, bounded, editable and cancellable',async()=>{
  const f=fixture();f.connect(undefined);
  const first=await f.service.register(f.current,input,true);
  assert.equal(first.error,'offline');
  assert.equal(f.calls.length,0);
  await f.service.retry([first.id]);await f.service.retry([first.id]);
  assert.equal((await f.service.retry([first.id]))[0].error,'retry_limit');
  assert.equal((await f.service.pending())[0].attempts,3);
  await assert.rejects(f.service.correct(first.id,input,false),/confirmation_required/);
  await f.service.correct(first.id,{...input,record_url:'https://ats.example/progress'},true);
  assert.equal((await f.service.pending())[0].attempts,0);
  await f.service.cancel(first.id);
  assert.deepEqual(await f.service.pending(),[]);
});

test('corrected response-loss queue binds committed identity before replay and retries only selected item',async()=>{
  const f=fixture();f.connect(undefined);
  const first=await f.service.register(f.current,{...input,job_id:'job-one'},true);
  const second=await f.service.register(f.current,{...input,title:'Other Engineer',job_id:'job-other'},true);
  await f.service.correctLink(first.id,'https://ats.example/progress','City',true);
  assert.equal((await f.service.pending()).find(x=>x.id===first.id).registration.title,input.title);
  f.connect({instanceId,origin:'http://127.0.0.1:19876',token:'fixture'});
  f.handler(async(_url,init)=>init.method==='GET'
    ? new Response(JSON.stringify({items:[
      {id:'wrong-job',company:input.company,title:input.title,job_id:'job-two'},
      {id:'already-committed',company:input.company,title:input.title,job_id:'job-one'},
    ]}))
    : new Response(JSON.stringify({ok:true,application_id:'already-committed',current_stage:'written'})));
  const result=await f.service.retry([first.id]);
  assert.equal(result[0].queued,false);
  const posted=JSON.parse(f.calls.at(-1).init.body);
  assert.equal(posted.application_id,'already-committed');
  assert.equal(posted.job_id,'job-one');
  assert.equal(posted.record_url,'https://ats.example/progress');
  assert.equal(posted.city,'City');
  const remaining=await f.service.pending();
  assert.deepEqual(remaining.map(x=>x.id),[second.id]);
  assert.equal(remaining[0].attempts,1);
});

test('ambiguous reconciliation never writes and correction cannot change company or role',async()=>{
  const f=fixture();f.connect(undefined);
  const first=await f.service.register(f.current,input,true);
  await assert.rejects(f.service.correct(first.id,{...input,title:'Different'},true),/pending_identity_immutable/);
  await assert.rejects(f.service.correctLink(first.id,'https://ats.example/progress','City',false),/confirmation_required/);
  await f.service.correctLink(first.id,'https://ats.example/progress',undefined,true);
  f.connect({instanceId,origin:'http://127.0.0.1:19876',token:'fixture'});
  f.handler(async()=>new Response(JSON.stringify({items:[{id:'a',company:input.company,title:input.title},{id:'b',company:input.company,title:input.title}]})));
  const [result]=await f.service.retry([first.id]);
  assert.equal(result.error,'pending_identity_ambiguous');
  assert.ok(f.calls.every(call=>call.init.method==='GET'));
  assert.equal((await f.service.pending()).length,1);
});

test('instance switch or forbidden connection never dispatches or migrates queued entries',async()=>{
  for(const connection of [
    {instanceId:'b'.repeat(32),origin:'http://127.0.0.1:19876',token:'fixture'},
    {instanceId,origin:'http://127.0.0.1:8012',token:'fixture'},
    {instanceId,origin:'https://foreign.example',token:'fixture'},
  ]) {
    const f=fixture();f.connect(connection);
    const result=await f.service.register(f.current,input,true);
    assert.equal(result.queued,true);assert.equal(f.calls.length,0);
    assert.equal((await f.service.pending())[0].instanceId,instanceId);
  }
  const f=fixture({store:{async load(){return {instanceId:'b'.repeat(32),items:[]};},async save(){assert.fail();}}});
  await assert.rejects(f.service.pending(),/queue_instance_mismatch/);
});

test('partial replay retains conflicts and 401 stops remaining batch without losing entries',async()=>{
  const f=fixture();f.connect(undefined);
  const a=await f.service.register(f.current,input,true);
  const b=await f.service.register(f.current,{...input,title:'Data Engineer'},true);
  f.connect({instanceId,origin:'http://127.0.0.1:19876',token:'fixture'});
  f.handler(async(_url,init)=>JSON.parse(init.body).title===input.title ?
    new Response('{"ok":true,"application_id":"one"}') : new Response('{}',{status:409}));
  const result=await f.service.retry([a.id,b.id]);
  assert.deepEqual(result.map(r=>r.queued),[false,true]);
  assert.equal((await f.service.pending())[0].error,'http_409');
  const c=await f.service.register(f.current,{...input,title:'Third Engineer'},true);
  f.handler(async()=>new Response('{}',{status:401}));
  const before=f.calls.length;
  await f.service.retry([b.id,c.id]);
  assert.equal(f.calls.length,before+1);
  assert.equal((await f.service.pending()).length,2);
});

test('sync forwards only observation binding, never queues arbitrary evidence or stages',async()=>{
  const f=fixture({handler:async()=>new Response('{"success":true,"status":"unchanged"}')});
  await f.service.sync(f.current,'target','persisted-observation');
  assert.deepEqual(JSON.parse(f.calls[0].init.body),{application_id:'target',observation_operation_id:'persisted-observation',page_url:input.record_url});
  assert.equal(f.refresh.length,1);
  f.connect(undefined);
  await assert.rejects(f.service.sync(f.current,'target','old-observation'),/offline/);
  assert.deepEqual(await f.service.pending(),[]);
});

test('desktop normalized observation sync is page-bound and contains no caller stage',async()=>{
  const f=fixture({handler:async()=>new Response('{"success":true,"status":"unchanged"}')});
  const observation={protocol_version:1,type:'result',operation_id:'owned-one',status:'SUCCEEDED',result:{page_url:input.record_url}};
  await f.service.syncObservation(f.current,'target',observation);
  assert.match(f.calls[0].url,/sync-local-observation$/);
  assert.deepEqual(JSON.parse(f.calls[0].init.body),{application_id:'target',page_url:input.record_url,observation});
  assert.equal(JSON.parse(f.calls[0].init.body).stage,undefined);
  await assert.rejects(f.service.syncObservation({...f.current,generation:2},'target',observation),/foreground_changed/);
});

test('request timeout retains write-ahead record and stable registration while concurrent action fails',async()=>{
  const f=fixture({handler:()=>new Promise(()=>{})});
  const operation=f.service.register(f.current,input,true);
  await new Promise(resolve=>setImmediate(resolve));
  await assert.rejects(f.service.register(f.current,input,true),/busy/);
  const result=await operation;
  assert.equal(result.error,'request_timeout');
  assert.equal((await f.service.pending()).length,1);
  assert.equal(f.calls[0].init.signal.aborted,true);
});
