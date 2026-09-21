const { test, expect, _electron: electron } = require('@playwright/test');
const { mkdtemp, rm, writeFile } = require('node:fs/promises');
const {readFileSync}=require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');

let server, origin, profile, desktop;
const siteRequests=[];
test.beforeAll(async () => {
  server = http.createServer((req, res) => {
    if(req.url==='/renderer-fixture') {
      res.setHeader('Content-Type','text/html; charset=utf-8');
      const html=readFileSync(path.join(__dirname,'../renderer/index.html'),'utf8')
        .replace("script-src 'self'","script-src 'self' 'unsafe-inline'")
        .replace('<script src="shell.js">','<script>window.fixtureState='+JSON.stringify(rendererFixtureState())+';window.fixtureCommands=[];window.fixtureReject="";window.fixturePending=false;window.desktop={state:async()=>structuredClone(window.fixtureState),onState:fn=>window.fixtureRender=fn,command:async value=>{window.fixtureCommands.push(value);if(window.fixtureReject===value.action)throw new Error("fixture_rejected");if(window.fixturePending)return new Promise(resolve=>window.fixtureResolve=()=>resolve(structuredClone(window.fixtureState)));if(value.action==="filler-close")window.fixtureState.filler.open=false;if(value.action==="filler-open")window.fixtureState.filler.open=true;if(value.action==="filler-profile-save"){window.fixtureState.filler.profile.data=value.profile;window.fixtureState.filler.profile.version++;}return structuredClone(window.fixtureState);}};</script><script src="shell.js">');
      res.end(html);return;
    }
    if(req.url==='/shell.js'||req.url==='/styles.css') {res.setHeader('Content-Type',req.url.endsWith('.js')?'text/javascript':'text/css');res.end(readFileSync(path.join(__dirname,'../renderer',req.url.slice(1))));return;}
    siteRequests.push({url:req.url,authorization:req.headers.authorization,referer:req.headers.referer});
    if (req.url === '/offline') { req.socket.destroy(); return; }
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end('<!doctype html><html><head><title>Anonymous ATS fixture</title></head><body style="font:18px sans-serif;padding:40px;background:#fafafa"><h1>招聘官网离线夹具</h1><p>Anonymous candidate / no submission endpoint</p><label>姓名 <input id="name"></label><label>简历 <input type="file" id="resume"></label><a id="next" href="/next">Next step</a><button id="popup" onclick="window.open(\'/sso\')">SSO popup</button><div style="height:800px"></div></body></html>');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  profile = await mkdtemp(path.join(os.tmpdir(), 'recruitops-desktop-匿名-'));
});
test.afterEach(async () => { if (desktop) { await desktop.close(); desktop = undefined; } });
test.afterAll(async () => { await new Promise(resolve => server.close(resolve)); await rm(profile, { recursive: true, force: true }); });
async function launch(extra = {}) {
  // Deliberately do not inherit credentials or application configuration.
  const env = Object.fromEntries(['SystemRoot', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'COMSPEC', 'APPDATA', 'LOCALAPPDATA', 'USERPROFILE'].filter(key => process.env[key]).map(key => [key, process.env[key]]));
  desktop = await electron.launch({ args: [path.resolve(__dirname, '..')], env: { ...env, RECRUITOPS_DESKTOP_DATA_DIR: profile, RECRUITOPS_DESKTOP_TEST: '1', RECRUITOPS_DESKTOP_FIXTURE_ORIGIN: origin, ...extra } });
  const shell = await desktop.firstWindow(); await shell.waitForLoadState(); return shell;
}
async function invoke(shell, value) { return shell.evaluate(value => window.desktop.command(value), value); }
async function sitePage() {
  await expect.poll(() => desktop.context().pages().find(page => page.url().startsWith(origin))?.url()).toBeTruthy();
  return desktop.context().pages().find(page => page.url().startsWith(origin));
}

function rendererFixtureState() {
  return {active:11,tabs:[{id:11,title:'匿名官网',url:'https://careers.example.test/form',loading:false}],
    runtime:{status:'ready',stage:'runtime',instanceId:'anonymous-renderer'},writesEnabled:true,browser:{connected:true},apiConfigured:true,
    filler:{available:true,open:true,pluginReady:true,profileReady:true,scanId:'scan-one',undoReady:true,
      capabilities:{persistentProfile:true,attachment:true,repeatedSections:true,frames:false,customAnswers:true,diagnostics:true,applications:true,offlineQueue:true},
      profile:{ready:true,mode:'personal',version:9,summary:'匿名资料',data:{basic:{fullName:'匿名候选人',email:'candidate@example.test'},education:[{school:'匿名学校甲',degree:'本科'},{school:'匿名学校乙',degree:'硕士'},{school:'匿名学校丙',degree:'进修'}],unknownSection:{customField:'应保留'}}},
      attachment:{ready:true,name:'synthetic-resume.pdf',size:4096},fields:[{fieldId:'name',label:'姓名',value:'匿名候选人'},{fieldId:'school',label:'学校',value:'匿名学校甲'}],
      diagnostics:[{code:'field_timeout',severity:'warning',fieldId:'school',message:'SECRET cookie=private; candidate@example.test',resume:'private resume'}],
      application:{candidates:[{id:'candidate-one',company:'匿名公司',title:'匿名岗位'}],existing:[{id:'application-one',company:'已有公司',title:'已有岗位'}],queue:[{queueId:'queue-one',company:'待传公司',title:'待传岗位',status:'queued'}],pendingCount:1,message:'待用户确认'}}};
}
async function rendererWindow(width=1280,height=720,zoom=1) {
  await launch();
  const opened=desktop.waitForEvent('window');
  await desktop.evaluate(async({BrowserWindow},{url,width,height,zoom})=>{
    const window=new BrowserWindow({show:true,width,height,webPreferences:{nodeIntegration:false,contextIsolation:true,sandbox:true}});
    window.setContentSize(width,height);await window.loadURL(url);window.webContents.setZoomFactor(zoom);
  },{url:origin+'/renderer-fixture',width,height,zoom});
  const page=await opened;await page.waitForLoadState();await expect(page.locator('#filler-panel')).toBeVisible();
  await expect.poll(()=>page.evaluate(()=>innerWidth)).toBeLessThanOrEqual(Math.ceil(width/zoom));
  await expect.poll(()=>page.evaluate(()=>innerWidth)).toBeGreaterThanOrEqual(Math.floor(width/zoom)-1);return page;
}
async function updateRenderer(page,change) {await page.evaluate(change);await page.evaluate(()=>window.fixtureRender(structuredClone(window.fixtureState)));}

test('T07 renderer preserves full profile, dirty edits and save failures with versioned payload',async()=>{
  const page=await rendererWindow();
  await page.getByRole('tab',{name:'简历资料',exact:true}).click();
  await expect(page.getByLabel('学校',{exact:true})).toHaveCount(3);
  await page.getByLabel('姓名',{exact:true}).fill('编辑后的匿名姓名');
  await expect(page.locator('#filler-profile-status')).toContainText('未保存');
  await updateRenderer(page,()=>{window.fixtureState.filler.message='heartbeat';});
  await expect(page.getByLabel('姓名',{exact:true})).toHaveValue('编辑后的匿名姓名');
  await page.getByRole('tab',{name:'扫描与填写',exact:true}).click();await expect(page.locator('#filler-confirm')).toBeDisabled();
  await page.getByRole('tab',{name:'简历资料',exact:true}).click();
  await page.evaluate(()=>window.fixtureReject='filler-profile-save');await page.locator('#filler-profile-save').click();
  await expect(page.locator('#filler-ui-error')).toContainText('未完成');await expect(page.locator('#filler-profile-status')).toContainText('未保存');
  await page.evaluate(()=>window.fixtureReject='');await page.locator('#filler-profile-save').click();await expect(page.locator('#filler-profile-status')).toContainText('已载入');
  const commands=await page.evaluate(()=>window.fixtureCommands),saved=commands.filter(c=>c.action==='filler-profile-save').at(-1);
  expect(saved.expectedVersion).toBe(9);expect(saved.profile.education).toHaveLength(3);expect(saved.profile.unknownSection.customField).toBe('应保留');
  await page.locator('#filler-profile-export').click();await page.locator('#filler-profile-import').click();
  await page.locator('#filler-attachment-select').click();await page.locator('#filler-attachment-clear').click();await page.locator('#filler-demo-enable').click();
  await updateRenderer(page,()=>{window.fixtureState.filler.profile.mode='demo';});await page.locator('#filler-demo-restore').click();
  expect((await page.evaluate(()=>window.fixtureCommands)).map(c=>c.action)).toEqual(expect.arrayContaining(['filler-profile-export','filler-profile-import','filler-attachment-select','filler-attachment-clear','filler-demo-enable','filler-demo-restore']));
});

test('normal business mode hides the notice bar and read-only mode keeps its warning',async()=>{
  const page=await rendererWindow();
  await expect(page.locator('#notice')).toBeHidden();
  await expect.poll(()=>page.evaluate(()=>getComputedStyle(document.documentElement).getPropertyValue('--desktop-content-top').trim())).toBe('112px');
  await updateRenderer(page,()=>{window.fixtureState.writesEnabled=false;});
  await expect(page.locator('#notice')).toBeVisible();
  await expect(page.locator('#notice')).toHaveText('只读模式 · 登录与文件选择由本人操作');
  await expect.poll(()=>page.evaluate(()=>getComputedStyle(document.documentElement).getPropertyValue('--desktop-content-top').trim())).toBe('144px');
});

test('T07 renderer exposes all 65 audited fields and supports multiple editable records without source defaults',async()=>{
  const page=await rendererWindow();
  await updateRenderer(page,()=>{window.fixtureState.filler.profile.data={basic:{},education:[{}],projects:[{}],awards:[{}],publications:[{}],certificates:[{}]};});
  await page.getByRole('tab',{name:'简历资料',exact:true}).click();
  const inventory=JSON.parse(readFileSync(path.join(__dirname,'fixtures/filler-parity/audit.json'),'utf8')).profileFields;
  const paths=await page.locator('[data-profile-path]').evaluateAll(inputs=>inputs.map(input=>input.dataset.profilePath));
  for(const [section,keys] of Object.entries(inventory))for(const key of keys)expect(paths).toContain(section==='scalar'?key:section==='basic'?`basic.${key}`:`${section}.0.${key}`);
  await page.getByRole('button',{name:'新增教育经历',exact:true}).click();
  await expect(page.locator('[data-profile-path="education.1.school"]')).toBeVisible();
  await page.locator('[data-profile-path="education.1.school"]').fill('新增匿名学校');
  await page.locator('#filler-profile-save').click();
  expect((await page.evaluate(()=>window.fixtureCommands.at(-1))).profile.education[1].school).toBe('新增匿名学校');
});

test('T07 renderer retains dirty draft on version conflict and gates unsupported operations',async()=>{
  const page=await rendererWindow();await page.getByRole('tab',{name:'简历资料',exact:true}).click();
  await page.getByLabel('姓名',{exact:true}).fill('未保存匿名修改');
  await updateRenderer(page,()=>{window.fixtureState.filler.profile.version=10;window.fixtureState.filler.profile.data.basic.fullName='别处更新';});
  await expect(page.getByLabel('姓名',{exact:true})).toHaveValue('未保存匿名修改');await expect(page.locator('#filler-profile-save')).toBeDisabled();
  await expect(page.locator('#filler-profile-status')).toContainText('冲突');
  await page.locator('#filler-profile-discard').click();await expect(page.getByLabel('姓名',{exact:true})).toHaveValue('别处更新');
  await updateRenderer(page,()=>{window.fixtureState.filler.supportedActions=['filler-close'];});
  for(const id of ['filler-profile-import','filler-profile-export','filler-demo-enable','filler-attachment-select'])await expect(page.locator('#'+id)).toBeDisabled();
});

test('T08 renderer selections, scoped custom answer, field results and capability refusal',async()=>{
  const page=await rendererWindow();await page.locator('#filler-select-all').uncheck();await expect(page.locator('#filler-confirm')).toBeDisabled();
  await page.getByLabel('填写 姓名',{exact:true}).check();await page.locator('#filler-confirm').click();
  expect(await page.evaluate(()=>window.fixtureCommands.at(-1))).toEqual({action:'filler-fill',scanId:'scan-one',fieldIds:['name']});
  await page.locator('#filler-custom summary').click();await page.locator('#filler-custom-field').selectOption('name');
  await page.locator('#filler-custom-answer').fill('匿名补充答案');await page.locator('#filler-custom-save').click();
  const customCommands=await page.evaluate(()=>window.fixtureCommands.filter(c=>['filler-custom-save','filler-scan'].includes(c.action)).slice(-2));
  expect(customCommands).toEqual([{action:'filler-custom-save',question:'姓名',answer:'匿名补充答案',scope:'site'}, {action:'filler-scan'}]);
  await expect(page.locator('#filler-diagnostic-panel')).toHaveCount(0);
  await expect(page.locator('#filler-diagnostics-copy')).toHaveCount(0);await expect(page.locator('#filler-diagnostics-clear')).toHaveCount(0);
  await updateRenderer(page,()=>{window.fixtureState.filler.results=[{fieldId:'name',status:'success'},{fieldId:'school',status:'failed'},{fieldId:'skip',status:'skipped'}];});
  await expect(page.locator('#filler-counts')).toHaveText('结果 3：成功 1 / 失败 1 / 跳过 1 / 未确认 0');
  await updateRenderer(page,()=>{window.fixtureState.filler.capabilities={};});
  for(const id of ['filler-prepare','filler-custom-save'])await expect(page.locator('#'+id)).toBeDisabled();
  expect((await page.evaluate(()=>window.fixtureCommands)).some(c=>/sms|captcha|focus|retry|register|identify/.test(c.action))).toBe(false);
});

test('T08 renderer application confirmation and queue actions do not fake acknowledgement',async()=>{
  const page=await rendererWindow();await page.getByRole('tab',{name:'投递记录',exact:true}).click();
  await expect(page.locator('#filler-application-existing')).toHaveCount(0);await expect(page.locator('#filler-application-sync')).toHaveCount(0);
  await expect(page.locator('#filler-view-applications')).not.toContainText('已有公司');
  expect(await page.evaluate(()=>window.fixtureCommands)).toEqual([]);
  await page.locator('#filler-application-detect').click();
  await page.locator('#filler-application-company').fill('匿名公司');await page.locator('#filler-application-title').fill('匿名岗位');await page.locator('#filler-application-url').fill('https://careers.example.test/progress');
  await page.locator('#filler-candidates input').check();
  await expect(page.locator('#filler-application-save')).toBeDisabled();await page.locator('#filler-application-confirm').check();
  await page.evaluate(()=>window.fixtureReject='filler-application-save');await page.locator('#filler-application-save').click();
  await expect(page.locator('#filler-ui-error')).toContainText('未完成');await expect(page.locator('#filler-application-message')).toHaveText('待用户确认');
  const command=await page.evaluate(()=>window.fixtureCommands.at(-1));expect(command).toEqual({action:'filler-application-save',company:'匿名公司',title:'匿名岗位',recordUrl:'https://careers.example.test/progress'});
  await page.locator('#filler-application-flush').click();await page.locator('#filler-queue .queue-cancel').click();
  expect(await page.evaluate(()=>window.fixtureCommands.at(-1))).toEqual({action:'filler-application-cancel',queueId:'queue-one'});
  await expect(page.locator('#filler-queue-count')).toHaveText('1');
  await updateRenderer(page,()=>{window.fixtureState.active=12;window.fixtureState.tabs=[{id:12,title:'另一官网',url:'https://other.example.test/'}];});
  await expect(page.locator('#filler-application-confirm')).not.toBeChecked();await expect(page.locator('#filler-application-company')).toHaveValue('');
  await page.getByRole('tab',{name:'扫描与填写',exact:true}).click();await expect(page.locator('#filler-confirm')).toBeDisabled();
});

test('T06 renderer busy state prevents reentry and tabs preserve document without commands',async()=>{
  const page=await rendererWindow();await page.evaluate(()=>window.fixturePending=true);await page.locator('#filler-scan').click();
  await expect(page.locator('#filler-scan')).toBeDisabled();await expect(page.locator('#filler-confirm')).toBeDisabled();
  await page.getByRole('tab',{name:'简历资料',exact:true}).click();await page.getByRole('tab',{name:'投递记录',exact:true}).click();
  expect(await page.evaluate(()=>window.fixtureCommands)).toEqual([{action:'filler-scan'}]);
  await page.evaluate(()=>{window.fixturePending=false;window.fixtureResolve();});await page.getByRole('tab',{name:'扫描与填写',exact:true}).click();await expect(page.locator('#filler-scan')).toBeEnabled();
  await page.locator('#filler-close').click();await expect(page.locator('#filler-panel')).toBeHidden();await page.locator('#filler-open').click();await expect(page.locator('#filler-panel')).toBeVisible();
  await expect(page.locator('#filler-plugin')).toBeHidden();await expect(page.locator('#filler-profile')).toBeHidden();
});

for(const [width,height,zoom] of [[1280,720,1.25],[1280,720,1.5],[1920,1080,1.25],[1920,1080,1.5]]) test(`T06 renderer sidebar geometry ${width}x${height} ${zoom}`,async()=>{
  const page=await rendererWindow(width,height,zoom);
  await page.evaluate(()=>{const site=document.getElementById('recruitment-surface');const input=document.createElement('input');input.setAttribute('aria-label','匿名官网输入');site.append(input);});
  await page.getByLabel('匿名官网输入').fill('保持官网可输入');
  const bounds=await page.evaluate(()=>{const sidebar=document.getElementById('filler-panel').getBoundingClientRect(),site=document.getElementById('recruitment-surface').getBoundingClientRect();return {sideWidth:sidebar.width,sideLeft:sidebar.left,siteRight:site.right,siteWidth:site.width,right:sidebar.right,viewport:innerWidth,overflow:document.documentElement.scrollWidth>innerWidth};});
  expect(bounds.sideWidth).toBeGreaterThanOrEqual(360);expect(bounds.sideWidth).toBeLessThanOrEqual(480);expect(bounds.siteRight).toBeLessThanOrEqual(bounds.sideLeft+1);expect(bounds.siteWidth).toBeGreaterThan(350);expect(bounds.right).toBeLessThanOrEqual(bounds.viewport+1);expect(bounds.overflow).toBe(false);
  for(const tab of ['scan','profile','applications']){
    await page.locator('#filler-tab-'+tab).click();
    // Electron's Playwright screenshot can crop zoomed surfaces; this mock window has no native child views.
    const capture=await desktop.evaluate(async({webContents},url)=>{
      const contents=webContents.getAllWebContents().find(item=>item.getURL()===url);
      const bitmap=await contents.capturePage();return {size:bitmap.getSize(),png:bitmap.toPNG().toString('base64')};
    },page.url());
    expect(capture.size.width).toBeGreaterThanOrEqual(width);
    await writeFile(`test-results/renderer-${width}-${height}-${zoom}-${tab}.png`,Buffer.from(capture.png,'base64'));
  }
  await expect(page.getByLabel('匿名官网输入')).toHaveValue('保持官网可输入');
});

test('initial parsed DOM exposes only startup placeholder before renderer scripts',async()=>{
  await launch();
  const initial=await desktop.evaluate(async({BrowserWindow},file)=>{
    const probe=new BrowserWindow({show:false,webPreferences:{javascript:false,nodeIntegration:false,contextIsolation:true,sandbox:true}});
    try {
      await probe.loadFile(file);
      probe.webContents.debugger.attach('1.3');
      const {root}=await probe.webContents.debugger.sendCommand('DOM.getDocument');
      const result={};
      for(const [key,selector] of Object.entries({home:'#home-page',writes:'#runtime-writes',progress:'#workbench-progress',error:'#startup-error'})) {
        const {nodeId}=await probe.webContents.debugger.sendCommand('DOM.querySelector',{nodeId:root.nodeId,selector});
        const {attributes}=await probe.webContents.debugger.sendCommand('DOM.getAttributes',{nodeId});
        result[key]=attributes.includes('hidden');
      }
      return result;
    } finally {probe.destroy();}
  },path.resolve(__dirname,'../renderer/index.html'));
  expect(initial).toEqual({home:true,writes:true,progress:false,error:true});
});

test('manual diagnostics before readiness suppress progress and survive ready heartbeats',async()=>{
  const shell=await launch({RECRUITOPS_DESKTOP_TEST_RUNTIME:'delayed-desktop'});
  await expect(shell.locator('#home-page')).toBeHidden();
  await shell.getByRole('button',{name:'启动状态',exact:true}).click();
  await expect(shell.locator('#home-page')).toBeVisible();
  await expect(shell.locator('#workbench-progress')).toBeHidden();
  await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).runtime.status).toBe('ready');
  await new Promise(resolve=>setTimeout(resolve,700));
  await expect(shell.locator('#home-page')).toBeVisible();
  await expect(shell.locator('#workbench-progress')).toBeHidden();
  expect((await shell.evaluate(()=>window.desktop.state())).active).toBe(null);
  await shell.getByRole('button',{name:'工作台',exact:true}).click();
  await expect(shell.locator('#home-page')).toBeHidden();
  await expect(shell.locator('#workbench-progress')).toBeVisible();
  await expect(shell.locator('#workbench-progress')).toBeHidden();
});

