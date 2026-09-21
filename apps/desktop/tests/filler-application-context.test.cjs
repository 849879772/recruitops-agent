const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const api=require('../../../packages/desktop_filler/index.cjs');
const executablePath=process.env.RECRUITOPS_TEST_CHROMIUM;
const bundle=api.loadDesktopFiller();
const recordUrl='https://tenant.jobs.feishu.cn/704852/position/application';
const card=title=>`<article class="application-card"><h2 class="job-title">${title}</h2><div>北京</div><div>投递简历</div><time>2026-09-15</time></article>`;

async function pageFixture(t,body,url=recordUrl) {
  const browser=await chromium.launch({headless:true,executablePath});
  t.after(()=>browser.close());
  const page=await browser.newPage();
  await page.route('**/*',route=>route.request().url()===url
    ?route.fulfill({contentType:'text/html; charset=utf-8',body}):route.abort());
  await page.goto(url);
  return page;
}

test('original-plugin registration reads Feishu company and role, without resume data or DOM writes',{skip:!executablePath},async t=>{
  const page=await pageFixture(t,'<title>应聘记录 - 去哪儿旅行校园招聘</title><header><a>首页</a><a>校招岗位</a><a>成长在Qunar</a></header>'+card('AI应用开发工程师（客户端开发）')+
    '<input type="password" value="never-read-secret"><button onclick="window.submitted=true">提交</button>');
  const before=await page.content();
  const result=await page.evaluate(api.buildApplicationContextScript(bundle));
  assert.equal(result.company,'去哪儿旅行');assert.deepEqual(result.titles,['AI应用开发工程师（客户端开发）']);assert.equal(result.url,recordUrl);
  assert.equal(result.records[0].date,'2026-09-15');assert.match(result.records[0].sourceStatus,/投递|已投/);
  assert.equal(await page.content(),before);
  assert.equal(await page.evaluate('Boolean(window.submitted)'),false);
  assert.doesNotMatch(JSON.stringify(result),/secret/);
});

test('original-plugin discovery keeps separate roles, supports page brand metadata and avoids domain guessing',{skip:!executablePath},async t=>{
  const page=await pageFixture(t,'<title>我的投递</title><meta property="og:site_name" content="匿名机器人校园招聘">'+
    card('测试开发工程师')+card('具身模型部署工程师'));
  const result=await page.evaluate(api.buildApplicationContextScript(bundle));
  assert.equal(result.company,'匿名机器人');
  assert.deepEqual(result.titles,['测试开发工程师','具身模型部署工程师']);
  await page.locator('meta').evaluate(el=>el.remove());
  assert.equal((await page.evaluate(api.buildApplicationContextScript(bundle))).company,'');
  const unknown=await pageFixture(t,'<title>我的投递</title>'+card('软件工程师'),'https://unknown-company.example.com/applications');
  assert.equal((await unknown.evaluate(api.buildApplicationContextScript(bundle))).company,'');
});

test('welcoming recruitment title does not become part of a company name',{skip:!executablePath},async t=>{
  const page=await pageFixture(t,'<title>应聘记录 - 欢迎加入原力灵机</title>'+card('【27届校招】软件开发工程师')+card('【27届校招】机器人系统开发工程师'));
  const result=await page.evaluate(api.buildApplicationContextScript(bundle));
  assert.equal(result.company,'原力灵机');assert.equal(result.records.length,2);
  assert.ok(result.records.every(row=>row.date==='2026-09-15'));
});

