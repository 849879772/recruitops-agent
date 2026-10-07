const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const http = require('node:http');
const path = require('node:path');
const {_electron} = require('playwright');
const {needsReviewRecordDetails, expandReviewRecordDetails} = require('../dist/review-record-details');

function observation(result = {}) {
  return {status: 'SUCCEEDED', result: {page: {text: '我的投递 软件工程师 投递日期 2026-10-06 展开详情'}, ...result}};
}

test('details follow-up requires readable review evidence and never crosses authentication/frame guards', () => {
  assert.equal(needsReviewRecordDetails(observation()), true);
  for (const value of [null, {...observation(), status: 'FAILED'}, {...observation(), error_code: 'LOGIN_REQUIRED'},
    observation({requires_user_action: true}), observation({pause: {reason: 'captcha'}}),
    observation({diagnostics: {scopeDeniedFrameCount: 1}}), observation({diagnostics: {unavailableFrameCount: 1}}),
    observation({page: {text: '我的投递 软件工程师'}})]) assert.equal(needsReviewRecordDetails(value), false);
});

test('details helper honors cancellation, absolute deadline and navigation provenance', async () => {
  let url = 'https://ats.example/records', calls = 0;
  const wc = Object.assign(new EventEmitter(), {isDestroyed: () => false, isLoadingMainFrame: () => false, getURL: () => url,
    executeJavaScriptInIsolatedWorld: () => {calls++; return new Promise(() => {});}});
  const controller = new AbortController(); controller.abort();
  await assert.rejects(expandReviewRecordDetails(wc, 'https://ats.example', Date.now() + 500, controller.signal), /browser_cancelled/);
  assert.equal(calls, 0);
  const started = Date.now();
  await assert.rejects(expandReviewRecordDetails(wc, 'https://ats.example', Date.now() + 30), /browser_observation_timeout/);
  assert.ok(Date.now() - started < 300);
  wc.executeJavaScriptInIsolatedWorld = async () => {url = 'https://ats.example/other'; return {outcome: 'expanded', expanded_count: 1};};
  await assert.rejects(expandReviewRecordDetails(wc, 'https://ats.example', Date.now() + 500), /browser_navigation_changed/);
});

function card(id, control = '<button class="expand" onclick="expand(this)">展开详情</button>', options = {}) {
  const title = options.title || `后端开发工程师-机器人 ${id}`;
  return `<article class="application-card" id="card-${id}"><h2>${title}</h2>
    <p>${options.context === undefined ? '投递日期 2026-10-06 03:37' : options.context}</p>
    <button onclick="mutate()">更改岗位</button><button onclick="mutate()">撤回投递</button><button onclick="mutate()">提交</button>
    ${control}<div id="panel-${id}" hidden>完整岗位名称：${title}/示例事业部</div></article>`;
}

function page(cards, header = '我的投递') {
  return `<!doctype html><title>Anonymous ATS fixture</title><style>body{margin:0}article{font-size:12px;height:140px}h2,p{margin:3px}button{height:22px}</style>
    <header>${header}</header>${cards}<script>
    window.expansions=0;window.mutations=0;
    function mutate(){window.mutations++}
    function expand(control){window.expansions++;control.setAttribute('aria-expanded','true');
      const owner=control.closest('article');owner.querySelector('[hidden]')?.removeAttribute('hidden');
      owner.querySelector('h2').textContent+='/示例事业部';control.textContent='收起详情'}
    </script>`;
}

