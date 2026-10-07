const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const adapter = require('../../../packages/desktop_browser/index.cjs');

test('saved SMS login wording and rendered challenges keep distinct auth evidence', {timeout: 45000}, async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.RECRUITOPS_TEST_CHROMIUM_EXECUTABLE ? {executablePath: process.env.RECRUITOPS_TEST_CHROMIUM_EXECUTABLE} : {})});
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => route.fulfill({body: '<!doctype html><body></body>', contentType: 'text/html'}));
    const pageUrl = 'https://auth-fixture.example.test/applications';
    await page.goto(pageUrl);
    const context = {operation_id: 'auth-fixture', page_url: pageUrl, application_ids: ['fixture-application']};
    const script = adapter.buildObservationScript({operation_id: context.operation_id, page_url: pageUrl});
    const samples = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../../extension/fixtures/authentication-gates.json'), 'utf8'));
    for (const sample of samples) {
      const controls = sample.expected === 'LOGIN_REQUIRED' ? '<input type="tel"><input id="captcha" placeholder="验证码">' : '';
      await page.setContent(`<main><p>${sample.text}</p>${controls}</main>`);
      const response = adapter.normalizeObservation(await page.evaluate(script), context);
      assert.equal(response.error_code || response.status, sample.expected, sample.id);
      if (sample.expected === 'LOGIN_REQUIRED') {
        assert.equal(response.result.auth_evidence.trigger, 'visible_text', sample.id);
        assert.equal(response.result.auth_evidence.selector, '', sample.id);
      }
    }
    for (const hidden of [
      '<div data-captcha style="width:300px;height:100px"></div>',
      '<div style="height:0;overflow:hidden"><div data-sitekey="preload">请完成安全验证</div></div>',
      '<div style="position:absolute;clip:rect(0px,0px,0px,0px)"><div data-captcha>人机验证</div></div>'
    ]) {
      await page.setContent(`<p>我的投递记录</p>${hidden}`);
      const response = adapter.normalizeObservation(await page.evaluate(script), context);
      assert.equal(response.status, 'SUCCEEDED');
    }
    await page.setContent('<p>手机号登录 获取验证码</p><input type="tel"><input id="captcha">' +
      '<div data-captcha>请完成滑块验证 alice@example.test 验证码:123456 token=fixture-secret</div>');
    const response = adapter.normalizeObservation(await page.evaluate(script), context);
    assert.equal(response.error_code, 'CAPTCHA_REQUIRED');
    for (const secret of ['alice@', '123456', 'fixture-secret']) assert.ok(!JSON.stringify(response).includes(secret));
  } finally {
    await browser.close();
  }
});
