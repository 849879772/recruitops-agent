"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

// Reuse the workbench's fake DOM and transport fixtures, without registering its tests.
const fixture = fs.readFileSync(path.join(__dirname, "workbench_stream.test.js"), "utf8").split("\ntest(")[0];
const { loadApp, frame, bytes, streamResponse, completeRead, jsonResponse } = new Function(
  "require", "__dirname", fixture + "\nreturn {loadApp, frame, bytes, streamResponse, completeRead, jsonResponse};",
)(require, __dirname);

function completedStream(source = "local-task", headers = true) {
  const response = streamResponse([{ value: bytes(
    frame("turn", { id: "turn-1", thread_id: "runtime-followup", source_thread_id: source }) +
    frame("text_delta", { event_type: "text_delta", thread_id: "runtime-followup", turn_id: "turn-1", text: "新增两条。" }) +
    frame("turn_completed", { event_type: "turn_completed", thread_id: "runtime-followup", turn_id: "turn-1" }),
  ), done: false }, completeRead()]);
  if (headers) response.headers = { get: name => name === "X-RecruitOps-Thread-ID" ? "runtime-followup" : null };
  return response;
}

for (const headers of [true, false]) {
  test(`local report followup follows real runtime stream with ${headers ? "header" : "turn receipt"}`, async () => {
    const calls = [];
    const { hooks } = loadApp(async (url, options) => {
      calls.push({ url, options });
      return completedStream("local-task", headers);
    });
    hooks.state.codexThreadId = "local-task";
    const task = await hooks.runCodexAssistantQuery("解释结果");
    assert.equal(task.answer, "新增两条。");
    assert.equal(task.source_thread_id, "local-task");
    assert.equal(task.thread_id, "runtime-followup");
    assert.equal(hooks.state.codexThreadId, "runtime-followup");
    assert.equal(hooks.state.activeAssistantController, null);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, "/api/codex/threads/local-task/turns/stream");
  });
}

test("local report can list and open while model runtime is unavailable", async () => {
  const calls = [];
  const history = { id: "local-task", turns: [], automation: { local_only: true, direct: false },
    messages: [{ id: "failed", role: "assistant", text: "本轮启动失败。" }] };
  const { hooks } = loadApp(async url => {
    calls.push(url);
    return jsonResponse(url === "/api/codex/threads?limit=20" ? { data: [history] } : history);
  });
  hooks.state.codexReady = false;
  assert.equal(await hooks.refreshConversationList(), true);
  assert.equal(await hooks.loadConversation("local-task"), true);
  assert.equal(hooks.state.messages[0].body, "本轮启动失败。");
  assert.deepEqual(calls, ["/api/codex/threads?limit=20", "/api/codex/threads/local-task"]);
});

test("followup startup fault displays its actionable message and retains local selection", async () => {
  const { hooks } = loadApp(async () => jsonResponse({ detail: {
    code: "history_runtime_unavailable", message: "助理连接暂未就绪，请稍后重试提问；定时任务报告已保留。",
  } }, 503));
  hooks.state.codexThreadId = "local-task";
  await assert.rejects(hooks.runCodexAssistantQuery("解释结果"), /助理连接暂未就绪/);
  assert.equal(hooks.state.codexThreadId, "local-task");
  assert.equal(hooks.state.activeAssistantController, null);
});

test("unconfirmed followup start keeps report and never resends the user turn", async () => {
  const calls = [];
  const { hooks } = loadApp(async (url, options) => {
    calls.push({ url, method: options?.method });
    if (url.endsWith("/turns/stream")) return jsonResponse({ detail: {
      code: "followup_start_unconfirmed", message: "助理未确认本轮是否启动，请先查看会话运行状态；定时任务报告已保留。",
    } }, 504);
    return jsonResponse([]);
  });
  hooks.state.codexThreadId = "local-task";
  hooks.state.messages = [{ id: "report", role: "assistant", body: "已保存的抓取结果", created_at: new Date().toISOString() }];
  assert.equal(await hooks.submitAssistantQuestion("解释結果"), null);
  assert.equal(hooks.state.messages[0].body, "已保存的抓取结果");
  assert.match(hooks.state.messages.at(-1).body, /先查看会话运行状态/);
  assert.equal(calls.filter(call => call.url.endsWith("/turns/stream")).length, 1);
  assert.equal(calls.some(call => call.url.endsWith("/events")), false);
  assert.equal(hooks.state.codexThreadId, "local-task");
  assert.equal(hooks.state.activeAssistantController, null);
});

test("followup migration cannot switch a different selected conversation", async () => {
  let release;
  const { hooks } = loadApp(() => new Promise(resolve => { release = resolve; }));
  hooks.state.codexThreadId = "local-task";
  const pending = hooks.runCodexAssistantQuery("解释结果");
  hooks.state.codexThreadId = "another-chat";
  release(completedStream());
  const task = await pending;
  assert.equal(task.thread_id, "runtime-followup");
  assert.equal(hooks.state.codexThreadId, "another-chat");
  assert.equal(hooks.state.tasks.length, 0);
});

test("send completion clears migrated streaming message and current run", async () => {
  const { hooks } = loadApp(async url => url.endsWith("/turns/stream") ? completedStream() : jsonResponse([]));
  hooks.state.codexThreadId = "local-task";
  const task = await hooks.submitAssistantQuestion("解释结果");
  assert.equal(task.thread_id, "runtime-followup");
  assert.equal(hooks.state.messages.at(-1).body, "新增两条。");
  assert.equal(hooks.state.messages.at(-1).streaming, false);
  assert.equal(hooks.state.activeAssistantController, null);
});

test("deleting last local report offline does not attempt to create another model chat", async () => {
  const calls = [];
  const { hooks } = loadApp(async (url, options) => {
    calls.push({ url, method: options?.method });
    return jsonResponse(url.includes("limit=20") ? { data: [] } : { status: "deleted" });
  });
  hooks.state.codexReady = false;
  hooks.state.codexThreadId = "local-task";
  hooks.state.conversations = [{ id: "local-task", automation: { local_only: true } }];
  assert.equal(await hooks.deleteConversation("local-task"), true);
  assert.equal(hooks.state.codexThreadId, "");
  assert.equal(calls.some(call => call.method === "POST"), false);
});