test('closing foreground recruitment tab returns to workbench; background close keeps foreground',async()=>{
  const shell=await launch({RECRUITOPS_DESKTOP_TEST_RUNTIME:'desktop'});
  await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).active).toBe('workbench');
  await invoke(shell,{action:'open',url:origin+'/jobs'});
  const site=await sitePage();await site.waitForLoadState();
  const foreground=(await shell.evaluate(()=>window.desktop.state())).active;
  await desktop.evaluate(async(_electron,{origin,mainPath})=>{
    const main=process.getBuiltinModule('module').createRequire(mainPath)(mainPath);
    const lease=main.createBackgroundPage(origin+'/review');lease.close();
  },{origin,mainPath:path.resolve(__dirname,'../dist/main.js')});
  expect((await shell.evaluate(()=>window.desktop.state())).active).toBe(foreground);
  await shell.getByRole('button',{name:'关闭 Anonymous ATS fixture',exact:true}).click();
  await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).active).toBe('workbench');
  await expect(shell.locator('#home-page')).toBeHidden();
  await expect(shell.locator('#workbench-progress')).toBeHidden();
  const page=desktop.context().pages().find(p=>p.url().startsWith('http://127.0.0.1:')&&!p.url().startsWith(origin));
  await expect(page.locator('h1')).toHaveText('Owned fixture workbench');
  const visible=await desktop.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].contentView.children.filter(v=>v.getVisible()).map(v=>v.webContents?.getURL()));
  expect(visible).toContain(page.url());
});

