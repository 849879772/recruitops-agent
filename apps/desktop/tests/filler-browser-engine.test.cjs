const test = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');
const api = require('../../../packages/desktop_filler/index.cjs');
const executablePath = process.env.RECRUITOPS_TEST_CHROMIUM;
const bundle = api.loadDesktopFiller();

async function fixture(t, body) {
  const browser = await chromium.launch({ headless: true, executablePath });
  t.after(() => browser.close());
  const page = await browser.newPage();
  await page.route('**/*', route => route.request().url() === 'https://fixture.example/form'
    ? route.fulfill({ contentType: 'text/html; charset=utf-8', body }) : route.abort());
  await page.goto('https://fixture.example/form');
  return page;
}

test('desktop uses the three packaged original DOM modules, not the compact engine', () => {
  assert.deepEqual(bundle.files.map(f => f.name), ['core.js', 'repeater-engine.js', 'content.js']);
  assert.notEqual(bundle.hash, api.loadBundledFiller().hash);
  for (const f of bundle.files) assert.match(f.sha256, /^[a-f0-9]{64}$/);
});

test('original engine fills all recognized fields while excluding secrets and submission', { skip: !executablePath }, async t => {
  const page = await fixture(t, `<form>
    <div class="form-item"><label for="name">姓名</label><input id="name"></div>
    <div class="form-item"><label for="phone">手机号码</label><input id="phone"></div>
    <div class="form-item"><label for="email">电子邮箱</label><input id="email"></div>
    <div class="form-item"><label for="site">面试地点</label><select id="site"><option value="">请选择</option><option>武汉</option></select></div>
    <div class="form-item"><label for="otp">验证码</label><input id="otp"></div>
    <div class="form-item"><label for="password">密码</label><input type="password" id="password"></div>
    <button type="submit">预览并提交</button>
  </form><script>window.submits=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submits++}</script>`);
  const scan = await page.evaluate(api.buildScanScript(bundle, { basic: {
    fullName: 'Synthetic Candidate', phone: '13000000000', email: 'fixture@example.test', interviewSite: '武汉', password: 'never', otp: 'never'
  }}));
  assert.equal(scan.matches.length, 4, JSON.stringify(scan.matches));
  const result = await page.evaluate(api.buildFillScript(bundle, { scanId: scan.scanId, fieldIds: scan.matches.map(m => m.fieldId), confirmed: true }));
  assert.equal(result.filled, 4, JSON.stringify(result));
  assert.equal(await page.locator('#name').inputValue(), 'Synthetic Candidate');
  assert.equal(await page.locator('#site').inputValue(), '武汉');
  assert.equal(await page.locator('#otp').inputValue(), '');
  assert.equal(await page.locator('#password').inputValue(), '');
  assert.equal(await page.evaluate('window.submits'), 0);
  await page.locator('#name').fill('Manual');
  await assert.rejects(page.evaluate(api.buildUndoScript(bundle)), /filler_undo_document_changed/);
  assert.equal(await page.locator('#name').inputValue(), 'Manual');
});

test('original engine accepts site-scoped custom answers and rejects stale nodes', { skip: !executablePath }, async t => {
  const page = await fixture(t, '<div class="form-item"><label for="extra">补充说明</label><textarea id="extra"></textarea></div>');
  const profile = { customAnswers: [{ origin: 'https://fixture.example', pathname: '/form', label: '补充说明', value: 'Synthetic answer' }] };
  const scan = await page.evaluate(api.buildScanScript(bundle, profile));
  assert.equal(scan.matches.length, 1);
  await page.evaluate(() => { const el = document.querySelector('textarea'); el.replaceWith(el.cloneNode()); });
  await assert.rejects(page.evaluate(api.buildFillScript(bundle, { scanId: scan.scanId, fieldIds: scan.matches.map(m => m.fieldId), confirmed: true })), /filler_field_changed_rescan/);
  const wrong = await page.evaluate(api.buildScanScript(bundle, { customAnswers: [{ ...profile.customAnswers[0], origin: 'https://other.example' }] }));
  assert.equal(wrong.matches.length, 0);
});

test('original engine respects cancellation queued before fill', { skip: !executablePath }, async t => {
  const page = await fixture(t, '<div class="form-item"><label for="name">姓名</label><input id="name"></div>');
  const scan = await page.evaluate(api.buildScanScript(bundle, { basic: { fullName: 'Synthetic' } }));
  const script = api.buildFillScript(bundle, { scanId: scan.scanId, fieldIds: scan.matches.map(m => m.fieldId), confirmed: true });
  await page.evaluate(api.buildCancelScript(bundle));
  assert.equal((await page.evaluate(script)).code, 'filler_fill_cancelled');
  assert.equal(await page.locator('input').inputValue(), '');
});

