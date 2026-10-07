const {test}=require('node:test');
const assert=require('node:assert/strict');
const {EventEmitter}=require('node:events');
const {captureReviewImages,attachReviewVision,canCaptureReview}=require('../dist/review-vision');
const {captureReviewIdentity,bindReviewObservation}=require('../dist/review-observation-binding');
const {ReviewPageCache}=require('../dist/review-page-cache');
const {ReviewNavigationPolicy}=require('../dist/review-readiness');
const observation={status:'SUCCEEDED',result:{page:{text:'Software engineer 当前状态: 面试'},diagnostics:{}}};
function fixture(height=1500){
  let url='https://ats.example/applications',scroll=42;
  const actions=[];
  const wc=Object.assign(new EventEmitter(),{isDestroyed:()=>false,isLoadingMainFrame:()=>false,getURL:()=>url,
    executeJavaScriptInIsolatedWorld:async(world,scripts)=>{
      assert.equal(world,1005);
      const [,action,value]=scripts[0].code.match(/\("(prepare|scroll|sample|restore)",(\d+)\)$/);
      actions.push(action);
      if(action==='restore'){scroll=42;return true;}
      if(action!=='sample')scroll=Math.min(+value,height-600);
      return {height,viewport:600,width:900,y:scroll,truncatedPrivacyScan:false};
    },capturePage:async()=>({isEmpty:()=>false,toJPEG:()=>Buffer.from('test-jpeg')})});
  return {wc,actions,observation:bindReviewObservation({...observation},captureReviewIdentity(wc)),
    scroll:()=>scroll,navigate:()=>{url='https://ats.example/other';}};
}
test('bounded segmented capture restores scroll even on failure',async()=>{
  const f=fixture(9000);
  const capture=await captureReviewImages(f.wc,f.wc.getURL(),Date.now()+3000);
  assert.equal(capture.images.length,4);
  assert.equal(capture.coverage.truncated,true);
  assert.equal(capture.coverage.pagination_followed,false);
  assert.equal(f.scroll(),42);
  f.wc.capturePage=async()=>{throw new Error('capture failed');};
  await assert.rejects(captureReviewImages(f.wc,f.wc.getURL(),Date.now()+2000),/capture failed/);
  assert.equal(f.actions.at(-1),'restore');
});
test('internal-list coverage records actual offsets, not a short document as complete cards',async()=>{
  const f=fixture(600);let containerY=93;
  f.wc.executeJavaScriptInIsolatedWorld=async(world,scripts)=>{
    assert.equal(world,1005);
    const [,action,value]=scripts[0].code.match(/\("(prepare|scroll|sample|restore)",(\d+)\)$/);
    f.actions.push(action);
    if(action==='restore'){containerY=93;return true;}
    if(action!=='sample')containerY=Math.min(+value,9000-480);
    return {height:600,viewport:600,width:900,y:0,truncatedPrivacyScan:false,
      scrollSurface:'application_container',targetHeight:9000,targetViewport:480,targetY:containerY,
      containerY,containerTop:80,containerBottom:560};
  };
  const result=await captureReviewImages(f.wc,f.wc.getURL(),Date.now()+3000);
  assert.equal(result.images.length,4);
  assert.equal(result.coverage.scroll_surface,'application_container');
  assert.equal(result.coverage.document_height,600);
  assert.equal(result.coverage.target_height,9000);
  assert.equal(result.coverage.truncated,true);
  assert.equal(result.coverage.target_bottom_reached,false);
  assert.equal(result.coverage.cards_complete,false);
  assert.deepEqual(result.coverage.per_segment_offsets.map(s=>s.container_y),[0,400,800,1200]);
  assert.equal(containerY,93);
});
test('capture-size and elapsed-deadline failures restore the original surface',async()=>{
  for(const mode of ['size','deadline']){
    const f=fixture(600);
    if(mode==='size')f.wc.capturePage=async()=>({isEmpty:()=>false,getSize:()=>({width:9000,height:600}),toJPEG:()=>Buffer.from('x')});
    else f.wc.capturePage=async()=>{await new Promise(resolve=>setTimeout(resolve,150));return {isEmpty:()=>false,toJPEG:()=>Buffer.from('x')};};
    await assert.rejects(captureReviewImages(f.wc,f.wc.getURL(),Date.now()+(mode==='size'?2000:200)),/vision_image_too_large|browser_observation_timeout/);
    assert.equal(f.actions.at(-1),'restore');
    assert.equal(f.scroll(),42);
  }
});
test('navigation immediately after a scroll prevents capture of the replacement document',async()=>{
  const f=fixture(600);let captures=0;
  const execute=f.wc.executeJavaScriptInIsolatedWorld;
  f.wc.executeJavaScriptInIsolatedWorld=async(...args)=>{
    const result=await execute(...args);
    if(f.actions.at(-1)==='scroll')f.navigate();
    return result;
  };
  f.wc.capturePage=async()=>{captures++;return {isEmpty:()=>false,toJPEG:()=>Buffer.from('x')};};
  await assert.rejects(captureReviewImages(f.wc,f.wc.getURL(),Date.now()+2000),/browser_navigation_changed/);
  assert.equal(captures,0);
  assert.notEqual(f.actions.at(-1),'restore','never restore scroll into a new document');
});
test('a pending bitmap cannot outlive the absolute deadline or cancellation',async()=>{
  for(const mode of ['deadline','cancel']){
    const f=fixture(600),controller=new AbortController();
    f.wc.capturePage=()=>{
      if(mode==='cancel')setTimeout(()=>controller.abort(),20);
      return new Promise(()=>{});
    };
    const started=Date.now();
    await assert.rejects(captureReviewImages(f.wc,f.wc.getURL(),started+(mode==='deadline'?200:2000),controller.signal),/browser_observation_timeout|browser_cancelled/);
    assert.ok(Date.now()-started<800);
    assert.equal(f.actions.at(-1),'restore');
    assert.equal(f.scroll(),42);
  }
});
test('login, blank and restricted frames cannot trigger capture',()=>{
  for(const data of [{page:{text:'请先登录'}},{page:{text:''}},
    {...observation.result,diagnostics:{scopeDeniedFrameCount:1}},
    {...observation.result,requires_user_action:true}])assert.equal(canCaptureReview({...observation,result:data}),false);
  assert.equal(canCaptureReview(observation),true);
});
test('one request sends all segments and preserves exact persisted vision payload',async()=>{
  const f=fixture();let calls=0;
  const reading={text:'Software engineer 当前状态: 面试',confidence:.95,model:'deepseek-flash',image_sha256:'a'.repeat(64),usage:{total_tokens:20}};
  const result=await attachReviewVision(f.wc,f.observation,'operation','https://ats.example/applications',
    'http://127.0.0.1:1234','Bearer test',Date.now()+3000,undefined,async(url,request)=>{
      calls++;assert.equal(url,'http://127.0.0.1:1234/api/browser/vision');
      assert.equal(request.redirect,'error');
      assert.equal(JSON.parse(request.body).image_data_urls.length,3);
      return {ok:true,json:async()=>reading};
    });
  assert.equal(calls,1);assert.deepEqual(result.result.vision,reading);
  assert.equal(result.result.vision_capture.truncated,false);
  assert.ok(!JSON.stringify(result).includes('test-jpeg'));
});
test('model failures preserve DOM with a safe separate diagnostic',async()=>{
  const f=fixture(600);
  const result=await attachReviewVision(f.wc,f.observation,'op',f.wc.getURL(),'http://127.0.0.1:1234','Bearer test',Date.now()+2000,
    undefined,async()=>({ok:false,json:async()=>({detail:{code:'http_503'}})}));
  assert.equal(result.status,'SUCCEEDED');assert.equal(result.result.vision_error,'http_503');
  assert.deepEqual(result.result.page,observation.result.page);
});
test('cancel or navigation invalidates capture, never uploads a different page',async()=>{
  for(const mode of ['cancel','navigate']){
    const f=fixture(600),controller=new AbortController();let calls=0;
    f.wc.capturePage=async()=>{mode==='cancel'?controller.abort():f.navigate();return {isEmpty:()=>false,toJPEG:()=>Buffer.from('x')};};
    await assert.rejects(attachReviewVision(f.wc,f.observation,'op',f.wc.getURL(),'http://127.0.0.1:1','Bearer test',Date.now()+2000,
      controller.signal,async()=>{calls++;}),/browser_cancelled|browser_navigation_changed/);
    assert.equal(calls,0);
  }
});

