"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

// Isolate transport/recovery state; unrelated heavy workbench renderers are spies.
const source = fs.readFileSync(path.resolve(__dirname, "../../apps/web/app.js"), "utf8").replace(
  "  const testHooks = {", `
  renderConversation = () => { rendered.push(state.messages.map(item => item.body)); };
  renderConversationList = renderAssistantContext = renderTaskHistory = resetCodexEventStatuses = () => {};
  persistConversation = () => { persisted.push(state.messages.map(item => item.body)); };
  setAssistantStatus = (message) => { statuses.push(message); };
  const testHooks = {`,
);
class Node {
  constructor() { this.children = []; this.dataset = {}; this.textContent = ""; this.listeners = {}; }
  appendChild(node) { node.parent = this; this.children.push(node); return node; }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  setAttribute() {}
  addEventListener(name, listener) { this.listeners[name] = listener; }
}
function response(payload, status = 200) { return { ok: status < 400, status, json: async () => payload }; }
function setup(fetch) {
  const nodes = new Map();
  const context = {
    __RECRUITOPS_TEST_MODE__: true, fetch, console, Date, URL, URLSearchParams, AbortController,
    TextEncoder, TextDecoder, Uint8Array, rendered: [], persisted: [], statuses: [],
    window: {}, document: {
      getElementById(id) { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); },
      createElement() { return new Node(); },
    },
  };
  vm.runInNewContext(source, context);
  const hooks = context.__RECRUITOPS_TEST_HOOKS__;
  Object.assign(hooks.state, {
    codexEnabled: true, codexReady: true, codexThreadId: "old",
    messages: [{ body: "cached original", role: "assistant" }],
  });
  return { hooks, context, nodes };
}
function history(id) { return { id, turns: [{ id: "turn", items: [{ type: "agentMessage", text: "server history" }] }] }; }

test("renders read history before resume finishes and preserves it on resume failure", async () => {
  let finishResume;
  const calls = [];
  const { hooks, context } = setup(async url => {
    calls.push(url);
    return url.endsWith("/resume") ? new Promise(resolve => { finishResume = resolve; }) : response(history("next"));
  });
  const loading = hooks.loadConversation("next");
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(Array.from(context.rendered.at(-1)), ["server history"]);
  finishResume(response({ detail: { message: "runtime unavailable", code: "history_runtime_unavailable", request_id: "safe-id" } }, 503));
  assert.equal(await loading, true);
  assert.equal(hooks.state.messages[0].body, "server history");
  assert.equal(hooks.state.codexHistoryFailure.phase, "resume");
  assert.equal(hooks.state.codexResumePendingThreadId, "next");
  assert.equal(calls.some(url => url.includes("turns")), false);
});

test("retry restores runtime only, without replaying history business actions", async () => {
  const calls = [];
  const { hooks } = setup(async url => { calls.push(url); return response({ id: "old" }); });
  hooks.state.codexResumePendingThreadId = "old";
  assert.equal(await hooks.resumeConversationRuntime("old"), true);
  assert.deepEqual(calls, ["/api/codex/threads/old/resume"]);
  assert.equal(hooks.state.messages[0].body, "cached original");
  assert.equal(hooks.state.codexResumePendingThreadId, "");
});

test("read failure leaves currently visible messages and selected thread intact", async () => {
  const { hooks, context } = setup(async () => response({ detail: { message: "missing", code: "history_not_found" } }, 404));
  assert.equal(await hooks.loadConversation("missing"), false);
  assert.equal(hooks.state.codexThreadId, "old");
  assert.equal(hooks.state.messages[0].body, "cached original");
  assert.equal(hooks.state.codexHistoryFailure.code, "history_not_found");
  assert.equal(context.persisted.length, 0);
});

test("missing turns or temporarily unmaterialized history is not treated as an empty old chat", async () => {
  for (const payload of [{ id: "old" }, { id: "old", turns: [] }, { id: "old", turns: [], history_status: "not_materialized" }]) {
    const { hooks } = setup(async () => response(payload));
    assert.equal(await hooks.loadConversation("old"), false);
    assert.equal(hooks.state.messages[0].body, "cached original");
  }
});

test("stale late read cannot overwrite a newer selected conversation", async () => {
  let finish;
  const { hooks } = setup(async url => {
    if (url === "/api/codex/threads/late") return new Promise(resolve => { finish = resolve; });
    return response(url.endsWith("/resume") ? { id: "new" } : history("new"));
  });
  const oldLoad = hooks.loadConversation("late");
  assert.equal(await hooks.loadConversation("new"), true);
  finish(response(history("late")));
  assert.equal(await oldLoad, false);
  assert.equal(hooks.state.codexThreadId, "new");
});

test("failed resume prevents a newly requested model turn from starting", async () => {
  const calls = [];
  const { hooks } = setup(async url => {
    calls.push(url); return response({ detail: { message: "unavailable", code: "history_runtime_unavailable" } }, 503);
  });
  hooks.state.codexResumePendingThreadId = "old";
  await assert.rejects(hooks.runCodexAssistantQuery("new explicit request"), /unavailable/);
  assert.deepEqual(calls, ["/api/codex/threads/old/resume"]);
});
