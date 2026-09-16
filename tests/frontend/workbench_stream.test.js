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
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      this.dataset[key] = stringValue;
    }
  }

  removeAttribute(name) {
    delete this.attributes[name];
    if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      delete this.dataset[key];
    }
  }

  addEventListener() {}

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  querySelectorAll(selector) {
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
    AbortController,
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

function turnEvent() {
  return frame("turn", { id: "turn-1", thread_id: "thread-1" });
}

function turnEventPayload(eventType, eventId, extra = {}) {
  return {
    event_type: eventType,
    event_id: eventId,
    thread_id: "thread-1",
    turn_id: "turn-1",
    ...extra,
  };
}

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
  document.getElementById("assistant-knowledge-document").value = "a".repeat(32);
  document.getElementById("assistant-knowledge-enabled").checked = false;
  const task = await hooks.submitAssistantQuestion(input.value, jobId.value);

  assert.equal(task.status, "succeeded");
  const streamCall = calls.find((call) => /\/turns\/stream$/.test(call.url));
  assert.ok(streamCall);
  assert.deepEqual(JSON.parse(streamCall.options.body), {
    text: "查看今日岗位", job_id: "job-1", knowledge_enabled: false, knowledge_document_id: null,
  });
  assert.equal(input.value, "");
  assert.equal(jobId.value, "job-1");
  assert.equal(calls.filter(call => call.url === "/api/schedule").length, 1);
  assert.ok(calls.some(call => call.url.startsWith("/api/applications")));
  assert.ok(calls.some(call => call.url.startsWith("/api/recruitment-mails")));
});

test("deletes a conversation and reloads the server-synced next conversation", async () => {
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
  }, { confirm: () => true });

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

test("renders both markdown and plain personal knowledge citations as links", () => {
  const { hooks } = loadApp(async () => { throw new Error("no fetch"); });
  const root = new FakeElement("div");
  const url = `/?knowledge=${"a".repeat(32)}&page=2&revision=${"b".repeat(32)}`;
  hooks.renderMarkdown(root, `[原文第2页](${url})\n\n来源：${url}`);
  const links = root.querySelectorAll("a");
  assert.equal(links.length, 2);
  assert.equal(links[0].href, `http://localhost${url}`);
  assert.equal(links[1].textContent, "查看资料原文");
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
    "/api/recruitment-mails?limit=50",
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
  });
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
    assert.equal(url, "/api/recruitment-mails/mail-1");
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
