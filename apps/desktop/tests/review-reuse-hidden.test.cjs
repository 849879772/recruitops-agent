const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const {_electron} = require('playwright');

test('real hidden main reuses the renderer, reads fresh DOM, takes new pixels, and cold-reads after invalidation', {timeout: 55000}, async t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'recruitops-reuse-anonymous-'));
  const requests = new Map();
  const table = '<table><thead><tr><th>岗位名称</th><th>当前状态</th><th>投递日期</th></tr></thead>'+
    '<tbody><tr><td id="title">软件开发工程师</td><td id="status">申请成功</td><td>2026-10-06</td></tr></tbody></table>';
  const server = http.createServer((req, res) => {
    requests.set(req.url, (requests.get(req.url) || 0)+1);
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end('<!doctype html><title>Applications</title><style>body{background:#e9f1ff;font:24px sans-serif}table{margin:35px}</style>'+
      (req.url === '/unparsed' ? '<main>Application records Software engineer</main>' : '<h1>我的投递</h1>'+table));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const env = Object.fromEntries(['PATH','SystemRoot','WINDIR','TEMP','TMP','APPDATA','LOCALAPPDATA','USERPROFILE']
    .filter(key => process.env[key]).map(key => [key, process.env[key]]));
  let application;
  t.after(async () => {
    if (application) await application.close();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(directory, {recursive: true, force: true, maxRetries: 5});
  });
  application = await _electron.launch({args: [path.join(__dirname, 'review-reuse-hidden-fixture.cjs')], env: {...env,
    RECRUITOPS_DESKTOP_TEST: '1', RECRUITOPS_DESKTOP_DATA_DIR: directory,
    RECRUITOPS_DESKTOP_FIXTURE_ORIGIN: origin, RECRUITOPS_DESKTOP_TEST_RUNTIME: 'filler'}});
  const shell = await application.firstWindow(); await shell.waitForLoadState();
  await shell.waitForFunction(async () => !!(await window.desktop.state()).filler.capabilities.persistentProfile);
  const reviewed = await application.evaluate(async ({BrowserWindow}, origin) => {
    const {reviewPage} = globalThis.reuseFixture;
    const context = {reviewTaskId: 'anonymous-run', retainForVisionReuse: true};
    const source = await reviewPage(origin+'/records', 'dom-source', ['app-1','app-2'], undefined, Date.now()+5000,
      undefined, undefined, false, context);
    const view = BrowserWindow.getAllWindows().flatMap(window => window.contentView.children)
      .find(view => view.webContents?.isOffscreen());
    if (!view) throw new Error('source renderer was not retained');
    const wc = view.webContents, id = wc.id;
    await wc.executeJavaScript('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
    const {pixelsHash} = globalThis.reuseFixture;
    const oldPixels = pixelsHash(await wc.capturePage());
    await wc.executeJavaScript('document.body.style.background="#32c070";document.querySelector("#title").textContent="平台开发工程师";document.querySelector("#status").textContent="笔试中"');
    const captured = []; const capture = wc.capturePage.bind(wc);
    wc.capturePage = async (...args) => {const image = await capture(...args); captured.push(pixelsHash(image)); return image;};
    const destroyed = new Promise(resolve => wc.once('destroyed', resolve));
    const output = await reviewPage(origin+'/records', 'vision-fresh', ['app-2'], undefined, Date.now()+7000,
      undefined, undefined, true, {reviewTaskId: 'anonymous-run', reuseObservationOperationId: 'dom-source'});
    await destroyed;
    const result = {source, output, oldPixels, captured, id, closed: wc.isDestroyed(), hidden: !view.getVisible(),
      visible: BrowserWindow.getAllWindows().some(window => window.isVisible())};
    await reviewPage(origin+'/records', 'vision-replay', ['app-2'], undefined, Date.now()+7000,
      undefined, undefined, true, {reviewTaskId: 'anonymous-run', reuseObservationOperationId: 'dom-source'});
    return result;
  }, origin);
  assert.equal(reviewed.visible, false); assert.equal(reviewed.hidden, true);
  assert.equal(reviewed.closed, true); assert.equal(reviewed.output.operation_id, 'vision-fresh');
  assert.deepEqual(reviewed.output.result.application_ids, ['app-2']);
  assert.equal(reviewed.source.result.application_records[0].title, '软件开发工程师');
  assert.equal(reviewed.output.result.application_records[0].title, '平台开发工程师');
  assert.equal(reviewed.output.result.application_records[0].status, 'written');
  assert.equal(reviewed.output.result.observation_reuse.source_operation_id, 'dom-source');
  assert.ok(reviewed.captured.length > 0, 'a new real bitmap is captured rather than replaying old pixels');
  assert.ok(reviewed.captured.every(hash => hash !== reviewed.oldPixels));
  assert.equal(requests.get('/records'), 2, 'source+vision share one navigation; replay is a new cold read');

  const stable = await application.evaluate(async ({BrowserWindow}, origin) => {
    const {reviewPage} = globalThis.reuseFixture;
    const source = await reviewPage(origin+'/unparsed', 'stable-source', ['app-1'], undefined, Date.now()+16000,
      undefined, undefined, false, {reviewTaskId: 'stable-run', retainForVisionReuse: true});
    const started = Date.now();
    const output = await reviewPage(origin+'/unparsed', 'stable-vision', ['app-1'], undefined, Date.now()+7000,
      undefined, undefined, true, {reviewTaskId: 'stable-run', reuseObservationOperationId: 'stable-source'});
    return {source, output, elapsed: Date.now()-started, retained: BrowserWindow.getAllWindows()
      .flatMap(window => window.contentView.children).filter(view => view.webContents?.isOffscreen()).length};
  }, origin);
  assert.ok(stable.source.result.last_observation.sampling.elapsedMs >= 12000);
  assert.equal(stable.output.result.last_observation.sampling.count, 1);
  assert.equal(stable.output.result.observation_reuse.source_operation_id, 'stable-source');
  assert.ok(stable.elapsed < 6500); assert.equal(stable.retained, 0);
  assert.equal(requests.get('/unparsed'), 1, 'stable readiness proof skips both duplicate load and twelve-second wait');
  t.diagnostic(`stable source wait ${stable.source.result.last_observation.sampling.elapsedMs}ms; fresh DOM+capture ${stable.elapsed}ms; table fresh captures ${reviewed.captured.length}`);

  const reload = await application.evaluate(async ({BrowserWindow}, origin) => {
    const {reviewPage} = globalThis.reuseFixture;
    await reviewPage(origin+'/reload', 'reload-source', ['app-1'], undefined, Date.now()+5000,
      undefined, undefined, false, {reviewTaskId: 'reload-run', retainForVisionReuse: true});
    const wc = BrowserWindow.getAllWindows().flatMap(window => window.contentView.children)
      .find(view => view.webContents?.isOffscreen()).webContents;
    const destroyed = new Promise(resolve => wc.once('destroyed', resolve));
    wc.reload(); await destroyed;
    const output = await reviewPage(origin+'/reload', 'reload-vision', ['app-1'], undefined, Date.now()+7000,
      undefined, undefined, true, {reviewTaskId: 'reload-run', reuseObservationOperationId: 'reload-source'});
    return {closed: wc.isDestroyed(), output};
  }, origin);
  assert.equal(reload.closed, true); assert.equal(reload.output.result.observation_reuse, undefined);
  assert.ok(requests.get('/reload') >= 2, 'same-URL reload invalidation must navigate a new hidden page');

  const races = await application.evaluate(async ({BrowserWindow}, origin) => {
    const {reviewPage, visionRequests} = globalThis.reuseFixture;
    const results = [];
    for (const phase of ['preupload','paid']) {
      const sourceId = phase+'-source', operationId = phase+'-vision', url = origin+'/'+phase;
      await reviewPage(url, sourceId, ['app-1'], undefined, Date.now()+5000, undefined, undefined, false,
        {reviewTaskId: phase+'-run', retainForVisionReuse: true});
      const wc = BrowserWindow.getAllWindows().flatMap(window => window.contentView.children)
        .find(view => view.webContents?.isOffscreen()).webContents;
      const change = () => wc.session.cookies.set({url: origin, name: 'account_session', value: phase, httpOnly: true});
      if (phase === 'preupload') {
        const capture = wc.capturePage.bind(wc);
        wc.capturePage = async (...args) => {await change(); return capture(...args);};
      } else globalThis.reuseFixture.onVisionRequest = async request => {
        await change(); if (!request.signal.aborted) throw new Error('account switch did not abort the upload signal');
      };
      const stages = [];
      try {
        const result = await reviewPage(url, operationId, ['app-1'], undefined, Date.now()+7000,
          stage => stages.push(stage), undefined, true,
          {reviewTaskId: phase+'-run', reuseObservationOperationId: sourceId});
        results.push({phase, result, stages, uploads: visionRequests.filter(item => item.operationId === operationId).length});
      } catch (error) {
        results.push({phase, code: error.message, stages, uploads: visionRequests.filter(item => item.operationId === operationId).length});
      } finally {globalThis.reuseFixture.onVisionRequest = undefined;}
    }
    return results;
  }, origin);
  assert.equal(races[0].phase, 'preupload'); assert.equal(races[0].result.status, 'SUCCEEDED');
  assert.equal(races[0].result.result.observation_reuse, undefined, 'invalid account hint uses a fresh cold renderer');
  assert.equal(races[0].uploads, 1, 'only the cold retry can upload, never the old-account pixels');
  assert.equal(requests.get('/preupload'), 2);
  const validating = races[0].stages.indexOf('VALIDATING');
  assert.ok(validating >= 0); assert.ok(races[0].stages.slice(validating).every(stage => stage === 'VALIDATING'),
    'a bounded cold fallback must not regress the bridge lifecycle from VALIDATING');
  assert.equal(races[1].phase, 'paid'); assert.equal(races[1].code, 'browser_account_changed');
  assert.equal(races[1].uploads, 1); assert.equal(requests.get('/paid'), 1, 'paid upload is stopped, not retried');
});
