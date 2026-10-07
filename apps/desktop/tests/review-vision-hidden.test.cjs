const {test}=require('node:test');
const assert=require('node:assert/strict');
const { _electron }=require('playwright');
const path=require('node:path');

test('hidden Electron captures distinct segments, masks sensitive UI and restores scroll',{timeout:30000},async t=>{
  const environment={...process.env};delete environment.ELECTRON_RUN_AS_NODE;
  const app=await _electron.launch({executablePath:require('electron'),args:[path.join(__dirname,'review-vision-fixture.cjs')],env:environment});
  t.after(()=>app.close());
  const result=await app.evaluate(async()=>{
    const {BrowserWindow,captureReviewImages,waitForRenderedSurface}=globalThis.captureFixture;
    const win=new BrowserWindow({show:false,width:900,height:640,webPreferences:{offscreen:true,backgroundThrottling:false}});
    const wc=win.webContents;
    let paints=0,lastPaint=0,captures=0;
    wc.on('paint',(_event,_rect,image)=>{if(!image.isEmpty()){paints++;lastPaint=Date.now();}});
    try{
      await wc.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent('<!doctype html><style>body{margin:0}section{height:700px;background:#3574ea}section+section{background:#00aa66}section+section+section{background:#ef6633}</style><input value="private"><p>person@example.test 13912345678</p><section>Software engineer 当前状态: 笔试</section><section>Algorithm engineer 当前状态: 面试</section><section>Footer</section>'));
      await wc.executeJavaScript('window.scrollTo(0,137)');
      const readiness=await waitForRenderedSurface(wc);
      let masks=0;
      const capture=wc.capturePage.bind(wc);
      wc.capturePage=async()=>{
        masks=Math.max(masks,await wc.executeJavaScript('Array.from(document.querySelectorAll("html>div")).filter(e=>e.style.zIndex==="2147483647").length'));
        try{return await capture();}
        catch(error){throw new Error(`${error.message}; capture=${captures},paintCount=${paints},paintAge=${lastPaint?Date.now()-lastPaint:'never'},painting=${wc.isPainting()}`);}
        finally{captures++;}
      };
      const images=await captureReviewImages(wc,wc.getURL(),Date.now()+15000);
      const restored=await wc.executeJavaScript('({scroll:scrollY,masks:Array.from(document.querySelectorAll("html>div")).filter(e=>e.style.zIndex==="2147483647").length})');
      return {count:images.images.length,distinct:new Set(images.images).size,masks,restored,coverage:images.coverage,visible:win.isVisible(),readiness,paints};
    }finally{win.destroy();}
  });
  assert.equal(result.visible,false);
  assert.equal(result.readiness.document.readyState,'complete');
  assert.equal(result.readiness.document.scroll,137);
  assert.ok(result.readiness.frame.width>=result.readiness.document.width);
  assert.ok(result.readiness.frame.height>=result.readiness.document.height);
  assert.ok(result.paints>0,'hidden renderer must produce a real non-empty frame');
  assert.ok(result.count>=3&&result.count<=4);
  assert.ok(result.distinct>=3,'must capture real, distinct page content');
  assert.ok(result.masks>=2,'input and account text must be covered');
  assert.equal(result.restored.scroll,137);
  assert.equal(result.restored.masks,0);
  assert.equal(result.coverage.truncated,false);
});

