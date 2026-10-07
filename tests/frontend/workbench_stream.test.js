"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const APP_SOURCE = fs.readFileSync(
  path.resolve(__dirname, "../../apps/web/app.js"),
  "utf8",
);

class FakeTextNode {
  constructor(value) {
    this.nodeType = 3;
    this.parentNode = null;
    this._textContent = String(value ?? "");
  }

  get textContent() {
    return this._textContent;
  }

  set textContent(value) {
    this._textContent = String(value ?? "");
  }

  remove() {
    this.parentNode?.removeChild(this);
  }
}

class FakeElement {
  constructor(tagName = "div") {
    this.nodeType = 1;
    this.tagName = String(tagName).toUpperCase();
    this.parentNode = null;
    this.childNodes = [];
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.open = false;
    this.value = "";
    this.id = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this._textContent = "";
    this.listeners = new Map();
  }

  get children() {
    return this.childNodes.filter((child) => child.nodeType === 1);
  }

  get childElementCount() {
    return this.children.length;
  }

  get firstChild() {
    return this.childNodes[0] || null;
  }

  get firstElementChild() { return this.children[0] || null; }

  get nextElementSibling() {
    const siblings = this.parentNode?.children || [];
    return siblings[siblings.indexOf(this) + 1] || null;
  }

  get isConnected() {
    let root = this;
    while (root.parentNode) root = root.parentNode;
    return root.tagName === "BODY";
  }

  get textContent() {
    if (!this.childNodes.length) return this._textContent;
    return this.childNodes.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    this.childNodes = [];
    this._textContent = String(value ?? "");
  }

  appendChild(child) {
    if (child === null || child === undefined) return child;
    child.parentNode?.removeChild(child);
    this._textContent = "";
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }

  append(...children) {
    children.forEach((child) => this.appendChild(
      typeof child === "string" ? new FakeTextNode(child) : child,
    ));
  }

  prepend(child) { this.insertBefore(child, this.firstChild); }

  insertBefore(child, reference) {
    if (child === reference) return child;
    child.parentNode?.removeChild(child);
    const index = reference ? this.childNodes.indexOf(reference) : this.childNodes.length;
    child.parentNode = this;
    this.childNodes.splice(index, 0, child);
    return child;
  }

  replaceWith(child) {
    this.parentNode?.insertBefore(child, this);
    this.remove();
  }

  removeChild(child) {
    const index = this.childNodes.indexOf(child);
    if (index >= 0) {
      this.childNodes.splice(index, 1);
      child.parentNode = null;
    }
    return child;
  }

  remove() {
    this.parentNode?.removeChild(this);
  }

  setAttribute(name, value) {
    const stringValue = String(value);
    this.attributes[name] = stringValue;
    if (name === "open") this.open = true;
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      this.dataset[key] = stringValue;
    }
  }

  removeAttribute(name) {
    delete this.attributes[name];
    if (name === "open") this.open = false;
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      delete this.dataset[key];
    }
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  dispatchEvent(event) {
    event.target ||= this;
    event.currentTarget = this;
    for (const listener of this.listeners.get(event.type) || []) listener.call(this, event);
    return !event.defaultPrevented;
  }

  click() {
    if (!this.disabled) this.dispatchEvent({ type: "click", preventDefault() { this.defaultPrevented = true; } });
  }

  showModal() { this.setAttribute("open", ""); }

  close() { this.removeAttribute("open"); }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  querySelectorAll(selector) {
    if (selector.includes(",")) return [...new Set(selector.split(",").flatMap(item => this.querySelectorAll(item.trim())))];
    if (selector.startsWith(":scope > ")) return this.children.filter(node => matchesSelector(node, selector.slice(9)));
    const matches = [];
    const visit = (node) => {
      if (node.nodeType !== 1) return;
      if (matchesSelector(node, selector)) matches.push(node);
      node.childNodes.forEach(visit);
    };
    this.childNodes.forEach(visit);
    return matches;
  }
}

function matchesSelector(node, selector) {
  const className = selector.match(/^\.([\w-]+)$/);
  if (className) return node.className.split(/\s+/).includes(className[1]);
  const dataAttribute = selector.match(/^\[data-([\w-]+)\]$/);
  if (dataAttribute) return Object.hasOwn(node.dataset, dataAttribute[1].replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()));
  if (selector === ".message") return node.className.split(/\s+/).includes("message");
  if (selector === ".message-copy") return node.className.split(/\s+/).includes("message-copy");
  if (selector === ".status-dot") return node.className.split(/\s+/).includes("status-dot");
  const status = selector.match(/^\[data-codex-status="([^"]+)"\]$/);
  if (status) return node.dataset.codexStatus === status[1];
  return selector.toUpperCase() === node.tagName;
}

class FakeDocument {
  constructor() {
    this.body = new FakeElement("body");
    this.elements = new Map();
  }

  register(id, element = new FakeElement("div")) {
    element.id = id;
    this.elements.set(id, element);
    this.body.appendChild(element);
    return element;
  }

  registerStatus(key) {
    const element = new FakeElement("span");
    element.dataset.codexStatus = key;
    this.body.appendChild(element);
    return element;
  }

  getElementById(id) {
    if (!this.elements.has(id)) this.register(id);
    return this.elements.get(id);
  }

  createElement(tagName) {
    return new FakeElement(tagName);
  }

  createElementNS(_namespace, tagName) {
    return this.createElement(tagName);
  }

  createTextNode(value) {
    return new FakeTextNode(value);
  }

  querySelector(selector) {
    return this.body.querySelector(selector);
  }

  querySelectorAll(selector) {
    return this.body.querySelectorAll(selector);
  }

  addEventListener() {}
}

function loadApp(fetch, options = {}) {
  const document = new FakeDocument();
  [
    "assistant-live-run",
    "assistant-live-label",
    "assistant-live-detail",
    "assistant-task-progress",
    "assistant-task-progress-title",
    "assistant-task-progress-state",
    "assistant-task-progress-detail",
    "assistant-task-progress-bar",
    "assistant-task-progress-stages",
    "assistant-message-status",
    "assistant-intent",
    "assistant-messages",
    "assistant-form-error",
    "assistant-message",
    "assistant-job-id",
    "run-task-button",
    "assistant-stop-button",
    "toast-region",
    "task-history",
    "task-history-count",
    "nav-task-count",
    "assistant-thread-label",
    "mail-list",
    "mail-refresh-button",
    "mail-sync-status",
    "mail-sync-status-title",
    "mail-sync-status-detail",
    "mail-freshness-note",
    "mail-freshness-title",
    "mail-freshness-detail",
    "mail-total-count",
    "mail-confirm-count",
    "mail-linked-count",
    "mail-list-count",
    "application-summary",
    "application-kanban",
    "nav-application-count",
  ].forEach((id) => document.register(id));
  const intent = document.getElementById("assistant-intent");
  const dot = new FakeElement("span");
  dot.className = "status-dot";
  intent.appendChild(dot);
  ["thread", "turn", "item", "tool", "progress", "error"].forEach((key) => document.registerStatus(key));

  const context = {
    __RECRUITOPS_TEST_MODE__: true,
    document,
    window: {
      location: { href: "http://localhost/" },
      setTimeout: () => 0,
      clearTimeout: () => {},
      confirm: typeof options.confirm === "function" ? options.confirm : () => true,
    },
    fetch,
    console,
    TextDecoder,
    TextEncoder,
    URL,
    URLSearchParams,
    AbortController,
    localStorage: options.localStorage,
    crypto: require("node:crypto").webcrypto,
    Date: options.Date || Date,
    Uint8Array,
  };
  vm.runInNewContext(APP_SOURCE, context, { filename: "apps/web/app.js" });
  const hooks = context.__RECRUITOPS_TEST_HOOKS__;
  hooks.state.codexEnabled = true;
  hooks.state.codexReady = true;
  hooks.state.codexThreadId = "thread-1";
  return { document, hooks };
}

function frame(eventName, payload) {
  return `event: ${eventName}\ndata: ${JSON.stringify(payload)}\n\n`;
}

function bytes(value) {
  return new TextEncoder().encode(value);
}

function streamResponse(reads) {
  let index = 0;
  let cancelled = false;
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        return {
          read: async () => {
            const next = reads[index++];
            if (typeof next === "function") return next();
            return next || { value: new Uint8Array(), done: true };
          },
          cancel: async () => {
            cancelled = true;
          },
        };
      },
    },
    wasCancelled: () => cancelled,
  };
}

function completeRead() {
  return { value: new Uint8Array(), done: true };
}

function jsonResponse(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  };
}

test("review dialog groups login jobs by company and official host with one explicit scoped recheck", async () => {
  const calls = [];
  const blocked = (id, company, host, job) => ({application_id:id,company_name:company,job_title:job,
    state:"blocked",reason:"login_required",record_url:`https://${host}/applications`,checked_at:"2026-10-04T03:15:00"});
  const {document,hooks}=loadApp(async(url,options={})=>{
    calls.push({url,options});
    if(url==="/api/applications/review-results/recheck")return jsonResponse({run_id:"status-review-"+"a".repeat(32),scope_complete:true});
    return jsonResponse({run_id:"status-review-"+"b".repeat(32),total:4,has_more:false,items:[
      blocked("a1","示例公司","jobs.example.test","开发工程师"),
      blocked("a2","示例公司","jobs.example.test","测试工程师"),
      blocked("b1","另一公司","jobs.example.test","应用工程师"),
      blocked("c1","示例公司","other.example.test","产品工程师"),
    ]});
  });
  await hooks.openReviewResults();
  assert.equal(calls.length,1,"opening results does not start a recheck");
  assert.match(calls[0].url,/limit=50/);
  const groups=document.getElementById("review-results-content").querySelectorAll(".review-login-group");
  assert.equal(groups.length,3,"company and host both distinguish groups");
  const group=groups.find(row=>row.textContent.includes("本组已加载 2 个岗位"));
  assert.match(group.textContent,/开发工程师/);assert.match(group.textContent,/测试工程师/);
  assert.equal(group.querySelectorAll("a").length,1,"a company has one login entry");
  const retry=group.querySelectorAll("button").find(button=>button.textContent==="登录后重新复核此公司");
  retry.click();retry.click();
  await new Promise(resolve=>setImmediate(resolve));
  const starts=calls.filter(call=>call.url.endsWith("/recheck"));
  assert.equal(starts.length,1);
  assert.deepEqual(JSON.parse(starts[0].options.body).application_ids,["a1","a2"]);
  assert.equal(starts[0].options.headers["X-RecruitOps-Local-UI"],"1");
  assert.match(JSON.parse(starts[0].options.body).request_id,/^[0-9a-f-]{36}$/);
  assert.equal(retry.disabled,true);assert.equal(retry.textContent,"复核完成");
});

test("review dialog explains SSO recovery and public-home uncertainty without declaring expired login", async () => {
  const {document,hooks}=loadApp(async()=>jsonResponse({total:2,has_more:false,items:[
    {application_id:"sso",company_name:"示例甲",job_title:"开发",state:"failed",reason:"authentication_recovery_timeout",record_url:"https://jobs.example.test/applications"},
    {application_id:"home",company_name:"示例乙",job_title:"测试",state:"unresolved",reason:"unparsed_page",navigation_reason:"returned_to_home_without_application_records"},
  ]}));
  await hooks.openReviewResults();
  const copy=document.getElementById("review-results-content").textContent;
  assert.match(copy,/统一认证页未在时限内返回招聘页；尚不能确认登录是否失效/);
  assert.match(copy,/首页的登录入口不能证明登录已失效/);
  assert.match(copy,/打开官网检查认证/);
  assert.match(copy,/检查后重新复核此公司/);
  assert.doesNotMatch(copy,/Cookie 已失效|需要重新登录|登录一次后|完成官网登录后/);
});

test("TME downgrade receipt displays its precise safe navigation cause instead of generic failure", async () => {
  const {document,hooks}=loadApp(async()=>jsonResponse({total:1,has_more:false,items:[{
    application_id:"tme-fixture",company_name:"腾讯音乐",job_title:"工程师",state:"failed",
    reason:"desktop_navigation_changed",navigation_reason:"https_downgrade",
    navigation_diagnostics:{phase:"initial_load",restriction:"https_downgrade"},
  }]}));
  await hooks.openReviewResults();
  const copy=document.getElementById("review-results-content").textContent;
  assert.match(copy,/腾讯音乐/);
  assert.match(copy,/官网尝试从 HTTPS 跳转到 HTTP，已安全拦截；尚未核验状态/);
  assert.doesNotMatch(copy,/页面跳转受限，未完成核验|需要重新登录/);
});

test("review dialog explains distinct failed navigation and timeout causes in Chinese", async () => {
  const causes=["desktop_load_timeout","desktop_observation_timeout","desktop_review_queue_timeout",
    "desktop_review_timeout","observation_timeout","https_downgrade","official_sso_hop_limit"];
  const {document,hooks}=loadApp(async()=>jsonResponse({total:causes.length,has_more:false,
    items:causes.map((reason,index)=>({application_id:"failure-"+index,company_name:"离线公司"+index,
      job_title:"工程师",state:"failed",reason}))}));
  await hooks.openReviewResults();
  const copy=document.getElementById("review-results-content").textContent;
  assert.match(copy,/首屏加载超时/);assert.match(copy,/页面内容读取超时/);
  assert.match(copy,/复核排队超时/);assert.match(copy,/超过单页时限/);
  assert.match(copy,/等待官网页面证据超时/);assert.match(copy,/从 HTTPS 跳转到 HTTP，已安全拦截/);
  assert.match(copy,/统一认证跳转次数超限/);
  assert.doesNotMatch(copy,/desktop_load_timeout|desktop_review_timeout|official_sso_hop_limit/);
});

test("review dialog keeps one company group across pages and failed recheck can be retried", async () => {
  let pages=0,starts=0;
  const {document,hooks}=loadApp(async(url)=>{
    if(url.endsWith("/recheck")) {starts++;return jsonResponse({detail:"浏览器连接暂不可用"},503);}
    pages++;
    return jsonResponse({total:2,has_more:pages===1,next_cursor:pages===1?"next":null,items:[
      {application_id:"app-"+pages,company_name:"同一公司",job_title:"岗位"+pages,state:"blocked",reason:"login_required",record_url:"https://jobs.example.test/records"},
    ]});
  });
  await hooks.openReviewResults();
  const content=document.getElementById("review-results-content");
  content.querySelectorAll("button").find(button=>button.textContent==="加载更多").click();
  await new Promise(resolve=>setImmediate(resolve));
  const groups=content.querySelectorAll(".review-login-group");assert.equal(groups.length,1);
  assert.match(groups[0].textContent,/本组已加载 2 个岗位/);
  const retry=groups[0].querySelectorAll("button").find(button=>button.textContent==="登录后重新复核此公司");
  retry.click();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(starts,1);assert.equal(retry.disabled,false);assert.match(groups[0].textContent,/复核未完成：浏览器连接暂不可用/);
});

test("company recheck continues only its returned frozen run and preserves request identity", async () => {
  const calls=[];let wave=0;
  const runId="status-review-"+"c".repeat(32);
  const {document,hooks}=loadApp(async(url,options={})=>{
    calls.push({url,options});
    if(url.endsWith("/recheck")) {
      wave++;return jsonResponse({run_id:runId,continuation_required:wave===1,scope_complete:wave!==1,message:wave===1?"本批完成":"已完成"});
    }
    return jsonResponse({total:1,has_more:false,items:[{application_id:"app-a",company_name:"示例公司",job_title:"开发工程师",
      state:"blocked",reason:"login_required",record_url:"https://jobs.example.test/applications"}]});
  });
  await hooks.openReviewResults();
  document.getElementById("review-results-content").querySelectorAll("button").find(button=>button.textContent==="登录后重新复核此公司").click();
  await new Promise(resolve=>setImmediate(resolve));
  const starts=calls.filter(call=>call.url.endsWith("/recheck")).map(call=>JSON.parse(call.options.body));
  assert.equal(starts.length,2);assert.deepEqual(starts[0].application_ids,["app-a"]);
  assert.equal(starts[1].run_id,runId);assert.equal(starts[1].request_id,starts[0].request_id);
  assert.equal(starts[1].application_ids,undefined,"continuation cannot recreate or widen company scope");
  assert.ok(calls.some(call=>call.url.includes("run_id="+runId)),"completed run details are shown");
});

test("failed company continuation retries the exact original run and request instead of rebinding scope", async () => {
  const calls=[];let wave=0;
  const runId="status-review-"+"d".repeat(32);
  const {document,hooks}=loadApp(async(url,options={})=>{
    if(url.endsWith("/recheck")) {
      calls.push(JSON.parse(options.body));wave++;
      if(wave===2)return jsonResponse({detail:"临时断开"},503);
      return jsonResponse({run_id:runId,continuation_required:wave===1,scope_complete:wave!==1});
    }
    return jsonResponse({total:1,has_more:false,items:[{application_id:"app-a",company_name:"示例公司",job_title:"开发工程师",
      state:"blocked",reason:"login_required",record_url:"https://jobs.example.test/applications"}]});
  });
  await hooks.openReviewResults();
  const retry=document.getElementById("review-results-content").querySelectorAll("button").find(button=>button.textContent==="登录后重新复核此公司");
  retry.click();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(retry.disabled,false);assert.equal(retry.textContent,"重试本组复核");
  retry.click();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.length,3);
  assert.equal(calls[2].run_id,runId);assert.equal(calls[2].request_id,calls[0].request_id);
  assert.equal(calls[2].application_ids,undefined);
});

test("first-call disconnect followed by another page keeps the original company subset on retry", async () => {
  const starts=[];let pages=0;
  const {document,hooks}=loadApp(async(url,options={})=>{
    if(url.endsWith("/recheck")) {
      starts.push(JSON.parse(options.body));
      if(starts.length===1)return jsonResponse({detail:"连接中断，回执未知"},503);
      return jsonResponse({run_id:"status-review-"+"e".repeat(32),scope_complete:true});
    }
    pages++;
    return jsonResponse({total:2,has_more:pages===1,next_cursor:pages===1?"next":null,items:[
      {application_id:"app-"+pages,company_name:"示例公司",job_title:"岗位"+pages,state:"blocked",reason:"login_required",record_url:"https://jobs.example.test/applications"},
    ]});
  });
  await hooks.openReviewResults();
  const content=document.getElementById("review-results-content");
  content.querySelectorAll("button").find(button=>button.textContent==="登录后重新复核此公司").click();
  await new Promise(resolve=>setImmediate(resolve));
  content.querySelectorAll("button").find(button=>button.textContent==="加载更多").click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.match(content.textContent,/本组已加载 2 个岗位/);
  content.querySelectorAll("button").find(button=>button.textContent==="登录后重新复核此公司").click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(starts.length,2);
  assert.deepEqual(starts[0].application_ids,["app-1"]);
  assert.deepEqual(starts[1].application_ids,["app-1"],"loading another page cannot widen a retried click");
  assert.equal(starts[1].request_id,starts[0].request_id);
});

