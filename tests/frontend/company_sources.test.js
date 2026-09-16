"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  MAX_POLL_ATTEMPTS,
  localTime,
  normalizeDetail,
  pollRecord,
  requestJson,
  resultTone,
  safeHttpUrl,
  statusLabel,
  urlDisplay,
} = require("../../apps/web/company-sources.js");

test("来源时间转为本地短格式并保留未知语义", () => {
  assert.equal(localTime(null), "尚无记录");
  assert.equal(localTime("bad-date"), "时间未知");
  assert.equal(localTime("2026-09-08T10:00:00Z"), new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date("2026-09-08T10:00:00Z")));
});

test("status文案覆盖抓取状态且未知值不伪装成完成", () => {
  assert.equal(statusLabel("complete"), "已完成");
  assert.equal(statusLabel("partial"), "部分完成");
  assert.equal(statusLabel("failed"), "抓取失败");
  assert.equal(statusLabel("pending"), "待抓取");
  assert.equal(statusLabel("unusable"), "unusable");
  assert.equal(statusLabel("unknown-status"), "unknown-status");
  assert.equal(resultTone("complete"), "success");
  assert.equal(resultTone("partial"), "warn");
  assert.equal(resultTone("failed"), "error");
});

test("URL只允许绝对http(s)，拒绝凭据和敏感签名且不改写合法入口", () => {
  assert.equal(safeHttpUrl("javascript:alert(1)"), null);
  assert.equal(safeHttpUrl("abc"), null);
  assert.equal(safeHttpUrl("https://user:password@example.test/jobs"), null);
  assert.equal(safeHttpUrl("https://example.test/jobs?signature=secret-value&page=2#x"), null);
  assert.equal(urlDisplay("https://example.test/jobs?signature=secret-value&page=2#x"), "含敏感参数，入口不可直接打开");
  assert.equal(safeHttpUrl("https://example.test/jobs?page=2#x"), "https://example.test/jobs?page=2#x");
});

test("详情兼容顶层record和record+attempts形状", () => {
  assert.deepEqual(normalizeDetail({ id: "one", company_name: "A" }).record.id, "one");
  assert.deepEqual(normalizeDetail({ record: { id: "two" }, attempts: [{ status: "failed" }] }).attempts, [{ status: "failed" }]);
});

test("202只表示重试启动，不被requestJson当成完成结果", async () => {
  const calls = [];
  const result = await requestJson(async (_path, options) => {
    calls.push(options);
    return { ok: true, status: 202, async json() { return { status: "running" }; } };
  }, "/api/company-sources/x/retry", { method: "POST" });
  assert.equal(result.response.status, 202);
  assert.equal(result.payload.status, "running");
  assert.equal(calls[0].headers["X-RecruitOps-Local-UI"], "1");
});

test("只读GET不携带本地写操作标记", async () => {
  const calls = [];
  await requestJson(async (_path, options) => {
    calls.push(options);
    return { ok: true, status: 200, async json() { return { items: [] }; } };
  }, "/api/company-sources");
  assert.equal(calls[0].headers["X-RecruitOps-Local-UI"], undefined);
});

test("来源列表不暴露抓取来源标识和内部记录ID", () => {
  const fs = require("node:fs");
  const source = fs.readFileSync(require.resolve("../../apps/web/company-sources.js"), "utf8");
  assert.equal(source.includes("record.source_record_id"), false);
  assert.equal(source.includes("valueOrUnknown(record.source)"), false);
});

test("503转成可恢复错误并保留状态码", async () => {
  await assert.rejects(
    requestJson(async () => ({ ok: false, status: 503, async json() { return { detail: "来源服务未接线" }; } }), "/api/company-sources"),
    (error) => error.status === 503 && error.message === "来源服务未接线",
  );
});

test("轮询最多6次且running不会伪报完成", async () => {
  let reads = 0;
  const result = await pollRecord(async () => { reads += 1; return { record: { status: "running" } }; }, async () => {}, { intervalMs: 0 });
  assert.equal(reads, MAX_POLL_ATTEMPTS);
  assert.equal(result.exhausted, true);
});

test("轮询遇到缺失record不能标记done", async () => {
  const result = await pollRecord(async () => ({ attempts: [] }), async () => {}, { maxAttempts: 1, intervalMs: 0 });
  assert.equal(result.done, undefined);
  assert.equal(result.exhausted, true);
});

test("isActive为false时轮询不发GET", async () => {
  let reads = 0;
  const result = await pollRecord(async () => { reads += 1; return { record: { status: "complete" } }; }, async () => {}, { isActive: () => false, intervalMs: 0 });
  assert.equal(reads, 0);
  assert.equal(result.cancelled, true);
});

test("failed会结束轮询但使用error tone，不是成功", async () => {
  const result = await pollRecord(async () => ({ record: { status: "failed" } }), async () => {}, { intervalMs: 0 });
  assert.equal(result.done, true);
  assert.equal(resultTone(result.detail.record.status), "error");
});