for(const earlyClick of [false,true]) test(`desktop automatic ready opens workbench; early click ${earlyClick}`,async()=>{
  const shell=await launch({RECRUITOPS_DESKTOP_TEST_RUNTIME:'delayed-desktop'});
  if(earlyClick) await shell.getByRole('button',{name:'工作台',exact:true}).click();
  await expect(shell.locator('#workbench-progress')).toBeVisible();
  await expect(shell.locator('#home-page')).toBeHidden();
  await expect(shell.locator('#runtime-writes')).toBeHidden();
  await expect(shell.locator('#startup-message')).toContainText(/正在/);
  await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).runtime.status).toBe('ready');
  await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).active).toBe('workbench');
  await expect(shell.locator('#startup-message')).toHaveText('正在加载工作台，请稍候…');
  await expect(shell.locator('#workbench-progress')).toBeHidden();
  await expect(shell.locator('#home-page')).toBeHidden();
  await expect(shell.locator('#runtime-writes')).toBeHidden();
  const state=await shell.evaluate(()=>window.desktop.state());expect(state.writesEnabled).toBe(true);
  const page=desktop.context().pages().find(p=>p.url().startsWith('http://127.0.0.1:'));
  await expect(page.locator('h1')).toHaveText('Owned fixture workbench');
  await shell.getByRole('button',{name:'启动状态',exact:true}).click();
  await expect(shell.locator('#home-page')).toBeVisible();
  await expect(shell.locator('#workbench-progress')).toBeHidden();
  await new Promise(resolve=>setTimeout(resolve,700));
  expect((await shell.evaluate(()=>window.desktop.state())).active).toBe(null);
  await shell.getByRole('button',{name:'工作台',exact:true}).click();
  await expect(shell.locator('#home-page')).toBeHidden();
});
test('real window, isolated tabs, navigation, popup, upload and non-stealing background', async () => {
  const shell = await launch();
  await expect(shell.locator('h1')).toHaveText('RecruitOps 工作台');
  const state = await shell.evaluate(() => window.desktop.state());
  expect(state.writesEnabled).toBe(false); expect(state.apiConfigured).toBe(false);
  await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1280, 720));
  await shell.screenshot({ path: 'test-results/offline-1280.png' });
  await invoke(shell, { action: 'open', url: origin + '/jobs' });
  const site = await sitePage(); await site.waitForLoadState();
  expect(await site.evaluate(() => [typeof window.desktop, typeof require, typeof process])).toEqual(['undefined', 'undefined', 'undefined']);
  await site.locator('#name').fill('Anonymous Candidate');
  await site.locator('#resume').setInputFiles({ name: 'anonymous-resume.txt', mimeType: 'text/plain', buffer: Buffer.from('Anonymous test resume') });
  expect(await site.locator('#resume').evaluate(input => input.files[0].name)).toBe('anonymous-resume.txt');
  await site.evaluate(() => { localStorage.setItem('fixture-login', 'anonymous'); document.cookie = 'fixture=anonymous; SameSite=Lax'; });
  await invoke(shell, { action: 'home' });
  await invoke(shell, { action: 'select', id: 1 });
  await expect(site.locator('#name')).toHaveValue('Anonymous Candidate');
  await site.locator('#next').click(); await expect(site).toHaveURL(origin + '/next');
  await invoke(shell, { action: 'back' }); await expect(site).toHaveURL(origin + '/jobs');
  await invoke(shell, { action: 'forward' }); await expect(site).toHaveURL(origin + '/next');
  const before = (await shell.evaluate(() => window.desktop.state())).active;
  await desktop.evaluate(async (_electron, { origin, mainPath }) => { const main = process.getBuiltinModule('module').createRequire(mainPath)(mainPath); globalThis.reviewLease = main.createBackgroundPage(origin + '/review'); }, { origin, mainPath: path.resolve(__dirname, '../dist/main.js') });
  expect((await shell.evaluate(() => window.desktop.state())).active).toBe(before);
  await expect(site).toHaveURL(origin + '/next');
  await desktop.evaluate(() => globalThis.reviewLease.close());
  await site.locator('#popup').click();
  await expect.poll(async () => (await shell.evaluate(() => window.desktop.state())).tabs.length).toBe(2);
  await expect.poll(async () => (await shell.evaluate(() => window.desktop.state())).tabs.every(t => !t.loading)).toBe(true);
  const popup = desktop.context().pages().find(page => page.url() === origin + '/sso');
  await expect(popup.locator('h1')).toHaveText('招聘官网离线夹具');
  await popup.screenshot({ path: 'test-results/recruitment-page.png' });
  const prefs = await desktop.evaluate(({ webContents }) => webContents.getAllWebContents().filter(w => w.getType() !== 'remote').map(w => w.getLastWebPreferences()));
  for (const pref of prefs) { expect(pref.nodeIntegration).toBe(false); expect(pref.contextIsolation).toBe(true); expect(pref.sandbox).toBe(true); }
  const png = await desktop.evaluate(async ({ BrowserWindow }) => Array.from((await BrowserWindow.getAllWindows()[0].capturePage()).toPNG()));
  await writeFile('test-results/tabs-1280.png', Buffer.from(png));
  const windowPng = await desktop.evaluate(async ({ BrowserWindow, desktopCapturer }) => {
    const windowId = BrowserWindow.getAllWindows()[0].getMediaSourceId();
    const sources = await desktopCapturer.getSources({ types: ['window'], thumbnailSize: { width: 1280, height: 720 }, fetchWindowIcons: false });
    const ownWindow = sources.find(source => source.id === windowId);
    if (!ownWindow || ownWindow.thumbnail.isEmpty()) throw new Error('Native window capture unavailable');
    return Array.from(ownWindow.thumbnail.toPNG());
  });
  await writeFile('test-results/native-window.png', Buffer.from(windowPng));
});
test('persistent dedicated login, local-network blocking and renderer IPC isolation', async () => {
  let shell = await launch();
  await invoke(shell, { action: 'open', url: origin + '/jobs' });
  let page = await sitePage(); await page.waitForLoadState();
  await page.evaluate(() => localStorage.setItem('fixture-login', 'anonymous'));
  await desktop.evaluate(async ({ session }) => { const s = session.fromPartition('persist:recruitment'); s.flushStorageData(); await s.cookies.flushStore(); });
  await desktop.close(); desktop = undefined;
  shell = await launch();
  await invoke(shell, { action: 'open', url: origin + '/jobs' });
  const site = await sitePage(); await site.waitForLoadState();
  expect(await site.evaluate(() => localStorage.getItem('fixture-login'))).toBe('anonymous');
  await expect(invoke(shell, { action: 'open', url: 'http://127.0.0.1:8012' })).rejects.toThrow();
  const requestResult = await site.evaluate(async () => { try { await fetch('http://127.0.0.1:5433'); return 'allowed'; } catch { return 'blocked'; } });
  expect(requestResult).toBe('blocked');
  await invoke(shell, { action: 'home' });
  await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setSize(1920, 1080));
  await shell.screenshot({ path: 'test-results/offline-1920.png' });
  await invoke(shell, { action: 'select', id: 1 });
  await desktop.evaluate(async ({ session, dialog }, origin) => {
    await session.fromPartition('persist:recruitment').cookies.set({ url: origin + '/nested', name: 'nested', value: 'anonymous', path: '/nested' });
    await session.fromPartition('persist:recruitment').cookies.set({ url: 'https://unrelated.example', name: 'unrelated', value: 'preserve' });
    dialog.showMessageBox = async () => ({ response: 1, checkboxChecked: false });
  }, origin);
  await invoke(shell, { action: 'clear-site' });
  const cookies = await desktop.evaluate(({ session }) => session.fromPartition('persist:recruitment').cookies.get({}));
  expect(cookies.some(cookie => cookie.name === 'nested')).toBe(false);
  expect(cookies.some(cookie => cookie.name === 'unrelated')).toBe(true);
  await invoke(shell, { action: 'close', id: 1 });
  expect((await shell.evaluate(() => window.desktop.state())).tabs).toHaveLength(0);
});
test('invalid API configuration stays offline and load failure is explicit', async () => {
  const shell = await launch({ RECRUITOPS_DESKTOP_API_ORIGIN: 'http://127.0.0.1:8012' });
  await expect(shell.locator('#configuration-error')).toContainText('Only an owned runtime');
  await invoke(shell, { action: 'open', url: origin + '/offline' });
  await expect.poll(async () => (await shell.evaluate(() => window.desktop.state())).tabs[0].error).toContain('Page load failed');
});

