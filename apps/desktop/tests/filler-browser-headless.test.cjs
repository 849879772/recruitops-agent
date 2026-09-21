const test = require('node:test');
const assert = require('node:assert/strict');
const { chromium } = require('@playwright/test');
const api = require('../../../packages/desktop_filler/index.cjs');
const executablePath = process.env.RECRUITOPS_TEST_CHROMIUM;
const bundle = api.loadBundledFiller();

async function fixture(t, html) {
  const browser = await chromium.launch({ headless: true, executablePath });
  t.after(() => browser.close());
  const context = await browser.newContext();
  await context.route('**/*', route => route.request().url() === 'https://fixture.example/form'
    ? route.fulfill({ contentType: 'text/html; charset=utf-8', body: html }) : route.abort());
  const page = await context.newPage();
  await page.goto('https://fixture.example/form');
  return page;
}

test('headless browser native fields use exact labels and undo preserves manual edits', { skip: !executablePath }, async t => {
  const page = await fixture(t, `<form>
    <label>姓名<input name="fullName"></label>
    <label>性别<input type="radio" name="gender" value="m" aria-label="男"></label>
    <label>性别<input type="radio" name="gender" value="f" aria-label="女"></label>
    <label>技能<input type="checkbox" name="skills" value="cpp" aria-label="C++"></label>
    <label>技能<input type="checkbox" name="skills" value="py" aria-label="Python"></label>
    <input name="password" type="password"><input name="captcha">
    <button type="submit">提交</button>
  </form><script>window.submits=0;document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submits++}</script>`);
  const scan = await page.evaluate(api.buildScanScript(bundle, { fullName: 'Synthetic', gender: '女', skills: ['C++'] }));
  assert.ok(scan.matches.length >= 3);
  const result = await page.evaluate(api.buildFillScript(bundle, { scanId: scan.scanId, fieldIds: scan.matches.map(x => x.fieldId), confirmed: true }));
  assert.equal(result.failed.length, 0);
  assert.equal(await page.locator('[name=fullName]').inputValue(), 'Synthetic');
  assert.equal(await page.locator('[value=f]').isChecked(), true);
  assert.equal(await page.locator('[value=m]').isChecked(), false);
  assert.equal(await page.locator('[value=cpp]').isChecked(), true);
  assert.equal(await page.locator('[value=py]').isChecked(), false);
  assert.equal(await page.locator('[name=password]').inputValue(), '');
  await page.locator('[name=fullName]').fill('Manual change');
  await page.evaluate(api.buildUndoScript(bundle));
  assert.equal(await page.locator('[name=fullName]').inputValue(), 'Manual change');
  assert.equal(await page.evaluate('window.submits'), 0);
});

test('headless changed document rejects stale preview instead of filling replacement', { skip: !executablePath }, async t => {
  const page = await fixture(t, '<label>姓名<input name="fullName"></label>');
  const scan = await page.evaluate(api.buildScanScript(bundle, { fullName: 'Synthetic' }));
  await page.evaluate(() => { const input = document.querySelector('input'); input.replaceWith(input.cloneNode()); });
  await assert.rejects(page.evaluate(api.buildFillScript(bundle, { scanId: scan.scanId, fieldIds: scan.matches.map(x => x.fieldId), confirmed: true })), /filler_field_changed_rescan/);
  assert.equal(await page.locator('input').inputValue(), '');
});

test('headless early stop cancels only its own fill and a new scan can fill normally', { skip: !executablePath }, async t => {
  const page = await fixture(t, '<label>姓名<input name="fullName"></label>');
  const scan = await page.evaluate(api.buildScanScript(bundle, { fullName: 'Synthetic' }));
  const fill = api.buildFillScript(bundle, { scanId: scan.scanId, fieldIds: scan.matches.map(x => x.fieldId), confirmed: true });
  await page.evaluate(api.buildCancelScript(bundle));
  const stopped = await page.evaluate(fill);
  assert.equal(stopped.code, 'filler_fill_cancelled');
  assert.equal(await page.locator('input').inputValue(), '');
  const next = await page.evaluate(api.buildScanScript(bundle, { fullName: 'Synthetic' }));
  const result = await page.evaluate(api.buildFillScript(bundle, { scanId: next.scanId, fieldIds: next.matches.map(x => x.fieldId), confirmed: true }));
  assert.equal(result.filled, 1);
  assert.equal(await page.locator('input').inputValue(), 'Synthetic');
});

test('headless ARIA combobox fills only its explicitly owned exact option', { skip: !executablePath }, async t => {
  const page = await fixture(t, `<label>性别<input name="gender" role="combobox" aria-controls="choices" aria-expanded="false" readonly></label>
    <div id="choices" role="listbox" hidden><div role="option">男</div><div role="option">女</div></div>
    <div role="listbox"><div role="option">女</div></div>
    <script>
      const input=document.querySelector('input'), list=document.getElementById('choices');
      input.onclick=()=>{list.hidden=false;input.setAttribute('aria-expanded','true')};
      for(const option of list.children) option.onclick=()=>{input.value=option.textContent;list.hidden=true;input.setAttribute('aria-expanded','false')};
    </script>`);
  const scan = await page.evaluate(api.buildScanScript(bundle, { gender: '女' }));
  assert.equal(scan.matches.length, 1);
  const result = await page.evaluate(api.buildFillScript(bundle, { scanId: scan.scanId, fieldIds: scan.matches.map(x => x.fieldId), confirmed: true }));
  assert.equal(result.filled, 1);
  assert.equal(await page.locator('input').inputValue(), '女');
});
