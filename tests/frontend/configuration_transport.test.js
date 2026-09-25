"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const source = fs.readFileSync(path.resolve(__dirname, "../../apps/web/configuration.js"), "utf8");
const start = source.indexOf("  async function post(");
const end = source.indexOf("  function message(", start);
function postWith(response, budgets) {
  const context = vm.createContext({
    fetch: async () => response,
    AbortSignal: {timeout: value => { budgets.push(value); return undefined; }},
  });
  return vm.runInContext(source.slice(start, end) + "\npost", context);
}
test("non-JSON server errors surface HTTP status without leaking raw response", async () => {
  const post = postWith({ok: false, status: 500, text: async () => "Internal Server Error: private"}, []);
  await assert.rejects(post("resume/parse"), error =>
    error.message.includes("HTTP 500") && !error.message.includes("Unexpected token") &&
    !error.message.includes("private"));
});
test("resume and model checks allow bounded server retries to finish", async () => {
  const budgets = [];
  const post = postWith({ok: true, status: 200, text: async () => '{"saved":true}'}, budgets);
  assert.equal((await post("resume/parse")).saved, true);
  await post("model/test");
  await post("save");
  assert.deepEqual(budgets, [105000, 50000, 30000]);
});
test("configuration offers only official DeepSeek", () => {
  assert.ok(!source.includes("openai-compatible"));
  assert.ok(source.includes('base.control.readOnly = true'));
  assert.ok(source.includes('["deepseek-flash", "deepseek-v4-pro"]'));
});
