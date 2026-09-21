const { test, expect, _electron: electron } = require('@playwright/test');
const fs = require('node:fs/promises');
const path = require('node:path');
const { createHash } = require('node:crypto');
const root = path.resolve(__dirname,'../../..');

test('NEW native package owns backend, authenticates workbench and persists synthetic record across restart',async()=>{
  test.setTimeout(900000);
  const executable=process.env.RECRUITOPS_DESKTOP_TEST_NEW_BUILD;
  test.skip(!executable,'No integrated native package supplied; fixtures are not packaged runtime acceptance');
  const buildRoot=path.join(root,'artifacts/desktop-builds');
  const relative=path.relative(buildRoot,path.resolve(executable));
  expect(relative.startsWith('..')||path.isAbsolute(relative)).toBe(false);
  const isolated=path.join(root,'.desktop-runtime-tests');
  await fs.mkdir(isolated,{recursive:true});
  const profile=await fs.mkdtemp(path.join(isolated,'packaged-shell-acceptance-'));
  const instanceRoot=path.join(isolated,'shell-runtime-'+createHash('sha256').update(profile.toLowerCase()).digest('hex').slice(0,24));
  let longPathFixture;
  const env=Object.fromEntries(['SystemRoot','WINDIR','COMSPEC'].filter(k=>process.env[k]).map(k=>[k,process.env[k]]));
  Object.assign(env,{RECRUITOPS_DESKTOP_DATA_DIR:profile,RECRUITOPS_DESKTOP_ISOLATION_ROOT:isolated,
    HOME:profile,USERPROFILE:profile,APPDATA:profile,LOCALAPPDATA:profile,TEMP:profile,TMP:profile,
    NO_PROXY:'127.0.0.1,localhost,::1'});
  let desktop;
  const lifecycle=[];
  let fillerAcceptance=null;
  async function waitForRuntime(shell, writes = false) {
    const started = Date.now();
    const deadline = started + 360000;
    let last = '';
    while (Date.now() < deadline) {
      const state = await shell.evaluate(()=>window.desktop.state());
      const summary = JSON.stringify({runtime:state.runtime,configurationError:state.configurationError,runtimeRestarting:state.runtimeRestarting});
      if (summary !== last) {
        const elapsed=Date.now()-started;
        console.log(`Packaged runtime +${elapsed}ms:`,summary);
        lifecycle.push({elapsed_ms:elapsed,...JSON.parse(summary)}); last = summary;
      }
      if (state.configurationError || state.runtime.status === 'failed') throw new Error(`Packaged runtime failed: ${summary}`);
      if (state.runtime.status === 'ready' && (!writes || state.writesEnabled)) return;
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
    throw new Error(`Packaged runtime readiness timed out: ${last}`);
  }
  async function launch(readOnly = false) {
    desktop=await electron.launch({executablePath:path.resolve(executable),args:readOnly?['--read-only']:[],env,timeout:60000});
    const shell=await desktop.firstWindow();
    await expect(shell.locator('#home-page')).toBeHidden();
    await expect(shell.locator('#runtime-writes')).toBeHidden();
    await expect(shell.locator('#workbench-progress')).toBeVisible();
    await shell.screenshot({path:`test-results/packaged-native-startup${readOnly?'-readonly':''}.png`});
    await waitForRuntime(shell);
    const state=await shell.evaluate(()=>window.desktop.state());
    expect(state.apiConfigured).toBe(true);expect(state.writesEnabled).toBe(!readOnly);
    expect(await desktop.evaluate(({app})=>app.isPackaged)).toBe(true);
    await expect.poll(async()=>(await shell.evaluate(()=>window.desktop.state())).active).toBe('workbench');
    await expect.poll(()=>desktop.context().pages().find(p=>p.url().startsWith('http://127.0.0.1:'))?.url()).toBeTruthy();
    const workbench=desktop.context().pages().find(p=>p.url().startsWith('http://127.0.0.1:'));
    await expect(workbench.locator('#sidebar')).toBeVisible({timeout:30000});
    await expect(shell.locator('#home-page')).toBeHidden();
    await expect(shell.locator('#workbench-progress')).toBeHidden();
    if(process.env.RECRUITOPS_DESKTOP_TEST_BUTTON_DIAGNOSTIC==='1') {
      for(let tick=0;tick<4;tick++) {
        await new Promise(resolve=>setTimeout(resolve,1100));
        console.log('Real button heartbeat state:',JSON.stringify(await shell.evaluate(()=>window.desktop.state())));
        console.log('Native views:',JSON.stringify(await desktop.evaluate(({BrowserWindow})=>BrowserWindow.getAllWindows()[0].contentView.children.map(v=>({visible:v.getVisible(),bounds:v.getBounds(),url:v.webContents?.getURL()})))));
      }
      const png=await desktop.evaluate(async({BrowserWindow})=>Array.from((await BrowserWindow.getAllWindows()[0].capturePage()).toPNG()));
      await fs.writeFile('test-results/real-button-native-window.png',Buffer.from(png));
    }
    const result=await workbench.evaluate(async()=>{const response=await fetch('/api/applications');return {status:response.status,rows:await response.json()};});
    expect(result.status).toBe(200);expect(Array.isArray(result.rows)).toBe(true);
    return {shell,workbench,state,rows:result.rows};
  }
  async function stop() {if(desktop){await desktop.close();desktop=undefined;}}
  async function readConfiguration(workbench) {
    const result=await workbench.evaluate(async()=>{
      const response=await fetch('/api/local-ui/configuration/read',{method:'POST',headers:{'Content-Type':'application/json','X-RecruitOps-Local-UI':'1'},body:'{}'});
      return {status:response.status,body:await response.json()};
    });
    expect(result.status).toBe(200);
    expect(Array.isArray(result.body.model_connections)).toBe(true);
    expect(Array.isArray(result.body.options.industry_groups)).toBe(true);
    expect(Array.isArray(result.body.options.mail_providers)).toBe(true);
    expect(typeof result.body.onboarding.ready).toBe('boolean');
    expect(typeof result.body.profile).toBe('object');
    return result.body;
  }
  try {
    let running=await launch();const instance=running.state.runtime.instanceId;
    expect(running.rows).toHaveLength(0);
    if(process.env.RECRUITOPS_DESKTOP_TEST_FILLER==='1') fillerAcceptance=await require('./packaged-filler.cjs')(desktop,running.shell,running.workbench,profile);
    await running.shell.getByRole('button',{name:'启动状态',exact:true}).click();
    await expect(running.shell.locator('#runtime-writes')).toBeVisible();
    await running.shell.screenshot({path:'test-results/packaged-native-ready.png'});
    if (process.env.RECRUITOPS_DESKTOP_TEST_CONFIGURATION_DIAGNOSTIC === '1') {
      await running.shell.evaluate(()=>window.desktop.command({action:'workbench'}));
      const failures=[];
      running.workbench.on('requestfailed',request=>{
        if(request.url().includes('/configuration/')) failures.push({url:request.url(),method:request.method(),error:request.failure()?.errorText});
      });
      await running.workbench.locator('[data-view="configuration"]').click();
      const result=await running.workbench.evaluate(async()=>{
        try {
          const response=await fetch('/api/local-ui/configuration/read',{method:'POST',headers:{'Content-Type':'application/json','X-RecruitOps-Local-UI':'1'},body:'{}'});
          return {status:response.status,body:await response.text()};
        } catch(error) { return {status:null,error:error.message}; }
      });
      await running.workbench.screenshot({path:'test-results/packaged-native-configuration.png'});
      const evidence={endpoint:'/api/local-ui/configuration/read',method:'POST',result,failures};
      console.log('Configuration diagnostic:',JSON.stringify(evidence));
      await fs.writeFile(path.join(profile,'configuration-diagnostic.json'),JSON.stringify(evidence,null,2));
      return;
    }
    const initialConfiguration=await readConfiguration(running.workbench);
    await running.shell.evaluate(()=>window.desktop.command({action:'workbench'}));
    await running.workbench.locator('[data-view="configuration"]').click();
    await expect(running.workbench.locator('#configuration-readiness')).toHaveText('首次配置尚未完成');
    await running.workbench.screenshot({path:'test-results/packaged-native-configuration.png'});
    await stop();
    if(process.env.RECRUITOPS_DESKTOP_TEST_LONG_PATH==='1') {
      longPathFixture=path.join(instanceRoot,'codex','plugins','cache','synthetic-plugin',
        'nested-fixture-'.repeat(6),'nested-fixture-'.repeat(6),'fixture.txt');
      expect(longPathFixture.length).toBeGreaterThan(260);
      await fs.mkdir(path.toNamespacedPath(path.dirname(longPathFixture)),{recursive:true});
      await fs.writeFile(path.toNamespacedPath(longPathFixture),'synthetic long path; no credentials');
      console.log('Anonymous Codex cache fixture pathname length:',longPathFixture.length);
    }
    running=await launch(true);expect(running.state.runtime.instanceId).toBe(instance);
    if(longPathFixture) expect(await fs.readFile(path.toNamespacedPath(longPathFixture),'utf8')).toBe('synthetic long path; no credentials');
    expect((await readConfiguration(running.workbench)).bootstrap).toEqual({companies_created:false,profile_created:false});
    await desktop.evaluate(({dialog},id)=>{dialog.showMessageBox=async(_window,options)=>{
      if(!options.detail.includes(id)) throw new Error('wrong instance confirmation');return {response:1};
    };},instance);
    await running.shell.getByRole('button',{name:'启动状态',exact:true}).click();
    await running.shell.locator('#runtime-writes').click();
    await waitForRuntime(running.shell,true);
    await running.shell.evaluate(()=>window.desktop.command({action:'workbench'}));
    await expect.poll(()=>desktop.context().pages().find(p=>p.url().startsWith('http://127.0.0.1:'))?.url()).toBeTruthy();
    running.workbench=desktop.context().pages().find(p=>p.url().startsWith('http://127.0.0.1:'));
    await expect(running.workbench.locator('#sidebar')).toBeVisible({timeout:30000});
    const savedConfiguration=await running.workbench.evaluate(async()=>{
      const response=await fetch('/api/local-ui/configuration/save',{method:'POST',headers:{'Content-Type':'application/json','X-RecruitOps-Local-UI':'1'},
        body:JSON.stringify({settings:{mail_imap_mailbox:'Synthetic-Desktop-Acceptance',mail_enabled:false,llm_enabled:false}})});
      return {status:response.status,body:await response.json()};
    });
    expect(savedConfiguration.status).toBe(200);
    const created=await running.workbench.evaluate(async()=>{
      const response=await fetch('/api/local-ui/applications/manual',{method:'POST',headers:{'Content-Type':'application/json','X-RecruitOps-Local-UI':'1'},
        body:JSON.stringify({company_name:'Synthetic Desktop Acceptance',job_title:'Synthetic Role',stage:'applied',record_url:'https://ats.example/applications',note:'Isolated synthetic native package restart acceptance; no live account.'})});
      return {status:response.status,body:await response.json()};
    });
    expect(created.status).toBe(200);expect(created.body.created).toBe(true);
    await stop();
    running=await launch(true);expect(running.state.runtime.instanceId).toBe(instance);
    expect(running.rows.some(row=>row.id===created.body.application_id)).toBe(true);
    if(longPathFixture) expect(await fs.readFile(path.toNamespacedPath(longPathFixture),'utf8')).toBe('synthetic long path; no credentials');
    expect((await readConfiguration(running.workbench)).settings.mail_imap_mailbox).toBe('Synthetic-Desktop-Acceptance');
    await running.workbench.locator('[data-view="applications"]').click();
    await expect(running.workbench.locator('#applications-view .application-company').filter({hasText:'Synthetic Desktop Acceptance'})).toBeVisible();
    await running.workbench.screenshot({path:'test-results/packaged-native-workbench.png'});
    await fs.writeFile(path.join(profile,'shell-acceptance.json'),JSON.stringify({release_accepted:false,instance_id:instance,application_id:created.body.application_id,
      result:'native startup/workbench/opt-in synthetic write/restart passed',configuration_read_status:200,configuration_save_status:200,
      configuration_retained:true,long_path_restart:longPathFixture?{pathname_length:longPathFixture.length,retained:true}:null,
      filler:fillerAcceptance,model_or_live_site_acceptance:false,executable,lifecycle},null,2));
  } finally {await stop();}
});
