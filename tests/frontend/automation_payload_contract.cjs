"use strict";

// Consume actual FastAPI payloads produced by the temporary-storage integration
// test. Reuse only the workbench DOM harness, never its test registrations.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const harnessPath = path.join(__dirname, "workbench_stream.test.js");
const harness = fs.readFileSync(harnessPath, "utf8").split('\ntest("')[0];
const { loadApp, jsonResponse } = new Function("require", "__dirname",
  harness + "; return { loadApp, jsonResponse };")(require, __dirname);

async function main() {
  const payload = JSON.parse(fs.readFileSync(0, "utf8"));
  let phase = "running";
  const calls = [];
  const { hooks, document } = loadApp(async (url, options = {}) => {
    calls.push({ url, method: options.method || "GET" });
    if (url === "/api/approvals") return jsonResponse([]);
    if (url.startsWith("/api/local-ui/mail-confirmations")) return jsonResponse({ runs: [] });
    if (url === "/api/local-ui/tasks/progress") return jsonResponse(payload[phase].progress);
    if (url === "/api/codex/threads?limit=20") return jsonResponse(payload[phase].list);
    if (url === `/api/codex/threads/${payload.direct_thread}`) {
      return jsonResponse(options.method === "DELETE" ? payload.deleted.receipt : payload[phase].direct);
    }
    if (url === "/api/codex/threads/review-chat") return jsonResponse(payload.review_history);
    if (url === "/api/codex/threads/review-chat/resume") return jsonResponse({ id: "review-chat" });
    if (url === `/api/codex/threads/${payload.followup_thread}`) return jsonResponse(payload.followup_history);
    if (url === `/api/codex/threads/${payload.followup_thread}/resume`) return jsonResponse({ id: payload.followup_thread });
    throw new Error(`Unexpected integration request: ${url}`);
  });
  hooks.state.codexThreadId = "review-chat";
  hooks.state.messages = [{ id: "review-message", role: "assistant", body: "复核会话保留" }];
  await hooks.refreshConversationList({ quiet: true });
  assert.equal(hooks.state.codexThreadId, "review-chat");
  assert.equal(hooks.state.messages[0].body, "复核会话保留");
  await hooks.refreshDailyProgress();
  assert.equal(document.getElementById("assistant-task-progress").dataset.runId, payload.review_run);
  await hooks.loadConversation(payload.direct_thread);
  await hooks.refreshDailyProgress();
  assert.equal(document.getElementById("assistant-task-progress").dataset.runId, payload.direct_run);
  assert.equal(document.getElementById("assistant-task-progress").dataset.threadId, payload.direct_thread);
  assert.equal(Object.keys(hooks.state.dailyNotices).length, 0);
  assert.equal(hooks.state.messages.length, 2);

  phase = "completed";
  await hooks.refreshDailyProgress();
  assert.equal(document.getElementById("assistant-task-progress").hidden, true);
  await hooks.refreshConversationList({ quiet: true });
  assert.equal(hooks.state.messages.length, 3);
  const completed = hooks.state.messages.at(-1);
  assert.equal(completed.task_id, payload.direct_run);
  assert.equal(completed.result.run_id, payload.direct_run);
  assert.equal(completed.result.thread_id, payload.direct_thread);
  assert.equal(completed.result.task_id, "daily_recruitment_intelligence");

  phase = "followup";
  await hooks.refreshConversationList({ quiet: true });
  assert.equal(hooks.state.messages.length, 3, "the saved task report remains in its local conversation");
  await hooks.loadConversation(payload.followup_thread);
  assert.match(hooks.state.messages.at(-1).body, /追问回答/);
  assert.equal(hooks.state.messages.length, 2);
  const reads = calls.filter(call => call.url === `/api/codex/threads/${payload.direct_thread}`).length;
  await hooks.refreshConversationList({ quiet: true });
  assert.equal(calls.filter(call => call.url === `/api/codex/threads/${payload.direct_thread}`).length, reads,
    "list and read timestamps converge after the follow-up");

  await hooks.loadConversation(payload.direct_thread);
  phase = "deleted";
  await hooks.deleteConversation(payload.direct_thread);
  assert.equal(hooks.state.codexThreadId, payload.followup_thread);
  assert.ok(!hooks.state.conversations.some(thread => thread.id === payload.direct_thread));
  // Even a cached response from before the real backend delete cannot revive it.
  phase = "followup";
  await hooks.refreshConversationList({ quiet: true });
  assert.ok(!hooks.state.conversations.some(thread => thread.id === payload.direct_thread));
  assert.equal(await hooks.loadConversation(payload.direct_thread), false);
  assert.ok(!calls.some(call => /\/turns(?:\/stream)?$/.test(call.url)));
  assert.ok(!calls.some(call => call.url === `/api/codex/threads/${payload.direct_thread}/resume`));
  console.log(JSON.stringify({ states: ["running", "completed", "followup", "deleted"],
    messages_after_followup: 2, isolated_progress: true, model_turns_started: 0 }));
}

main().catch(error => { console.error(error); process.exitCode = 1; });