for(const status of ["paused","stopped"]) {
  test(`company recheck ${status} receipt does not become a completed task`,async()=>{
    const calls=[];const runId="status-review-"+"f".repeat(32);
    const {document,hooks}=loadApp(async(url,options={})=>{
      calls.push({url,options});
      if(url.endsWith("/recheck"))return jsonResponse({run_id:runId,continuation_required:false,
        scope_complete:false,summary:{run_status:status,scope_complete:false},message:"本组复核尚未完成，已保存进度"});
      return jsonResponse({total:1,has_more:false,items:[{application_id:"app-a",company_name:"示例公司",job_title:"开发工程师",
        state:"blocked",reason:"login_required",record_url:"https://jobs.example.test/applications"}]});
    });
    await hooks.openReviewResults();
    const content=document.getElementById("review-results-content");
    const retry=content.querySelectorAll("button").find(button=>button.textContent==="登录后重新复核此公司");
    retry.click();await new Promise(resolve=>setImmediate(resolve));
    assert.equal(retry.textContent,"重试本组复核");assert.equal(retry.disabled,false);
    assert.match(content.textContent,/复核未完成/);assert.doesNotMatch(content.textContent,/复核完成/);
    assert.equal(calls.filter(call=>!call.url.endsWith("/recheck")).length,1,"incomplete receipt never opens completed results");
    retry.click();await new Promise(resolve=>setImmediate(resolve));
    const starts=calls.filter(call=>call.url.endsWith("/recheck")).map(call=>JSON.parse(call.options.body));
    assert.equal(starts[1].run_id,runId);assert.equal(starts[1].request_id,starts[0].request_id);
    assert.equal(starts[1].application_ids,undefined);
  });
}

test("assistant company progress counts all persisted outcomes without showing retry or failure counts", async () => {
  const calls = [];
  const { document, hooks } = loadApp(async (url, options) => {
    calls.push({ url, options });
    return jsonResponse({ run: {
      thread_id: "thread-1", run_id: "company-run", status: "running", phase: "companies",
      stages: { discovery: "succeeded", crawl: "running" },
      progress: { stage: "companies", scope_total: 3321, attempted_unique: 1400, active_count: 10,
        confirmed_complete: 1283, retry_pending: 117 },
    } });
  });
  await hooks.refreshDailyProgress();
  assert.equal(calls[0].url, "/api/local-ui/tasks/progress");
  assert.equal(calls[0].options.headers["X-RecruitOps-Local-UI"], "1");
  assert.equal(document.getElementById("assistant-task-progress").hidden, false);
  assert.match(document.getElementById("assistant-task-progress-detail").textContent, /已处理 1400 \/ 总计 3321 家/);
  assert.match(document.getElementById("assistant-task-progress-detail").textContent, /当前正在处理 10 家/);
  assert.doesNotMatch(document.getElementById("assistant-task-progress-detail").textContent, /待重试|失败|已确认完成/);
  assert.equal(document.getElementById("assistant-task-progress-bar").hidden, false);
  assert.equal(document.getElementById("assistant-task-progress-bar").value, 1400 / 3321 * 100);
  hooks.renderDailyProgress({ run: { status: "stopped", phase: "discovery", stages: {}, progress: null } });
  assert.equal(document.getElementById("assistant-task-progress-bar").hidden, true);
  assert.equal(document.getElementById("assistant-task-progress").hidden, true);
  hooks.renderDailyProgress({ run: { thread_id: "thread-1", status: "running", mode: "score_only", phase: "matching", stages: {},
    progress: { stage: "matching", confirmed_complete: 300, scope_total: 500,
      run_attempted: 20, run_total: 200 } } });
  assert.match(document.getElementById("assistant-task-progress-title").textContent, /后台岗位评分/);
  assert.match(document.getElementById("assistant-task-progress-detail").textContent, /已确认完成 300 \/ 500/);
});

function turnEvent() {
  return frame("turn", { id: "turn-1", thread_id: "thread-1" });
}

test("discovery does not show a full-range percentage before confirming sources", () => {
  const { document, hooks } = loadApp(async () => jsonResponse({ run: null }));
  hooks.renderDailyProgress({ run: { thread_id: "thread-1", status: "running", phase: "discovery", stages: {},
    progress: { stage: "discovery", pages_fetched: 4, pages_total: 30, records_seen: 200,
      total_confirmed: false } } });
  assert.equal(document.getElementById("assistant-task-progress-bar").hidden, true);
  assert.match(document.getElementById("assistant-task-progress-detail").textContent, /来源范围确认中/);
  hooks.renderDailyProgress({ run: { thread_id: "thread-1", status: "running", phase: "companies",
    stages: { discovery: "partial", offline_reconciliation: "skipped" },
    progress: { stage: "companies", attempted_unique: 1, scope_total: 2 } } });
  assert.match(document.getElementById("assistant-task-progress-stages").textContent, /公司发现：部分完成/);
  assert.match(document.getElementById("assistant-task-progress-stages").textContent, /岗位状态整理：已跳过/);
});

test("a finished background crawl triggers one read-only assistant summary in its original conversation", async () => {
  const runId = "a".repeat(32);
  let finished = false;
  const calls = [];
  const answer = `本轮完成，新增 12 个岗位。${runId}`;
  const { hooks, document } = loadApp(async (url, options) => {
    calls.push({ url, options });
    if (url === "/api/local-ui/tasks/progress") {
      return jsonResponse(finished ? { runs: [], run: null } : { runs: [{
        run_id: runId, task_kind: "daily", thread_id: "thread-1", mode: "full", status: "running", phase: "companies",
        progress: { stage: "companies", attempted_unique: 2, scope_total: 3 },
      }] });
    }
    if (url === `/api/local-ui/tasks/progress?run_id=${runId}`) {
      return jsonResponse({ run: { run_id: runId, task_kind: "daily", thread_id: "thread-1", mode: "full", status: "completed" } });
    }
    if (url === "/api/approvals") return jsonResponse([]);
    if (url.includes("/turns/stream")) return streamResponse([
      { value: bytes([
        turnEvent(),
        frame("text_delta", turnEventPayload("text_delta", "event-1", { text: answer })),
        frame("turn_completed", turnEventPayload("turn_completed", "event-2")),
      ].join("")), done: false },
      completeRead(),
    ]);
    if (url === "/api/codex/threads") return jsonResponse({ threads: [] });
    throw new Error(`Unexpected ${url}`);
  });

  await hooks.refreshDailyProgress();
  assert.equal(hooks.state.dailyNotices[runId].status, "active");
  finished = true;
  await hooks.refreshDailyProgress();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(hooks.state.dailyNotices[runId].status, "reported");
  const starts = calls.filter((call) => call.url.includes("/turns/stream"));
  assert.equal(starts.length, 1);
  const prompt = JSON.parse(starts[0].options.body).text;
  assert.match(prompt, /daily_recruitment_sync_status/);
  assert.match(prompt, /不得启动、恢复、取消任务或写入数据/);
  assert.equal(hooks.state.messages.at(-1).body, "本轮完成，新增 12 个岗位。本次任务");
  assert.equal(hooks.state.tasks.at(-1).user_request, "后台爬取结果自动汇报");
  assert.equal(document.getElementById("assistant-live-run").hidden, true);
  assert.equal(document.getElementById("run-task-button").disabled, false);
  assert.equal(hooks.state.activeAssistantController, null);
  await hooks.refreshDailyProgress();
  assert.equal(calls.filter((call) => call.url.includes("/turns/stream")).length, 1);
});

for (const outcome of ["error", "cancelled"]) {
  test(`background summary cleans up its live indicator after ${outcome}`, async () => {
    const runId = "f".repeat(32);
    let modelTurns = 0;
    const { hooks, document } = loadApp(async (url) => {
      if (url === "/api/local-ui/tasks/progress") return jsonResponse({ runs: [] });
      if (url === "/api/approvals") return jsonResponse([]);
      if (url.includes("/turns/stream")) {
        modelTurns += 1;
        return streamResponse([
          { value: bytes(turnEvent()), done: false },
          () => {
            assert.equal(document.getElementById("assistant-live-run").hidden, false);
            if (outcome === "cancelled") {
              hooks.state.activeAssistantController.abort();
              const error = new Error("Cancelled by user"); error.name = "AbortError"; throw error;
            }
            return { value: bytes([
              frame("error", turnEventPayload("error", "error-1", { text: "服务暂不可用" })),
              frame("turn_completed", turnEventPayload("turn_completed", "end-1")),
            ].join("")), done: false };
          },
        ]);
      }
      throw new Error(`Unexpected ${url}`);
    });
    hooks.state.dailyNotices[runId] = { thread_id: "thread-1", status: "ready", terminal_status: "completed", mode: "full" };
    await hooks.refreshDailyProgress();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(hooks.state.dailyNotices[runId].status, "reported");
    assert.equal(document.getElementById("assistant-live-run").hidden, true);
    assert.equal(document.getElementById("run-task-button").disabled, false);
    assert.equal(hooks.state.activeAssistantController, null);
    assert.match(hooks.state.messages.at(-1).body, /自动汇报未能生成/);
    assert.equal(hooks.state.messages.at(-1).streaming, false);
    await hooks.refreshDailyProgress();
    assert.equal(modelTurns, 1); // Reporting failure must never restart the crawl or model turn.
  });
}

test("a crawl that fails before the first progress poll is still tracked from its tool receipt", async () => {
  const runId = "e".repeat(32);
  let modelTurns = 0;
  const { hooks } = loadApp(async (url) => {
    if (url.includes("/turns/stream")) {
      modelTurns += 1;
      const events = modelTurns === 1 ? [
        turnEvent(),
        frame("item_completed", turnEventPayload("item_completed", "event-tool", {
          payload: { tool_name: "daily_recruitment_sync", output: JSON.stringify({ data: { run_id: runId, run_status: "failed" } }) },
        })),
        frame("turn_completed", turnEventPayload("turn_completed", "event-end")),
      ] : [turnEvent(), frame("turn_completed", turnEventPayload("turn_completed", "event-report"))];
      return streamResponse([{ value: bytes(events.join("")), done: false }, completeRead()]);
    }
    if (url === "/api/local-ui/tasks/progress") return jsonResponse({ runs: [] });
    if (url === `/api/local-ui/tasks/progress?run_id=${runId}`) {
      return jsonResponse({ run: { run_id: runId, task_kind: "daily", thread_id: "thread-1", mode: "full", status: "failed" } });
    }
    if (url === "/api/approvals") return jsonResponse([]);
    if (url === "/api/codex/threads") return jsonResponse({ threads: [] });
    throw new Error(`Unexpected ${url}`);
  });
  await hooks.runCodexAssistantQuery("全量爬取", "", "");
  assert.equal(hooks.state.dailyNotices[runId].status, "active");
  await hooks.refreshDailyProgress();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(modelTurns, 2);
  assert.equal(hooks.state.dailyNotices[runId].status, "reported");
});

test("a failed crawl waits for the original conversation before reporting", async () => {
  const runId = "b".repeat(32);
  let finished = false;
  let starts = 0;
  const { hooks } = loadApp(async (url) => {
    if (url === "/api/local-ui/tasks/progress") return jsonResponse(finished ? { runs: [] } : {
      runs: [{ run_id: runId, task_kind: "daily", thread_id: "thread-1", mode: "full", status: "running" }],
    });
    if (url === `/api/local-ui/tasks/progress?run_id=${runId}`) {
      return jsonResponse({ run: { run_id: runId, task_kind: "daily", thread_id: "thread-1", mode: "full", status: "failed" } });
    }
    if (url === "/api/approvals") return jsonResponse([]);
    if (url.includes("/turns/stream")) {
      starts += 1;
      return streamResponse([{ value: bytes([turnEvent(), frame("turn_completed", turnEventPayload("turn_completed", "event-1"))].join("")), done: false }]);
    }
    if (url === "/api/codex/threads") return jsonResponse({ threads: [] });
    throw new Error(`Unexpected ${url}`);
  });
  await hooks.refreshDailyProgress();
  hooks.state.codexThreadId = "thread-2";
  finished = true;
  await hooks.refreshDailyProgress();
  assert.equal(starts, 0);
  assert.equal(hooks.state.dailyNotices[runId].status, "ready");
  hooks.state.codexThreadId = "thread-1";
  await hooks.refreshDailyProgress();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(starts, 1);
  assert.equal(hooks.state.dailyNotices[runId].status, "reported");
});

test("a deliberately paused crawl does not trigger a completion summary", async () => {
  const runId = "c".repeat(32);
  let finished = false;
  let terminalReads = 0;
  const { hooks } = loadApp(async (url) => {
    if (url === "/api/local-ui/tasks/progress") return jsonResponse(finished ? { runs: [] } : {
      runs: [{ run_id: runId, task_kind: "daily", thread_id: "thread-1", mode: "full", status: "running" }],
    });
    if (url === `/api/local-ui/tasks/progress?run_id=${runId}`) {
      terminalReads += 1;
      return jsonResponse({ run: { run_id: runId, task_kind: "daily", thread_id: "thread-1", status: "paused" } });
    }
    if (url === "/api/approvals") return jsonResponse([]);
    throw new Error(`Unexpected ${url}`);
  });
  await hooks.refreshDailyProgress();
  finished = true;
  await hooks.refreshDailyProgress();
  await hooks.refreshDailyProgress();
  assert.equal(hooks.state.dailyNotices[runId].status, "dismissed");
  assert.equal(terminalReads, 1);
});

test("conversation recovery hides the internal automatic-report prompt and run ID", () => {
  const runId = "d".repeat(32);
  const { hooks } = loadApp(async () => { throw new Error("No fetch expected"); });
  const messages = hooks.codexHistoryMessages({ id: "thread-1", turns: [{ id: "turn-auto", items: [
    { id: "user-auto", type: "userMessage", content: `[RecruitOps 自动任务汇报] run_id="${runId}"` },
    { id: "answer-auto", type: "agentMessage", text: `本轮失败。${runId}` },
  ] }] });
  assert.equal(messages.length, 1);
  assert.equal(messages[0].role, "assistant");
  assert.equal(messages[0].body, "本轮失败。本次任务");
});

function turnEventPayload(eventType, eventId, extra = {}) {
  return {
    event_type: eventType,
    event_id: eventId,
    thread_id: "thread-1",
    turn_id: "turn-1",
    ...extra,
  };
}

test("localizes runtime interruption messages without assuming a configured time limit", () => {
  const { document, hooks } = loadApp(async () => { throw new Error("Unexpected network"); });
  hooks.appendMessage("assistant", "Codex turn interrupted after reaching the runtime time limit.");
  const content = document.getElementById("assistant-messages").textContent;
  assert.match(content, /达到运行时限/);
  assert.match(content, /后台任务不会因此自动取消/);
  assert.doesNotMatch(content, /10 分钟|Codex turn interrupted/);
});

test("automation explanation uses only the latest recorded execution and forbids rerun", () => {
  const { hooks } = loadApp(async () => { throw new Error("Unexpected network"); });
  const prompt = hooks.automationExplanationPrompt({
    task_label: "Fixture", latest_execution: {id: "execution-test", status: "failed", error: "Offline test"},
  });
  assert.match(prompt, /execution-test/);
  assert.match(prompt, /Offline test/);
  assert.match(prompt, /不要重新运行任务/);
});

test("desktop capture draft validates typed fields without invoking any API", () => {
  const { hooks } = loadApp(async () => { throw new Error("Draft must not persist"); });
  const normalized = hooks.normalizeApplicationDraft({company_name: "", job_title: " Engineer ", note: "https://example.test/job"});
  assert.equal(normalized.company_name, "");
  assert.equal(normalized.job_title, "Engineer");
  assert.equal(normalized.record_url, "");
  for (const draft of [null, [], {job_title: ""}, {job_title: {}}, {job_title: "a".repeat(513)},
    {job_title: "Engineer", token: "secret"}, {job_title: "Engineer", stage: "offer"},
    {job_title: "Engineer", record_url: "javascript:alert(1)"},
    {job_title: "Engineer", record_url: "https://user:pass@example.test"},
    {job_title: "Engineer", record_url: "https://example.test/#/job/123"}]) {
    assert.throws(() => hooks.normalizeApplicationDraft(draft));
  }
});

test("assistant diagnoses missing model, restart, deliberate disable and runtime failure separately", () => {
  const { hooks, document } = loadApp(async () => { throw new Error("No model or runtime calls"); });
  hooks.state.codexEnabled = false;
  hooks.state.codexReady = false;
  hooks.state.codexHealth = {state: "failed", detail: "Codex App Server missing API key"};
  for (const [status, expected] of [["missing_model", "尚未配置模型连接"], ["restart_required", "重启桌面"],
    ["disabled", "高级配置中关闭"], ["configured", "本地助理服务连接失败"]]) {
    hooks.state.assistantConfiguration = {status};
    hooks.renderCodexRuntimeStatus();
    assert.match(hooks.assistantAvailability().message, new RegExp(expected));
    assert.doesNotMatch(hooks.assistantAvailability().message, /Codex|App Server/);
    assert.equal(document.getElementById("run-task-button").disabled, true);
    assert.equal(document.getElementById("assistant-codex-event-strip").hidden, true);
    assert.equal(document.getElementById("assistant-thread-label").hidden, true);
  }
  hooks.state.codexEnabled = true;
  hooks.state.codexReady = true;
  hooks.renderCodexRuntimeStatus();
  assert.equal(hooks.assistantAvailability().status, "ready");
  assert.equal(document.getElementById("run-task-button").disabled, false);
  assert.equal(document.getElementById("assistant-codex-event-strip").hidden, true);
});