test('registration panel renders readable horizontal candidates and prefills a single role at desktop widths',{skip:!executablePath},async t=>{
  const browser=await chromium.launch({headless:true,executablePath});t.after(()=>browser.close());
  const errors=[],page=await browser.newPage();page.on('pageerror',e=>errors.push(e.message));
  const renderer=path.join(__dirname,'../renderer'),origin='https://desktop.fixture';
  await page.route('**/*',route=>{
    const url=new URL(route.request().url()),file=url.pathname==='/'?'index.html':url.pathname.slice(1);
    if(url.origin!==origin||!['index.html','shell.js','styles.css'].includes(file))return route.abort();
    return route.fulfill({contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html; charset=utf-8',body:fs.readFileSync(path.join(renderer,file))});
  });
  await page.addInitScript(({url})=>{
    const state={active:11,tabs:[{id:11,title:'应聘记录 - 去哪儿旅行校园招聘',url,loading:false}],
      runtime:{status:'ready',instanceId:'anonymous'},browser:{connected:true},writesEnabled:true,
      filler:{available:true,open:true,pluginReady:true,busy:false,fields:[],results:[],message:'页面已变化，请重新扫描。',
        capabilities:{applications:true,offlineQueue:true},supportedActions:['filler-application-detect','filler-application-save','filler-application-save-batch'],
        profile:{},application:{candidates:[],queue:[],pendingCount:0,message:''}}};
    window.saved=[];window.testState=state;window.desktop={state:async()=>structuredClone(state),onState:fn=>{window.pushState=()=>fn(structuredClone(state));},
      command:async command=>{
        if(command.action==='filler-application-detect'){
          state.filler.application.candidates=[{id:'one',company:'去哪儿旅行',title:'AI应用开发工程师（客户端开发）',recordUrl:url}];
          state.filler.application.message='已读取 1 个岗位，请核对后新增。';
        }
        if(command.action==='filler-application-save')window.saved.push(command);
        if(command.action==='filler-application-save-batch'){
          window.saved.push(command);state.filler.application.batchResults=command.records.map((row,index)=>({index,status:index?'failed':'saved',...(index?{error:'foreground_changed'}:{})}));
          state.filler.application.message='新增完成：已保存 1 条，待补传 0 条，失败 1 条。';
        }
        return structuredClone(state);
      }};
  },{url:recordUrl});
  await page.goto(origin);
  await page.locator('#filler-tab-applications').click();
  await page.locator('#filler-application-detect').click();
  assert.equal(await page.locator('#filler-application-company').inputValue(),'去哪儿旅行');
  assert.equal(await page.locator('#filler-application-title').inputValue(),'AI应用开发工程师（客户端开发）');
  assert.equal(await page.locator('#filler-application-url').inputValue(),recordUrl);
  const out=path.resolve(__dirname,'../../../artifacts/desktop-qa/registration-20260920');fs.mkdirSync(out,{recursive:true});
  for(const width of [1920,1366,1024]){
    await page.setViewportSize({width,height:1000});
    const bounds=await page.locator('#filler-candidates .filler-check').evaluate(el=>{
      const radio=el.querySelector('input').getBoundingClientRect(),text=el.querySelector('span').getBoundingClientRect();
      return {radio:radio.width,text:text.width,height:text.height,overlap:radio.right>text.left,panelOverflow:el.scrollWidth>el.clientWidth};
    });
    assert.equal(bounds.radio,16);assert.ok(bounds.text>250);assert.ok(bounds.height<80);assert.equal(bounds.overlap,false);assert.equal(bounds.panelOverflow,false);
    await page.locator('#filler-panel').screenshot({path:path.join(out,`panel-${width}.png`)});
  }
  await page.locator('#filler-application-confirm').check();
  await page.locator('#filler-application-save').click();
  assert.deepEqual(await page.evaluate('window.saved'),[{action:'filler-application-save',company:'去哪儿旅行',title:'AI应用开发工程师（客户端开发）',recordUrl}]);
  await page.evaluate(url=>{
    window.testState.tabs[0].title='应聘记录 - 欢迎加入原力灵机';
    window.testState.filler.application.message='已读取 3 个岗位，请核对后新增。';
    window.testState.filler.application.candidates=['软件开发工程师','软件工程师','机器人系统开发工程师'].map((title,index)=>({
      id:`three-${index}`,company:'原力灵机',title:`【27届校招】${title}`,date:'2026-08-18',sourceStatus:'投递简历',recordUrl:url}));
    window.pushState();
  },recordUrl);
  const choices=page.locator('#filler-candidates input');
  assert.equal(await choices.count(),3);assert.equal(await choices.first().getAttribute('type'),'checkbox');
  await page.locator('#filler-candidate-all').check();
  assert.equal(await page.locator('#filler-candidates input:checked').count(),3);
  assert.equal(await page.locator('#filler-application-title-label').isVisible(),false);
  for(const width of [1920,1366,1024]){
    await page.setViewportSize({width,height:1000});
    assert.equal(await page.locator('#filler-candidates').evaluate(el=>el.scrollWidth>el.clientWidth),false);
    for(const item of await page.locator('#filler-candidates .filler-check').all()){
      const box=await item.evaluate(el=>{const c=el.querySelector('input').getBoundingClientRect(),text=el.querySelector('span').getBoundingClientRect();return {width:text.width,overlap:c.right>text.left,height:text.height};});
      assert.ok(box.width>230);assert.equal(box.overlap,false);assert.ok(box.height<150);
    }
    await page.locator('#filler-panel').screenshot({path:path.join(out,`multi-${width}.png`)});
  }
  await page.locator('#filler-candidate-all').uncheck();
  await page.locator('#filler-application-confirm').check();
  assert.equal(await page.locator('#filler-application-save').isDisabled(),true,'zero selected never falls back to manual title');
  await choices.first().check();
  await page.locator('#filler-application-title').fill('更正后的软件开发工程师');
  await choices.nth(2).check();
  await page.locator('#filler-application-company').fill('原力灵机科技');
  await page.evaluate('window.pushState()');
  assert.equal(await page.locator('#filler-candidates input:checked').count(),2);
  await page.locator('#filler-application-confirm').check();
  await page.locator('#filler-application-save').click();
  const submitted=await page.evaluate('window.saved.at(-1)');
  assert.equal(submitted.action,'filler-application-save-batch');
  assert.deepEqual(submitted.records.map(row=>row.title),['更正后的软件开发工程师','【27届校招】机器人系统开发工程师']);
  assert.ok(submitted.records.every(row=>row.company==='原力灵机科技'&&row.recordUrl===recordUrl));
  assert.equal(await page.locator('#filler-candidates input:checked').count(),1);
  assert.equal(await choices.first().isDisabled(),true);
  assert.match(await page.locator('#filler-candidates').innerText(),/已保存[\s\S]*未保存：页面已变化/);
  assert.deepEqual(errors,[]);
});
