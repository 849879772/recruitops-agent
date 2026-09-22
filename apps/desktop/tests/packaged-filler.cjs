const {expect}=require('@playwright/test');
const fs=require('node:fs/promises');
const path=require('node:path');

module.exports=async function packagedFiller(desktop,shell,workbench,profile) {
  const target='https://example.com/recruitops-anonymous-fixture';
  const requests=[];
  await desktop.context().route('https://example.com/**',async route=>{
    requests.push(await route.request().allHeaders());
    await route.fulfill({status:200,contentType:'text/html; charset=utf-8',body:'<!doctype html><title>Synthetic recruitment form</title><h1>Synthetic recruitment form</h1><form><label>姓名<input id="name"></label><label>密码<input type="password" id="password"></label><label>验证码<input id="otp" autocomplete="one-time-code"></label><button type="submit">Submit fixture</button></form><a href="/recruitops-next">Next fixture</a>'});
  });
  const record=await workbench.evaluate(async url=>{
    const response=await fetch('/api/local-ui/applications/manual',{method:'POST',headers:{'Content-Type':'application/json','X-RecruitOps-Local-UI':'1'},
      body:JSON.stringify({company_name:'Synthetic Link Fixture',job_title:'Synthetic Link Role',stage:'applied',record_url:url,note:'Anonymous link/filler acceptance only'})});
    return {status:response.status,body:await response.json()};
  },target);
  expect(record.status).toBe(200);expect(record.body.created).toBe(true);
  await workbench.locator('[data-view="applications"]').click();
  await workbench.getByRole('link',{name:'查看投递进度 ›',exact:true}).click();
  await expect.poll(()=>desktop.context().pages().some(p=>p.url()===target)).toBe(true);
  const page=desktop.context().pages().find(p=>p.url()===target);
  await expect(page.locator('h1')).toHaveText('Synthetic recruitment form');
  await expect.poll(async()=>typeof (await shell.evaluate(()=>window.desktop.state())).active).toBe('number');
  await expect(shell.locator('#filler-open')).toBeEnabled();
  expect(requests.length).toBeGreaterThan(0);
  for(const headers of requests){expect(headers.authorization).toBeUndefined();expect(headers.referer).toBeUndefined();}
  const profileFile=path.join(profile,'synthetic-filler.json');
  const resumeFile=path.join(profile,'synthetic-resume.pdf');
  await fs.writeFile(profileFile,JSON.stringify({basic:{fullName:'Synthetic Packaged Candidate'}}));
  await fs.writeFile(resumeFile,Buffer.from('%PDF-1.4\n% anonymous packaged fixture\n'));
  await desktop.evaluate(({dialog},paths)=>{
    const picks=[paths.profile,paths.resume];
    dialog.showOpenDialog=async()=>({canceled:false,filePaths:[picks.shift()]});
    dialog.showMessageBox=async()=>({response:1});
  },{profile:profileFile,resume:resumeFile});
  await shell.locator('#filler-open').click();
  await shell.getByRole('tab',{name:'简历资料',exact:true}).click();
  await shell.locator('#filler-profile-import').click();
  await shell.locator('#filler-attachment-select').click();
  await shell.getByRole('tab',{name:'扫描与填写',exact:true}).click();
  await shell.locator('#filler-advanced > summary').click();
  await shell.locator('#filler-scan').click();
  await expect(shell.locator('#filler-fields')).toContainText('Synthetic Packaged Candidate');
  await shell.screenshot({path:'test-results/packaged-filler-preview.png'});
  await expect(page.locator('#name')).toHaveValue('');
  await expect(page.locator('#password')).toHaveValue('');
  await expect(page.locator('#otp')).toHaveValue('');
  await shell.locator('#filler-confirm').click();await expect(page.locator('#name')).toHaveValue('Synthetic Packaged Candidate');
  await expect(page.locator('#password')).toHaveValue('');await expect(page.locator('#otp')).toHaveValue('');
  expect(page.url()).toBe(target);
  await page.screenshot({path:'test-results/packaged-filler-filled.png'});
  await shell.locator('#filler-open').click();await shell.locator('#filler-undo').click();await expect(page.locator('#name')).toHaveValue('');
  await shell.locator('#filler-open').click();await shell.locator('#filler-scan').click();await expect(shell.locator('#filler-confirm')).toBeEnabled();
  await shell.locator('#filler-close').click();await page.getByRole('link',{name:'Next fixture'}).click();
  await shell.locator('#filler-open').click();await expect(shell.locator('#filler-confirm')).toBeDisabled();await expect(shell.locator('#filler-undo')).toBeDisabled();
  await shell.getByRole('button',{name:'工作台',exact:true}).click();
  return {link_actual_click:true,isolated_public_url_intercepted_no_live_site:true,preview_fill_undo:true,navigation_invalidates:true,source:'bundled filler engine with anonymous profile; no private extension or profile'};
};