test("assistant presents business stages without raw runtime metadata", () => {
  const { document, hooks } = loadApp(async () => { throw new Error("no fetch"); });
  assert.equal(hooks.friendlyRuntimeProgress("discovery:running"), "公司发现");
  assert.equal(hooks.friendlyRuntimeProgress("matching:12/48"), "岗位评分 本轮 12/48");
  assert.equal(hooks.friendlyRuntimeProgress("companies:19/30"), "公司岗位列表抓取 已处理 19/30");
  assert.equal(hooks.friendlyRuntimeProgress("reporting:succeeded"), "生成结果");

  hooks.state.codexThreadId = "thread-secret-id";
  hooks.appendMessage("assistant", "正在处理");
  const label = document.getElementById("assistant-thread-label").textContent;
  assert.equal(label, "当前会话 · 1 条");
  assert.doesNotMatch(label, /thread-secret-id/);
});

test("assistant keeps structured execution metadata collapsed by default", () => {
  const { document, hooks } = loadApp(async () => { throw new Error("no fetch"); });
  hooks.state.tasks = [{
    task_id: "task-secret-id",
    task_type: "operation_run",
    status: "succeeded",
    steps: 1,
    tool_response: {data: {run_id: "run-secret-id"}},
  }];
  hooks.state.messages = [{
    id: "message-1",
    role: "assistant",
    body: "全量爬取正在运行",
    created_at: new Date().toISOString(),
    task_id: "task-secret-id",
    streaming: false,
  }];
  hooks.renderConversation();

  const disclosure = document.getElementById("assistant-messages").querySelector("details");
  assert.ok(disclosure);
  assert.equal(disclosure.open, false);
  assert.doesNotMatch(document.getElementById("assistant-messages").textContent, /run-secret-id|task-secret-id/);
});

test("consumes SSE events in wire order and completes the turn", async () => {
  const source = [
    turnEvent(),
    frame("thread_started", turnEventPayload("thread_started", "event-1")),
    frame("turn_started", turnEventPayload("turn_started", "event-2")),
    frame("item_started", turnEventPayload("item_started", "event-3", { item_id: "item-1" })),
    frame("text_delta", turnEventPayload("text_delta", "event-4", { text: "A" })),
    frame("item_completed", turnEventPayload("item_completed", "event-5", { item_id: "item-1" })),
    frame("turn_completed", turnEventPayload("turn_completed", "event-6")),
  ].join("");
  const encoded = bytes(source);
  const response = streamResponse([
    { value: encoded.slice(0, 47), done: false },
    { value: encoded.slice(47, 113), done: false },
    { value: encoded.slice(113), done: false },
    completeRead(),
  ]);
  const calls = [];
  const { hooks } = loadApp(async (url, options) => {
    calls.push({ url, options });
    return response;
  });

  const task = await hooks.runCodexAssistantQuery("查看岗位", "", "");

  assert.equal(calls.length, 1);
  assert.equal(calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].options.body), { text: "查看岗位" });
  assert.equal(task.answer, "A");
  assert.deepEqual(
    Array.from(task.codex_events, (event) => event.event_type),
    ["turn", "thread_started", "turn_started", "item_started", "item_completed", "turn_completed"],
  );
  assert.equal(task.codex_events.at(-1).status, "completed");
  assert.equal(hooks.state.activeAssistantController, null);
});

test("reconnects on the same thread and ignores replayed events", async () => {
  const firstResponse = streamResponse([
    {
      value: bytes([
        turnEvent(),
        frame("text_delta", turnEventPayload("text_delta", "event-1", { text: "A" })),
      ].join("")),
      done: false,
    },
    () => Promise.reject(new Error("network disconnected")),
  ]);
  const secondResponse = streamResponse([
    {
      value: bytes([
        frame("text_delta", turnEventPayload("text_delta", "event-1", { text: "A" })),
        frame("text_delta", turnEventPayload("text_delta", "event-2", { text: "B" })),
        frame("turn_completed", turnEventPayload("turn_completed", "event-3")),
      ].join("")),
      done: false,
    },
  ]);
  const calls = [];
  const { hooks } = loadApp(async (url, options = {}) => {
    calls.push({ url, options });
    if (options.method === "POST") return firstResponse;
    assert.equal(options.method, "GET");
    return secondResponse;
  });

  const task = await hooks.runCodexAssistantQuery("继续", "", "");

  assert.equal(calls.length, 2);
  assert.match(calls[1].url, /\/api\/codex\/threads\/thread-1\/events$/);
  assert.equal(calls[1].options.headers["Last-Event-ID"], "event-1");
  assert.equal(task.answer, "AB");
  assert.deepEqual(Array.from(task.codex_events, (event) => event.event_type), ["turn", "turn_completed"]);
  assert.equal(Array.from(task.codex_events).filter((event) => event.event_type === "turn_completed").length, 1);
});

test("cancellation leaves one stopped, non-streaming assistant message", async () => {
  let rejectPendingRead;
  let streamSignal;
  const pendingRead = new Promise((_, reject) => {
    rejectPendingRead = reject;
  });
  const response = {
    ok: true,
    status: 200,
    body: {
      getReader() {
        let reads = 0;
        return {
          read() {
            reads += 1;
            if (reads === 1) return Promise.resolve({ value: bytes(turnEvent()), done: false });
            return pendingRead;
          },
        };
      },
    },
  };
  const calls = [];
  const { document, hooks } = loadApp(async (url, options = {}) => {
    calls.push({ url, options });
    if (/\/turns\/stream$/.test(url)) {
      streamSignal = options.signal;
      streamSignal.addEventListener("abort", () => {
        const error = new Error("aborted");
        error.name = "AbortError";
        rejectPendingRead(error);
      }, { once: true });
      return response;
    }
    assert.match(url, /\/interrupt$/);
    return { ok: true, status: 200, json: async () => ({ status: "interrupt_requested" }) };
  });

  const submit = hooks.submitAssistantQuestion("请查询并停止", null);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(hooks.state.codexTurnId, "turn-1");
  assert.ok(streamSignal);
  await Promise.all([submit, hooks.stopAssistantExecution()]);

  const assistant = hooks.state.messages.at(-1);
  const article = document.getElementById("assistant-messages")
    .querySelectorAll(".message")
    .find((node) => node.dataset.messageId === assistant.id);
  assert.equal(calls.filter((call) => /\/interrupt$/.test(call.url)).length, 1);
  assert.equal(assistant.streaming, false);
  assert.match(assistant.body, /^已按你的要求停止/);
  assert.equal(article.dataset.streaming, undefined);
  assert.equal(document.getElementById("assistant-message-status").textContent, "已停止");
  assert.equal(hooks.state.activeAssistantController, null);
  assert.equal(hooks.state.codexStopRequested, false);
  assert.equal(streamSignal.aborted, true);
  assert.ok(calls.some(call => call.url === "/api/schedule"));
  assert.ok(calls.some(call => call.url.startsWith("/api/applications")));
});

test("clears the composer after a successful message send", async () => {
  const calls = [];
  const response = streamResponse([
    {
      value: bytes([
        turnEvent(),
        frame("text_delta", turnEventPayload("text_delta", "event-1", { text: "岗位结果" })),
        frame("turn_completed", turnEventPayload("turn_completed", "event-2")),
      ].join("")),
      done: false,
    },
    completeRead(),
  ]);
  const { document, hooks } = loadApp(async (url, options = {}) => {
    calls.push({ url, options });
    if (/\/turns\/stream$/.test(url)) return response;
    if (/\/api\/approvals$/.test(url)) return jsonResponse([]);
    if (/\/api\/codex\/threads\?/.test(url)) return jsonResponse({ data: [] });
    return jsonResponse({ items: [] });
  });

  const input = document.getElementById("assistant-message");
  const jobId = document.getElementById("assistant-job-id");
  input.value = "查看今日岗位";
  jobId.value = "job-1";
  const task = await hooks.submitAssistantQuestion(input.value, jobId.value);

  assert.equal(task.status, "succeeded");
  const streamCall = calls.find((call) => /\/turns\/stream$/.test(call.url));
  assert.ok(streamCall);
  assert.deepEqual(JSON.parse(streamCall.options.body), {
    text: "查看今日岗位", job_id: "job-1",
  });
  assert.equal(input.value, "");
  assert.equal(jobId.value, "");
  assert.equal(calls.filter(call => call.url === "/api/schedule").length, 1);
  assert.ok(calls.some(call => call.url.startsWith("/api/applications")));
  assert.ok(calls.some(call => call.url.startsWith("/api/recruitment-mails")));
});

test("deletes a conversation without confirmation and reloads the server-synced next conversation", async () => {
  const serverThreads = new Map([
    ["thread-1", { id: "thread-1", preview: "旧会话" }],
    ["thread-2", { id: "thread-2", preview: "保留会话" }],
  ]);
  const calls = [];
  const { hooks } = loadApp(async (url, options = {}) => {
    calls.push({ url, options });
    if (options.method === "DELETE") {
      serverThreads.delete("thread-1");
      return jsonResponse({ status: "deleted", thread_id: "thread-1" });
    }
    if (url === "/api/codex/threads?limit=20") {
      return jsonResponse({ data: [...serverThreads.values()] });
    }
    if (url === "/api/codex/threads/thread-2") {
      return jsonResponse({
        id: "thread-2",
        turns: [{
          id: "turn-2",
          items: [
            { type: "userMessage", content: "保留的上下文" },
            { type: "agentMessage", text: "恢复后的回答" },
          ],
        }],
      });
    }
    if (url.endsWith("/resume")) return jsonResponse({ id: "thread-2" });
    throw new Error(`unexpected request: ${url}`);
  }, { confirm: () => { throw new Error("Conversation deletion must not request confirmation"); } });

  hooks.state.conversations = [...serverThreads.values()];
  const deleted = await hooks.deleteConversation("thread-1");

  assert.equal(deleted, true);
  assert.deepEqual([...serverThreads.keys()], ["thread-2"]);
  const deleteCallIndex = calls.findIndex((call) => call.options.method === "DELETE");
  assert.notEqual(deleteCallIndex, -1);
  assert.equal(calls[deleteCallIndex].url, "/api/codex/threads/thread-1");
  const refreshCallIndex = calls.findIndex((call, index) => index > deleteCallIndex && call.url === "/api/codex/threads?limit=20");
  assert.notEqual(refreshCallIndex, -1);
  assert.deepEqual(hooks.state.conversations.map((item) => item.id), ["thread-2"]);
  assert.equal(hooks.state.codexThreadId, "thread-2");
  assert.deepEqual(Array.from(hooks.state.messages, (item) => item.body), ["保留的上下文", "恢复后的回答"]);
});

test("keeps a bounded local history and visibly marks older messages while retaining recent context", () => {
  const { document, hooks } = loadApp(async () => {
    throw new Error("history test should not fetch");
  });
  hooks.state.messages = Array.from({ length: 59 }, (_, index) => ({
    id: `message-${index}`,
    role: index % 2 ? "assistant" : "user",
    body: index === 58 ? "关键上下文：目标岗位与投递限制" : `历史消息 ${index}`,
    created_at: new Date().toISOString(),
    result: null,
    streaming: false,
  }));

  hooks.appendMessage("user", "最新问题");
  hooks.appendMessage("assistant", "最新回答");

  assert.equal(hooks.state.messages.length, 60);
  assert.equal(hooks.state.messages[0].id, "message-1");
  assert.ok(hooks.state.messages.some((item) => item.body.includes("关键上下文")));
  const list = document.getElementById("assistant-messages");
  assert.equal(list.querySelectorAll(".message").length, 50);
  assert.match(list.firstChild.textContent, /较早的 10 条消息未在当前窗口渲染/);
  assert.match(list.textContent, /关键上下文：目标岗位与投递限制/);
  assert.match(list.textContent, /最新回答/);
});

test("shows the configured automatic compaction threshold and context window", () => {
  const { document, hooks } = loadApp(async () => {
    throw new Error("context policy test should not fetch");
  });
  hooks.state.codexHealth = {
    context_management: {
      context_window_tokens: 1_000_000,
      auto_compact_token_limit: 96_000,
    },
  };
  hooks.renderCodexRuntimeStatus();

  const policy = document.getElementById("assistant-context-policy");
  assert.equal(policy.textContent, "96k 自动压缩");
  assert.match(policy.title, /96,000/);
  assert.match(policy.title, /1,000,000/);
});

test("renders markdown lists and aligned tables as DOM elements", () => {
  const { hooks } = loadApp(async () => {
    throw new Error("markdown test should not fetch");
  });
  const root = new FakeElement("div");
  hooks.renderMarkdown(root, [
    "岗位摘要",
    "",
    "- **后端开发**",
    "- `Python`",
    "",
    "1. 第一项",
    "2. 第二项",
    "",
    "| 岗位 | 城市 |",
    "| :--- | ---: |",
    "| 后端 | 上海 |",
  ].join("\n"));

  assert.deepEqual(root.children.map((node) => node.tagName), ["P", "UL", "OL", "DIV"]);
  assert.deepEqual(root.children[1].children.map((node) => node.textContent), ["后端开发", "Python"]);
  assert.deepEqual(root.children[2].children.map((node) => node.textContent), ["第一项", "第二项"]);

  const table = root.children[3].children[0];
  const headers = table.children[0].children[0].children;
  const row = table.children[1].children[0].children;
  assert.deepEqual([...headers].map((cell) => cell.textContent), ["岗位", "城市"]);
  assert.deepEqual([...row].map((cell) => cell.textContent), ["后端", "上海"]);
  assert.equal(headers[0].style.textAlign, "left");
  assert.equal(headers[1].style.textAlign, "right");
});

test("renders ordinary markdown source citations as links", () => {
  const { hooks } = loadApp(async () => { throw new Error("no fetch"); });
  const root = new FakeElement("div");
  const url = "/api/jobs/job-1";
  hooks.renderMarkdown(root, `[岗位来源](${url})`);
  const links = root.querySelectorAll("a");
  assert.equal(links.length, 1);
  assert.equal(links[0].href, `http://localhost${url}`);
  assert.equal(links[0].textContent, "岗位来源");
});

function mailFixture(overrides = {}) {
  return {
    id: "mail-1",
    subject: "星河科技一面邀请",
    sender: "hr@example.test",
    category: "interview",
    confidence: 0.94,
    processing_status: "processed_updated",
    requires_confirmation: false,
    application_id: "application-1",
    company_name: "星河科技",
    job_title: "机器人软件工程师",
    ...overrides,
  };
}

test("renders human-readable mail processing and association labels", () => {
  const { document, hooks } = loadApp(async () => {
    throw new Error("mail label test should not fetch");
  });
  hooks.renderMails([
    mailFixture(),
    mailFixture({
      id: "mail-2",
      subject: "测评通知",
      processing_status: "pending_association",
      requires_confirmation: true,
      application_id: null,
    }),
  ], false, { status: "synced", synced_at: "2026-09-07T08:00:00Z" });

  const list = document.getElementById("mail-list");
  assert.match(list.textContent, /处理：已更新投递阶段/);
  assert.match(list.textContent, /关联：已关联/);
  assert.match(list.textContent, /处理：待关联/);
  assert.match(list.textContent, /关联：待关联/);
  assert.doesNotMatch(list.textContent, /processed_updated|pending_association/);
  assert.equal(document.getElementById("mail-freshness-note").dataset.state, "synced");
});

test("shows distinct mail and Edge sources in application history", () => {
  const { document, hooks } = loadApp(async () => {
    throw new Error("application history test should not fetch");
  });
  hooks.renderApplications({
    total: 1,
    items: [{
      id: "application-1",
      company_name: "星河科技",
      job_title: "机器人软件工程师",
      stage: "interview1",
      updated_at: "2026-09-07T08:00:00Z",
      stage_history: [
        { stage: "applied", result: "进行中", date: "2026-09-01", source: "recruitment_mail", source_ref: "mail-1" },
        { stage: "interview1", result: "进行中", date: "2026-09-06", source: "edge_application_status_review", source_ref: "edge-operation-1" },
      ],
    }],
  });

  const history = document.getElementById("application-kanban");
  const details = history.querySelector("details");
  assert.doesNotMatch(history.textContent, /来源：招聘邮件|来源：Edge 官网/);
  details.open = true;
  details.dispatchEvent({ type: "toggle" });
  assert.match(history.textContent, /来源：招聘邮件/);
  assert.match(history.textContent, /来源：Edge 官网/);
  assert.doesNotMatch(history.textContent, /edge_application_status_review|recruitment_mail/);
});

test("reports a successful mail sync without invoking association or application writes", async () => {
  const calls = [];
  const { document, hooks } = loadApp(async (url, options = {}) => {
    calls.push({ url, options });
    if (url.includes("/sync?")) {
      return jsonResponse({ status: "synced", sync: { fetched: 3, inserted: 2, reused: 1 } });
    }
    return jsonResponse({
      items: [mailFixture()],
      total: 1,
      freshness: { status: "synced", synced_at: "2026-09-07T08:00:00Z" },
    });
  });

  await hooks.syncRecruitmentMails();

  assert.equal(document.getElementById("mail-sync-status").dataset.state, "success");
  assert.match(document.getElementById("mail-sync-status-detail").textContent, /新增 2 封/);
  assert.deepEqual(calls.map((call) => call.url), [
    "/api/recruitment-mails/sync?limit=100",
    "/api/recruitment-mails?limit=50&refresh=false",
  ]);
  assert.equal(calls[0].options.method, "POST");
  assert.ok(calls.every((call) => !call.url.includes("/review") && !call.url.includes("/applications")));
});

test("distinguishes a no-new sync from success", async () => {
  const { document, hooks } = loadApp(async (url) => {
    if (url.includes("/sync?")) return jsonResponse({ status: "synced", sync: { fetched: 0, inserted: 0, reused: 4 } });
    return jsonResponse({ items: [mailFixture()], freshness: { status: "synced" } });
  });

  await hooks.syncRecruitmentMails();

  const status = document.getElementById("mail-sync-status");
  assert.equal(status.dataset.state, "no-new");
  assert.match(document.getElementById("mail-sync-status-title").textContent, /已是最新/);
  assert.match(document.getElementById("mail-sync-status-detail").textContent, /没有新邮件/);
});

test("cached sync is not a failure and never replays old inserted counts", async () => {
  const { document, hooks } = loadApp(async (url) => jsonResponse(url.includes("/sync?")
    ? { status: "cached", synced_at: "2026-09-27T00:00:00Z", sync: { fetched: 14, inserted: 14, reused: 0 } }
    : { items: [], freshness: { status: "cached", synced_at: null } }));
  await hooks.syncRecruitmentMails();
  assert.equal(document.getElementById("mail-sync-status").dataset.state, "cached");
  assert.match(document.getElementById("mail-sync-status-detail").textContent, /最近.*同步|复用/);
  assert.doesNotMatch(document.getElementById("mail-sync-status-detail").textContent, /新增 14|失败|已是最新/);
  assert.equal(hooks.state.mailFreshness.synced_at, "2026-09-27T00:00:00Z");
});