test('single instance and close-to-tray preserve the first window', async () => {
  const shell = await launch({ RECRUITOPS_DESKTOP_TEST: '0' });
  const { spawn } = require('node:child_process');
  const env = Object.fromEntries(['SystemRoot', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'COMSPEC', 'APPDATA', 'LOCALAPPDATA', 'USERPROFILE'].filter(key => process.env[key]).map(key => [key, process.env[key]]));
  const second = spawn(require('electron'), [path.resolve(__dirname, '..')], { windowsHide: true, env: { ...env, RECRUITOPS_DESKTOP_DATA_DIR: profile }, stdio: 'ignore' });
  const exitCode = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { second.kill(); reject(new Error('Second instance did not exit')); }, 10000);
    second.on('error', error => { clearTimeout(timer); reject(error); });
    second.on('exit', code => { clearTimeout(timer); resolve(code); });
  });
  expect(exitCode).toBe(0);
  expect(await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length)).toBe(1);
  await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].close());
  expect(await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isVisible())).toBe(false);
  await desktop.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].show());
  await expect(shell.locator('h1')).toHaveText('RecruitOps 工作台');
});

for (const scale of [1.25, 1.5]) test(`Chromium device scale ${scale} keeps shell controls in bounds`, async () => {
  const env = Object.fromEntries(['SystemRoot', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'COMSPEC', 'APPDATA', 'LOCALAPPDATA', 'USERPROFILE'].filter(key => process.env[key]).map(key => [key, process.env[key]]));
  desktop = await electron.launch({ args: [path.resolve(__dirname, '..'), `--force-device-scale-factor=${scale}`], env: { ...env, RECRUITOPS_DESKTOP_DATA_DIR: profile, RECRUITOPS_DESKTOP_TEST: '1' } });
  const shell = await desktop.firstWindow(); await shell.waitForLoadState();
  expect(await shell.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await expect(shell.locator('#quit')).toBeInViewport();
  await shell.screenshot({ path: `test-results/scale-${scale}.png` });
});

for(const target of ['_blank','_self']) test(`workbench official progress anchor ${target} opens isolated website without runtime credentials`,async()=>{
  const shell=await launch({RECRUITOPS_DESKTOP_TEST_RUNTIME:'read-only'});
  await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).active).toBe('workbench');
  await expect.poll(()=>desktop.context().pages().some(p=>p!==shell&&p.url().startsWith('http://127.0.0.1:'))).toBe(true);
  const page=desktop.context().pages().find(p=>p!==shell&&p.url().startsWith('http://127.0.0.1:'));
  await page.waitForLoadState();
  const original=page.url();
  const url=origin+'/progress-'+target;
  await page.evaluate(({url,target})=>{
    const a=document.createElement('a');a.href=url;a.target=target;a.rel='noreferrer';a.textContent='查看投递进度';document.body.append(a);
  },{url,target});
  await page.getByRole('link',{name:'查看投递进度',exact:true}).click({noWaitAfter:true});
  const website=await sitePage();
  await expect(website.locator('h1')).toHaveText('招聘官网离线夹具');
  expect(page.url()).toBe(original);
  const state=await shell.evaluate(()=>window.desktop.state());
  expect(state.tabs).toHaveLength(1);expect(state.active).toBe(state.tabs[0].id);
  expect(await desktop.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows().length)).toBe(1);
  expect(await website.evaluate(()=>[typeof window.desktop,typeof window.recruitopsDesktop,typeof require])).toEqual(['undefined','undefined','undefined']);
  const request=siteRequests.find(r=>r.url==='/progress-'+target);
  expect(request).toBeTruthy();expect(request.authorization).toBeUndefined();expect(request.referer).toBeUndefined();
  await shell.getByRole('button',{name:'工作台',exact:true}).click();
  expect((await shell.evaluate(()=>window.desktop.state())).active).toBe('workbench');
  if(target==='_self') return;
  for(const blocked of ['http://127.0.0.1:8012/','https://127.0.0.1/','file:///C:/fixture','https://user:secret@example.com/']) {
    await page.evaluate(url=>{document.querySelector('a').href=url;document.querySelector('a').target='_blank';},blocked);
    await page.getByRole('link',{name:'查看投递进度',exact:true}).click();
    await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).notice).toContain('无法打开此链接');
    expect((await shell.evaluate(()=>window.desktop.state())).tabs).toHaveLength(1);
    expect(page.url()).toBe(original);
  }
});

