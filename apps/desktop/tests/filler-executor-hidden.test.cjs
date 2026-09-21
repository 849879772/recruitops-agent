const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { _electron } = require('playwright');
const api = require('../../../packages/desktop_filler/index.cjs');

test('hidden Electron executor isolates profile state, retains tickets and assigns the exact input', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'filler-executor-anonymous-'));
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const application = await _electron.launch({ args: [path.join(__dirname, 'filler-executor-fixture.cjs'), directory], env });
  t.after(async () => { await application.close(); fs.rmSync(directory, { recursive: true, force: true, maxRetries: 5 }); });
  const page = await application.firstWindow();
  await page.waitForURL('http://127.0.0.1:*/form');
  await page.setContent('<label>姓名<input name="fullName"></label><input type="file" id="resume">');
  const execute = code => application.evaluate(({ BrowserWindow }, script) => {
    const wc = BrowserWindow.getAllWindows()[0].webContents;
    return globalThis.fillerExecutor.execute(wc, wc.mainFrame, script);
  }, code);
  await execute('globalThis.privateFillerValue="Synthetic secret"; true');
  assert.equal(await page.evaluate('typeof globalThis.privateFillerValue'), 'undefined');
  assert.equal(await execute('globalThis.privateFillerValue'), 'Synthetic secret');
  const bundle = api.loadBundledFiller();
  const scan = await execute(api.buildScanScript(bundle, { fullName: 'Synthetic' }));
  const result = await execute(api.buildFillScript(bundle, { scanId: scan.scanId, fieldIds: scan.matches.map(x => x.fieldId), confirmed: true }));
  assert.equal(result.filled, 1);
  assert.equal(await page.locator('[name=fullName]').inputValue(), 'Synthetic');
  const file = path.join(directory, 'synthetic.pdf');
  fs.writeFileSync(file, '%PDF-1.4\nSynthetic fixture only\n');
  await application.evaluate(async ({ BrowserWindow }, file) => {
    const wc = BrowserWindow.getAllWindows()[0].webContents;
    await globalThis.fillerExecutor.assignFile(wc, wc.mainFrame, 'document.querySelector("#resume")', file, () => true);
  }, file);
  assert.equal(await page.locator('#resume').evaluate(el => el.files[0].name), 'synthetic.pdf');
  const concurrent = await application.evaluate(async ({ BrowserWindow }) => {
    const wc = BrowserWindow.getAllWindows()[0].webContents;
    const executor = globalThis.fillerExecutor;
    const filling = executor.execute(wc, wc.mainFrame, 'new Promise(resolve=>{globalThis.finishPendingFill=resolve})');
    let ready = false;
    for (let i = 0; i < 20 && !ready; i++) {
      await new Promise(resolve => setTimeout(resolve, 10));
      ready = await executor.execute(wc, wc.mainFrame, 'typeof globalThis.finishPendingFill === "function"');
    }
    if (!ready) throw new Error('pending fill did not start');
    await executor.execute(wc, wc.mainFrame, 'globalThis.finishPendingFill("cancelled"); true');
    return filling;
  });
  assert.equal(concurrent, 'cancelled');
  await page.evaluate(() => {
    const frame = document.createElement('iframe'); frame.src = location.origin + '/child'; document.body.append(frame);
  });
  await page.locator('iframe').contentFrame().locator('body').waitFor();
  const child = await application.evaluate(async ({ BrowserWindow }) => {
    const wc = BrowserWindow.getAllWindows()[0].webContents;
    const frame = wc.mainFrame.frames.find(frame => frame.url.endsWith('/child'));
    return globalThis.fillerExecutor.execute(wc, frame, 'globalThis.childSecret="Synthetic child"; document.body.textContent="Child changed"; true');
  });
  assert.equal(child, true);
  const childPage = page.frames().find(frame => frame.url().endsWith('/child'));
  assert.equal(await childPage.evaluate('typeof globalThis.childSecret'), 'undefined');
  assert.equal(await childPage.locator('body').innerText(), 'Child changed');
});