test("competing synchronization is pending, not failed, and allows a later click", async () => {
  let syncCalls = 0;
  const { document, hooks } = loadApp(async (url) => {
    if (url.includes("/sync?")) {
      syncCalls++;
      return jsonResponse(syncCalls === 1
        ? { status: "failed", error_type: "mail_sync_in_progress", timed_out: true }
        : { status: "synced", sync: { fetched: 0, inserted: 0, reused: 0 } });
    }
    return jsonResponse({ items: [], freshness: { status: "cached" } });
  });
  await hooks.syncRecruitmentMails();
  assert.equal(document.getElementById("mail-sync-status").dataset.state, "in-progress");
  assert.match(document.getElementById("mail-sync-status-title").textContent, /正在同步/);
  assert.equal(document.getElementById("mail-freshness-note").dataset.state, "syncing");
  assert.equal(document.getElementById("mail-refresh-button").disabled, false);
  await hooks.syncRecruitmentMails();
  assert.equal(syncCalls, 2);
  assert.equal(document.getElementById("mail-sync-status").dataset.state, "no-new");
});

test("cache-only list reload does not erase successful sync freshness", async () => {
  const { document, hooks } = loadApp(async (url) => jsonResponse(url.includes("/sync?")
    ? { status: "synced", synced_at: "2026-09-27T00:00:00Z", sync: { fetched: 0, inserted: 0, reused: 0 } }
    : { items: [], freshness: { status: "cached", synced_at: null } }));
  await hooks.syncRecruitmentMails();
  assert.equal(document.getElementById("mail-freshness-note").dataset.state, "synced");
  assert.equal(hooks.state.mailFreshness.synced_at, "2026-09-27T00:00:00Z");
});

test("keeps cached mail visible and warns when sync returns failed freshness", async () => {
  const { document, hooks } = loadApp(async (url) => {
    if (url.includes("/sync?")) {
      return jsonResponse({ status: "failed", error_type: "TimeoutError", sync: { fetched: 0, inserted: 0, reused: 0 } });
    }
    return jsonResponse({
      items: [mailFixture({ subject: "本地缓存的面试邮件" })],
      freshness: { status: "failed", synced_at: "2026-09-06T08:00:00Z", error_type: "TimeoutError" },
    });
  });

  await hooks.syncRecruitmentMails();

  assert.equal(document.getElementById("mail-sync-status").dataset.state, "failed");
  assert.equal(document.getElementById("mail-freshness-note").dataset.state, "failed");
  assert.match(document.getElementById("mail-freshness-title").textContent, /缓存/);
  assert.match(document.getElementById("mail-freshness-detail").textContent, /不是最新/);
  assert.match(document.getElementById("mail-list").textContent, /本地缓存的面试邮件/);
  assert.match(document.getElementById("mail-sync-status-detail").textContent, /TimeoutError/);
});

function scheduleFixture(overrides = {}) {
  return {
    id: "schedule-1",
    title: "星河科技一面",
    event_date: "2026-09-10",
    event_time: "10:00:00",
    event_type: "面试",
    time_kind: "appointment",
    status: "pending",
    company_name: "星河科技",
    job_title: "机器人软件工程师",
    application_id: "application-1",
    location_or_link: null,
    note: "准备项目经历",
    updated_at: "2026-09-08T08:00:00Z",
    source: "manual",
    source_ref: "schedule-1",
    ...overrides,
  };
}

test("renders overdue and due-soon todo groups without calling an undated event all-day", () => {
  const { document, hooks } = loadApp(async () => {
    throw new Error("schedule render should not fetch");
  }, { Date: class extends Date {
    constructor(...args) { super(...(args.length ? args : ["2026-09-15T12:00:00+08:00"])); }
    static now() { return new Date("2026-09-15T12:00:00+08:00").getTime(); }
  } });
  hooks.state.allSchedules = [
    scheduleFixture({ id: "overdue", title: "逾期面试", event_date: "2026-09-01" }),
    scheduleFixture({ id: "soon", title: "近期笔试", event_date: "2026-09-16", event_time: null }),
    scheduleFixture({ id: "undated", title: "邮件测评待定", event_date: null, event_time: null, job_title: "" }),
  ];
  hooks.state.scheduleStatus = "all";
  hooks.renderFullSchedule();

  const todo = document.getElementById("schedule-todo-view");
  assert.match(todo.textContent, /逾期/);
  assert.match(todo.textContent, /即将到期/);
  assert.match(todo.textContent, /日期待定/);
  assert.match(todo.textContent, /时间待定/);
  assert.doesNotMatch(todo.textContent, /全天/);
});

test("calendar shows dated events and only counts undated events", () => {
  const { document, hooks } = loadApp(async () => {
    throw new Error("calendar render should not fetch");
  });
  hooks.state.allSchedules = [
    scheduleFixture({ id: "dated", title: "日历可见面试", event_date: "2026-09-14" }),
    scheduleFixture({ id: "undated", title: "不应重复列出的事项", event_date: null, event_time: null }),
  ];
  hooks.state.scheduleStatus = "all";
  hooks.state.scheduleView = "calendar";
  hooks.state.scheduleMonth = "2026-09";
  hooks.renderFullSchedule();

  assert.match(document.getElementById("schedule-calendar-grid").textContent, /日历可见面试/);
  const undated = document.getElementById("schedule-calendar-undated");
  assert.match(undated.textContent, /1 项未定日期事项/);
  assert.match(undated.textContent, /切回待办/);
  assert.doesNotMatch(undated.textContent, /不应重复列出的事项/);
});

test("same-day events with a passed clock are overdue, while date-only events are not", () => {
  const { hooks } = loadApp(async () => {
    throw new Error("schedule due-group test should not fetch");
  });
  const now = new Date(2026, 8, 14, 10, 0, 0);

  assert.equal(hooks.scheduleDueGroup(scheduleFixture({ event_date: "2026-09-14", event_time: "09:59:00" }), now), "overdue");
  assert.equal(hooks.scheduleDueGroup(scheduleFixture({ event_date: "2026-09-14", event_time: null }), now), "soon");
  assert.equal(hooks.scheduleDueGroup(scheduleFixture({ event_date: "2026-09-14", event_time: "10:00:00" }), now), "soon");
});

test("calendar labels deadline clocks explicitly", () => {
  const { document, hooks } = loadApp(async () => {
    throw new Error("deadline calendar test should not fetch");
  });
  hooks.state.scheduleStatus = "all";
  hooks.state.scheduleMonth = "2026-09";
  hooks.renderScheduleCalendar([
    scheduleFixture({
      id: "deadline-1",
      title: "测评截止",
      event_date: "2026-09-14",
      event_time: "17:01:00",
      time_kind: "deadline",
    }),
  ]);

  assert.match(document.getElementById("schedule-calendar-grid").textContent, /截止 17:01/);
});

test("schedule locations stay text unless they are absolute HTTP URLs", () => {
  const { document, hooks } = loadApp(async () => {
    throw new Error("schedule location test should not fetch");
  });
  assert.equal(hooks.safeScheduleHref("上海某楼"), null);
  assert.equal(hooks.safeScheduleHref("/meeting-room"), null);
  assert.equal(hooks.safeScheduleHref("https://example.test/meeting"), "https://example.test/meeting");

  hooks.state.allSchedules = [scheduleFixture({ location_or_link: "上海某楼" })];
  hooks.renderFullSchedule();
  const todo = document.getElementById("schedule-todo-view");
  assert.match(todo.textContent, /上海某楼/);
  assert.equal(todo.querySelectorAll("a").length, 0);

  hooks.renderSchedule([scheduleFixture({ location_or_link: "上海某楼" })]);
  const dashboardSchedule = document.getElementById("today-schedule");
  assert.match(dashboardSchedule.textContent, /上海某楼/);
  assert.equal(dashboardSchedule.querySelectorAll("a").length, 0);
});

test("schedule editor offers existing applications and an unassociated option", () => {
  const { document, hooks } = loadApp(async () => {
    throw new Error("schedule association test should not fetch");
  });
  hooks.state.applications = [{ id: "application-2", company_name: "远山科技", job_title: "测试工程师" }];

  hooks.openScheduleEditor();

  const select = document.getElementById("schedule-application-id");
  assert.equal(select.children.length, 2);
  assert.equal(select.children[0].textContent, "无关联");
  assert.match(select.children[1].textContent, /远山科技 · 测试工程师/);
  assert.equal(select.children[1].value, "application-2");
});

test("status updates use the local-ui event route and optimistic version", async () => {
  const calls = [];
  const { hooks } = loadApp(async (url, options = {}) => {
    calls.push({ url, options });
    return jsonResponse(url === "/api/schedule" ? [] : { status: "updated" });
  });
  hooks.state.allSchedules = [scheduleFixture({ id: "status-1", updated_at: "2026-09-08T08:00:00Z" })];

  await hooks.updateScheduleStatus("status-1", "completed");

  assert.equal(calls[0].url, "/api/local-ui/events/status-1");
  assert.equal(calls[0].options.method, "PATCH");
  assert.equal(calls[0].options.headers["X-RecruitOps-Local-UI"], "1");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    status: "completed",
    expected_updated_at: "2026-09-08T08:00:00Z",
  });
});

for (const scenario of [
  {
    name: "omits unchanged labels for note and time edits with the same application",
    fields: { "schedule-note": "Updated reminder", "schedule-event-time": "11:30" },
    expected: { note: "Updated reminder", event_time: "11:30" },
    omitted: ["company_name", "job_title"],
  },
  {
    name: "preserves an intentional identity edit with the same application",
    fields: { "schedule-company-name": "Changed company", "schedule-job-title": "Changed role" },
    expected: { company_name: "Changed company", job_title: "Changed role" },
    omitted: [],
  },
  {
    name: "preserves labels when rebinding to another application",
    fields: { "schedule-application-id": "application-2" },
    expected: { application_id: "application-2", company_name: "星河科技", job_title: "机器人软件工程师" },
    omitted: [],
  },
]) {
  test(`schedule editor ${scenario.name}`, async () => {
    const calls = [];
    const { document, hooks } = loadApp(async (url, options = {}) => {
      calls.push({ url, options });
      return jsonResponse(url === "/api/schedule" ? [] : { status: "updated" });
    });
    const existing = scheduleFixture({ company_name: " 星河科技 ", job_title: " 机器人软件工程师 " });
    hooks.state.allSchedules = [existing];
    hooks.openScheduleEditor(existing);
    for (const [id, value] of Object.entries(scenario.fields)) document.getElementById(id).value = value;

    await hooks.submitScheduleForm({ preventDefault() {} });

    assert.equal(calls[0].url, "/api/local-ui/events/schedule-1");
    assert.equal(calls[0].options.method, "PATCH");
    const payload = JSON.parse(calls[0].options.body);
    assert.equal(payload.expected_updated_at, existing.updated_at);
    for (const [field, value] of Object.entries(scenario.expected)) assert.equal(payload[field], value);
    for (const field of scenario.omitted) assert.equal(Object.hasOwn(payload, field), false);
  });
}

test("schedule refresh updates dashboard and preserves filters while ignoring stale responses", async () => {
  let resolveOlder;
  let requests = 0;
  const date = "2026-09-18";
  class FixedDate extends Date {
    constructor(...args) { super(...(args.length ? args : [`${date}T12:00:00`])); }
  }
  const { document, hooks } = loadApp(async () => {
    requests += 1;
    if (requests === 1) return new Promise(resolve => { resolveOlder = resolve; });
    return jsonResponse([
      scheduleFixture({ id: "pending", event_date: date, status: "pending" }),
      scheduleFixture({ id: "done", event_date: date, status: "completed" }),
    ]);
  }, { Date: FixedDate });
  hooks.state.scheduleView = "calendar";
  hooks.state.scheduleStatus = "all";
  hooks.state.scheduleDateFilter = date;
  const old = hooks.loadFullSchedule();
  assert.equal(await hooks.loadFullSchedule(), true);
  resolveOlder(jsonResponse([]));
  assert.equal(await old, false);
  assert.equal(hooks.state.allSchedules.length, 2);
  assert.equal(hooks.state.schedules.length, 1);
  assert.equal(document.getElementById("metric-schedule").textContent, "1");
  assert.match(document.getElementById("today-todos").textContent, /1 项待看/);
  assert.equal(hooks.state.scheduleView, "calendar");
  assert.equal(hooks.state.scheduleStatus, "all");
  assert.equal(hooks.state.scheduleDateFilter, date);
});

test("application refresh ignores older responses after a manual update", async () => {
  let resolveOlder;
  let requests = 0;
  const { hooks } = loadApp(async () => {
    if (++requests === 1) return new Promise(resolve => { resolveOlder = resolve; });
    return jsonResponse({ items: [{id: "new", company_name: "Fixture", job_title: "Engineer", stage: "written"}], total: 1 });
  });
  const old = hooks.loadApplications();
  assert.equal(await hooks.loadApplications(), true);
  resolveOlder(jsonResponse({ items: [], total: 0 }));
  assert.equal(await old, false);
  assert.equal(hooks.state.applications[0].stage, "written");
});

test("manual schedule payload permits an empty job title and no application", () => {
  const { document, hooks } = loadApp(async () => {
    throw new Error("schedule form test should not fetch");
  });
  document.getElementById("schedule-title").value = "公司测评时间待定";
  document.getElementById("schedule-event-type").value = "测评";
  document.getElementById("schedule-event-date").value = "";
  document.getElementById("schedule-event-time").value = "";
  document.getElementById("schedule-time-kind").value = "unspecified";
  document.getElementById("schedule-company-name").value = "星河科技";
  document.getElementById("schedule-job-title").value = "";
  document.getElementById("schedule-application-id").value = "";
  document.getElementById("schedule-location").value = "";
  document.getElementById("schedule-note").value = "邮件未给出岗位名称";

  assert.deepEqual(JSON.parse(JSON.stringify(hooks.scheduleFormPayload())), {
    title: "公司测评时间待定",
    event_date: null,
    event_time: null,
    event_type: "测评",
    company_name: "星河科技",
    job_title: "",
    application_id: null,
    location_or_link: null,
    note: "邮件未给出岗位名称",
    time_kind: "unspecified",
  });
});

test("schedule mail sources reuse the existing detail dialog", async () => {
  const { document, hooks } = loadApp(async (url) => {
    assert.equal(url, "/api/recruitment-mails/mail-1?refresh=false");
    return jsonResponse({
      record_id: "mail-1",
      processing_status: "processed_unchanged",
      message: {
        subject: "星河科技测评通知",
        sender: "hr@example.test",
        received_at: "2026-09-08T08:00:00Z",
        body_text: "请完成测评。",
      },
    });
  });
  hooks.state.mails = [mailFixture({ id: "mail-1" })];

  await hooks.openRecruitmentMail("mail-1");

  assert.equal(document.getElementById("job-detail-company").textContent, "招聘邮件 · mail-1");
  assert.equal(document.getElementById("job-detail-title").textContent, "星河科技测评通知");
  assert.match(document.getElementById("job-detail-content").textContent, /请完成测评/);
});

test("active progress supports review and mail, never includes old finished cards or IDs", () => {
  const { document, hooks } = loadApp(async () => jsonResponse([]));
  hooks.renderDailyProgress({ runs: [
    { thread_id: "thread-1", run_id: "private-review-id", task_kind: "application_review", status: "running", phase: "application_review", completed: 12, total: 88, failed: 2, blocked: 1, actions: ["pause", "cancel"] },
    { thread_id: "thread-1", run_id: "private-mail-id", task_kind: "recruitment_mail", status: "running", phase: "processing", completed: 3, total: 10, unit: "封" },
    { task_kind: "daily", status: "failed", phase: "discovery", mode: "full" },
  ] });
  assert.match(document.getElementById("assistant-task-progress-title").textContent, /官网投递状态复核/);
  assert.match(document.getElementById("assistant-task-progress-detail").textContent, /12 \/ 88/);
  assert.match(document.getElementById("assistant-task-progress-detail").textContent, /失败 2/);
  assert.match(document.getElementById("assistant-more-task-progress").textContent, /处理招聘邮件/);
  assert.doesNotMatch(document.body.textContent, /private-review-id|private-mail-id|后台全量爬取/);
  hooks.renderDailyProgress({ runs: [{ status: "paused" }, { status: "succeeded" }, { status: "failed" }] });
  assert.equal(document.getElementById("assistant-task-progress").hidden, true);
  assert.equal(document.getElementById("assistant-more-task-progress").children.length, 0);
});

test("application board automatically loads every column without hiding other stages", async () => {
  const calls = [];
  const records = Array.from({ length: 86 }, (_, index) => ({ id: `a-${index}`, company_name: "示例公司",
    job_title: `岗位${index}`, stage: index < 82 ? "applied" : index < 85 ? "written" : "interview1" }));
  const { document, hooks } = loadApp(async url => {
    calls.push(url);
    const params = new URL(url, "http://localhost").searchParams;
    const rows = records.filter(row => params.getAll("stages").includes(row.stage));
    const offset = Number(params.get("offset"));
    return jsonResponse({ items: rows.slice(offset, offset + 50), total: rows.length, unfiltered_total: 888,
      stage_counts: { interview1: 1, applied: 82, written: 3 } });
  });
  document.getElementById("application-search").value = "  示例公司  ";
  await hooks.loadApplications();
  assert.equal(calls.length, 6);
  assert.ok(calls.every(url => new URL(url, "http://localhost").searchParams.get("query") === "示例公司"));
  assert.ok(calls.slice(0, 5).every(url => new URL(url, "http://localhost").searchParams.get("offset") === "0"));
  assert.equal(hooks.state.applications.length, 86);
  assert.equal(hooks.state.applicationBrowse.columns.interview.items.length, 1);
  assert.equal(hooks.state.applicationBrowse.columns.written.items.length, 3);
  assert.equal(document.getElementById("nav-application-count").textContent, "888");
  assert.match(document.getElementById("application-page-description").textContent, /匹配 86 条 · 已显示 86 条/);
  assert.equal(new URL(calls[5], "http://localhost").searchParams.get("offset"), "50");
  assert.equal(hooks.state.applications.length, 86);
  assert.equal(hooks.state.applicationBrowse.columns.interview.items.length, 1);
  assert.equal(hooks.state.applicationBrowse.columns.written.items.length, 3);
});