test('original engine fills Beisen framework labels and Phoenix dropdowns', { skip: !executablePath }, async t => {
  const page = await fixture(t, `<section class="form-part"><h2 class="head-title">个人信息</h2>
    <div class="form-item"><div class="form-item__title"><span class="form-item__text">姓名</span></div><input id="name" placeholder="请输入"></div>
    <div class="form-item"><div class="form-item__title"><span class="form-item__text">第几届应届生</span></div>
      <div class="phoenix-select"><span class="phoenix-select__placeHolder--show">请选择</span><span class="phoenix-select__singleLabel"></span><input id="year" class="phoenix-select__input" readonly></div>
    </div>
  </section><div id="choices" hidden><div class="phoenix-selectList__listItem">2026届</div><div class="phoenix-selectList__listItem">2027届</div></div>
  <script>
    const input=document.querySelector('#year'),list=document.querySelector('#choices');
    input.onclick=()=>{list.hidden=false};
    for(const item of list.children)item.onclick=()=>{
      document.querySelector('.phoenix-select__placeHolder--show').remove();
      document.querySelector('.phoenix-select__singleLabel').textContent=item.textContent;
      list.hidden=true;
    };
  </script>`);
  const scan = await page.evaluate(api.buildScanScript(bundle, { basic: { fullName: 'Synthetic' }, education: [{ endDate: '2027-06' }] }));
  assert.equal(scan.matches.length, 2, JSON.stringify(scan));
  const result = await page.evaluate(api.buildFillScript(bundle, { scanId: scan.scanId, fieldIds: scan.matches.map(m => m.fieldId), confirmed: true }));
  assert.equal(result.filled, 2, JSON.stringify(result));
  assert.equal(await page.locator('#name').inputValue(), 'Synthetic');
  assert.equal(await page.locator('.phoenix-select__singleLabel').textContent(), '2027届');
});

test('original repeater preparation adds a bounded education row and never submits', { skip: !executablePath }, async t => {
  const page = await fixture(t, `<form><section id="education"><h2>教育经历</h2>
    <div class="education-entry education-item"><label>学校名称<input></label><label>专业名称<input></label><label>学院名称<input></label></div>
    <button type="button" id="add">添加教育经历</button><button type="submit">提交</button>
  </section></form><script>
    window.submits=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submits++};
    document.querySelector('#add').onclick=()=>{const row=document.querySelector('.education-entry').cloneNode(true);document.querySelector('#add').before(row)};
  </script>`);
  const profile = { education: [{ school: 'Synthetic A', major: 'Engineering' }, { school: 'Synthetic B', major: 'Computing' }] };
  const scan = await page.evaluate(api.buildScanScript(bundle, profile));
  assert.equal(scan.repeaters.length, 1, JSON.stringify(scan.repeaters));
  const result = await page.evaluate(api.buildPrepareScript(bundle, { scanId: scan.scanId, sectionIds: scan.repeaters.map(r => r.sectionId), confirmed: true }));
  assert.equal(result.code, 'filler_prepare_complete', JSON.stringify(result));
  assert.equal(await page.locator('.education-entry').count(), 2);
  const again = await page.evaluate(api.buildScanScript(bundle, profile));
  assert.equal(again.repeaters.length, 0);
  assert.equal(await page.evaluate('window.submits'), 0);
});

test('desktop preparation is add-only and cancellation stops later additions in the same section', { skip: !executablePath }, async t => {
  const page = await fixture(t, `<section id="education"><h2>教育经历</h2>
    <div class="education-item"><label>学校名称<input></label><label>专业名称<input></label><label>学院名称<input></label></div>
    <button type="button" id="add">添加教育经历</button>
  </section><section><h2>实习经历</h2><label><input type="checkbox" id="none">无实习经历</label></section>
  <section><h2>技能</h2><button id="remove">删除技能</button></section><script>
    window.additions=0;window.deletions=0;
    document.querySelector('#remove').onclick=()=>window.deletions++;
    document.querySelector('#add').onclick=()=>{window.additions++;setTimeout(()=>{
      const row=document.querySelector('.education-item').cloneNode(true);document.querySelector('#add').before(row);
    },200)};
  </script>`);
  const profile = { education: [{ school: 'A' }, { school: 'B' }, { school: 'C' }] };
  const scan = await page.evaluate(api.buildScanScript(bundle, profile));
  const preparing = page.evaluate(api.buildPrepareScript(bundle, { scanId: scan.scanId, sectionIds: scan.repeaters.map(r => r.sectionId), confirmed: true }));
  await page.waitForFunction('window.additions===1');
  await page.evaluate(api.buildCancelScript(bundle));
  assert.equal((await preparing).code, 'filler_prepare_cancelled');
  await page.waitForFunction('document.querySelectorAll(".education-item").length===2');
  assert.equal(await page.evaluate('window.additions'), 1);
  assert.equal(await page.evaluate('window.deletions'), 0);
  assert.equal(await page.locator('#none').isChecked(), false);
});