test('local filler real buttons preview, confirm, undo and invalidate after navigation',async()=>{
  const shell=await launch({RECRUITOPS_DESKTOP_TEST_RUNTIME:'filler'});
  await expect.poll(async () => (await shell.evaluate(() => window.desktop.state())).runtime.status).toBe('ready');
  await invoke(shell,{action:'open',url:origin+'/fill'});
  const page=await sitePage();await page.waitForLoadState();
  const profileFile=path.join(profile,'synthetic-resume.json');
  const resumeFile=path.join(profile,'synthetic-resume.pdf');
  await writeFile(profileFile,JSON.stringify({name:'Synthetic Candidate'}));
  await writeFile(resumeFile,Buffer.from('%PDF-1.4\n% anonymous fixture\n'));
  await desktop.evaluate(({dialog},paths)=>{
    const queue=[paths.profile,paths.resume];
    dialog.showOpenDialog=async()=>({canceled:false,filePaths:[queue.shift()]});
    dialog.showMessageBox=async()=>({response:1});
  },{profile:profileFile,resume:resumeFile});
  await shell.locator('#filler-open').click();
  await expect(shell.locator('#filler-panel')).toBeVisible();
  await invoke(shell,{action:'filler-profile-import'});
  await invoke(shell,{action:'filler-attachment-select'});
  await shell.locator('#filler-scan').click();
  await expect(shell.locator('#filler-fields')).toContainText('Synthetic Candidate');
  await expect(shell.locator('#filler-attachment-upload')).toBeEnabled();
  await shell.locator('#filler-attachment-upload').click();
  await expect.poll(()=>page.locator('#resume').evaluate(input=>input.files?.[0]?.name||'')).toBe('synthetic-resume.pdf');
  await shell.locator('#filler-scan').click();
  expect(await page.locator('#name').inputValue()).toBe('');
  await shell.locator('#filler-confirm').click();
  await expect(shell.locator('#filler-panel')).toBeHidden();
  await expect(page.locator('#name')).toHaveValue('Synthetic Candidate');
  expect(await page.evaluate(()=>typeof window.__RECRUITOPS_LOCAL_FILLER_V1__)).toBe('undefined');
  await shell.locator('#filler-open').click();
  await shell.locator('#filler-undo').click();
  await expect(page.locator('#name')).toHaveValue('');
  await shell.locator('#filler-open').click();
  await shell.locator('#filler-scan').click();
  await expect(shell.locator('#filler-confirm')).toBeEnabled();
  const stale=(await shell.evaluate(()=>window.desktop.state())).filler.scanId;
  await shell.locator('#filler-close').click();
  await page.locator('#next').click();
  await shell.locator('#filler-open').click();
  await expect(shell.locator('#filler-confirm')).toBeDisabled();
  await expect(shell.locator('#filler-undo')).toBeDisabled();
  await expect(invoke(shell,{action:'filler-fill',scanId:stale,fieldIds:['fixture-name']})).rejects.toThrow();
  await expect(page.locator('#name')).toHaveValue('');
});