test("application channels validate the progress URL independently of stage and source URLs", () => {
  const { hooks } = loadApp(async () => { throw new Error("classification must not fetch"); });
  for (const record_url of ["https://careers.example.test/progress", " HTTP://example.test:8080/#/applications ", "https://[::1]:443/progress"]) {
    assert.equal(hooks.applicationProgressChannel({ record_url }), "official_page", record_url);
  }
  for (const record_url of [null, "", " ", "/progress", "javascript:alert(1)", "mailto:hr@example.test", "https://", "https:///careers.example.test", "https://user:secret@example.test", "https://example.test:bad", "https://example.test:65536", "https://bad host/progress"]) {
    assert.equal(hooks.applicationProgressChannel({ record_url, stage: "offer", detail_url: "https://example.test/job", source_url: "https://example.test" }), "mail_only", String(record_url));
  }
});

test("application channels include every page, combine with search, and never discard schedule choices", async () => {
  const calls = [];
  const records = Array.from({ length: 122 }, (_, index) => ({ id: `mixed-${index}`, company_name: "公司",
    job_title: `岗位${index}`, stage: "applied", record_url: index % 2 ? null : "https://example.test/progress" }));
  const { document, hooks } = loadApp(async url => {
    calls.push(url);
    const params = new URL(url, "http://localhost").searchParams;
    const matches = records.filter(row => row.job_title.includes(params.get("query") || ""));
    const rows = matches.filter(row => params.getAll("stages").includes(row.stage));
    const offset = Number(params.get("offset"));
    return jsonResponse({ items: rows.slice(offset, offset + 50), total: rows.length, unfiltered_total: records.length, stage_counts: { applied: matches.length } });
  });
  await hooks.loadApplications();
  const loaded = hooks.state.applications;
  const requestCount = calls.length;
  hooks.setApplicationChannel("mail_only");
  assert.equal(calls.length, requestCount);
  assert.equal(hooks.state.applications, loaded);
  assert.equal(loaded.length, 122);
  assert.equal(document.getElementById("application-kanban").querySelectorAll("article").length, 61);
  assert.match(document.getElementById("application-channel-filter").textContent, /全部 122可官网复核 61仅邮件更新 61/);
  assert.match(document.getElementById("application-page-description").textContent, /仅邮件更新 · 共 61 条/);
  assert.match(document.getElementById("application-kanban").textContent, /没有官网进度链接，不参与官网复核，通过邮件更新/);
  hooks.openScheduleEditor();
  assert.equal(document.getElementById("schedule-application-id").children.length, 123);
  document.getElementById("application-search").value = "岗位121";
  await hooks.loadApplications();
  assert.equal(hooks.state.applicationBrowse.channel, "mail_only");
  assert.equal(document.getElementById("application-kanban").querySelectorAll("article").length, 1);
  hooks.setApplicationChannel("official_page");
  assert.equal(document.getElementById("application-kanban").querySelectorAll("article").length, 0);
  assert.equal(hooks.state.applications.length, 1);
  document.getElementById("application-search").value = "";
  await hooks.loadApplications();
  assert.equal(document.getElementById("application-kanban").querySelectorAll("article").length, 61);
  assert.equal(hooks.state.applications.length, 122);
});

test("application channel counts mark partial loads and never reuse unclassified server totals", async () => {
  let failMore = true;
  const { document, hooks } = loadApp(async url => {
    const params = new URL(url, "http://localhost").searchParams;
    const offset = Number(params.get("offset"));
    if (offset && failMore) throw new Error("synthetic second-page failure");
    const applied = params.getAll("stages").includes("applied");
    return jsonResponse({ items: applied ? Array.from({ length: offset ? 1 : 50 }, (_, index) => ({
      id: `mixed-${offset + index}`, stage: "applied", job_title: "岗位", record_url: index % 2 ? null : "https://example.test/progress",
    })) : [], total: applied ? 51 : 0, stage_counts: { applied: 51 }, unfiltered_total: 51 });
  });
  hooks.setApplicationChannel("mail_only");
  assert.equal(await hooks.loadApplications(), false);
  assert.match(document.getElementById("application-page-description").textContent, /已加载 25 条（加载未完成）/);
  assert.match(document.getElementById("application-channel-count-note").textContent, /已加载/);
  assert.equal(document.getElementById("application-summary").querySelectorAll("strong")[0].textContent, "25");
  assert.match(document.getElementById("application-summary").textContent, /已加载记录/);
  const retry = document.getElementById("application-kanban").querySelectorAll("button").find(button => button.dataset.applicationMore === "applied");
  assert.match(retry.textContent, /本列已加载 50 \/ 51/);
  failMore = false;
  assert.equal(await hooks.loadApplications({ moreColumn: "applied" }), true);
  assert.equal(hooks.state.applications.length, 51);
  assert.doesNotMatch(document.getElementById("application-page-description").textContent, /加载未完成/);
  assert.match(document.getElementById("application-channel-filter").textContent, /可官网复核 26仅邮件更新 25/);
});

test("mail-only review exclusions are presented as skipped in results and progress", () => {
  const { document, hooks } = loadApp(async () => jsonResponse([]));
  hooks.renderDailyProgress({ runs: [{ thread_id: "thread-1", task_kind: "application_review", status: "running", completed: 1, total: 1,
    summary: { excluded_mail_only: 3 } }] });
  assert.match(document.getElementById("assistant-task-progress-detail").textContent, /已跳过（仅邮件更新）3 条/);
  assert.doesNotMatch(document.getElementById("assistant-task-progress-detail").textContent, /失败|待确认|已挂/);
  const output = document.createElement("div");
  hooks.renderResultData(output, { summary: { excluded_mail_only: 3 }, items: [
    { application_id: "mail-1", state: "excluded", reason: "mail_only" },
  ] });
  assert.match(output.querySelector("p").textContent, /已跳过（仅邮件更新）3 条/);
  assert.match(output.querySelector("li").textContent, /已跳过（仅邮件更新）/);
  assert.doesNotMatch(output.querySelector("li").textContent, /失败|待确认|已挂/);
});

test("application refresh resets each column offset and preserves search", async () => {
  const calls = [];
  const { document, hooks } = loadApp(async url => {
    calls.push(url); return jsonResponse({ items: [], total: 0, unfiltered_total: 70, stage_counts: {} });
  });
  document.getElementById("application-search").value = "测试岗";
  hooks.state.applicationBrowse.columns.applied = { items: [{ id: "old" }], total: 120, offset: 100 };
  await hooks.loadApplications();
  assert.equal(calls.length, 5);
  assert.ok(calls.every(url => new URL(url, "http://localhost").searchParams.get("offset") === "0"));
  assert.equal(hooks.state.applicationBrowse.columns.applied.offset, 0);
  assert.equal(hooks.state.applications.length, 0);
  assert.equal(document.getElementById("application-search").value, "测试岗");
});

test("review results distinguish matching, access and model limitations without blaming missing URLs", () => {
  const { document, hooks } = loadApp(async () => jsonResponse([]));
  const output = document.createElement("div");
  hooks.renderResultData(output, { items: [
    { application_id: "a", company_name: "示例甲", job_title: "AI应用岗", state: "unresolved", reason: "target_record_not_matched" },
    { application_id: "b", state: "unresolved", reason: "frame_scope_denied" },
    { application_id: "c", state: "unresolved", reason: "model_timeout" },
    { application_id: "d", state: "unresolved", reason: "application_page_unavailable" },
  ] });
  const rows = output.querySelectorAll("li");
  assert.match(rows[0].textContent, /示例甲.*AI应用岗.*未找到与目标岗位一致/);
  assert.doesNotMatch(rows[0].textContent, /链接失效|超时/);
  assert.match(rows[1].textContent, /不可访问的页面框架/);
  assert.doesNotMatch(rows[1].textContent, /超时|网络/);
  assert.match(rows[2].textContent, /辅助判读超时，保留原阶段/);
  assert.doesNotMatch(rows[2].textContent, /官网故障|执行失败/);
  assert.match(rows[3].textContent, /官网投递记录页不可用/);
});

test("failed later application page retains rows and retry appends without duplication", async () => {
  let failMore = true;
  const calls = [];
  const { document, hooks } = loadApp(async url => {
    const params = new URL(url, "http://localhost").searchParams;
    calls.push(params);
    const offset = Number(params.get("offset"));
    if (failMore && offset) throw new Error("synthetic load-more failure");
    const applied = params.getAll("stages").includes("applied");
    return jsonResponse({ items: applied ? [{ id: offset ? "next-applied" : "keep-applied", stage: "applied", job_title: offset ? "后续岗位" : "保留岗位" }] : [],
      total: applied ? 2 : 0, stage_counts: { applied: 2 }, unfiltered_total: 2 });
  });
  assert.equal(await hooks.loadApplications(), false);
  assert.equal(await hooks.loadApplications({ moreColumn: "applied" }), false);
  assert.equal(hooks.state.applicationBrowse.columns.applied.offset, 1);
  assert.match(document.getElementById("application-kanban").textContent, /保留岗位/);
  assert.doesNotMatch(document.getElementById("application-kanban").textContent, /投递记录加载失败/);
  const retry = document.getElementById("application-kanban").querySelectorAll("button").find(button => button.dataset.applicationMore === "applied");
  assert.equal(retry.parentNode.className, "kanban-list");
  assert.equal(retry.disabled, false);
  failMore = false;
  assert.equal(await hooks.loadApplications({ moreColumn: "applied" }), true);
  assert.equal(hooks.state.applications.length, 2);
  assert.equal(hooks.state.applicationBrowse.columns.applied.offset, 2);
  assert.deepEqual(calls.slice(5).map(params => params.get("offset")), ["1", "1", "1"]);
});

test("application pagination loads more than 200 rows in each independent stage", async () => {
  const stages = ["applied", "written", "interview1", "offer", "rejected"];
  const stageCounts = Object.fromEntries(stages.map((stage, index) => [stage, 201 + index]));
  const calls = [];
  const { document, hooks } = loadApp(async url => {
    const params = new URL(url, "http://localhost").searchParams;
    calls.push(params);
    const stage = stages.find(stage => params.getAll("stages").includes(stage));
    const total = stageCounts[stage];
    const offset = Number(params.get("offset"));
    const length = Math.min(50, total - offset);
    return jsonResponse({ items: Array.from({ length }, (_, index) => ({ id: `${stage}-${offset + index}`, stage,
      company_name: "公司", job_title: `岗位${offset + index}` })), total, stage_counts: stageCounts, unfiltered_total: 1015 });
  });
  assert.equal(await hooks.loadApplications(), true);
  assert.equal(calls.length, 25);
  assert.ok(calls.every(params => params.get("limit") === "50"));
  for (const stage of stages) {
    assert.deepEqual(calls.filter(params => params.getAll("stages").includes(stage)).map(params => params.get("offset")), ["0", "50", "100", "150", "200"]);
    assert.equal(hooks.state.applications.filter(row => row.stage === stage).length, stageCounts[stage]);
  }
  assert.equal(hooks.state.applications.length, 1015);
  assert.match(document.getElementById("application-page-description").textContent, /共 1015 条 · 已显示 1015 条/);
});

test("one failed application column does not stop other columns or lose retry offsets", async () => {
  let failApplied = true;
  const { hooks } = loadApp(async url => {
    const params = new URL(url, "http://localhost").searchParams;
    const stage = params.getAll("stages").find(stage => ["applied", "written"].includes(stage));
    const total = stage === "applied" ? 120 : stage === "written" ? 80 : 0;
    const offset = Number(params.get("offset"));
    if (stage === "applied" && offset && failApplied) throw new Error("applied page failed");
    return jsonResponse({ items: Array.from({ length: Math.min(50, total - offset) }, (_, index) => ({
      id: `${stage}-${offset + index}`, stage, company_name: "公司", job_title: "岗位",
    })), total, stage_counts: { applied: 120, written: 80 }, unfiltered_total: 200 });
  });
  assert.equal(await hooks.loadApplications(), false);
  assert.equal(hooks.state.applicationBrowse.columns.applied.offset, 50);
  assert.equal(hooks.state.applicationBrowse.columns.written.offset, 80);
  assert.equal(hooks.state.applications.length, 130);
  failApplied = false;
  assert.equal(await hooks.loadApplications({ moreColumn: "applied" }), true);
  assert.equal(hooks.state.applicationBrowse.columns.applied.offset, 120);
  assert.equal(hooks.state.applicationBrowse.columns.written.items.length, 80);
  assert.equal(hooks.state.applications.length, 200);
});

for (const writtenCount of [0, 1]) {
test(`first-page column failure shows local retry with ${writtenCount} other visible records`, async () => {
  let failApplied = true;
  const { document, hooks } = loadApp(async url => {
    const params = new URL(url, "http://localhost").searchParams;
    const stage = params.getAll("stages").find(stage => ["applied", "written"].includes(stage));
    const offset = Number(params.get("offset"));
    if (stage === "applied" && failApplied) throw new Error("first page failed");
    const total = stage === "applied" ? 51 : stage === "written" ? writtenCount : 0;
    return jsonResponse({ items: Array.from({ length: Math.min(50, total - offset) }, (_, index) => ({
      id: `${stage}-${offset + index}`, stage, company_name: "公司", job_title: "岗位",
    })), total, stage_counts: { applied: 51, written: writtenCount }, unfiltered_total: 51 + writtenCount });
  });
  assert.equal(await hooks.loadApplications(), false);
  assert.equal(hooks.state.applicationBrowse.columns.applied.offset, 0);
  assert.equal(hooks.state.applicationBrowse.columns.applied.total, 51);
  assert.equal(hooks.state.applicationBrowse.columns.written.items.length, writtenCount);
  const retry = document.getElementById("application-kanban").querySelectorAll("button").find(button => button.dataset.applicationMore === "applied");
  assert.equal(retry.disabled, false);
  assert.match(retry.parentNode.textContent, /加载未完成，请重试/);
  failApplied = false;
  assert.equal(await hooks.loadApplications({ moreColumn: "applied" }), true);
  assert.equal(hooks.state.applications.length, 51 + writtenCount);
  assert.equal(hooks.state.applicationBrowse.columns.applied.offset, 51);
});
}

test("application pagination renders first wave and ignores stale secondary page after query change", async () => {
  let resolveSecondary;
  let secondaryStarted;
  const secondary = new Promise(resolve => { secondaryStarted = resolve; });
  const calls = [];
  const { document, hooks } = loadApp(async (url, options) => {
    const params = new URL(url, "http://localhost").searchParams;
    calls.push({ params, signal: options.signal });
    const applied = params.getAll("stages").includes("applied");
    if (params.get("query") === "old" && applied && params.get("offset") === "50") {
      secondaryStarted();
      return new Promise(resolve => { resolveSecondary = resolve; });
    }
    const old = params.get("query") === "old";
    return jsonResponse({ items: applied ? Array.from({ length: old ? 50 : 1 }, (_, index) => ({ id: `${old ? "old" : "new"}-${index}`, stage: "applied", job_title: old ? "旧岗位" : "新岗位" })) : [],
      total: applied ? old ? 51 : 1 : 0, stage_counts: { applied: old ? 51 : 1 }, unfiltered_total: 51 });
  });
  document.getElementById("application-search").value = "old";
  const oldLoad = hooks.loadApplications();
  await secondary;
  assert.equal(hooks.state.applications.length, 50);
  assert.match(document.getElementById("application-kanban").textContent, /旧岗位/);
  document.getElementById("application-search").value = "new";
  assert.equal(await hooks.loadApplications(), true);
  assert.equal(calls[5].signal.aborted, true);
  resolveSecondary(jsonResponse({ items: [{ id: "stale", stage: "applied", job_title: "不应显示" }], total: 51 }));
  assert.equal(await oldLoad, false);
  assert.equal(hooks.state.applications.length, 1);
  assert.equal(hooks.state.applications[0].id, "new-0");
  assert.doesNotMatch(document.getElementById("application-kanban").textContent, /旧岗位|不应显示/);
});

test("empty next application page stops automatic pagination and exposes retry", async () => {
  let calls = 0;
  const { document, hooks } = loadApp(async url => {
    ++calls;
    const params = new URL(url, "http://localhost").searchParams;
    const applied = params.getAll("stages").includes("applied");
    const first = params.get("offset") === "0";
    return jsonResponse({ items: applied && first ? [{ id: "keep", stage: "applied", job_title: "保留" }] : [],
      total: applied ? 2 : 0, stage_counts: { applied: 2 } });
  });
  assert.equal(await hooks.loadApplications(), false);
  assert.equal(calls, 6);
  assert.equal(hooks.state.applicationBrowse.columns.applied.offset, 1);
  assert.equal(hooks.state.applications.length, 1);
  assert.match(document.getElementById("application-kanban").textContent, /重试加载/);
});

test("warm job pages omit heavy summaries without clearing featured jobs", async () => {
  let requested;
  const { document, hooks } = loadApp(async url => {
    requested = new URL(url, "http://localhost");
    return jsonResponse({ items: [], total: 400, stats: null, facets: null, featured: [], summary_included: false });
  });
  hooks.state.jobSummaryMode = hooks.state.jobBrowse.mode;
  hooks.state.jobSummaryAt = Date.now();
  hooks.state.featuredJobs = [{ id: "keep-featured" }];
  await hooks.loadJobBrowser();
  assert.equal(requested.searchParams.get("include_summary"), "false");
  assert.equal(hooks.state.featuredJobs[0].id, "keep-featured");
  assert.match(document.getElementById("jobs-result-count").textContent, /400/);
});

test("mail semantic labels do not portray legacy zero confidence as a probability", () => {
  const { document, hooks } = loadApp(async () => jsonResponse([]));
  hooks.renderMails([
    mailFixture({ id: "one", confidence: 0, binding_state: "not_required", association_required: false, processing_label: "已处理" }),
    mailFixture({ id: "two", confidence: null, binding_state: "confirmed", application_id: "a" }),
  ]);
  const label = document.getElementById("mail-list").textContent;
  assert.doesNotMatch(label, /置信度|0%/);
  assert.match(label, /无需关联/);
  assert.match(label, /用户已确认/);
});

