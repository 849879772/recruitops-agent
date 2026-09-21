const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {_electron}=require('playwright');

test('hidden desktop workbench refreshes the current section and confirms deletion with visible failures',{timeout:60000},async t=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'recruitops-delete-anonymous-'));
  const env=Object.fromEntries(['SystemRoot','WINDIR','PATH','TEMP','TMP','COMSPEC','APPDATA','LOCALAPPDATA','USERPROFILE']
    .filter(key=>process.env[key]).map(key=>[key,process.env[key]]));
  const application=await _electron.launch({args:[path.join(__dirname,'review-hidden-fixture.cjs')],
    env:{...env,RECRUITOPS_DESKTOP_TEST:'1',RECRUITOPS_DESKTOP_DATA_DIR:root,RECRUITOPS_DESKTOP_TEST_RUNTIME:'desktop'}});
  t.after(async()=>{await application.close();fs.rmSync(root,{recursive:true,force:true,maxRetries:5});});
  const shell=await application.firstWindow();
  await shell.waitForFunction(async()=>{const state=await window.desktop.state();return state.runtime.status==='ready'&&state.active==='workbench'&&!state.workbenchLoading;});
  const page=application.context().pages().find(p=>p!==shell)||await application.context().waitForEvent('page');
  await page.waitForURL(/^http:\/\/127\.0\.0\.1:/);
  assert.ok(page,'owned workbench must exist');
  const origin=new URL(page.url()).origin,web=path.resolve(__dirname,'../../web'),errors=[],calls=[];
  const record={id:'anonymous-delete-fixture',company_name:'匿名测试公司',job_title:'匿名测试工程师',stage:'applied',
    updated_at:'2026-09-20T00:00:00Z',stage_history:[],record_url:'https://careers.example.test/applications'};
  let rows=[record],failure=403,scheduleReads=0,documentLoads=0;
  await page.addInitScript(()=>{window.confirm=()=>{throw new Error('Native confirmation must not be called');};});
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url());
    if(url.origin!==origin)return route.abort();
    const file=url.pathname==='/'?'index.html':url.pathname.slice(1);
    if(file==='index.html')documentLoads++;
    if(['index.html','app.js','configuration.js','knowledge.js','company-sources.js','styles.css'].includes(file))
      return route.fulfill({contentType:file.endsWith('html')?'text/html; charset=utf-8':file.endsWith('css')?'text/css':'text/javascript',body:fs.readFileSync(path.join(web,file))});
    if(url.pathname==='/health')return route.fulfill({json:{status:'ok',mode:'anonymous-test'}});
    if(url.pathname==='/api/codex/health')return route.fulfill({json:{enabled:false,ready:false}});
    if(['/api/companies','/api/approvals','/api/codex/traces'].includes(url.pathname))return route.fulfill({json:[]});
    if(['/api/jobs/browse','/api/recruitment-mails'].includes(url.pathname))return route.fulfill({json:{items:[],total:0}});
    if(url.pathname==='/api/applications/page')return route.fulfill({json:{items:rows,total:rows.length}});
    if(url.pathname==='/api/schedule'){scheduleReads++;return route.fulfill({json:[]});}
    if(url.pathname==='/api/local-ui/applications/'+record.id&&request.method()==='DELETE'){
      calls.push(request.postDataJSON());
      assert.equal(request.headers()['x-recruitops-local-ui'],'1');
      if(failure)return route.fulfill({status:failure,json:{detail:failure===403?'Writes are disabled':failure===409?'记录已变化，请刷新后重试':'匿名删除故障'}});
      rows=[];return route.fulfill({json:{status:'deleted'}});
    }
    return route.fulfill({status:503,json:{detail:'Anonymous fixture unavailable'}});
  });
  await page.reload();
  await page.locator('[data-view="applications"]').click();
  assert.equal(await shell.locator('#reload').isEnabled(),true,'workbench refresh must be enabled');
  const loadsBefore=documentLoads;
  await Promise.all([page.waitForEvent('load'),shell.locator('#reload').click()]);
  assert.equal(documentLoads,loadsBefore+1);
  assert.equal(await page.locator('[data-view-panel="applications"]').isVisible(),true,'refresh keeps the current section');
  await page.getByRole('button',{name:'编辑 匿名测试公司 的投递记录',exact:true}).click();
  const deletion=page.getByRole('button',{name:'删除投递记录',exact:true});
  await deletion.click();
  const panel=page.locator('.application-delete-confirmation');
  assert.equal(await panel.isVisible(),true);assert.equal(calls.length,0);
  assert.match(await panel.innerText(),/匿名测试公司.*匿名测试工程师/);
  assert.match(await panel.innerText(),/邮件保留/);
  await panel.getByRole('button',{name:'取消',exact:true}).click();
  assert.equal(await panel.isVisible(),false);assert.equal(calls.length,0);
  await deletion.click();
  const out=path.resolve(__dirname,'../../../artifacts/desktop-qa/application-delete-20260920');fs.mkdirSync(out,{recursive:true});
  await panel.screenshot({path:path.join(out,'confirm.png')});
  for(const status of [403,409,500]){
    failure=status;
    await panel.getByRole('button',{name:'确认删除',exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector('.application-delete-error').hidden);
    assert.equal(await panel.isVisible(),true);
    assert.match(await panel.locator('.application-delete-error').innerText(),/删除未完成/);
    assert.equal(await page.locator('.application-card').count(),1);
    assert.equal(await panel.getByRole('button',{name:'确认删除',exact:true}).isEnabled(),true);
  }
  assert.equal(calls.length,3);
  failure=0;const readsBefore=scheduleReads;
  await panel.getByRole('button',{name:'确认删除',exact:true}).evaluate(el=>{el.click();el.click();});
  await page.waitForFunction(()=>document.querySelectorAll('.application-card').length===0);
  assert.equal(calls.length,4,'a repeated click must not delete twice');
  assert.ok(calls.every(body=>body.expected_updated_at===record.updated_at));
  assert.equal(await page.locator('#nav-application-count').innerText(),'0');
  assert.ok(scheduleReads>readsBefore);
  assert.deepEqual(errors,[]);
  assert.equal(await application.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().some(window=>window.isVisible())),false);
});