test('hidden Electron expands only proved same-card readonly details and re-reads expanded evidence', {timeout: 60000}, async t => {
  const cases = {
    allowed: page(card('one')),
    deliveryTitle: page(card('one', undefined, {title: '投递岗位：后端开发工程师-机器人', context: '信息技术类 佛山市 2026-10-06 03:37'}), '历史投递记录'),
    panel: page(card('one', '<button aria-controls="panel-one" aria-expanded="false" onclick="expand(this)">查看详情</button>')),
    native: page(card('one', '<details><summary>查看详情</summary><p>只读投递详情</p></details>')),
    noPanel: page(card('one', '<button onclick="mutate()">查看详情</button>')),
    otherPanel: page(card('one', '<button aria-controls="outside" onclick="mutate()">查看详情</button>') + '<div id="outside">其他记录</div>'),
    generic: page(card('one'), '招聘岗位'),
    navOnly: page(card('one'), '<nav>我的投递</nav>招聘岗位'),
    publicApply: page(card('one', undefined, {context: '发布日期 2026-10-06 <button onclick="mutate()">投递简历</button>'}), '<nav>我的投递</nav>招聘岗位'),
    publicDateWithRecordHeader: page(card('one', undefined, {context: '发布日期 2026-10-06 <button onclick="mutate()">投递简历</button>'})),
    borrowedContext: page('<div class="application-records"><p>投递日期 2026-10-06</p>' +
      card('one', undefined, {context: '发布日期 2026-10-06'}).replace('application-card', 'job-card') + '</div>'),
    bareDate: page(card('one', undefined, {context: '发布日期 2026-10-06'})),
    noTitle: page(card('one', undefined, {title: '招聘计划'})),
    form: page(`<form>${card('one')}</form>`),
    navigation: page(card('one', '<a href="/job-description" onclick="mutate()">展开详情</a>')),
    foreign: page(card('one', '<a href="https://foreign.example/job" onclick="mutate()">展开详情</a>')),
    scriptNavigation: page(card('one', '<button onclick="location.assign(\'/job-description\')">展开详情</button>')),
    disabled: page(card('one', '<button disabled onclick="mutate()">展开详情</button>')),
    hidden: page(card('one', '<button hidden onclick="mutate()">展开详情</button>')),
    ambiguous: page(card('one', '<button onclick="mutate()">展开详情</button><button onclick="mutate()">展开详情</button>')),
    multipleRoles: page(card('one').replace('</h2>', '</h2><h3>软件测试工程师</h3>')),
    duplicateRoles: page(card('one').replace('</h2>', '</h2><h3>后端开发工程师-机器人 one</h3>')),
    controlChanged: page(card('one', '<button onclick="expand(this);document.querySelector(\'#card-two .expand\').textContent=\'撤回投递\'">展开详情</button>') + card('two')),
    hrefChanged: page(card('one', '<button onclick="expand(this);document.querySelector(\'#card-two .expand\').setAttribute(\'href\',\'/job-description\')">展开详情</button>') + card('two')),
    pageChanged: page(card('one', '<button onclick="expand(this);document.querySelector(\'header\').textContent=\'招聘岗位\'">展开详情</button>') + card('two')),
    bounded: page(['one', 'two', 'three', 'four'].map(id => card(id)).join('')),
    offscreen: page(`<div style="height:900px"></div>${card('one')}`),
    alreadyOpen: page(card('one', '<button aria-expanded="true" onclick="mutate()">展开详情</button>')),
  };
  let unknownRequests = 0;
  const server = http.createServer((req, res) => {
    const name = req.url.slice(1);
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    if (!cases[name]) {unknownRequests++; res.statusCode = 404; res.end('unexpected fixture request');}
    else res.end(cases[name]);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const environment = {...process.env}; delete environment.ELECTRON_RUN_AS_NODE;
  const app = await _electron.launch({executablePath: require('electron'),
    args: [path.join(__dirname, 'review-record-details-fixture.cjs')], env: environment});
  t.after(() => app.close());

  for (const name of Object.keys(cases)) {
    const result = await app.evaluate(async (_electron, {origin, name}) => {
      const {BrowserWindow, expandReviewRecordDetails} = globalThis.detailsFixture;
      const win = new BrowserWindow({show: false, width: 900, height: 640,
        webPreferences: {offscreen: true, backgroundThrottling: false}}), wc = win.webContents;
      try {
        await wc.loadURL(origin + '/' + name);
        await wc.executeJavaScript('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
        const first = await expandReviewRecordDetails(wc, origin, Date.now() + 2500);
        const second = await expandReviewRecordDetails(wc, origin, Date.now() + 2500);
        const state = await wc.executeJavaScript('({expansions:window.expansions,mutations:window.mutations,text:document.body.innerText})');
        return {first, second, state, visible: win.isVisible(), url: wc.getURL()};
      } finally {win.destroy();}
    }, {origin, name});
    assert.equal(result.visible, false, name);
    assert.equal(result.url, origin + '/' + name, name);
    assert.equal(result.state.mutations, 0, name + ': must not click mutations or generic details');
    if (name === 'allowed' || name === 'panel' || name === 'deliveryTitle') {
      assert.equal(result.first.expanded_count, 1, name);
      assert.equal(result.second.expanded_count, 0, name);
      assert.equal(result.state.expansions, 1, name);
      assert.match(result.state.text, /后端开发工程师-机器人(?: one)?\/示例事业部/);
    } else if (name === 'native') {
      assert.equal(result.first.expanded_count, 1);
      assert.equal(result.second.expanded_count, 0);
      assert.match(result.state.text, /只读投递详情/);
    } else if (name === 'controlChanged' || name === 'hrefChanged' || name === 'pageChanged') {
      assert.equal(result.first.expanded_count, 1, name);
      assert.equal(result.first.outcome, 'blocked', name);
      assert.equal(result.state.expansions, 1, name);
    } else if (name === 'bounded') {
      assert.equal(result.first.expanded_count, 3);
      assert.equal(result.first.outcome, 'limit_reached');
      assert.equal(result.first.reason, 'candidate_limit');
      // A standalone second request is allowed its own bounded attempt; the
      // service below proves that one review operation invokes this only once.
      assert.ok(result.state.expansions <= 4);
    } else {
      assert.equal(result.first.expanded_count, 0, name);
      assert.equal(result.state.expansions, 0, name);
    }
  }
  for (const name of ['allowed', 'bounded']) {
    const reviewed = await app.evaluate(async (_electron, {origin, name}) => {
      const {BrowserWindow, BrowserService} = globalThis.detailsFixture;
      const win = new BrowserWindow({show: false, width: 900, height: 640,
        webPreferences: {offscreen: true, backgroundThrottling: false}}), wc = win.webContents;
      let detailScripts = 0, observations = 0;
      const execute = wc.executeJavaScriptInIsolatedWorld.bind(wc);
      wc.executeJavaScriptInIsolatedWorld = async (world, scripts) => {
        if (scripts[0].code.includes('const pinned = () =>')) detailScripts++;
        else observations++;
        return execute(world, scripts);
      };
      const adapter = {buildObservationScript: () => `({status:'SUCCEEDED',result:{
        page:{page_url:location.href,text:document.body.innerText},
        application_records:Array.from(document.querySelectorAll('article')).map(card=>({title:card.querySelector('h2').innerText,status:'applied',context:card.innerText})),
        diagnostics:{readyState:document.readyState,recordCount:1,loadingVisible:false}}})`, normalizeObservation: value => value};
      try {
        await wc.loadURL(origin + '/' + name);
        const output = await new BrowserService(adapter).observeForReview(wc, 'anonymous-details', ['fixture-app'],
          {ownedOrigin: origin, requestedUrl: wc.getURL(), deadline: Date.now() + 4000});
        return {output, detailScripts, observations, visible: win.isVisible()};
      } finally {win.destroy();}
    }, {origin, name});
    assert.equal(reviewed.visible, false);
    assert.equal(reviewed.detailScripts, 1);
    assert.equal(reviewed.observations, 2);
    assert.equal(reviewed.output.review_readiness, 'records');
    assert.equal(reviewed.output.result.application_records[0].title, '后端开发工程师-机器人 one/示例事业部');
    if (name === 'allowed') assert.deepEqual(reviewed.output.result.record_details_followup, {outcome: 'expanded', expanded_count: 1});
    else {
      assert.deepEqual(reviewed.output.result.record_details_followup, {outcome: 'limit_reached', expanded_count: 3, reason: 'candidate_limit'});
      assert.match(reviewed.output.result.application_records[2].title, /\/示例事业部$/);
      assert.equal(reviewed.output.result.application_records[3].title, '后端开发工程师-机器人 four');
    }
  }
  assert.equal(unknownRequests, 0, 'no navigation to public job details or unrelated routes');
});