test("binding candidate dialog reads only and does not auto propose or bind", async () => {
  const calls = [];
  const { document, hooks } = loadApp(async url => {
    calls.push(url); return jsonResponse({ candidates: [{ application_id: "a", company_name: "示例科技", job_title: "测试工程师" }], content_digest: "a".repeat(64), binding_revision: 0 });
  });
  await hooks.openMailBinding("mail-1");
  assert.deepEqual(calls, ["/api/recruitment-mails/mail-1/binding-candidates?query=&offset=0"]);
  assert.match(document.getElementById("mail-binding-content").textContent, /示例科技 · 测试工程师/);
});

test("mail list displays every confirmed application but counts the source mail only once", () => {
  const { document, hooks } = loadApp(async () => jsonResponse([]));
  hooks.renderMails([mailFixture({ id: "shared", binding_state: "confirmed", application_id: null,
    application_ids: ["a", "b"], applications: [
      { application_id: "a", company_name: "示例科技", job_title: "软件开发" },
      { application_id: "b", company_name: "示例科技", job_title: "测试开发" },
    ] })]);
  assert.equal(document.getElementById("mail-linked-count").textContent, "1");
  const list = document.getElementById("mail-list");
  assert.match(list.textContent, /关联 2 条投递/);
  assert.match(list.textContent, /软件开发；测试开发/);
  assert.match(list.textContent, /修改关联/);
});

test("one shared mail schedule displays all roles without creating extra events", () => {
  const { document, hooks } = loadApp(async () => { throw new Error("render must remain read-only"); });
  const event = scheduleFixture({ application_id: null, application_ids: ["a", "b"], associated_jobs: [
    { application_id: "a", job_title: "软件开发" }, { application_id: "b", job_title: "测试开发" },
  ] });
  hooks.renderScheduleTodo([event]);
  const list = document.getElementById("schedule-todo-view");
  assert.equal(list.querySelectorAll("article").length, 1);
  assert.match(list.textContent, /关联 2 个岗位/);
  assert.match(list.textContent, /软件开发；测试开发/);
});

test("editing a shared mail schedule preserves approved identities and later manual editors unlock fields", async () => {
  const calls = [];
  const { document, hooks } = loadApp(async (url, options = {}) => {
    calls.push({ url, options }); return jsonResponse(url === "/api/schedule" ? [] : { status: "updated" });
  });
  const event = scheduleFixture({ source: "recruitment_mail_schedule", application_id: null,
    application_ids: ["a", "b"], job_title: "软件开发；测试开发" });
  hooks.state.allSchedules = [event];
  hooks.openScheduleEditor(event);
  assert.equal(document.getElementById("schedule-application-id").disabled, true);
  document.getElementById("schedule-note").value = "更新备注";
  await hooks.submitScheduleForm({ preventDefault() {} });
  const payload = JSON.parse(calls[0].options.body);
  assert.equal(payload.note, "更新备注");
  for (const field of ["application_id", "company_name", "job_title"]) assert.equal(Object.hasOwn(payload, field), false);
  hooks.openScheduleEditor();
  assert.equal(document.getElementById("schedule-application-id").disabled, false);
  assert.equal(document.getElementById("schedule-company-name").disabled, false);
});

test("human binding approval must succeed before exact proposal execution", async () => {
  const calls = [];
  const { hooks } = loadApp(async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("/approve")) return jsonResponse({ allowed: false, status: "expired" });
    throw new Error("must not execute after rejected approval");
  });
  await hooks.confirmMailBinding({ token_id: "token", status: "pending" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/approvals/token/approve");
});

test("unscoped mail approval stays a compact dialog entry in the approval center", () => {
  const { document, hooks } = loadApp(async () => jsonResponse([]));
  hooks.state.approvals = [{ token_id: "secret-token", status: "pending", operation: "recruitment_mail_binding",
    preview: { before: { subject: "笔试通知", sender: "hr@example.test" },
      after: { action: "bind", company_name: "示例科技", job_title: "研发工程师" } } }];
  hooks.renderApprovals();
  assert.equal(document.getElementById("assistant-mail-binding-approvals").textContent, "");
  const card = document.getElementById("approval-list");
  const label = card.textContent;
  assert.match(label, /笔试通知/); assert.match(label, /选择岗位并确认/);
  assert.equal(card.querySelectorAll("button").length, 1);
  assert.doesNotMatch(label, /确认关联|批准预览|拒绝/);
  assert.doesNotMatch(label, /secret-token/);
});

test("official identity dialog reads actual cards without proposing or binding", async () => {
  const calls = [];
  const { document, hooks } = loadApp(async url => {
    calls.push(url); return jsonResponse({ company_name: "示例企业", job_title: "AI开发", captured_at: "2026-09-28T00:00:00Z",
      candidates: [{ raw_title: "2027届-AI开发（方向A）", context: "当前状态：筛选", external_job_id: "J101", selectable: true }], binding_revision: 0 });
  });
  await hooks.openApplicationIdentityBinding("a");
  assert.deepEqual(calls, ["/api/applications/a/identity-candidates"]);
  assert.match(document.getElementById("job-detail-content").textContent, /2027届-AI开发（方向A）/);
  assert.match(document.getElementById("job-detail-content").textContent, /不会立即更改阶段/);
  assert.match(document.getElementById("job-detail-content").textContent, /页面读取于/);
  assert.match(document.getElementById("job-detail-content").textContent, /官网岗位编号：J101/);
});

test("official identity approvals never create chat confirmation controls", () => {
  const { document, hooks } = loadApp(async () => jsonResponse([]));
  hooks.state.approvals = [{ token_id: "internal-token", status: "pending", operation: "application_identity_binding",
    preview: { before: { company_name: "示例企业", job_title: "AI开发", page_url: "https://example.test/records" },
      after: { action: "bind", official_title: "AI开发（方向A）" } } }];
  hooks.renderMailBindingApprovals();
  const label = document.getElementById("assistant-mail-binding-approvals").textContent;
  assert.equal(label, "");
  assert.equal(document.getElementById("assistant-mail-binding-approvals").querySelectorAll("button").length, 0);
});

function officialIdentityCandidates(applicationId = "a") {
  return { company_name: "示例企业", job_title: `本地岗位 ${applicationId}`,
    identity_digest: "d".repeat(64), binding_revision: 2, operation_id: `observation-${applicationId}`,
    candidates: ["a", "b"].map(key => ({ candidate_id: key.repeat(64),
      raw_title: `官网岗位 ${key}`, context: "当前状态：筛选", selectable: true })) };
}

function officialIdentityProposal(candidate = "a", applicationId = "a") {
  return { success: true, data: { approval_id: `identity-${applicationId}-${candidate}`, approval_status: "pending",
    preview: { before: { company_name: "示例企业", job_title: `本地岗位 ${applicationId}`,
      page_url: "https://example.test/records" }, after: { action: "bind", official_title: `官网岗位 ${candidate}` } } } };
}

const settleIdentityUi = () => new Promise(resolve => setImmediate(resolve));

test("official identity selection clears old confirmation and ignores an older proposal response", async () => {
  const calls = [], pending = [];
  const { hooks, document } = loadApp(async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("identity-candidates")) return jsonResponse(officialIdentityCandidates());
    if (url.endsWith("identity-proposals")) return new Promise(resolve => pending.push(resolve));
    throw new Error(`Unexpected write before confirmation: ${url}`);
  });
  await hooks.openApplicationIdentityBinding("a");
  const body = document.getElementById("job-detail-content").children[0];
  const [chooseA, chooseB] = body.querySelectorAll("button");
  const preview = body.children.at(-1);
  chooseA.click();
  pending[0](jsonResponse(officialIdentityProposal("a")));
  await settleIdentityUi();
  assert.match(preview.textContent, /官网岗位：官网岗位 a/);
  chooseA.click();
  assert.equal(preview.childElementCount, 0, "old confirmation must disappear as soon as another choice starts");
  chooseB.click();
  pending[2](jsonResponse(officialIdentityProposal("b")));
  await settleIdentityUi();
  pending[1](jsonResponse(officialIdentityProposal("a")));
  await settleIdentityUi();
  assert.match(preview.textContent, /官网岗位：官网岗位 b/);
  assert.doesNotMatch(preview.textContent, /官网岗位：官网岗位 a/);
  assert.equal(chooseA.disabled, false); assert.equal(chooseB.disabled, false);
  assert.deepEqual(JSON.parse(calls[3].options.body), {
    application_id: "a", action: "bind", identity_digest: "d".repeat(64), binding_revision: 2,
    operation_id: "observation-a", candidate_id: "b".repeat(64) });
  assert.ok(calls.every(call => !/\/(approve|execute)$/.test(call.url)));
});

for (const replacement of [null, "another identity", "mail detail"]) {
  test(`official identity confirmation closes only its own current dialog: ${replacement || "unchanged"}`, async () => {
    const calls = [];
    let finishExecution;
    const { hooks, document } = loadApp(async (url, options) => {
      calls.push({ url, options });
      if (url.endsWith("identity-candidates")) return jsonResponse(officialIdentityCandidates(url.includes("/b/") ? "b" : "a"));
      if (url.endsWith("identity-proposals")) return jsonResponse(officialIdentityProposal());
      if (url.endsWith("/approve")) return jsonResponse({ allowed: true, status: "approved" });
      if (url.endsWith("/execute")) return new Promise(resolve => { finishExecution = resolve; });
      if (url === "/api/approvals") return jsonResponse([]);
      if (url.startsWith("/api/recruitment-mails/")) return jsonResponse({ subject: "后来打开的邮件", body_text: "合成邮件说明" });
      throw new Error(`Unexpected request: ${url}`);
    });
    await hooks.openApplicationIdentityBinding("a");
    const dialog = document.getElementById("job-detail-dialog");
    const content = document.getElementById("job-detail-content");
    const body = content.children[0];
    body.querySelectorAll("button")[0].click();
    await settleIdentityUi();
    body.children.at(-1).querySelectorAll("button")[0].click();
    await settleIdentityUi();
    assert.equal(typeof finishExecution, "function");
    if (replacement === "another identity") await hooks.openApplicationIdentityBinding("b");
    if (replacement === "mail detail") await hooks.openRecruitmentMail("synthetic-mail");
    const currentContent = content.textContent;
    finishExecution(jsonResponse({ success: true }));
    await settleIdentityUi();
    assert.equal(dialog.open, Boolean(replacement));
    assert.equal(content.textContent, currentContent);
    assert.match(document.getElementById("toast-region").textContent, /对应关系已保存/);
    const writes = calls.filter(call => /\/(approve|execute)$/.test(call.url));
    assert.deepEqual(writes.map(call => call.url), ["/api/approvals/identity-a-a/approve", "/api/approvals/identity-a-a/execute"]);
    assert.ok(writes.every(call => call.options.method === "POST"));
    assert.deepEqual(JSON.parse(writes[1].options.body), { operator: "local-ui-user" });
  });
}

test("official identity approval history keeps audit only and does not close other dialogs", async () => {
  const { hooks, document } = loadApp(async url => {
    if (url.endsWith("/approve")) return jsonResponse({ allowed: true, status: "approved" });
    if (url.endsWith("/execute")) return jsonResponse({ success: true });
    if (url === "/api/approvals") return jsonResponse([]);
    throw new Error(`Unexpected request: ${url}`);
  });
  const dialog = document.getElementById("job-detail-dialog"); dialog.showModal();
  document.getElementById("job-detail-content").textContent = "保留另外的详情";
  const proposal = officialIdentityProposal().data;
  hooks.state.approvals = [{ token_id: proposal.approval_id, status: "pending",
    operation: "application_identity_binding", preview: proposal.preview }];
  hooks.renderMailBindingApprovals();
  assert.equal(document.getElementById("assistant-mail-binding-approvals").querySelectorAll("button").length, 0);
  hooks.renderApprovals();
  assert.match(document.getElementById("approval-list").textContent, /历史确认仅作审计/);
  assert.doesNotMatch(document.getElementById("approval-list").textContent, /确认是同一岗位/);
  await settleIdentityUi();
  assert.equal(dialog.open, true);
  assert.equal(document.getElementById("job-detail-content").textContent, "保留另外的详情");
});

test("official identity write success remains success when approval refresh fails", async () => {
  let applied = 0;
  const { hooks, document } = loadApp(async url => {
    if (url.endsWith("/approve")) return jsonResponse({ allowed: true, status: "approved" });
    if (url.endsWith("/execute")) return jsonResponse({ success: true });
    if (url === "/api/approvals") throw new Error("synthetic refresh failure");
    throw new Error(`Unexpected request: ${url}`);
  });
  const proposal = officialIdentityProposal().data;
  const card = hooks.applicationIdentityCard({ token_id: proposal.approval_id, status: "pending", preview: proposal.preview },
    { onApplied: () => { applied += 1; } });
  const confirm = card.querySelectorAll("button")[0]; confirm.click();
  await settleIdentityUi();
  const toasts = document.getElementById("toast-region");
  assert.equal(applied, 1); assert.equal(confirm.disabled, true);
  assert.match(toasts.textContent, /对应关系已保存，但审批列表刷新失败/);
  assert.ok(toasts.children.some(item => item.className === "toast toast--success"));
  assert.ok(toasts.children.every(item => item.className !== "toast toast--error"));
  assert.doesNotMatch(toasts.textContent, /未保存|synthetic refresh failure/);
});

for (const failure of ["approval denied", "execution rejected"]) {
  test(`official identity confirmation reports ${failure} without applying or further writes`, async () => {
    const calls = [];
    let applied = false;
    const { hooks, document } = loadApp(async url => {
      calls.push(url);
      if (url.endsWith("/approve")) return jsonResponse(failure === "approval denied"
        ? { allowed: false, status: "expired" } : { allowed: true, status: "approved" });
      if (url.endsWith("/execute")) return jsonResponse({ success: false });
      throw new Error(`Unexpected request: ${url}`);
    });
    const proposal = officialIdentityProposal().data;
    const card = hooks.applicationIdentityCard({ token_id: proposal.approval_id, status: "pending", preview: proposal.preview },
      { onApplied: () => { applied = true; } });
    const confirm = card.querySelectorAll("button")[0]; confirm.click();
    await settleIdentityUi();
    assert.equal(applied, false); assert.equal(confirm.disabled, true);
    assert.equal(calls.length, failure === "approval denied" ? 1 : 2);
    if (failure === "approval denied") assert.ok(!calls.some(url => url.endsWith("/execute")));
    assert.match(document.getElementById("toast-region").textContent, failure === "approval denied" ? /预览已失效/ : /未保存/);
  });
}

test("review distinguishes record presence and stable unparsed page from unchanged and timeout", () => {
  const { hooks } = loadApp(async () => jsonResponse([]));
  assert.match(hooks.reviewReasonLabel("record_present_status_unknown"), /当前阶段不明确/);
  assert.match(hooks.reviewReasonLabel("unparsed_page"), /页面已加载/);
  assert.doesNotMatch(hooks.reviewReasonLabel("unparsed_page"), /超时/);
  assert.match(hooks.reviewReasonLabel("talent_pool_status_unmapped"), /人才库.*保留原阶段/);
  assert.match(hooks.reviewReasonLabel("position_recommendation_unmapped"), /其他岗位.*保留原阶段/);
});

test("review displays the original noncanonical website label as text only", () => {
  const { hooks, document } = loadApp(async () => jsonResponse([]));
  const output = document.createElement("div");
  hooks.renderResultData(output, { items: [{ application_id: "fixture", state: "unresolved",
    reason: "position_recommendation_unmapped", observed_label: "推荐到其他职位 <img src=x>",
    company_name: "示例公司", job_title: "示例工程师" }] });
  assert.match(output.textContent, /官网原文：推荐到其他职位 <img src=x>/);
  assert.equal(output.querySelectorAll("img").length, 0);
});

test("review evidence labels distinguish cached interpretation and failed screenshot", () => {
  const { hooks, document } = loadApp(async () => jsonResponse([]));
  const output = document.createElement("div");
  hooks.renderResultData(output, { items: [{ application_id: "fixture", state: "unresolved",
    reason: "model_uncertain", company_name: "示例公司", job_title: "示例岗位",
    model_disposition: "cache_hit", vision_disposition: "http_503" }] });
  assert.match(output.textContent, /复用已保存的模型判读/);
  assert.match(output.textContent, /截图判读未完成，未据此确认状态/);
  assert.doesNotMatch(output.textContent, /本轮已调用模型|状态未变化/);
});

test("review wave waits in current turn with settled and retry counts kept separate", () => {
  const { hooks, document } = loadApp(async () => jsonResponse([]));
  hooks.renderDailyProgress({ runs: [{ thread_id: "thread-1", task_kind: "application_review", status: "awaiting_continuation",
    phase: "application_review", completed: 14, total: 86, processed: 16, remaining: 72,
    retry_pending: 2, failed: 2, unit: "条记录" }] });
  assert.equal(document.getElementById("assistant-task-progress").hidden, false);
  assert.equal(document.getElementById("assistant-task-progress-state").textContent, "等待助理继续下一批");
  assert.match(document.getElementById("assistant-task-progress-detail").textContent, /14 \/ 86/);
  assert.match(document.getElementById("assistant-task-progress-detail").textContent, /待完成 72/);
  assert.match(document.getElementById("assistant-task-progress-detail").textContent, /含待重试 2/);
  hooks.renderDailyProgress({ runs: [{ task_kind: "application_review", status: "stopped" }] });
  assert.equal(document.getElementById("assistant-task-progress").hidden, true);
});

test("late task progress cannot resurrect an already finished card", async () => {
  let completeOld;
  let progressReads = 0;
  const { document, hooks } = loadApp(async url => {
    if (url === "/api/approvals") return jsonResponse([]);
    if (++progressReads === 1) return new Promise(resolve => { completeOld = resolve; });
    return jsonResponse({ runs: [], run: null });
  });
  const old = hooks.refreshDailyProgress();
  await hooks.refreshDailyProgress();
  completeOld(jsonResponse({ runs: [{ status: "running", task_kind: "recruitment_mail", phase: "analysis", completed: 1, total: 2 }] }));
  await old;
  assert.equal(document.getElementById("assistant-task-progress").hidden, true);
});