test('hidden Electron captures the internal list bottom, with real third-card date and restored offsets',{timeout:30000},async t=>{
  const environment={...process.env};delete environment.ELECTRON_RUN_AS_NODE;
  const app=await _electron.launch({executablePath:require('electron'),args:[path.join(__dirname,'review-vision-fixture.cjs')],env:environment});
  t.after(()=>app.close());
  const result=await app.evaluate(async()=>{
    const {BrowserWindow,captureReviewImages,waitForRenderedSurface,internalListFixtureHtml}=globalThis.captureFixture;
    const win=new BrowserWindow({show:false,width:900,height:640,webPreferences:{offscreen:true,backgroundThrottling:false}}),wc=win.webContents;
    let paints=0;wc.on('paint',(_event,_rect,image)=>{if(!image.isEmpty())paints++;});
    try{
      await wc.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent(internalListFixtureHtml()));
      await wc.executeJavaScript('document.querySelector("#records").scrollTop=93');
      await waitForRenderedSurface(wc);
      const visible=[];let masks=0;
      const capture=wc.capturePage.bind(wc);
      wc.capturePage=async()=>{
        visible.push(await wc.executeJavaScript(`(()=>{const pane=document.querySelector('#records'),card=document.querySelector('#last-card'),date=document.querySelector('#last-date'),p=pane.getBoundingClientRect(),c=card.getBoundingClientRect(),d=date.getBoundingClientRect();return {offset:pane.scrollTop,titleVisible:c.top<p.bottom&&c.top+35>p.top,dateVisible:d.top>=p.top&&d.bottom<=p.bottom,date:date.textContent,navOffset:document.querySelector('nav').scrollTop}})()`));
        masks=Math.max(masks,await wc.executeJavaScript('Array.from(document.querySelectorAll("html>div")).filter(e=>e.style.zIndex==="2147483647").length'));
        return capture();
      };
      const captured=await captureReviewImages(wc,wc.getURL(),Date.now()+10000);
      const restored=await wc.executeJavaScript('({document:scrollY,container:document.querySelector("#records").scrollTop,masks:Array.from(document.querySelectorAll("html>div")).filter(e=>e.style.zIndex==="2147483647").length})');
      wc.capturePage=async()=>{throw new Error('fixture_capture_failure');};
      let failure='';try{await captureReviewImages(wc,wc.getURL(),Date.now()+5000);}catch(error){failure=error.message;}
      const afterFailure=await wc.executeJavaScript('({document:scrollY,container:document.querySelector("#records").scrollTop,masks:Array.from(document.querySelectorAll("html>div")).filter(e=>e.style.zIndex==="2147483647").length})');
      await wc.executeJavaScript('document.querySelector("#last-card").style.height="4000px"');
      wc.capturePage=capture;
      const limited=await captureReviewImages(wc,wc.getURL(),Date.now()+5000);
      const afterLimit=await wc.executeJavaScript('document.querySelector("#records").scrollTop');
      wc.capturePage=async()=>{await wc.loadURL('data:text/html,<h1>Replacement route</h1>');await waitForRenderedSurface(wc);return capture();};
      let navigation='';try{await captureReviewImages(wc,wc.getURL(),Date.now()+5000);}catch(error){navigation=error.message;}
      return {count:captured.images.length,distinct:new Set(captured.images).size,coverage:captured.coverage,visible,masks,restored,afterFailure,failure,limitCount:limited.images.length,limitCoverage:limited.coverage,afterLimit,navigation,paints,windowVisible:win.isVisible()};
    }finally{win.destroy();}
  });
  assert.equal(result.windowVisible,false);
  assert.ok(result.paints>0);
  assert.ok(result.count>=2&&result.count<=4);
  assert.ok(result.distinct>=2,'internal scroll must produce different real bitmaps');
  assert.equal(result.visible[0].titleVisible,true,'last card title already appears in the first viewport');
  assert.equal(result.visible[0].dateVisible,false,'first viewport must reproduce the missing last-card date');
  assert.ok(result.visible.slice(1).some(v=>v.dateVisible&&v.date==='投递简历 2026-10-05'),'a subsequent real captured viewport contains the last-card date');
  assert.ok(result.visible.every(v=>v.navOffset===0),'navigation scroller must never move');
  assert.ok(result.masks>=2);
  assert.equal(result.coverage.scroll_surface,'application_container');
  assert.equal(result.coverage.target_bottom_reached,true);
  assert.equal(result.coverage.truncated,false);
  assert.equal(result.coverage.cards_complete,false,'reaching the scroll bottom is not proof of complete model extraction');
  assert.ok(result.coverage.target_height>result.coverage.document_height);
  assert.deepEqual(result.coverage.per_segment_offsets.map(v=>v.container_y),result.visible.map(v=>v.offset));
  assert.deepEqual(result.restored,{document:0,container:93,masks:0});
  assert.equal(result.failure,'fixture_capture_failure');
  assert.deepEqual(result.afterFailure,result.restored);
  assert.equal(result.limitCount,4);
  assert.equal(result.limitCoverage.truncated,true);
  assert.equal(result.afterLimit,93);
  assert.equal(result.navigation,'browser_navigation_changed');
  assert.equal(JSON.stringify(result).includes('data:image'),false,'diagnostic output must not retain screenshots');
});

test('hidden Electron never explores navigation, forms or invisible internal scrollers',{timeout:30000},async t=>{
  const environment={...process.env};delete environment.ELECTRON_RUN_AS_NODE;
  const app=await _electron.launch({executablePath:require('electron'),args:[path.join(__dirname,'review-vision-fixture.cjs')],env:environment});
  t.after(()=>app.close());
  const results=await app.evaluate(async()=>{
    const {BrowserWindow,captureReviewImages,waitForRenderedSurface}=globalThis.captureFixture;
    const win=new BrowserWindow({show:false,width:900,height:640,webPreferences:{offscreen:true,backgroundThrottling:false}}),wc=win.webContents;
    const results=[];
    try{
      for(const mode of ['nav','aside','form','hidden','offscreen']){
        const tag=['nav','aside','form'].includes(mode)?mode:'div';
        const hidden=mode==='hidden'?'display:none;':mode==='offscreen'?'position:fixed;left:-2000px;':'';
        await wc.loadURL('data:text/html;charset=utf-8,'+encodeURIComponent(`<style>body{margin:0;height:100vh;overflow:hidden}#excluded{width:700px;height:400px;overflow:auto;${hidden}}</style><${tag} id="excluded"><article style="height:1400px">投递岗位一 软件工程师 投递岗位二 测试工程师</article></${tag}>`));
        await wc.executeJavaScript('document.querySelector("#excluded").scrollTop=37');
        await waitForRenderedSurface(wc);
        const captured=await captureReviewImages(wc,wc.getURL(),Date.now()+5000);
        results.push({mode,surface:captured.coverage.scroll_surface,offset:await wc.executeJavaScript('document.querySelector("#excluded").scrollTop')});
      }
      return results;
    }finally{win.destroy();}
  });
  assert.ok(results.every(r=>r.surface==='document'));
  assert.ok(results.filter(r=>r.mode!=='hidden').every(r=>r.offset===37),'ineligible visible or offscreen containers retain their positions');
});
