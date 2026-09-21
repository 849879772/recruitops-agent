'use strict';
const { test, before, after } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { _electron } = require('../../apps/desktop/node_modules/playwright');
const api = require('../../packages/desktop_filler/index.cjs');
const bundle = api.loadBundledFiller();
let application, page, directory;
before(async () => {
  directory = fs.mkdtempSync(path.join(os.tmpdir(), 'public-filler-dom-'));
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  application = await _electron.launch({ args: [path.join(__dirname, 'electron-fixture.cjs'), directory], env });
  page = await application.firstWindow();
  await page.waitForURL('http://127.0.0.1:*/form');
});
after(async () => {
  if (application) await application.close();
  if (directory) fs.rmSync(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
});
async function form(html) { await page.reload(); await page.setContent(html); }
const run = script => application.evaluate(({ BrowserWindow }, code) =>
  BrowserWindow.getAllWindows()[0].webContents.executeJavaScriptInIsolatedWorld(1005, [{ code: '(async()=>(' + code + '))()' }]), script);
const scan = profile => run(api.buildScanScript(bundle, profile));
const fill = (preview, fieldIds = preview.matches.map(m => m.fieldId)) => run(api.buildFillScript(bundle, { scanId: preview.scanId, fieldIds, confirmed: true }));

test('trusted bundled source is branded, deterministic and requires no external folder', () => {
  assert.equal(bundle.hash, api.loadBundledFiller().hash);
  assert.equal(bundle.files[0].name, 'builtin-engine');
  assert.throws(() => api.buildScanScript({ ...bundle }, {}), /bundle_required/);
});

test('real DOM native types, selection, events and partial undo preserve later user edits', async () => {
  await form(`<input id="name" data-resume-key="basic.fullName"><textarea data-resume-key="summary"></textarea>
    <input type="date" data-resume-key="date"><input type="checkbox" data-resume-key="consent">
    <select data-resume-key="city"><option value="a">Alpha</option><option value="b">Beta</option></select>
    <select multiple data-resume-key="skills"><option>JS</option><option>SQL</option><option>CSS</option></select>
    <input type="radio" name="choice" value="A" data-resume-key="choice" checked>
    <input type="radio" name="choice" value="B" data-resume-key="choice">
    <div contenteditable="true" data-resume-key="notes"></div>
    <input type="password" data-resume-key="summary"><input name="otp" data-resume-key="summary">
    <input type="hidden" data-resume-key="summary"><button type="submit">Submit</button>`);
  await page.evaluate(() => { window.events = []; document.addEventListener('change', e => window.events.push(e.target.tagName)); });
  const preview = await scan({ basic: { fullName: 'Synthetic' }, summary: 'Example', date: '2000-01-02', consent: true,
    city: 'Beta', skills: ['JS', 'SQL'], choice: 'B', notes: 'Note' });
  assert.equal(preview.matches.length, 8);
  assert.equal(await page.locator('#name').inputValue(), '');
  assert.equal((await fill(preview)).filled, 8);
  assert.deepEqual(await page.locator('select[multiple]').evaluate(el => Array.from(el.selectedOptions).map(o => o.value)), ['JS', 'SQL']);
  assert.equal(await page.locator('input[value="B"]').isChecked(), true);
  assert.equal((await page.evaluate(() => window.events)).length, 8);
  await page.locator('#name').fill('User revision');
  const undone = await run(api.buildUndoScript(bundle));
  assert.equal(undone.code, 'filler_undo_partial'); assert.equal(undone.failed.length, 1);
  assert.equal(await page.locator('#name').inputValue(), 'User revision');
  assert.equal(await page.locator('textarea').inputValue(), '');
  assert.equal(await page.locator('input[value="A"]').isChecked(), true);
});

test('custom answers match exact origin/path/label and conflicts never choose a value', async () => {
  await form('<label for="why">Why this role?</label><textarea id="why"></textarea><input role="combobox" aria-label="City">');
  const url = new URL(page.url());
  const answer = { id: 'a', origin: url.origin, pathname: url.pathname, label: 'Why this role? why', value: 'Synthetic answer' };
  let preview = await scan({ customAnswers: [{ ...answer, origin: 'https://other.invalid' }] });
  assert.equal(preview.matches.length, 0);
  preview = await scan({ customAnswers: [answer, { ...answer, id: 'b' }] });
  assert.ok(preview.diagnostics.some(d => d.reason === 'filler_answer_conflict'));
  preview = await scan({ customAnswers: [answer] });
  assert.equal((await fill(preview)).filled, 1);
  assert.equal(await page.locator('textarea').inputValue(), 'Synthetic answer');
  assert.ok(preview.diagnostics.some(d => d.reason === 'filler_custom_control_unsupported'));
});

test('Phoenix sibling titles map existing education records without sharing the first profile entry', async () => {
  const item = title => `<div class="form-item form-item--phoenix"><div class="form-item__title"><label class="form-item__text">*${title}：</label></div><div class="form-item__control"><input placeholder="请输入"></div></div>`;
  const record = () => `<div class="ux-standard-form"><div class="form" id="Recruitment.EducationExperience"><div class="form-part"><h3 class="head-title">教育经历</h3>${item('学校名称')}${item('专业名称')}</div></div></div>`;
  await form(`<header><input placeholder="搜索岗位关键词"></header>${record()}${record()}`);
  const profile = { education: [{ school: 'School A', major: 'Major A' }, { school: 'School B', major: 'Major B' }] };
  let preview = await scan(profile);
  assert.equal(preview.totalFields, 4);
  assert.deepEqual(preview.matches.map(match => [match.section, match.recordIndex]), [['education', 0], ['education', 0], ['education', 1], ['education', 1]]);
  await page.evaluate(() => document.body.insertBefore(document.querySelectorAll('.ux-standard-form')[1], document.querySelector('.ux-standard-form')));
  await assert.rejects(fill(preview), /filler_field_changed_rescan/);
  preview = await scan(profile);
  assert.equal((await fill(preview)).filled, 4);
  assert.deepEqual(await page.locator('.form-item input').evaluateAll(elements => elements.map(el => el.value)), ['School A', 'Major A', 'School B', 'Major B']);
  assert.equal(await page.locator('header input').inputValue(), '');
  assert.equal((await run(api.buildUndoScript(bundle))).restored, 4);
});

test('ambiguous Phoenix titles and empty extra records do not guess answers', async () => {
  await form(`<div class="ux-standard-form"><div class="form" id="Recruitment.EducationExperience">
    <div class="form-item"><div class="form-item__title"><label class="form-item__text">学校</label><label class="form-item__text">专业</label></div><input placeholder="请输入"></div>
    </div></div><div class="ux-standard-form"><div class="form" id="Recruitment.EducationExperience">
    <div class="form-item"><div class="form-item__title"><label class="form-item__text">学校</label></div><input placeholder="请输入"></div>
    </div></div>`);
  const preview = await scan({ education: [{ school: 'School A', major: 'Major A' }] });
  assert.equal(preview.matches.length, 0);
  assert.equal(preview.candidates.length, 2);
  assert.deepEqual(await page.locator('input').evaluateAll(elements => elements.map(el => el.value)), ['', '']);
});

test('mixed layout keeps labeled fields outside the Phoenix wrapper and ignores hidden record offsets', async () => {
  await form(`<div class="ux-standard-form"><div class="form-item"><div class="form-item__title"><label class="form-item__text">姓名</label></div><input placeholder="请输入"></div></div>
    <label>电子邮箱<input type="email"></label><input type="search" placeholder="搜索岗位">
    <div class="ux-standard-form" hidden><div class="form" id="Recruitment.EducationExperience"><input></div></div>
    <div class="ux-standard-form"><div class="form" id="Recruitment.EducationExperience"><div class="form-item"><div class="form-item__title"><label class="form-item__text">学校</label></div><input placeholder="请输入"></div></div></div>`);
  const preview = await scan({ basic: { fullName: 'Synthetic', email: 'synthetic@example.test' }, education: [{ school: 'School A' }, { school: 'School B' }] });
  assert.equal(preview.matches.length, 2);
  assert.equal(preview.candidates.length, 1);
  assert.equal(preview.candidates[0].reason, 'filler_answer_label_ambiguous');
  assert.equal((await fill(preview)).filled, 2);
  assert.equal(await page.locator('input[type="email"]').inputValue(), 'synthetic@example.test');
  assert.equal(await page.locator('.form[id] .form-item input').inputValue(), '');
  assert.equal(await page.locator('input[type="search"]').inputValue(), '');
});

test('aria labelled-by text participates in both matching and stale-label detection', async () => {
  await form('<span id="title">电子邮箱</span><input aria-labelledby="title" placeholder="请输入">');
  const preview = await scan({ basic: { email: 'synthetic@example.test' } });
  assert.equal(preview.matches.length, 1);
  await page.locator('#title').evaluate(el => { el.textContent = '紧急联系人邮箱'; });
  await assert.rejects(fill(preview), /filler_field_changed_rescan/);
  assert.equal(await page.locator('input').inputValue(), '');
});

test('Phoenix user search text changed after scan invalidates the pending selection', async () => {
  await form(`<div class="ux-standard-form"><div class="form-item"><div class="form-item__title"><label class="form-item__text">性别</label></div>
    <div class="phoenix-select"><input class="phoenix-select__input"><span class="phoenix-select__placeHolder--show">请选择</span></div>
    </div></div>`);
  const preview = await scan({ basic: { gender: '女' } });
  assert.equal(preview.matches.length, 1);
  await page.locator('input').fill('User search');
  await assert.rejects(fill(preview), /filler_field_changed_rescan/);
  assert.equal(await page.locator('input').inputValue(), 'User search');
});

for (const scenario of ['disabled', 'missing', 'unverified', 'relabeled']) {
  test(`Phoenix ${scenario} selection cannot report a successful write`, async () => {
    await form(`<div class="ux-standard-form"><div class="form-item"><div class="form-item__title"><label class="form-item__text">性别</label></div>
      <div class="phoenix-select"><input class="phoenix-select__input" readonly><span class="phoenix-select__placeHolder--show">请选择</span></div>
      </div></div>`);
    await page.evaluate(kind => {
      window.optionClicks = 0;
      document.querySelector('input').onclick = () => {
        const popup = document.createElement('div'); popup.className = 'phoenix-selectList';
        const option = document.createElement('div');
        option.className = 'phoenix-selectList__listItem' + (kind === 'disabled' ? ' phoenix-selectList__listItem--disabled' : '');
        option.textContent = kind === 'missing' ? '女性' : '女';
        option.onclick = () => { window.optionClicks++; document.querySelector('input').value = '女'; };
        popup.append(option); document.body.append(popup);
        if (kind === 'relabeled') document.querySelector('label').textContent = '紧急联系人性别';
      };
    }, scenario);
    const preview = await scan({ basic: { gender: '女' } });
    assert.equal(preview.matches.length, 1);
    const result = await fill(preview);
    assert.equal(result.filled, 0);
    assert.equal(result.ok, false);
    const reasons = { disabled: 'filler_option_disabled', missing: 'filler_option_missing', unverified: 'filler_option_selection_unverified', relabeled: 'filler_combobox_ambiguous' };
    assert.equal(result.results[0].reason, reasons[scenario]);
    assert.equal(await page.evaluate(() => window.optionClicks), scenario === 'unverified' ? 1 : 0);
    assert.equal(await page.locator('.phoenix-select__placeHolder--show').count(), 1);
  });
}

test('repeat preparation is explicit, bounded, idempotent and pairs three synthetic records', async () => {
  await form('<section data-resume-section="education"><div data-resume-record><input data-resume-key="school"></div><button type="button" data-resume-add>Add</button></section>');
  await page.evaluate(() => document.querySelector('button').addEventListener('click', () => {
    const record = document.createElement('div'); record.setAttribute('data-resume-record', '');
    record.innerHTML = '<input data-resume-key="school">'; document.querySelector('section').append(record);
  }));
  const profile = { education: [{ school: 'School A' }, { school: 'School B' }, { school: 'School C' }] };
  const first = await scan(profile);
  assert.equal(await page.locator('input').count(), 1);
  const prepared = await run(api.buildPrepareScript(bundle, { scanId: first.scanId, sectionIds: ['education'], confirmed: true }));
  assert.equal(prepared.results[0].added, 2); assert.equal(prepared.structureUndoSupported, false);
  const second = await scan(profile);
  assert.equal((await fill(second)).filled, 3);
  assert.deepEqual(await page.locator('input').evaluateAll(els => els.map(e => e.value)), ['School A', 'School B', 'School C']);
  const third = await scan(profile);
  assert.equal((await run(api.buildPrepareScript(bundle, { scanId: third.scanId, sectionIds: ['education'], confirmed: true }))).results[0].added, 0);
});

test('upload only authorizes selected resume target, invalidates preview and observes explicit completion', async () => {
  await form('<input type="file" aria-label="Resume" accept=".pdf" data-max-bytes="1024"><input type="file" aria-label="Photo"><input type="file" aria-label="CV"><input id="name">');
  const preview = await scan({ name: 'Synthetic' });
  assert.equal(preview.attachments.length, 2);
  const attachment = { id: 'fixture', name: 'synthetic.pdf', size: 4, mime: 'application/pdf' };
  const ticket = await run(api.buildUploadScript(bundle, { scanId: preview.scanId, fieldId: preview.attachments[0].fieldId, attachment, confirmed: true }));
  assert.equal(await page.locator(ticket.selector).evaluate(e => e.files.length), 0);
  await assert.rejects(fill(preview), /stale_scan/);
  await assert.rejects(scan({}), /upload_pending/);
  assert.equal((await run(api.buildUploadStatusScript(bundle, ticket.uploadId))).code, 'filler_upload_pending');
  assert.equal(await run(api.buildUploadTargetScript(bundle, ticket.uploadId) + '.tagName'), 'INPUT');
  await assert.rejects(run(api.buildUploadTargetScript(bundle, ticket.uploadId)), /upload_invalid/);
  await page.locator(ticket.selector).setInputFiles({ name: attachment.name, mimeType: attachment.mime, buffer: Buffer.from('test') });
  await page.locator(ticket.selector).evaluate(el => el.setAttribute('data-resume-upload-status', 'complete'));
  assert.equal((await run(api.buildUploadStatusScript(bundle, ticket.uploadId))).code, 'filler_upload_complete');
  await assert.rejects(run(api.buildUploadStatusScript(bundle, ticket.uploadId)), /upload_invalid/);
});

test('same/cross-origin nested frames use explicit context and cannot share scans', async () => {
  const url = new URL(page.url());
  const crossOrigin = await application.evaluate(() => globalThis.fixtureCrossOrigin);
  await form(`<input id="name"><iframe src="${url.origin}/child"></iframe>`);
  const child = await page.waitForEvent('framenavigated', { predicate: f => f !== page.mainFrame(), timeout: 1000 }).catch(() => page.frames()[1]);
  await child.setContent('<input id="name"><iframe src="' + crossOrigin + '/nested"></iframe>');
  const nested = page.frames().find(f => f.url() === crossOrigin + '/nested');
  assert.ok(nested);
  await nested.setContent('<input id="name">');
  const route = { instanceId: 'fixture', tabId: 'tab', frameId: 'child', documentId: 'doc1', profileVersion: 'v1', href: child.url(), allowSubframe: true };
  const evaluate = code => child.evaluate(code => eval(code), code);
  const preview = await evaluate(api.buildScanScript(bundle, { name: 'Child' }, route));
  assert.equal(preview.route.frameId, 'child');
  const request = { scanId: preview.scanId, fieldIds: preview.matches.map(m => m.fieldId), confirmed: true };
  await assert.rejects(evaluate(api.buildFillScript(bundle, request, { ...route, frameId: 'other' })), /context_changed/);
  assert.equal((await evaluate(api.buildFillScript(bundle, request, route))).filled, 1);
  assert.equal(await page.locator('#name').inputValue(), '');
  assert.equal(await child.locator('#name').inputValue(), 'Child');
  const nestedRoute = { ...route, href: nested.url(), frameId: 'nested' };
  await assert.rejects(nested.evaluate(code => eval(code), api.buildScanScript(bundle, { name: 'Cross' })), /top_frame/);
  const crossScan = await nested.evaluate(code => eval(code), api.buildScanScript(bundle, { name: 'Cross' }, nestedRoute));
  assert.equal(crossScan.matches[0].fieldId, preview.matches[0].fieldId, 'same local ID must not imply same frame');
  assert.equal((await nested.evaluate(code => eval(code), api.buildFillScript(bundle, {
    scanId: crossScan.scanId, fieldIds: [crossScan.matches[0].fieldId], confirmed: true,
  }, nestedRoute))).filled, 1);
  assert.equal(await nested.locator('#name').inputValue(), 'Cross');
  await child.locator('iframe').evaluate(el => el.remove());
  await assert.rejects(nested.evaluate(code => eval(code), api.buildUndoScript(bundle, nestedRoute)), /detached/);
  await child.goto(url.origin + '/child');
  await assert.rejects(evaluate(api.buildFillScript(bundle, request, route)), /stale_scan/);
});

test('built-in partial results account for all fields and do not overwrite a newly hidden field', async () => {
  await form('<input data-resume-key="first"><input data-resume-key="second"><input data-resume-key="third">');
  await page.evaluate(() => document.querySelector('input').addEventListener('change', () => {
    document.querySelectorAll('input')[1].hidden = true;
  }));
  const result = await fill(await scan({ first: 'A', second: 'B', third: 'C' }));
  assert.equal(result.code, 'filler_fill_partial'); assert.equal(result.filled, 1);
  assert.equal(result.skipped.length, 2); assert.equal(result.failed.length, 0);
  assert.equal(result.results.length, result.filled + result.skipped.length + result.failed.length);
  assert.equal(await page.locator('input').nth(2).inputValue(), '');
});

test('radio sibling edit and changed select options fail before overwriting user intent', async () => {
  await form('<input type="radio" name="choice" value="A" data-resume-key="choice"><input type="radio" name="choice" value="B" data-resume-key="choice"><select data-resume-key="city"><option>A</option></select>');
  const preview = await scan({ choice: 'B', city: 'A' });
  await page.locator('[value="A"]').check();
  const result = await fill(preview, [preview.matches.find(m => m.key === 'choice').fieldId]);
  assert.equal(result.filled, 0); assert.equal(await page.locator('[value="A"]').isChecked(), true);
  const second = await scan({ city: 'A' });
  await page.locator('option').evaluate(el => el.textContent = 'Changed');
  await assert.rejects(fill(second), /field_changed/);
});

test('attachment constraints, replacement, expiry, rejection and unknown parse outcomes are explicit', async () => {
  const attachment = { id: 'fixture', name: 'synthetic.pdf', size: 4, mime: 'application/pdf' };
  const previewUpload = async (extra = '') => {
    await form('<input type="file" aria-label="Resume" ' + extra + '>');
    const preview = await scan({});
    return { scanId: preview.scanId, fieldId: preview.attachments[0].fieldId, confirmed: true, attachment };
  };
  await assert.rejects(run(api.buildUploadScript(bundle, await previewUpload('accept=".doc"'))), /type_rejected/);
  await assert.rejects(run(api.buildUploadScript(bundle, await previewUpload('data-max-bytes="2"'))), /size_rejected/);
  let ticket = await run(api.buildUploadScript(bundle, await previewUpload()));
  await page.locator('input').evaluate(el => el.replaceWith(el.cloneNode()));
  await assert.rejects(run(api.buildUploadTargetScript(bundle, ticket.uploadId)), /changed_rescan/);
  assert.equal((await run(api.buildUploadStatusScript(bundle, ticket.uploadId))).code, 'filler_upload_dom_changed_unknown');
  ticket = await run(api.buildUploadScript(bundle, await previewUpload()));
  await run('globalThis.__RECRUITOPS_LOCAL_FILLER_V1__.upload.issuedAt-=10001');
  await assert.rejects(run(api.buildUploadTargetScript(bundle, ticket.uploadId)), /upload_expired/);
  await run('globalThis.__RECRUITOPS_LOCAL_FILLER_V1__.upload.issuedAt-=60000');
  assert.equal((await run(api.buildUploadStatusScript(bundle, ticket.uploadId))).code, 'filler_upload_timeout_unknown');
  ticket = await run(api.buildUploadScript(bundle, await previewUpload()));
  await page.locator('input').setInputFiles({ name: attachment.name, mimeType: attachment.mime, buffer: Buffer.from('test') });
  assert.equal((await run(api.buildUploadStatusScript(bundle, ticket.uploadId))).code, 'filler_upload_selected_parse_unknown');
  ticket = await run(api.buildUploadScript(bundle, await previewUpload()));
  await page.locator('input').evaluate(el => el.setAttribute('data-resume-upload-status', 'failed'));
  assert.equal((await run(api.buildUploadStatusScript(bundle, ticket.uploadId))).code, 'filler_upload_rejected');
});

test('prepare timeout is unknown, locks reentry until reload, and never guesses added records', async () => {
  await form('<section data-resume-section="education"><div data-resume-record></div><button type="button" data-resume-add>Add</button></section>');
  const preview = await scan({ education: [{}, {}] });
  const pending = run(api.buildPrepareScript(bundle, { scanId: preview.scanId, sectionIds: ['education'], confirmed: true }));
  await assert.rejects(scan({}), /operation_busy/);
  const result = await pending;
  assert.equal(result.results[0].code, 'filler_prepare_timeout_unknown');
  assert.equal(result.results[0].added, 0);
  await assert.rejects(scan({}), /operation_busy/);
  await form('<input id="name">');
  assert.equal((await scan({ name: 'Synthetic' })).matches.length, 1);
});