test("progress belongs to its declared thread and keeps simultaneous runs separate", () => {
  const { document, hooks } = loadApp(async () => jsonResponse({}));
  const runs = [
    { run_id: "other-crawl", thread_id: "thread-2", task_kind: "daily", mode: "full", status: "running", phase: "companies", completed: 999, total: 1000 },
    { run_id: "review", thread_id: "thread-1", task_kind: "application_review", status: "running", completed: 3, total: 20 },
    { run_id: "crawl", thread_id: "thread-1", task_kind: "daily", mode: "full", status: "running", phase: "companies", progress: { attempted_unique: 7, scope_total: 80 } },
    { run_id: "unowned", task_kind: "application_review", status: "running", completed: 123, total: 234 },
  ];
  hooks.renderDailyProgress({ runs });
  assert.equal(document.getElementById("assistant-task-progress").dataset.runId, "review");
  assert.match(document.getElementById("assistant-task-progress-detail").textContent, /3 \/ 20/);
  const extra = document.getElementById("assistant-more-task-progress").children;
  assert.equal(extra.length, 1); assert.equal(extra[0].dataset.runId, "crawl");
  assert.match(extra[0].textContent, /7 \/ 总计 80/);
  assert.doesNotMatch(document.body.textContent, /999|123|234/);
  hooks.state.codexThreadId = "thread-2";
  hooks.renderDailyProgress({ runs });
  assert.equal(document.getElementById("assistant-task-progress").dataset.runId, "other-crawl");
  assert.equal(document.getElementById("assistant-more-task-progress").children.length, 0);
});

test("a late progress response from the previous conversation cannot repaint the new one", async () => {
  let finish;
  const { document, hooks } = loadApp(async () => new Promise(resolve => { finish = resolve; }));
  const reading = hooks.refreshDailyProgress("thread-1");
  hooks.state.codexThreadId = "thread-2";
  hooks.renderDailyProgress({ runs: [{ run_id: "review-b", thread_id: "thread-2", task_kind: "application_review", status: "running", completed: 8, total: 50 }] });
  finish(jsonResponse({ runs: [{ run_id: "crawl-a", thread_id: "thread-1", task_kind: "daily", status: "running" }] }));
  await reading;
  assert.equal(document.getElementById("assistant-task-progress").dataset.runId, "review-b");
  assert.match(document.getElementById("assistant-task-progress-detail").textContent, /8 \/ 50/);
  assert.equal(Object.keys(hooks.state.dailyNotices).length, 0);
});

test("stream rejects foreign threads before latching a turn or consuming their receipts", async () => {
  const foreign = (kind, text) => frame(kind, { event_type: kind, thread_id: "thread-2", turn_id: "turn-foreign", text,
    payload: { tool_name: "daily_recruitment_sync", output: { run_id: "a".repeat(32) } } });
  const { hooks } = loadApp(async () => streamResponse([{ value: bytes([
    foreign("turn_started"), foreign("text_delta", "别的任务结果"), foreign("item_completed"), foreign("turn_completed"),
    turnEvent(), frame("text_delta", turnEventPayload("text_delta", "own-text", { text: "本次复核结果" })),
    frame("turn_completed", turnEventPayload("turn_completed", "own-end")),
  ].join("")), done: false }]));
  const task = await hooks.runCodexAssistantQuery("复核");
  assert.equal(task.answer, "本次复核结果"); assert.equal(task.turn_id, "turn-1");
  assert.equal(Object.keys(hooks.state.dailyNotices).length, 0);
  assert.ok(task.stages.every(stage => stage.thread_id === "thread-1"));
});

test("switching conversations allows separate live turns and old completion cannot clear the new controller", async () => {
  const pending = {};
  const { document, hooks } = loadApp(async url => {
    if (url.endsWith("/turns/stream")) {
      const thread = url.includes("thread-2") ? "thread-2" : "thread-1";
      return streamResponse([
        { value: bytes(frame("turn", { id: `turn-${thread}`, thread_id: thread })), done: false },
        () => new Promise(resolve => { pending[thread] = resolve; }),
      ]);
    }
    if (url === "/api/codex/threads/thread-2") return jsonResponse({ id: "thread-2", turns: [] });
    throw new Error(`Unexpected ${url}`);
  });
  const firstMessage = hooks.appendMessage("assistant", "", null, { streaming: true });
  const first = hooks.runCodexAssistantQuery("爬取", "", firstMessage.id);
  await new Promise(resolve => setImmediate(resolve));
  await hooks.loadConversation("thread-2");
  assert.equal(hooks.state.activeAssistantController, null);
  const secondMessage = hooks.appendMessage("assistant", "", null, { streaming: true });
  const second = hooks.runCodexAssistantQuery("复核", "", secondMessage.id);
  await new Promise(resolve => setImmediate(resolve));
  const secondController = hooks.state.activeAssistantController;
  pending["thread-1"]({ value: bytes([
    frame("text_delta", { event_type: "text_delta", thread_id: "thread-1", turn_id: "turn-thread-1", text: "爬取完成" }),
    frame("turn_completed", { event_type: "turn_completed", thread_id: "thread-1", turn_id: "turn-thread-1" }),
  ].join("")), done: false });
  const firstTask = await first;
  assert.equal(firstTask.turn_id, "turn-thread-1");
  assert.equal(hooks.state.activeAssistantController, secondController);
  assert.equal(hooks.state.codexTurnId, "turn-thread-2");
  assert.equal(document.getElementById("assistant-live-run").hidden, false);
  assert.doesNotMatch(document.getElementById("assistant-messages").textContent, /爬取完成/);
  pending["thread-2"]({ value: bytes([
    frame("text_delta", { event_type: "text_delta", thread_id: "thread-2", turn_id: "turn-thread-2", text: "复核完成" }),
    frame("turn_completed", { event_type: "turn_completed", thread_id: "thread-2", turn_id: "turn-thread-2" }),
  ].join("")), done: false });
  const secondTask = await second;
  assert.equal(secondTask.turn_id, "turn-thread-2");
  assert.equal(hooks.state.tasks.length, 1); assert.equal(hooks.state.tasks[0].thread_id, "thread-2");
  assert.match(document.getElementById("assistant-messages").textContent, /复核完成/);
  assert.equal(hooks.state.activeAssistantController, null);
});

test("scheduled direct conversations appear without changing selection or starting a model turn", async () => {
  const calls = [];
  const scheduled = { id: "scheduled-thread", preview: "定时爬取", updatedAt: "2026-10-06T10:00:00Z",
    turns: [], automation: { direct: true, task_id: "schedule-1", run_id: "a".repeat(32), status: "completed" },
    messages: [{ id: "start", role: "user", text: "定时爬取开始", task_id: "schedule-1" },
      { id: "end", role: "assistant", text: "已完成定时爬取，新增 8 个岗位。", task_id: "schedule-1", result: { run_id: "a".repeat(32) } }] };
  const { document, hooks } = loadApp(async url => {
    calls.push(url);
    if (url === "/api/codex/threads?limit=20") return jsonResponse({ data: [scheduled] });
    if (url === "/api/codex/threads/scheduled-thread") return jsonResponse(scheduled);
    if (url === "/api/local-ui/tasks/progress") return jsonResponse({ runs: [{ run_id: "a".repeat(32), thread_id: "scheduled-thread", task_kind: "daily", status: "running", automation: { direct: true } }] });
    if (url === "/api/approvals") return jsonResponse([]);
    throw new Error(`Unexpected ${url}`);
  });
  hooks.appendMessage("assistant", "当前会话保留");
  await hooks.refreshConversationList({ quiet: true });
  assert.equal(hooks.state.codexThreadId, "thread-1");
  assert.equal(hooks.state.messages[0].body, "当前会话保留");
  assert.ok(hooks.state.conversations.some(thread => thread.id === "scheduled-thread"));
  await hooks.loadConversation("scheduled-thread");
  assert.match(document.getElementById("assistant-messages").textContent, /新增 8 个岗位/);
  assert.equal(hooks.state.messages[1].task_id, "schedule-1");
  await hooks.refreshDailyProgress();
  assert.equal(Object.keys(hooks.state.dailyNotices).length, 0);
  assert.ok(calls.every(url => !/\/resume$|\/turns\/stream$/.test(url)));
});

test("direct history keeps persisted run messages and later ordinary conversation turns", () => {
  const { hooks } = loadApp(async () => jsonResponse({}));
  const messages = hooks.codexHistoryMessages({ id: "scheduled-thread", automation: { direct: true },
    messages: [
      { id: "start", role: "user", text: "定时任务开始", createdAt: 100, task_id: "schedule" },
      { id: "result", role: "assistant", text: "定时爬取完成", createdAt: 110, task_id: "schedule", result: { run_id: "crawl-run" } },
    ], turns: [{ id: "followup", createdAt: 120, items: [
      { id: "question", type: "userMessage", text: "解释结果" },
      { id: "answer", type: "agentMessage", text: "本轮新增八个岗位" },
    ] }] });
  assert.deepEqual(Array.from(messages, message => message.body), ["定时任务开始", "定时爬取完成", "解释结果", "本轮新增八个岗位"]);
  assert.equal(messages[1].result.run_id, "crawl-run");
  assert.equal(messages[1].task_id, "schedule");
});

test("quiet scheduled history refresh reads saved completion without starting another turn", async () => {
  let finished = false;
  const calls = [];
  const thread = () => ({ id: "scheduled-thread", preview: "定时爬取", updatedAt: finished ? 200 : 100,
    turns: [], automation: { direct: true }, messages: [{ id: "status", role: "assistant", createdAt: finished ? 200 : 100,
      text: finished ? "已完成定时爬取" : "定时任务正在运行" }] });
  const { hooks } = loadApp(async url => {
    calls.push(url);
    if (url === "/api/codex/threads?limit=20") return jsonResponse({ data: [thread()] });
    if (url === "/api/codex/threads/scheduled-thread") return jsonResponse(thread());
    throw new Error(`Unexpected ${url}`);
  });
  await hooks.loadConversation("scheduled-thread");
  assert.equal(hooks.state.messages[0].body, "定时任务正在运行");
  finished = true;
  await hooks.refreshConversationList({ quiet: true });
  assert.equal(hooks.state.codexThreadId, "scheduled-thread");
  assert.equal(hooks.state.messages[0].body, "已完成定时爬取");
  assert.ok(calls.every(url => !/\/resume$|\/turns\/stream$/.test(url)));
});

test("unowned background runs are never claimed by the current conversation", async () => {
  const runId = "b".repeat(32);
  const { hooks, document } = loadApp(async url => jsonResponse(url === "/api/approvals" ? [] : {
    runs: [{ run_id: runId, task_kind: "daily", status: "running", phase: "companies" }],
  }));
  await hooks.refreshDailyProgress("thread-1", new Set());
  assert.equal(hooks.state.dailyNotices[runId], undefined);
  assert.equal(document.getElementById("assistant-task-progress").hidden, true);
});

test("a terminal response for another run cannot complete the tracked crawl", async () => {
  const runId = "c".repeat(32);
  const { hooks } = loadApp(async url => {
    if (url === "/api/approvals") return jsonResponse([]);
    if (url.includes("?run_id=")) return jsonResponse({ run: { run_id: "d".repeat(32), thread_id: "thread-1", task_kind: "daily", status: "completed" } });
    if (url === "/api/local-ui/tasks/progress") return jsonResponse({ runs: [] });
    throw new Error(`Must not start reporting: ${url}`);
  });
  hooks.state.dailyNotices[runId] = { thread_id: "thread-1", status: "active" };
  await hooks.refreshDailyProgress();
  assert.equal(hooks.state.dailyNotices[runId].status, "active");
  assert.equal(hooks.state.messages.length, 0);
});

test("stored message cache cannot be relabeled as a different selected conversation", () => {
  for (const matches of [true, false]) {
    const storage = new Map([
      ["recruitops.assistant.codex_thread_id", "selected-thread"],
      ["recruitops.assistant.recent.v1", JSON.stringify({ codexThreadId: matches ? "selected-thread" : "old-thread",
        messages: [{ body: "cached content" }], tasks: [{ task_id: "cached-task" }] })],
    ]);
    const { hooks } = loadApp(async () => jsonResponse({}), { localStorage: {
      getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value),
    } });
    assert.equal(hooks.state.messages.length, matches ? 1 : 0);
    assert.equal(hooks.state.tasks.length, matches ? 1 : 0);
  }
});

test("a list request started before deletion cannot resurrect the deleted conversation", async () => {
  let finishList;
  const { hooks } = loadApp(async (url, options = {}) => {
    if (options.method === "DELETE") return jsonResponse({ status: "deleted", thread_id: "thread-1" });
    if (url === "/api/codex/threads?limit=20") return new Promise(resolve => { finishList = resolve; });
    if (url === "/api/codex/threads/thread-2") return jsonResponse({ id: "thread-2", turns: [] });
    throw new Error(`Unexpected ${url}`);
  });
  hooks.state.conversations = [{ id: "thread-1" }, { id: "thread-2" }];
  const refresh = hooks.refreshConversationList({ quiet: true });
  assert.equal(await hooks.deleteConversation("thread-1"), true);
  assert.equal(hooks.state.codexThreadId, "thread-2");
  finishList(jsonResponse({ data: [{ id: "thread-1" }, { id: "thread-2" }] }));
  await refresh;
  assert.deepEqual(Array.from(hooks.state.conversations, thread => thread.id), ["thread-2"]);
  assert.equal(await hooks.loadConversation("thread-1"), false);
});

test("finishing deletion after a user switches chats leaves the new selection intact", async () => {
  let finishDelete;
  const { hooks } = loadApp(async (url, options = {}) => {
    if (options.method === "DELETE") return new Promise(resolve => { finishDelete = resolve; });
    if (url === "/api/codex/threads/thread-2") return jsonResponse({ id: "thread-2", turns: [],
      automation: { direct: true }, messages: [{ id: "result", role: "assistant", text: "第二任务结果" }] });
    if (url === "/api/codex/threads?limit=20") return jsonResponse({ data: [{ id: "thread-2" }] });
    throw new Error(`Unexpected ${url}`);
  });
  hooks.state.conversations = [{ id: "thread-1" }, { id: "thread-2" }];
  const deletion = hooks.deleteConversation("thread-1");
  await hooks.loadConversation("thread-2");
  finishDelete(jsonResponse({ status: "deleted", thread_id: "thread-1" }));
  await deletion;
  assert.equal(hooks.state.codexThreadId, "thread-2");
  assert.equal(hooks.state.messages[0].body, "第二任务结果");
});

test("a history response requested before deletion cannot select the deleted thread", async () => {
  let finishRead;
  const { hooks } = loadApp(async (url, options = {}) => {
    if (options.method === "DELETE") return jsonResponse({ status: "deleted", thread_id: "thread-2" });
    if (url === "/api/codex/threads/thread-2") return new Promise(resolve => { finishRead = resolve; });
    if (url === "/api/codex/threads?limit=20") return jsonResponse({ data: [{ id: "thread-1" }] });
    throw new Error(`Unexpected ${url}`);
  });
  hooks.state.conversations = [{ id: "thread-1" }, { id: "thread-2" }];
  const reading = hooks.loadConversation("thread-2");
  await hooks.deleteConversation("thread-2");
  finishRead(jsonResponse({ id: "thread-2", turns: [] }));
  assert.equal(await reading, false);
  assert.equal(hooks.state.codexThreadId, "thread-1");
});

test("navigation entry and home redirects show precise non-login-expiry reasons", () => {
  const { hooks } = loadApp(async () => jsonResponse({}));
  assert.equal(hooks.reviewReasonLabel("application_record_entry_not_entered"), "官网应聘记录入口未能进入，请打开官网完成进入后重试");
  assert.equal(hooks.reviewReasonLabel("application_record_home_redirect"), "投递地址返回官网首页，未读取到应聘记录；不代表登录过期");
});

test("cancel needs confirmation and sends only the exact displayed run", async () => {
  const calls = [];
  const denied = loadApp(async url => { calls.push(url); return jsonResponse({}); }, { confirm: () => false });
  await denied.hooks.controlBackgroundTask({ run_id: "mail-fixture", task_kind: "recruitment_mail" }, "cancel");
  assert.equal(calls.length, 0);
  const { hooks } = loadApp(async (url, options) => {
    calls.push({ url, options });
    return jsonResponse(url === "/api/approvals" ? [] : url.endsWith("/progress") ? { runs: [] } : { status: "cancelling" });
  });
  await hooks.controlBackgroundTask({ run_id: "mail-fixture", task_kind: "recruitment_mail" }, "cancel");
  assert.equal(calls[0].url, "/api/local-ui/tasks/mail-fixture/control");
  assert.deepEqual(JSON.parse(calls[0].options.body), { task_kind: "recruitment_mail", action: "cancel" });
});

function retainedReviewSummary(overrides = {}) {
  return { scope_complete: true, retained_count: 1, unchanged_or_retained_count: 2,
    attention_required_count: 0, blocked: 0, failed: 0,
    identity_confirmation_items: [{ application_id: "a", company_name: "示例企业", job_title: "本地岗位 a",
      reason: "target_record_not_matched", operation_id: "observation-a" }], ...overrides };
}

test("retained review rows keep the saved stage neutral while preserving collapsed reasons", () => {
  const { hooks, document } = loadApp(async () => jsonResponse([]));
  const output = document.createElement("div");
  const reasons = ["record_present_status_unknown", "unparsed_page", "target_record_not_matched", "status_unmapped"];
  hooks.renderResultData(output, { items: reasons.map((reason, index) => ({ application_id: String(index), reason,
    state: "unresolved", presentation_state: "retained", saved_stage: index ? "interview1" : "applied" })) });
  const rows = output.querySelectorAll("li");
  assert.equal(rows[0].querySelector("span").textContent, "保留原阶段：已投递");
  assert.equal(rows[1].querySelector("span").textContent, "保留原阶段：一面");
  for (const row of rows) {
    assert.doesNotMatch(row.querySelector("span").textContent, /未确认|无法确认|状态未变化/);
    assert.equal(row.querySelector("details").open, false);
    assert.match(row.querySelector("details").textContent, /查看复核说明/);
  }
  assert.equal(hooks.reviewStateLabel({ state: "unresolved", presentation_state: "retained" }), "保留原阶段");
  assert.equal(hooks.reviewStateLabel({ state: "unresolved", reason: "model_timeout" }), "未确认，保留原阶段");
});

