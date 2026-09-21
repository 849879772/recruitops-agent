const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const { _electron } = require('playwright');

test('hidden real main reviews rAF, iframe and auth evidence without presenting a window', {timeout: 60000}, async t => {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'recruitops-review-anonymous-'));
  let foreignRequests=0;
  const foreignServer=http.createServer((_req,res)=>{foreignRequests++;res.end('foreign');});
  await new Promise(resolve=>foreignServer.listen(0,'127.0.0.1',resolve));
  const foreignOrigin=`http://127.0.0.1:${foreignServer.address().port}`;
  const oneRecordTable='<table><thead><tr><th>岗位名称</th><th>当前状态</th><th>投递日期</th></tr></thead>'+
    '<tbody><tr><td>测试开发工程师</td><td>申请成功</td><td>2026-09-14</td></tr></tbody></table>';
  const djiText='查询投递记录 请进行身份认证！ 填写个人信息 '+
    '请填写简历中的手机号码 手机号* +86 发送验证码使用邮箱验证';
  const server=http.createServer((req,res)=>{
    if (req.url==='/missing') { req.socket.destroy(); return; }
    if (req.url==='/normal-redirect') { res.writeHead(302,{Location:'/redirected-records'});res.end();return; }
    if (req.url==='/foreign-redirect') { res.writeHead(302,{Location:foreignOrigin+'/redirected'});res.end();return; }
    res.setHeader('Content-Type','text/html; charset=utf-8');
    if (req.url==='/helper-frame') { res.end('<!doctype html><title>Helper</title>');return; }
    if (req.url==='/candidate/applications/deliver-query/dji') {
      res.end(`<!doctype html><main></main><script>
        requestAnimationFrame(()=>document.querySelector('main').textContent=${JSON.stringify(djiText)});
      </script>`);return;
    }
    if (req.url==='/auth-frame') {
      res.end('<!doctype html><iframe src="/candidate/applications/deliver-query/dji" width="900" height="500"></iframe>');return;
    }
    if (req.url==='/top-auth' || req.url==='/captcha-auth') {
      res.end(`<!doctype html><main>${djiText}</main><iframe src="/raf-records" width="900" height="500"></iframe>`+
        (req.url==='/captcha-auth'?'<div data-captcha>Challenge</div>':''));return;
    }
    if (req.url==='/denied-frame') {
      res.end(`<!doctype html><iframe src="${foreignOrigin}/records" width="900" height="500"></iframe>`);return;
    }
    if (req.url==='/hidden-record-frame') {
      res.end('<!doctype html><iframe hidden src="/redirected-records"></iframe>');return;
    }
    if (req.url==='/raf-records') {
      res.end(`<!doctype html><main>Loading</main><script>
        requestAnimationFrame(()=>requestAnimationFrame(()=>document.querySelector('main').innerHTML=${JSON.stringify(oneRecordTable)}));
      </script>`);return;
    }
    if (req.url==='/real-iframe') {
      res.end('<!doctype html><iframe hidden src="/helper-frame"></iframe><iframe src="/raf-records" width="900" height="500"></iframe>');return;
    }
    if (req.url==='/704852/position/application') {
      res.end('<!doctype html><title>应聘记录 - 去哪儿旅行校园招聘</title><header><a>首页</a><a>校招岗位</a></header>'+
        '<article class="application-card"><h2 class="job-title">AI应用开发工程师（客户端开发）</h2><div>北京</div><div>投递简历</div><time>2026-09-15</time></article>');return;
    }
    if (req.url==='/iframe-loading' || req.url==='/iframe-never-ready') {
      res.end(`<!doctype html><title>Dynamic ATS</title><iframe hidden src="/helper-frame"></iframe>
        <iframe hidden src="/helper-frame"></iframe><main>Loading</main>
        ${req.url==='/iframe-loading' ? `<script>setTimeout(()=>document.querySelector('main').innerHTML=${JSON.stringify(oneRecordTable)},1400)</script>` : ''}`);
      return;
    }
    if (req.url==='/client-login-redirect' || req.url==='/client-record-redirect' || req.url==='/account.html') {
      const target=req.url==='/client-login-redirect'?'/login':req.url==='/account.html'?'/index.html':'/redirected-records';
      res.end(`<!doctype html><title>Session check</title><main>Loading</main><script>setTimeout(()=>location.replace(${JSON.stringify(target)}),350)</script>`);
      return;
    }
    if (req.url==='/login') { res.end('<!doctype html><title>Recruitment</title><main>欢迎登录<br>微信扫码登录/注册</main>');return; }
    if (req.url==='/index.html') { res.end('<!doctype html><title>Recruitment</title><main>应届生<br><span>登录/注册</span></main>');return; }
    if (req.url==='/redirected-records') {
      res.end(`<!doctype html><title>Redirect fixture</title>${oneRecordTable}`);
      return;
    }
    if (req.url==='/hash-change' || req.url==='/feishu-spa' || req.url==='/foreign-navigation') {
      const transition=req.url==='/hash-change'
        ? "location.hash='/applications'"
        : req.url==='/feishu-spa'
          ? "history.replaceState({},'',location.pathname+'#/app/application_center?tenant=anonymous')"
          : `location.href=${JSON.stringify(foreignOrigin+'/foreign')}`;
      res.end(`<!doctype html><title>Navigation fixture</title><main>Loading applications</main><script>
        setTimeout(()=>{${transition}},100);
        setTimeout(()=>{document.querySelector('main').innerHTML=${JSON.stringify(oneRecordTable)}},600);
      </script>`);
      return;
    }
    if (req.url==='/two-applications') {
      res.end('<!doctype html><title>匿名公司校园招聘</title><table><thead><tr><th>岗位名称</th><th>当前状态</th><th>投递日期</th></tr></thead>'+
        '<tbody><tr><td>测试开发工程师</td><td>申请成功</td><td>2026-09-14</td></tr>'+
        '<tr><td>具身模型部署工程师</td><td>暂不匹配</td><td>2026-09-14</td></tr></tbody></table>');
      return;
    }
    res.end(`<!doctype html><html><head><title>Application fixture</title></head><body>
      <script src="http://127.0.0.1:9/optional.js"></script><main>Loading applications</main>
      <script>setTimeout(()=>document.querySelector('main').innerHTML=
      '<table><thead><tr><th>岗位名称</th><th>当前状态</th><th>投递日期</th></tr></thead>'+
      '<tbody><tr><td>测试开发工程师</td><td>申请成功</td><td>2026-09-14</td></tr></tbody></table>',600)</script>
      </body></html>`);
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin=`http://127.0.0.1:${server.address().port}`;
  const env=Object.fromEntries(['SystemRoot','WINDIR','PATH','TEMP','TMP','COMSPEC','APPDATA','LOCALAPPDATA','USERPROFILE']
    .filter(key=>process.env[key]).map(key=>[key,process.env[key]]));
  let application;
  t.after(async()=>{
    if(application)await application.close();
    await new Promise(resolve=>server.close(resolve));
    await new Promise(resolve=>foreignServer.close(resolve));
    fs.rmSync(root,{recursive:true,force:true,maxRetries:5});
  });
  application=await _electron.launch({args:[path.join(__dirname,'review-hidden-fixture.cjs')],
    env:{...env,RECRUITOPS_DESKTOP_TEST:'1',RECRUITOPS_DESKTOP_DATA_DIR:root,RECRUITOPS_DESKTOP_FIXTURE_ORIGIN:origin,
      RECRUITOPS_DESKTOP_TEST_RUNTIME:'filler'}});
  const shell=await application.firstWindow();
  await shell.waitForLoadState();
  await shell.waitForFunction(async()=>!!(await window.desktop.state()).filler.capabilities.persistentProfile);
  const output=await application.evaluate(async({BrowserWindow},origin)=>{
    const stages=[];
    const result=await globalThis.reviewFixture.reviewPage(origin+'/applications','hidden-review',['fixture-app'],
      undefined,Date.now()+7000,stage=>stages.push(stage));
    return {result,stages,visible:BrowserWindow.getAllWindows().some(window=>window.isVisible())};
  },origin);
  assert.equal(output.visible,false);
  assert.equal(output.result.review_readiness,'records');
  assert.ok(output.stages.includes('WAITING_FOR_CONTENT'));
  assert.equal(output.result.result.application_records.length,1);
  assert.equal(output.result.result.application_records[0].title,'测试开发工程师');
  assert.equal(output.result.result.database_updated,false);
  for (const route of ['/raf-records','/real-iframe']) {
    const reviewed=await application.evaluate(async({BrowserWindow},{origin,route})=>{
      const pending=globalThis.reviewFixture.reviewPage(origin+route,'render-'+route.slice(1),['fixture-app'],undefined,Date.now()+3000)
        .catch(error=>{throw new Error(route+': '+error.message);});
      const reviewView=BrowserWindow.getAllWindows().flatMap(window=>window.contentView.children)
        .find(view=>view.webContents?.isOffscreen());
      const hostedHidden=!!reviewView&&!reviewView.getVisible();
      const reviewContents=reviewView?.webContents;
      const destroyed=reviewContents?new Promise(resolve=>reviewContents.once('destroyed',resolve)):Promise.resolve();
      const result=await pending;
      let cleanupTimer;
      try {await Promise.race([destroyed,new Promise((_,reject)=>{
        cleanupTimer=setTimeout(()=>reject(new Error('review renderer was not destroyed')),1500);
      })]);} finally {clearTimeout(cleanupTimer);}
      return {result,hostedHidden,closed:reviewContents?.isDestroyed(),
        visible:BrowserWindow.getAllWindows().some(window=>window.isVisible())};
    },{origin,route});
    assert.equal(reviewed.visible,false);
    assert.equal(reviewed.hostedHidden,true,'rAF must render in an attached, invisible offscreen view');
    assert.equal(reviewed.closed,true,'the operation must release its temporary renderer');
    assert.equal(reviewed.result.review_readiness,'records',route);
    assert.equal(reviewed.result.result.application_records[0].title,'测试开发工程师');
    if(route==='/real-iframe') {
      const row=reviewed.result.result.application_records[0];
      assert.ok(row.frameId>0);
      assert.equal(row.frameUrl,origin+'/raf-records');
      assert.equal(reviewed.result.result.page.page_url,origin+route);
    }
  }
  for (const route of ['/candidate/applications/deliver-query/dji','/auth-frame','/top-auth','/captcha-auth']) {
    const auth=await application.evaluate(async(_electron,{origin,route})=>{
      const stages=[],started=Date.now();
      const result=await globalThis.reviewFixture.reviewPage(origin+route,'auth-'+route.split('/').pop(),['fixture-app'],
        undefined,Date.now()+3000,stage=>stages.push(stage));
      return {result,elapsed:Date.now()-started,stages};
    },{origin,route});
    assert.equal(auth.result.error_code,route==='/captcha-auth'?'CAPTCHA_REQUIRED':'LOGIN_REQUIRED',route);
    assert.equal(auth.result.review_readiness,route==='/captcha-auth'?'terminal':'login_required',route);
    assert.ok(auth.elapsed<2500,`${route} must return the auth result without exhausting readiness`);
    assert.equal(auth.result.result.database_updated,false);
    assert.equal(auth.result.result.application_records?.length||0,0);
  }
  for (const route of ['/denied-frame','/hidden-record-frame']) {
    const failure=await application.evaluate(async(_electron,{origin,route})=>{
      try {await globalThis.reviewFixture.reviewPage(origin+route,'scope-'+route.slice(1),['fixture-app'],undefined,Date.now()+1100);}
      catch(error){return {code:error.message,summary:error.lastObservation};}
    },{origin,route});
    assert.equal(failure.code,'browser_readiness_timeout',route);
    assert.equal(failure.summary.recordCount,0);
    if(route==='/denied-frame') {
      assert.equal(failure.summary.pageState,'frame_scope_denied');
      assert.equal(failure.summary.skippedFrameCount,1);
    }
  }
  assert.equal(foreignRequests,0);
  const delayedFrames=await application.evaluate(async(_electron,origin)=>globalThis.reviewFixture.reviewPage(
    origin+'/iframe-loading','helper-iframe-wait',['fixture-app'],undefined,Date.now()+5000),origin);
  assert.equal(delayedFrames.review_readiness,'records');
  assert.equal(delayedFrames.result.diagnostics.iframeCount,2);
  assert.equal(delayedFrames.result.application_records.length,1);
  const neverReady=await application.evaluate(async(_electron,origin)=>{
    try { await globalThis.reviewFixture.reviewPage(origin+'/iframe-never-ready','iframe-deadline',['fixture-app'],undefined,Date.now()+1100); }
    catch(error){return {code:error.message,summary:error.lastObservation};}
  },origin);
  assert.equal(neverReady.code,'browser_readiness_timeout');
  assert.equal(neverReady.summary.iframeCount,2);
  assert.equal(neverReady.summary.pageState,'frame_unavailable');
  assert.equal(neverReady.summary.unavailableFrameCount,2);
  for(const route of ['/client-login-redirect','/account.html']) {
    const login=await application.evaluate(async(_electron,{origin,route})=>globalThis.reviewFixture.reviewPage(
      origin+route,'login-'+route.slice(1),['fixture-app'],undefined,Date.now()+3500),{origin,route});
    assert.equal(login.error_code,'LOGIN_REQUIRED',route);
    assert.equal(login.review_readiness,'login_required',route);
    assert.equal(login.result.database_updated,false);
  }
  const clientRedirect=await application.evaluate(async(_electron,origin)=>globalThis.reviewFixture.reviewPage(
    origin+'/client-record-redirect','client-record-redirect',['fixture-app'],undefined,Date.now()+3500),origin);
  assert.equal(clientRedirect.review_readiness,'records');
  const unbound=await application.evaluate(async(_electron,origin)=>globalThis.reviewFixture.reviewPage(
    origin+'/two-applications','unbound-two-cards',[],undefined,Date.now()+3000),origin);
  assert.deepEqual(unbound.result.application_ids,[]);
  assert.equal(Object.hasOwn(unbound.result,'application_id'),false);
  assert.deepEqual(unbound.result.application_records.map(row=>[row.title,row.status]),[
    ['测试开发工程师','applied'],['具身模型部署工程师','rejected']]);
  assert.equal(unbound.result.database_updated,false);
  const failed=await application.evaluate(async(_electron,origin)=>{
    try { await globalThis.reviewFixture.reviewPage(origin+'/missing','failed-review',['fixture-app'],undefined,Date.now()+3000); }
    catch(error){return error.message;}
  },origin);
  assert.equal(failed,'browser_load_failed');
  const redirected=await application.evaluate(async(_electron,origin)=>globalThis.reviewFixture.reviewPage(
    origin+'/normal-redirect','normal-redirect',['fixture-app'],undefined,Date.now()+3000),origin);
  assert.equal(redirected.review_readiness,'records');
  assert.equal(redirected.result.page.page_url,origin+'/redirected-records');
  const foreignRedirect=await application.evaluate(async(_electron,args)=>{
    try {
      await globalThis.reviewFixture.reviewPage(args.origin+'/foreign-redirect','foreign-redirect',
        ['fixture-app'],undefined,Date.now()+3000);
      return 'unexpectedly-reviewed';
    } catch(error) { return error.message; }
  },{origin,foreignOrigin});
  assert.equal(foreignRedirect,'browser_navigation_changed');
  assert.equal(foreignRequests,0,'the redirect target is rejected before any foreign request reaches the local fixture');
  for(const [route,expectedHash,operationId] of [
    ['/hash-change','#/applications','owned-hash-change'],
    ['/feishu-spa','#/app/application_center','owned-feishu-history']
  ]) {
    const transitioned=await application.evaluate(async({BrowserWindow},args)=>{
      const result=await globalThis.reviewFixture.reviewPage(args.origin+args.route,args.operationId,['fixture-app'],
        undefined,Date.now()+5000);
      return {result,visible:BrowserWindow.getAllWindows().some(window=>window.isVisible())};
    },{origin,route,operationId});
    assert.equal(transitioned.visible,false);
    assert.equal(transitioned.result.review_readiness,'records');
    assert.ok(transitioned.result.result.page.page_url.endsWith(expectedHash),transitioned.result.result.page.page_url);
  }
  const foreignAttempt=await application.evaluate(async(_electron,args)=>{
    try {
      await globalThis.reviewFixture.reviewPage(args.origin+'/foreign-navigation','foreign-navigation',
        ['fixture-app'],undefined,Date.now()+3000);
      return 'unexpectedly-reviewed';
    } catch(error) { return error.message; }
  },{origin,foreignOrigin});
  assert.equal(foreignAttempt,'browser_navigation_changed');
  assert.equal(foreignRequests,0,'the foreign navigation is rejected before any foreign request reaches the local fixture');
  await shell.evaluate(url=>window.desktop.command({action:'open',url}),origin+'/two-applications');
  await shell.waitForFunction(async()=>{
    const state=await window.desktop.state();
    return state.tabs.some(tab=>tab.id===state.active&&tab.url.endsWith('/two-applications')&&!tab.loading);
  });
  const detected=await shell.evaluate(()=>window.desktop.command({action:'filler-application-detect'}));
  assert.equal(detected.filler.application.candidates.length,2);
  assert.equal(detected.filler.application.candidates[0].company,'匿名公司');
  assert.equal(Object.hasOwn(detected.filler.application,'existing'),false);
  const requestLog=()=>application.evaluate(async({webContents},origin)=>{
    const page=webContents.getAllWebContents().find(wc=>wc.getURL().startsWith('http://127.0.0.1:')&&!wc.getURL().startsWith(origin));
    return page.executeJavaScript('fetch("/fixture/application-requests").then(response=>response.json())');
  },origin);
  assert.deepEqual(await requestLog(),[], 'DOM discovery must not query saved applications');
  const saved=await shell.evaluate(url=>window.desktop.command({action:'filler-application-save',
    company:'匿名公司',title:'测试开发工程师',recordUrl:url}),origin+'/two-applications');
  assert.equal(saved.filler.application.pendingCount,0);
  const requests=await requestLog();
  assert.equal(requests.length,1);
  assert.equal(requests[0].method,'POST');
  assert.equal(Object.hasOwn(requests[0].body,'application_id'),false);
  await shell.evaluate(url=>window.desktop.command({action:'open',url}),origin+'/704852/position/application');
  await shell.waitForFunction(async()=>{const state=await window.desktop.state();return state.tabs.some(tab=>tab.id===state.active&&tab.url.endsWith('/position/application')&&!tab.loading);});
  await shell.evaluate(()=>window.desktop.command({action:'filler-open'}));
  await shell.locator('#filler-tab-applications').click();
  await shell.locator('#filler-application-detect').click();
  await shell.waitForFunction(()=>document.getElementById('filler-application-company').value==='去哪儿旅行');
  assert.equal(await shell.locator('#filler-application-title').inputValue(),'AI应用开发工程师（客户端开发）');
  assert.equal(await shell.locator('#filler-application-url').inputValue(),origin+'/704852/position/application');
  assert.equal((await requestLog()).length,1,'reading another company still makes no saved-record query');
  // The fixture origin is HTTP; production UI requires HTTPS, so invoke the same validated IPC with this anonymous URL.
  const registered=await shell.evaluate(url=>window.desktop.command({action:'filler-application-save',company:'去哪儿旅行',
    title:'AI应用开发工程师（客户端开发）',recordUrl:url}),origin+'/704852/position/application');
  assert.equal(registered.filler.application.pendingCount,0);
  const allRequests=await requestLog();
  assert.equal(allRequests.length,2);assert.ok(allRequests.every(request=>request.method==='POST'));
  assert.equal(allRequests[1].body.company,'去哪儿旅行');
  assert.equal(allRequests[1].body.record_url,origin+'/704852/position/application');
  await shell.evaluate(url=>window.desktop.command({action:'open',url}),origin+'/two-applications');
  await shell.waitForFunction(async()=>{const state=await window.desktop.state();return state.tabs.some(tab=>tab.id===state.active&&tab.url.endsWith('/two-applications')&&!tab.loading);});
  await shell.evaluate(()=>window.desktop.command({action:'filler-open'}));
  await shell.locator('#filler-tab-applications').click();
  await shell.locator('#filler-application-detect').click();
  await shell.waitForFunction(()=>document.querySelectorAll('#filler-candidates input').length===2);
  await shell.locator('#filler-candidate-all').check();
  await shell.locator('#filler-application-company').fill('匿名批量公司');
  // The entered HTTPS URL is a registration value, never a browser/network destination in this fixture.
  await shell.locator('#filler-application-url').fill('https://careers.example.test/applications');
  await shell.locator('#filler-application-confirm').check();
  await shell.locator('#filler-application-save').click();
  await shell.waitForFunction(()=>document.getElementById('filler-application-message').textContent.includes('已保存 2 条'));
  const batchRequests=(await requestLog()).slice(2);
  assert.equal(batchRequests.length,2);assert.ok(batchRequests.every(request=>request.method==='POST'));
  assert.equal(new Set(batchRequests.map(request=>request.body.title)).size,2);
  assert.ok(batchRequests.every(request=>request.body.company==='匿名批量公司'&&!('application_id' in request.body)));
  assert.equal(await shell.locator('#filler-candidates input:checked').count(),0);
  assert.equal(await application.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().some(window=>window.isVisible())),false);
});