test('DOM provenance rejects missing binding, route changes and same-URL reloads before upload',async()=>{
  for(const mode of ['unbound','route','reload']){
    const f=fixture(600);let calls=0;
    if(mode==='route')f.navigate();
    if(mode==='reload')f.wc.emit('did-start-navigation',{},f.wc.getURL(),false,true);
    await assert.rejects(attachReviewVision(f.wc,mode==='unbound'?observation:f.observation,'op',f.wc.getURL(),
      'http://127.0.0.1:1','Bearer test',Date.now()+1000,undefined,async()=>{calls++;}),/browser_navigation_changed/);
    assert.equal(calls,0);
  }
});
test('navigation or cancel during final restore prevents the upload',async()=>{
  for(const mode of ['navigation','cancel']){
    const f=fixture(600),controller=new AbortController();let calls=0;
    const execute=f.wc.executeJavaScriptInIsolatedWorld;
    f.wc.executeJavaScriptInIsolatedWorld=async(...args)=>{
      const result=await execute(...args);
      if(f.actions.at(-1)==='restore')mode==='cancel'?controller.abort():f.navigate();
      return result;
    };
    await assert.rejects(attachReviewVision(f.wc,f.observation,'op',f.wc.getURL(),'http://127.0.0.1:1','Bearer test',Date.now()+2000,
      controller.signal,async()=>{calls++;}),/browser_navigation_changed|browser_cancelled/);
    assert.equal(calls,0);
  }
});