test('hidden Electron FillerService extracts Beisen-style controls from a same-origin frame', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'filler-beisen-anonymous-'));
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const application = await _electron.launch({ args: [path.join(__dirname, 'filler-executor-fixture.cjs'), directory], env });
  t.after(async () => { await application.close(); fs.rmSync(directory, { recursive: true, force: true, maxRetries: 5 }); });
  const page = await application.firstWindow();
  await page.waitForURL('http://127.0.0.1:*/form');
  await page.evaluate(() => new Promise(resolve => {
    const frame = document.createElement('iframe');
    frame.id = 'beisen-form-frame';
    frame.src = location.origin + '/form?fixture=beisen';
    frame.addEventListener('load', resolve, { once: true });
    document.body.append(frame);
  }));
  const formFrame = page.frames().find(frame => frame.url().includes('fixture=beisen'));
  assert.ok(formFrame, 'fixture form lives in a real Chromium child frame');
  await formFrame.evaluate(() => {
    document.body.innerHTML = `
      <form class="beisen-resume-form">
        <div class="form-item"><label for="full-name">姓名</label><input id="full-name" name="fullName"></div>
        <div class="form-item"><label for="email">邮箱</label><input id="email" name="email"></div>
        <div class="form-item"><label for="interview-site">面试站点</label><select id="interview-site" name="interviewSite"><option value="">请选择</option><option value="beijing">北京</option><option value="shanghai">上海</option></select></div>
        <div class="form-item"><label for="recommendation-code">推荐码</label><input id="recommendation-code" name="recommendationCode"></div>
        <div class="form-item"><label for="fresh-graduate">应届生</label><select id="fresh-graduate" name="freshGraduate"><option value="">请选择</option><option value="yes">是</option><option value="no">否</option></select></div>
        <div class="form-item"><label for="photo">照片</label><input id="photo" name="photo" type="file" accept="image/*"></div>
        <div class="form-item"><label for="password">密码</label><input id="password" name="password" type="password"></div>
      </form>`;
  });

  const profile = { basic: { fullName: 'Synthetic Candidate', email: 'candidate@example.test' } };
  const first = await application.evaluate(async ({ BrowserWindow }, args) => {
    const wc = BrowserWindow.getAllWindows()[0].webContents;
    const service = new globalThis.FillerService(globalThis.fillerAdapter, () => {}, 10000, globalThis.fillerExecutor);
    service.attach(wc);
    service.setProfile(args, 1);
    globalThis.beisenFixtureService = service;
    await service.scan(wc, 'a'.repeat(32));
    return service.snapshot(wc);
  }, profile);

  assert.equal(first.scanState, 'complete');
  assert.ok(first.scanId);
  assert.equal(first.scanSummary.framesScanned, 2);
  assert.equal(first.scanSummary.controlsSeen, 6, 'password is excluded before extraction');
  assert.equal(first.scanSummary.matched, 2);
  assert.equal(first.scanSummary.needsAnswer, 3);
  assert.equal(first.scanSummary.unsupported, 1);
  assert.deepEqual(first.attachmentTargets, [], 'a photo control is not exposed as a resume upload target');
  assert.equal(first.fields.length, 6);
  assert.equal(new Set(first.fields.map(field => field.frameId)).size, 1, 'all extracted fields bind to the child frame');
  for (const label of ['姓名', '邮箱', '面试站点', '推荐码', '应届生', '照片']) {
    assert.ok(first.fields.some(field => field.label.includes(label)), `extracted visible field: ${label}`);
  }
  const photo = first.fields.find(field => field.label.includes('照片'));
  assert.equal(photo.fillable, false);
  assert.equal(photo.blocked, true);
  assert.equal(photo.reason, 'filler_attachment_unsupported');
  assert.match(first.message, /没有唯一资料答案/);
  assert.match(first.message, /暂不支持自动填写/);
  assert.equal(await formFrame.evaluate(() => document.querySelectorAll('[data-resume-key]').length), 0);

  const customAnswers = first.fields.filter(field => field.fillable === false && !field.blocked).map((field, index) => ({
    id: `beisen-answer-${index}`, origin: new URL(formFrame.url()).origin, pathname: '/form', label: field.label,
    value: ['上海', 'SYNTHETIC-REF', '是'][index],
  }));
  const filled = await application.evaluate(async ({ BrowserWindow }, args) => {
    const wc = BrowserWindow.getAllWindows()[0].webContents;
    const service = globalThis.beisenFixtureService;
    service.setProfile({ ...args.profile, customAnswers: args.customAnswers }, 2);
    await service.scan(wc, 'a'.repeat(32));
    const scan = service.snapshot(wc);
    const ids = scan.fields.filter(field => field.fillable && !field.blocked).map(field => field.fieldId);
    await service.fill(wc, scan.scanId, ids);
    return service.snapshot(wc);
  }, { profile, customAnswers });
  assert.equal(filled.results.filter(result => result.status === 'success').length, 5, JSON.stringify(filled.results));
  assert.equal(filled.results.length, 5);
  assert.equal(await formFrame.locator('#full-name').inputValue(), 'Synthetic Candidate');
  assert.equal(await formFrame.locator('#email').inputValue(), 'candidate@example.test');
  assert.equal(await formFrame.locator('#interview-site').inputValue(), 'shanghai');
  assert.equal(await formFrame.locator('#recommendation-code').inputValue(), 'SYNTHETIC-REF');
  assert.equal(await formFrame.locator('#fresh-graduate').inputValue(), 'yes');
  assert.equal(await formFrame.locator('#photo').evaluate(element => element.files.length), 0);
  assert.equal(await formFrame.locator('#password').inputValue(), '');
});