test("completed review ambiguity is neutral without masking real failures or other tool ambiguity", () => {
  const { hooks, document } = loadApp(async () => jsonResponse([]));
  const result = { task_type: "application_status_review", status: "safe_stop", error: "AMBIGUOUS_MATCH",
    tool_response: { tool_name: "batch_observe_application_status", status: "ambiguous", success: false,
      error_code: "AMBIGUOUS_MATCH", error_message: "范围处理完毕", data: retainedReviewSummary({ identity_confirmation_items: [],
        retained_by_stage: { interview1: 1, unknown: 1 } }) } };
  hooks.renderTaskResult(result, "review-output");
  const output = document.getElementById("review-output");
  assert.match(output.children[0].textContent, /复核已完成/);
  assert.doesNotMatch(output.textContent, /失败或停止原因/);
  assert.match(output.textContent, /状态未变化或保留原阶段 2 条/);
  assert.match(output.textContent, /保留阶段：一面 1 条、原阶段 1 条/);
  assert.doesNotMatch(output.textContent, /保留阶段：已投递/);
  result.tool_response.data.failed = 1;
  hooks.renderTaskResult(result, "review-output");
  assert.match(output.textContent, /失败或停止原因/);
  result.tool_response.data.failed = 0;
  result.tool_response.tool_name = "recruitment_mail_process";
  hooks.renderTaskResult(result, "review-output");
  assert.match(output.textContent, /失败或停止原因/);
});

function officialIdentityQueue(items = [{ application_id: "a", ...officialIdentityCandidates() }]) {
  return { items, total: items.length, read_only: true };
}

test("review messages show only a shared queue count without candidate reads or proposals", async () => {
  const calls = [];
  const { hooks, document } = loadApp(async (url, options) => {
    calls.push({ url, options });
    return jsonResponse(officialIdentityQueue());
  });
  const output = document.createElement("div"); document.body.appendChild(output);
  const summary = retainedReviewSummary(); summary.identity_confirmation_items.push(summary.identity_confirmation_items[0]);
  hooks.renderResultData(output, { summary });
  await settleIdentityUi();
  assert.deepEqual(calls.map(call => call.url), ["/api/applications/identity-queue"]);
  assert.match(output.textContent, /待核对 1 项/);
  assert.doesNotMatch(output.textContent, /官网岗位 a|选择并查看确认预览/);
  assert.equal(output.querySelectorAll("button").length, 1);
  assert.equal(document.getElementById("job-detail-dialog").open, false);
});

test("queue selection is read only until one confirmation click and synchronizes every message count", async () => {
  const calls = [];
  let saved = false;
  const { hooks, document } = loadApp(async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("identity-queue")) return jsonResponse(officialIdentityQueue(saved ? [] : undefined));
    if (url.endsWith("identity-proposals")) return jsonResponse(officialIdentityProposal());
    if (url.endsWith("/approve")) return jsonResponse({ allowed: true, status: "approved" });
    if (url.endsWith("/execute")) { saved = true; return jsonResponse({ success: true }); }
    if (url === "/api/approvals") return jsonResponse([]);
    throw new Error(`Unexpected write: ${url}`);
  });
  const first = document.createElement("div"), second = document.createElement("div"); document.body.append(first, second);
  hooks.renderResultData(first, retainedReviewSummary()); hooks.renderResultData(second, retainedReviewSummary());
  await settleIdentityUi();
  const queue = document.getElementById("application-identity-queue-list");
  const confirm = queue.querySelectorAll("button")[0];
  assert.equal(confirm.disabled, true);
  queue.querySelectorAll("input")[0].dispatchEvent({ type: "change" });
  assert.equal(calls.length, 1);
  confirm.click(); confirm.click(); await settleIdentityUi();
  assert.equal(calls.filter(call => call.url.endsWith("identity-proposals")).length, 1);
  assert.deepEqual(calls.filter(call => call.options?.method === "POST").map(call => call.url), [
    "/api/applications/a/identity-proposals", "/api/approvals/identity-a-a/approve", "/api/approvals/identity-a-a/execute"]);
  assert.match(first.textContent, /待核对 0 项/); assert.match(second.textContent, /待核对 0 项/);
  assert.equal(queue.querySelectorAll("input").length, 0);
  hooks.renderResultData(first, retainedReviewSummary()); await settleIdentityUi();
  assert.equal(queue.querySelectorAll("input").length, 0, "old history must not restore candidates");
});

for (const candidateState of ["no candidates", "ambiguous candidates"]) {
  test(`queue cannot confirm ${candidateState}`, async () => {
    const calls = [];
    const { hooks, document } = loadApp(async url => {
      calls.push(url);
      return jsonResponse(officialIdentityQueue([{ application_id: "a", ...officialIdentityCandidates(),
        candidates: candidateState === "no candidates" ? [] : officialIdentityCandidates().candidates.map(candidate => ({ ...candidate, selectable: false })) }]));
    });
    await hooks.loadApplicationIdentityQueue();
    const queue = document.getElementById("application-identity-queue-list");
    const confirm = queue.querySelectorAll("button").find(button => button.textContent === "确认对应");
    if (candidateState === "no candidates") {
      assert.equal(confirm, undefined);
      assert.ok(queue.querySelectorAll("button").some(button => button.textContent === "重新读取该岗位" && !button.disabled));
      assert.match(queue.textContent, /尚未提取到可选择的官网岗位/);
    } else {
      assert.equal(confirm.disabled, true); confirm.click();
    }
    assert.ok(queue.querySelectorAll("input").every(input => input.disabled));
    assert.equal(calls.length, 1);
  });
}

test("queue refresh invalidates detached confirmation controls and pending approval cannot execute", async () => {
  const calls = [];
  let finishApproval;
  const { hooks, document } = loadApp(async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("identity-queue")) return jsonResponse(officialIdentityQueue());
    if (url.endsWith("identity-proposals")) return jsonResponse(officialIdentityProposal());
    if (url.endsWith("/approve")) return new Promise(resolve => { finishApproval = resolve; });
    throw new Error(`Unexpected request: ${url}`);
  });
  await hooks.loadApplicationIdentityQueue();
  const queue = document.getElementById("application-identity-queue-list");
  queue.querySelectorAll("input")[0].dispatchEvent({ type: "change" });
  const confirm = queue.querySelectorAll("button")[0]; confirm.click(); await settleIdentityUi();
  await hooks.loadApplicationIdentityQueue({ force: true });
  confirm.click();
  finishApproval(jsonResponse({ allowed: true, status: "approved" })); await settleIdentityUi();
  assert.equal(calls.filter(call => call.url.endsWith("identity-proposals")).length, 1);
  assert.ok(calls.every(call => !call.url.endsWith("/execute")));
  assert.equal(confirm.disabled, true);
});

for (const action of ["identity-reread", "identity-proposals"]) {
  test(`closed application from ${action} refreshes the queue without approving or executing`, async () => {
    const calls = [];
    let closed = false;
    const { hooks, document } = loadApp(async (url, options) => {
      calls.push({ url, options });
      if (url.endsWith("identity-queue")) return jsonResponse(officialIdentityQueue(closed ? [] : undefined));
      if (url.endsWith(action)) {
        closed = true;
        return jsonResponse({ detail: { code: "application_closed", message: "该投递已淘汰或已撤回，无需核对" } }, 409);
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    await hooks.loadApplicationIdentityQueue();
    const queue = document.getElementById("application-identity-queue-list");
    assert.ok(queue.querySelectorAll("input").every(input => input.checked === false));
    const label = action === "identity-reread" ? "重新读取该岗位" : "确认对应";
    if (action === "identity-proposals") queue.querySelectorAll("input")[0].dispatchEvent({ type: "change" });
    const button = queue.querySelectorAll("button").find(item => item.textContent === label);
    button.click(); await settleIdentityUi();
    assert.ok(closed);
    assert.equal(queue.querySelectorAll("input").length, 0);
    assert.match(queue.textContent, /暂无待核对事项/);
    assert.equal(document.getElementById("application-identity-queue-button").textContent, "待核对 0 项");
    assert.equal(calls.filter(call => call.options?.method === "POST").length, 1);
    assert.ok(calls.every(call => !call.url.endsWith("/approve") && !call.url.endsWith("/execute")));
  });
}

test("expired proposal requires another explicit click before a new attempt", async () => {
  const calls = [];
  const { hooks, document } = loadApp(async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("identity-queue")) return jsonResponse(officialIdentityQueue());
    if (url.endsWith("identity-proposals")) return jsonResponse({ success: false, data: { retry_of: "old-token" } });
    throw new Error(`Unexpected request: ${url}`);
  });
  await hooks.loadApplicationIdentityQueue();
  const queue = document.getElementById("application-identity-queue-list");
  queue.querySelectorAll("input")[0].dispatchEvent({ type: "change" });
  const confirm = queue.querySelectorAll("button")[0]; confirm.click(); await settleIdentityUi();
  assert.equal(calls.filter(call => call.options?.method === "POST").length, 1);
  assert.equal(confirm.textContent, "重新确认对应");
  confirm.click(); await settleIdentityUi();
  assert.equal(JSON.parse(calls.at(-1).options.body).retry_of, "old-token");
  assert.ok(calls.every(call => !call.url.endsWith("/execute")));
});

for (const wrapper of ["output", "mcp result", "status single run", "control single run"]) {
  test(`completed review SSE ${wrapper} displays queue entry and persists no candidate authority`, async () => {
    const calls = [];
    const receipt = { data: retainedReviewSummary(), page_text: "do not persist page content" };
    if (wrapper.includes("single run")) receipt.data = { run: receipt.data, runs: [receipt.data], selection: "selected" };
    const payload = wrapper === "output" ? { tool_name: "batch_observe_application_status", output: JSON.stringify(receipt) }
      : { item: { type: "mcpToolCall", tool: wrapper === "control single run" ? "application_review_control" : "mcp__recruitops__application_review_status", status: "completed",
        result: { content: [{ type: "text", text: JSON.stringify(receipt) }] } } };
    const { hooks, document } = loadApp(async url => {
      calls.push(url);
      if (url.endsWith("identity-queue")) return jsonResponse(officialIdentityQueue());
      if (url.endsWith("/turns/stream")) return streamResponse([{ value: bytes([
        turnEvent(), frame("item_completed", turnEventPayload("item_completed", "review-result", { payload })),
        frame("text_delta", turnEventPayload("text_delta", "review-answer", { text: "复核完成。" })),
        frame("turn_completed", turnEventPayload("turn_completed", "review-end")),
      ].join("")), done: false }, completeRead()]);
      throw new Error(`Unexpected request: ${url}`);
    });
    const message = hooks.appendMessage("assistant", "", null, { streaming: true });
    const result = await hooks.runCodexAssistantQuery("复核投递", "", message.id); await settleIdentityUi();
    const article = document.getElementById("assistant-messages").querySelector("article");
    assert.match(article.textContent, /待核对 1 项/);
    assert.doesNotMatch(article.textContent, /官网岗位 a/);
    const choose = article.querySelectorAll("button")[0];
    for (let parent = choose.parentNode; parent; parent = parent.parentNode) assert.notEqual(parent.tagName, "DETAILS");
    assert.equal(calls.filter(url => url.includes("identity-candidates")).length, 0);
    assert.equal(result.review_summary.identity_confirmation_required_count, 1);
    assert.equal(result.review_summary.identity_confirmation_items, undefined);
    assert.doesNotMatch(JSON.stringify(result), /do not persist page content/);
    assert.ok(calls.every(url => !/proposals|approve|execute/.test(url)));
  });
}

test("review queue entries recover from completed receipts without restoring historical candidates", async () => {
  const calls = [];
  const summary = retainedReviewSummary();
  const receipt = { structuredContent: { data: { run: summary, runs: [summary], selection: "selected" } } };
  const { hooks, document } = loadApp(async url => { calls.push(url); return jsonResponse(officialIdentityQueue()); });
  const thread = { id: "thread-1", turns: [{ id: "turn-1", items: [
    { id: "tool", type: "mcpToolCall", tool: "application_review_control", status: "completed", result: receipt },
    { id: "answer", type: "agentMessage", text: "复核完成" },
  ] }] };
  hooks.state.messages = hooks.codexHistoryMessages(thread); hooks.renderConversation(); await settleIdentityUi();
  assert.match(document.getElementById("assistant-messages").textContent, /待核对 1 项/);
  assert.doesNotMatch(document.getElementById("assistant-messages").textContent, /官网岗位 a/);
  assert.equal(calls.length, 1);
  thread.turns[0].items[0].tool = "read_web_page";
  thread.turns[0].items[1].text = JSON.stringify(receipt);
  hooks.state.messages = hooks.codexHistoryMessages(thread); hooks.renderConversation(); await settleIdentityUi();
  assert.equal(calls.length, 1);
  assert.equal(document.getElementById("assistant-messages").querySelectorAll("button").length, 0);
  thread.turns[0].items[0].tool = "application_review_control";
  thread.turns[0].items[0].status = "inProgress";
  hooks.state.messages = hooks.codexHistoryMessages(thread); hooks.renderConversation(); await settleIdentityUi();
  assert.equal(document.getElementById("assistant-messages").querySelectorAll("button").length, 0);
});

test("SSE ignores review-shaped webpage text and unfinished calls without failing on malformed summary items", async () => {
  const receipt = JSON.stringify({ data: retainedReviewSummary() });
  const { hooks, document } = loadApp(async url => {
    if (url.endsWith("identity-queue")) return jsonResponse(officialIdentityQueue([]));
    assert.ok(url.endsWith("/turns/stream"), `Unexpected candidate read: ${url}`);
    return streamResponse([{ value: bytes([
      turnEvent(),
      frame("item_completed", turnEventPayload("item_completed", "webpage", {
        payload: { tool_name: "read_web_page", output: receipt } })),
      frame("item_started", turnEventPayload("item_started", "unfinished", {
        payload: { tool_name: "application_review_status", output: receipt } })),
      frame("item_completed", turnEventPayload("item_completed", "malformed", {
        payload: { tool_name: "application_review_status", output: JSON.stringify({ data: retainedReviewSummary({ identity_confirmation_items: {} }) }) } })),
      frame("text_delta", turnEventPayload("text_delta", "prose", { text: receipt })),
      frame("turn_completed", turnEventPayload("turn_completed", "done")),
    ].join("")), done: false }, completeRead()]);
  });
  const message = hooks.appendMessage("assistant", "", null, { streaming: true });
  await hooks.runCodexAssistantQuery("查看复核", "", message.id); await settleIdentityUi();
  assert.equal(document.getElementById("assistant-messages").querySelectorAll("button").length, 1);
  assert.match(document.getElementById("assistant-messages").textContent, /待核对 0 项/);
});

test("status history does not pick or merge ambiguous review runs", () => {
  const { hooks } = loadApp(async url => { throw new Error(`Unexpected request: ${url}`); });
  const first = retainedReviewSummary({ run_id: "first" });
  const second = retainedReviewSummary({ run_id: "second", identity_confirmation_items: [{
    application_id: "b", company_name: "另一企业", job_title: "另一个岗位", operation_id: "observation-b",
  }] });
  for (const data of [
    { run: null, runs: [first, second], selection: "ambiguous" },
    { run: first, runs: [first, second] },
    { run: first, runs: [second], selection: "selected" },
  ]) {
    const records = hooks.codexHistoryMessages({ id: "thread-1", turns: [{ id: "turn-1", items: [
      { id: "tool", type: "mcpToolCall", tool: "application_review_status", status: "completed", result: { structuredContent: { data } } },
      { id: "answer", type: "agentMessage", text: "存在多个复核任务" },
    ] }] });
    assert.equal(records[0].result, null);
  }
});

test("different historical waves share only the latest server queue and never their old candidates", async () => {
  const calls = [];
  const { hooks, document } = loadApp(async url => {
    calls.push(url);
    return jsonResponse(officialIdentityQueue([]));
  });
  const first = document.createElement("div"), second = document.createElement("div");
  document.body.append(first, second);
  const earlier = retainedReviewSummary();
  const later = retainedReviewSummary({ identity_confirmation_items: [{ ...earlier.identity_confirmation_items[0], operation_id: "observation-new" }] });
  hooks.renderResultData(first, earlier); await settleIdentityUi();
  hooks.renderResultData(second, later); await settleIdentityUi();
  while (first.firstChild) first.removeChild(first.firstChild);
  hooks.renderResultData(first, earlier); await settleIdentityUi();
  assert.ok(calls.every(url => url === "/api/applications/identity-queue"));
  assert.match(first.textContent, /待核对 0 项/); assert.match(second.textContent, /待核对 0 项/);
  assert.equal(document.getElementById("application-identity-queue-list").querySelectorAll("input").length, 0);
});

test("mail retry button is only offered for failed messages and starts no work on render", () => {
  const calls = [];
  const { hooks, document } = loadApp(async url => { calls.push(url); return jsonResponse({}); });
  hooks.renderMails(["failed", "failed_terminal", "processed_updated", "pending"].map((status, index) => ({
    id: `mail-${index}`, subject: `Synthetic ${index}`, processing_status: status,
  })));
  const buttons = document.getElementById("mail-list").querySelectorAll("button");
  assert.equal(buttons.filter(button => button.textContent === "重试分析").length, 2);
  assert.deepEqual(calls, []);
});

test("mail retry keeps one run, waits for its terminal result and never synchronizes the mailbox", async () => {
  const calls = [];
  const { hooks, document } = loadApp(async (url, options) => {
    calls.push({ url, options });
    if (url.endsWith("/retry")) return jsonResponse({ run_id: "synthetic-run", task_kind: "recruitment_mail", status: "accepted" });
    if (url.includes("/tasks/progress?")) return jsonResponse({ run: {
      run_id: "synthetic-run", task_kind: "recruitment_mail", status: "completed", completed: 1, total: 1, failed: 0,
    } });
    if (url.startsWith("/api/recruitment-mails?")) return jsonResponse({ items: [{ id: "failed-mail", subject: "Synthetic", processing_status: "processed" }] });
    if (url.startsWith("/api/applications/page?")) return jsonResponse({ items: [], total: 0, stage_counts: {} });
    if (url.startsWith("/api/schedule")) return jsonResponse([]);
    throw new Error(`Unexpected retry effect: ${url}`);
  });
  hooks.renderMails([{ id: "failed-mail", subject: "Synthetic", processing_status: "failed_terminal" }]);
  const retry = document.getElementById("mail-list").querySelectorAll("button").find(button => button.textContent === "重试分析");
  retry.click(); retry.click(); await settleIdentityUi();
  assert.deepEqual(calls.filter(call => call.options?.method === "POST").map(call => call.url), ["/api/recruitment-mails/failed-mail/retry"]);
  assert.ok(calls.some(call => call.url === "/api/local-ui/tasks/progress?run_id=synthetic-run"));
  assert.ok(calls.every(call => !call.url.includes("/sync")));
  assert.equal(document.getElementById("mail-list").querySelectorAll("button").filter(button => button.textContent === "重试分析").length, 0);
});