test('same-URL reload during image capture remains navigation failure, not a model error',async()=>{
  const f=fixture(600);let uploads=0;
  f.wc.capturePage=async()=>{
    f.wc.emit('did-start-navigation',{},f.wc.getURL(),false,true);
    return {isEmpty:()=>false,toJPEG:()=>Buffer.from('obsolete-image')};
  };
  await assert.rejects(attachReviewVision(f.wc,f.observation,'op',f.wc.getURL(),
    'http://127.0.0.1:1','Bearer test',Date.now()+2000,undefined,async()=>{uploads++;}),/browser_navigation_changed/);
  assert.equal(uploads,0);
});
test('account change during bitmap capture prevents upload; change after upload aborts without a second request',async()=>{
  for (const stage of ['pixels','request']) {
    const f=fixture(600),cache=new ReviewPageCache(),session={}; let uploads=0;
    const watch=cache.watchReusedProfile(session,f.wc.getURL(),new ReviewNavigationPolicy(f.wc.getURL()));
    const change=()=>cache.cookieChanged(session,'session_id',true,'ats.example');
    const capture=f.wc.capturePage;
    if(stage==='pixels')f.wc.capturePage=async()=>{change();return capture();};
    try {
      await assert.rejects(attachReviewVision(f.wc,f.observation,'account-race',f.wc.getURL(),
        'http://127.0.0.1:1','Bearer test',Date.now()+2000,watch.signal,async(_url,request)=>{
          uploads++;change();assert.equal(request.signal.aborted,true);
          throw new Error('request aborted');
        }),/browser_cancelled/);
      assert.equal(uploads,stage==='pixels'?0:1);
      assert.throws(watch.check,/browser_account_changed/);
    }finally{watch.dispose();}
  }
});
test('vision HTTP budget allows backend queue+provider but remains clipped to the parent deadline',async()=>{
  const timeout=AbortSignal.timeout, seen=[];
  AbortSignal.timeout=milliseconds=>{seen.push(milliseconds);return new AbortController().signal;};
  try {
    for(const budget of [70000,900]){
      const f=fixture(600),reading={text:'Software engineer 面试中',confidence:.9,model:'fixture',image_sha256:'b'.repeat(64)};
      const result=await attachReviewVision(f.wc,f.observation,'budget-op',f.wc.getURL(),
        'http://127.0.0.1:1','Bearer test',Date.now()+budget,undefined,async()=>({ok:true,json:async()=>reading}));
      assert.equal(result.result.vision.model,'fixture');
    }
    assert.equal(seen[0],67000); assert.ok(seen[1]>0&&seen[1]<900);
  }finally{AbortSignal.timeout=timeout;}
});