test('hidden Electron maps Beisen Phoenix sibling labels, excludes header search, and rejects stale labels', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'filler-phoenix-anonymous-'));
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const application = await _electron.launch({ args: [path.join(__dirname, 'filler-executor-fixture.cjs'), directory], env });
  t.after(async () => { await application.close(); fs.rmSync(directory, { recursive: true, force: true, maxRetries: 5 }); });
  const page = await application.firstWindow();
  await page.waitForURL('http://127.0.0.1:*/form');
  await page.setContent(`
    <header class="top-search"><input placeholder="请输入"></header>
    <main class="ux-standard-form">
      <div class="form-item--phoenix form-item">
        <div class="form-item__title"><label class="form-item__text">真实姓名 <span class="required-marker" aria-hidden="true">*</span></label></div>
        <div class="form-item__control"><input placeholder="请输入"></div>
      </div>
      <div class="form-item--phoenix form-item">
        <div class="form-item__title"><label class="form-item__text">手机号码 <span class="required-marker" aria-hidden="true">*</span></label></div>
        <div class="form-item__control"><input placeholder="请输入"></div>
      </div>
      <div class="form-item--phoenix form-item">
        <div class="form-item__title"><label class="form-item__text">电子邮箱 <span class="required-marker" aria-hidden="true">*</span></label></div>
        <div class="form-item__control"><input placeholder="请输入"></div>
      </div>
    </main>`);
  assert.equal(await page.locator('input').evaluateAll(elements => elements.every(element => !element.name && !element.id)), true);

  const profile = { basic: { fullName: 'Synthetic Candidate', phone: '555-0107', email: 'candidate@example.test' } };
  const scanned = await application.evaluate(async ({ BrowserWindow }, resume) => {
    const wc = BrowserWindow.getAllWindows()[0].webContents;
    const adapter = globalThis.fillerAdapter;
    const result = await globalThis.fillerExecutor.execute(wc, wc.mainFrame,
      adapter.buildScanScript(adapter.loadBundledFiller(), resume));
    return result;
  }, profile);
  assert.equal(scanned.totalFields, 3, JSON.stringify({ totalFields: scanned.totalFields, matches: scanned.matches, candidates: scanned.candidates }));
  assert.equal(scanned.matches.length, 3);
  assert.equal(scanned.candidates.length, 0, 'the header search is outside the resume form and is not a candidate');
  const fieldFor = title => scanned.matches.find(match => match.label.includes(title));
  assert.equal(fieldFor('真实姓名')?.value, profile.basic.fullName);
  assert.equal(fieldFor('手机号码')?.value, profile.basic.phone);
  assert.equal(fieldFor('电子邮箱')?.value, profile.basic.email);

  await page.locator('.form-item__title .form-item__text').first().evaluate(element => { element.textContent = '申请人姓名 *'; });
  const staleAttempt = await application.evaluate(async ({ BrowserWindow }, request) => {
    const wc = BrowserWindow.getAllWindows()[0].webContents;
    const adapter = globalThis.fillerAdapter;
    try {
      const result = await globalThis.fillerExecutor.execute(wc, wc.mainFrame,
        adapter.buildFillScript(adapter.loadBundledFiller(), request));
      return { rejected: false, result };
    } catch (error) {
      return { rejected: true, error: String(error?.message || error) };
    }
  }, { scanId: scanned.scanId, fieldIds: [fieldFor('真实姓名').fieldId], confirmed: true });
  assert.equal(staleAttempt.rejected, true);
  assert.ok(
    ['filler_field_changed_rescan', 'filler_executor_script_failed'].includes(staleAttempt.error),
    `stale fill should be rejected by the engine or the executor wrapper: ${staleAttempt.error}`,
  );
  assert.deepEqual(await page.locator('.ux-standard-form .form-item__control input').evaluateAll(elements => elements.map(element => element.value)), ['', '', '']);

  await page.locator('.form-item__title .form-item__text').first().evaluate(element => { element.textContent = '真实姓名 *'; });
  const refreshed = await application.evaluate(async ({ BrowserWindow }, resume) => {
    const wc = BrowserWindow.getAllWindows()[0].webContents;
    const adapter = globalThis.fillerAdapter;
    return globalThis.fillerExecutor.execute(wc, wc.mainFrame,
      adapter.buildScanScript(adapter.loadBundledFiller(), resume));
  }, profile);
  const filled = await application.evaluate(async ({ BrowserWindow }, request) => {
    const wc = BrowserWindow.getAllWindows()[0].webContents;
    const adapter = globalThis.fillerAdapter;
    return globalThis.fillerExecutor.execute(wc, wc.mainFrame,
      adapter.buildFillScript(adapter.loadBundledFiller(), request));
  }, { scanId: refreshed.scanId, fieldIds: refreshed.matches.map(match => match.fieldId), confirmed: true });
  assert.equal(filled.filled, 3);
  assert.deepEqual(await page.locator('.ux-standard-form .form-item__control input').evaluateAll(elements => elements.map(element => element.value)),
    ['Synthetic Candidate', '555-0107', 'candidate@example.test']);
});