for (const mode of ['read-only', 'writes']) test(`owned ${mode} workbench authenticates HTTP and gates business writes/WS`, async () => {
  const shell = await launch({ RECRUITOPS_DESKTOP_TEST_RUNTIME: mode });
  await expect.poll(async () => (await shell.evaluate(() => window.desktop.state())).runtime.status).toBe('ready');
  await invoke(shell, { action: 'workbench' });
  await expect.poll(() => desktop.context().pages().find(page => page !== shell && page.url().startsWith('http://127.0.0.1:'))?.url()).toBeTruthy();
  const page = desktop.context().pages().find(page => page !== shell && page.url().startsWith('http://127.0.0.1:'));
  await expect(page.locator('h1')).toHaveText('Owned fixture workbench');
  expect(await page.evaluate(() => [typeof window.desktop, typeof require, typeof process])).toEqual(['undefined', 'undefined', 'undefined']);
  expect(await page.evaluate(() => Object.keys(window.recruitopsDesktop))).toEqual(['onApplicationDraft','onDataChanged']);
  expect(await page.evaluate(() => fetch('/api/read').then(r => r.json()))).toEqual({ fixture: true, read: true });
  const post = await page.evaluate(() => fetch('/api/write', { method: 'POST' }).then(r => r.json()).catch(() => 'blocked'));
  expect(post).toEqual(mode === 'writes' ? { fixture: true, saved: true } : 'blocked');
  const ws = await page.evaluate(() => new Promise(resolve => {
    const socket = new WebSocket(location.origin.replace('http:', 'ws:') + '/ws');
    const timer = setTimeout(() => { socket.close(); resolve('timeout'); }, 3000);
    socket.onmessage = event => { clearTimeout(timer); socket.close(); resolve(event.data); };
    socket.onerror = () => { clearTimeout(timer); resolve('blocked'); };
  }));
  expect(ws).toBe(mode === 'writes' ? 'fixture' : 'blocked');
  expect(await page.evaluate(async () => { try { await fetch('http://127.0.0.1:5433/'); return 'allowed'; } catch { return 'blocked'; } })).toBe('blocked');
  await invoke(shell, { action: 'open', url: origin + '/jobs' });
  const site = await sitePage();
  expect(await site.evaluate(() => typeof window.recruitopsDesktop)).toBe('undefined');
  expect(await site.evaluate(async target => { try { await fetch(target); return 'allowed'; } catch { return 'blocked'; } }, page.url())).toBe('blocked');
  await site.waitForLoadState();
  await invoke(shell, { action: 'capture' });
  await invoke(shell, { action: 'use-capture' });
  const draft = await page.evaluate(() => new Promise(resolve => window.recruitopsDesktop.onApplicationDraft(resolve)));
  expect(draft.company_name).toBe(''); expect(draft.record_url).toBe('');
  expect(draft.job_title).toBeTruthy(); expect(draft.note).toContain(origin);
  await invoke(shell, { action: 'home' });
  await shell.screenshot({ path: `test-results/runtime-${mode}.png` });
});