test('hidden Electron selects Phoenix dropdown by exact text and rejects ambiguous or pre-open popups', async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'filler-phoenix-select-anonymous-'));
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const application = await _electron.launch({ args: [path.join(__dirname, 'filler-executor-fixture.cjs'), directory], env });
  t.after(async () => { await application.close(); fs.rmSync(directory, { recursive: true, force: true, maxRetries: 5 }); });
  const page = await application.firstWindow();
  await page.waitForURL('http://127.0.0.1:*/form');
  const profile = { basic: { gender: '女' } };

  const installFixture = async ({ openOptions = [], preopenOptions = [], openOnClick = true }) => {
    await page.setContent(`
      <style>
        body { min-height: 360px; margin: 0; font: 14px sans-serif; }
        .form-item { display: flex; align-items: center; gap: 12px; margin: 24px; }
        .form-item__title { width: 72px; }
        .phoenix-select { position: relative; width: 220px; height: 36px; }
        .phoenix-select__input { box-sizing: border-box; width: 100%; height: 100%; }
        .phoenix-select__placeHolder--show, .phoenix-select__tipEle { position: absolute; left: 8px; top: 9px; }
        .phoenix-select__clearIcon { position: absolute; right: 4px; top: 4px; }
        .phoenix-selectList { position: fixed; z-index: 100; top: 90px; left: 112px; min-width: 180px; background: white; border: 1px solid #777; }
        .phoenix-selectList__listItem { min-height: 26px; padding: 4px 8px; color: black; }
      </style>
      <main class="ux-standard-form">
        <div class="form-item--phoenix form-item">
          <div class="form-item__title"><label class="form-item__text">性别</label></div>
          <div class="form-item__control">
            <div class="phoenix-select">
              <input class="phoenix-select__input" readonly>
              <span class="phoenix-select__placeHolder--show">请选择</span>
              <span class="phoenix-select__tipEle" hidden></span>
              <button type="button" class="phoenix-select__clearIcon" aria-label="清空性别" hidden>清空</button>
            </div>
          </div>
        </div>
      </main>`);
    await page.evaluate(config => {
      const input = document.querySelector('.phoenix-select__input');
      const placeholder = document.querySelector('.phoenix-select__placeHolder--show');
      const selected = document.querySelector('.phoenix-select__tipEle');
      const clear = document.querySelector('.phoenix-select__clearIcon');
      window.__phoenixInputClicks = 0;
      const createPopup = (items, source) => {
        const popup = document.createElement('div');
        popup.className = 'phoenix-selectList';
        popup.dataset.fixtureSource = source;
        for (const text of items) {
          const option = document.createElement('div');
          option.className = 'phoenix-selectList__listItem';
          option.dataset.clicks = '0';
          option.textContent = text;
          option.addEventListener('click', () => {
            option.dataset.clicks = String(Number(option.dataset.clicks) + 1);
            selected.textContent = text;
            selected.hidden = false;
            placeholder.classList.remove('phoenix-select__placeHolder--show');
            placeholder.hidden = true;
            clear.hidden = false;
            popup.style.display = 'none';
          });
          popup.appendChild(option);
        }
        document.body.appendChild(popup);
        return popup;
      };
      if (config.preopenOptions.length) createPopup(config.preopenOptions, 'unrelated-preopen');
      input.addEventListener('click', () => {
        window.__phoenixInputClicks++;
        if (config.openOnClick) createPopup(config.openOptions, 'opened-by-control');
      });
      clear.addEventListener('click', () => {
        selected.textContent = '';
        selected.hidden = true;
        placeholder.classList.add('phoenix-select__placeHolder--show');
        placeholder.hidden = false;
        clear.hidden = true;
      });
    }, { openOptions, preopenOptions, openOnClick });
  };
  const scan = () => application.evaluate(async ({ BrowserWindow }, resume) => {
    const wc = BrowserWindow.getAllWindows()[0].webContents;
    const adapter = globalThis.fillerAdapter;
    return globalThis.fillerExecutor.execute(wc, wc.mainFrame,
      adapter.buildScanScript(adapter.loadBundledFiller(), resume));
  }, profile);
  const fill = scanResult => application.evaluate(async ({ BrowserWindow }, request) => {
    const wc = BrowserWindow.getAllWindows()[0].webContents;
    const adapter = globalThis.fillerAdapter;
    return globalThis.fillerExecutor.execute(wc, wc.mainFrame,
      adapter.buildFillScript(adapter.loadBundledFiller(), request));
  }, { scanId: scanResult.scanId, fieldIds: scanResult.matches.map(match => match.fieldId), confirmed: true });

  await installFixture({ openOptions: ['女性', '女'] });
  const exactScan = await scan();
  assert.equal(exactScan.totalFields, 1);
  assert.equal(exactScan.matches.length, 1, JSON.stringify({ matches: exactScan.matches, candidates: exactScan.candidates }));
  assert.equal(exactScan.matches[0].label, '性别');
  assert.equal(exactScan.matches[0].value, '女');
  const exactFill = await fill(exactScan);
  assert.equal(exactFill.filled, 1);
  assert.equal(await page.locator('.phoenix-select__tipEle').textContent(), '女');
  assert.equal(await page.locator('.phoenix-select__placeHolder--show').count(), 0);
  assert.equal(await page.locator('.phoenix-selectList:visible').count(), 0);
  assert.deepEqual(await page.locator('.phoenix-selectList[data-fixture-source="opened-by-control"] .phoenix-selectList__listItem')
    .evaluateAll(options => options.map(option => Number(option.dataset.clicks))), [0, 1]);
  assert.equal(await page.locator('.phoenix-select__clearIcon').isVisible(), true);
  const undone = await application.evaluate(async ({ BrowserWindow }) => {
    const wc = BrowserWindow.getAllWindows()[0].webContents;
    const adapter = globalThis.fillerAdapter;
    return globalThis.fillerExecutor.execute(wc, wc.mainFrame,
      adapter.buildUndoScript(adapter.loadBundledFiller()));
  });
  assert.equal(undone.restored, 1);
  assert.equal(await page.locator('.phoenix-select__tipEle').textContent(), '');
  assert.equal(await page.locator('.phoenix-select__placeHolder--show').count(), 1);

  await installFixture({ openOptions: ['女', '女'] });
  const duplicateScan = await scan();
  const duplicateFill = await fill(duplicateScan);
  assert.equal(duplicateFill.failed.length, 1);
  assert.equal(duplicateFill.results[0].reason, 'filler_option_ambiguous');
  assert.equal(await page.locator('.phoenix-selectList:visible').count(), 1, 'ambiguous popup stays open');
  assert.equal(await page.locator('.phoenix-select__placeHolder--show').count(), 1);
  assert.deepEqual(await page.locator('.phoenix-selectList__listItem').evaluateAll(options => options.map(option => Number(option.dataset.clicks))), [0, 0]);

  await installFixture({ preopenOptions: ['女'], openOnClick: false });
  const preopenedScan = await scan();
  const preopenedFill = await fill(preopenedScan);
  assert.equal(preopenedFill.failed.length, 1);
  assert.equal(preopenedFill.results[0].reason, 'filler_combobox_ambiguous');
  assert.equal(await page.evaluate(() => window.__phoenixInputClicks), 0, 'a pre-open popup blocks the click');
  assert.deepEqual(await page.locator('.phoenix-selectList__listItem').evaluateAll(options => options.map(option => Number(option.dataset.clicks))), [0]);
  assert.equal(await page.locator('.phoenix-select__placeHolder--show').count(), 1);
});