test('runtime identity failure is visible and cannot open a workbench', async () => {
  const shell = await launch({ RECRUITOPS_DESKTOP_TEST_RUNTIME: 'identity-failure' });
  await expect.poll(async () => (await shell.evaluate(() => window.desktop.state())).runtime.status).toBe('failed');
  await expect(shell.locator('#api-status')).toContainText('runtime_identity_failed');
  await expect(shell.locator('#startup-error')).toBeVisible();
  await expect(shell.locator('#startup-error-message')).toContainText('runtime_identity_failed');
  await expect(shell.locator('#home-page')).toBeHidden();
  await expect(shell.locator('#workbench-progress')).toBeHidden();
  await shell.getByRole('button',{name:'查看启动状态',exact:true}).click();
  await expect(shell.locator('#home-page')).toBeVisible();
  await expect(shell.locator('#startup-error')).toBeHidden();
  await invoke(shell, { action: 'workbench' });
  expect((await shell.evaluate(() => window.desktop.state())).active).toBe(null);
});

test('trusted write confirmation restarts exact owned instance and never persists opt-in', async () => {
  let shell=await launch({RECRUITOPS_DESKTOP_TEST_RUNTIME:'read-only'});
  await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).runtime.status).toBe('ready');
  await desktop.evaluate(({dialog})=>{dialog.showMessageBox=async()=>({response:0});});
  await invoke(shell,{action:'enable-writes'});
  expect((await shell.evaluate(()=>window.desktop.state())).writesEnabled).toBe(false);
  await desktop.evaluate(({dialog})=>{dialog.showMessageBox=async(_window,options)=>{
    if(!options.detail.includes('fixture-instance')) throw new Error('Wrong instance confirmation');
    return {response:1};
  };});
  await invoke(shell,{action:'enable-writes'});
  await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).writesEnabled).toBe(true);
  await expect(shell.locator('#runtime-writes')).toHaveText('高级：切换只读模式');
  await invoke(shell,{action:'disable-writes'});
  await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).runtime.status).toBe('ready');
  expect((await shell.evaluate(()=>window.desktop.state())).writesEnabled).toBe(false);
  await invoke(shell,{action:'enable-writes'});
  await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).writesEnabled).toBe(true);
  await desktop.close();desktop=undefined;
  shell=await launch({RECRUITOPS_DESKTOP_TEST_RUNTIME:'read-only'});
  await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).runtime.status).toBe('ready');
  expect((await shell.evaluate(()=>window.desktop.state())).writesEnabled).toBe(false);
});

test('adapter manual capture is evidence-only and hidden review keeps foreground untouched', async () => {
  const shell = await launch({RECRUITOPS_DESKTOP_TEST_RUNTIME:'read-only'});
  await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).runtime.status).toBe('ready');
  await expect.poll(()=>desktop.context().pages().some(p=>p.url().startsWith('http://127.0.0.1:')&&!p.url().startsWith(origin))).toBe(true);
  const workbench=desktop.context().pages().find(p=>p.url().startsWith('http://127.0.0.1:')&&!p.url().startsWith(origin));
  await workbench.waitForLoadState();
  await workbench.evaluate(()=>{window.fixtureDrafts=[];window.recruitopsDesktop.onApplicationDraft(draft=>window.fixtureDrafts.push(draft));});
  await invoke(shell, { action: 'open', url: origin + '/jobs' });
  const page = await sitePage(); await page.waitForLoadState();
  await expect.poll(async () => (await shell.evaluate(() => window.desktop.state())).browser.connected).toBe(true);
  const review = await desktop.evaluate(async (_electron, { origin, mainPath }) => {
    const main = process.getBuiltinModule('module').createRequire(mainPath)(mainPath);
    return main.reviewPage(origin + '/review', 'fixture-review', ['fixture-application']);
  }, { origin, mainPath: path.resolve(__dirname, '../dist/main.js') });
  expect(review.result.evidence_only).toBe(true);
  expect(review.result.database_updated).toBe(false);
  expect(review.result.application_ids).toEqual(['fixture-application']);
  expect((await shell.evaluate(() => window.desktop.state())).active).toBe(1);
  await expect(page).toHaveURL(origin + '/jobs');
  await shell.getByRole('button',{name:'采集草稿',exact:true}).click();
  await expect(shell.locator('#home-page')).toBeVisible();
  await expect(shell.locator('#workbench-progress')).toBeHidden();
  await expect(shell.locator('#capture-result')).toBeVisible();
  await expect(shell.locator('#use-capture')).toBeVisible();
  const state = await shell.evaluate(() => window.desktop.state());
  expect(state.captureDraft.result.kind).toBe('manual_capture');
  expect(state.captureDraft.result.requires_user_confirmation).toBe(true);
  expect(state.captureDraft.result.database_updated).toBe(false);
  await expect(shell.locator('#capture-result')).toContainText('manual_capture');
  expect(state.tabs).toHaveLength(1);
  expect(state.workbenchRequested).toBe(false);
  await shell.getByRole('button',{name:'在手动表单中使用草稿',exact:true}).click();
  await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).active).toBe('workbench');
  await expect.poll(()=>workbench.evaluate(()=>window.fixtureDrafts.length)).toBe(1);
  const draft=await workbench.evaluate(()=>window.fixtureDrafts[0]);
  expect(draft.company_name).toBe('');expect(draft.record_url).toBe('');
  expect(draft.job_title.length).toBeLessThanOrEqual(512);expect(draft.note.length).toBeLessThanOrEqual(4000);
  expect((await shell.evaluate(()=>window.desktop.state())).writesEnabled).toBe(false);
  await expect(shell.locator('#home-page')).toBeHidden();
  await expect(shell.locator('#use-capture')).toBeHidden();
});
