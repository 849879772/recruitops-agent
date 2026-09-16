"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const {
  JOB_PAGE_SIZE,
  SOURCE_FILTER_OPTIONS,
  availabilityStatusLabel,
  buildJobsPath,
  buildSourceListPath,
  captureStatusLabel,
  formatMatchScore,
  mergeSourcePages,
  normalizeJobs,
  normalizeList,
  safeHttpUrl,
  sourceStatusLabel,
} = require("../../apps/web/company-sources.js");

test("来源状态使用抓取完整性语义，并将失败入口合并为无法抓取", () => {
  assert.equal(sourceStatusLabel("pending"), "待抓取");
  assert.equal(sourceStatusLabel("running"), "抓取中");
  assert.equal(sourceStatusLabel("complete"), "抓取完整");
  assert.equal(sourceStatusLabel("partial"), "抓取不完整");
  assert.equal(sourceStatusLabel("failed"), "无法抓取");
  assert.equal(sourceStatusLabel("unusable"), "无法抓取");
  assert.deepEqual(SOURCE_FILTER_OPTIONS.at(-1), { value: "ungrabbable", label: "无法抓取" });
});

test("来源列表查询不会把空状态发给后端，合并筛选仍使用原始状态码", () => {
  const all = buildSourceListPath({ page: 1, pageSize: 30, query: "" });
  assert.equal(all, "/api/company-sources?page=1&page_size=30");
  assert.match(buildSourceListPath({ page: 2, pageSize: 30, query: "Alpha", status: "partial" }), /status=partial/);
  assert.doesNotMatch(buildSourceListPath({ status: "ungrabbable" }), /status=/);
});

test("无法抓取筛选合并两种失败来源并按来源分页", () => {
  const result = mergeSourcePages([
    { items: [{ id: "failed", updated_at: "2026-09-09T10:00:00Z", status: "failed" }], total: 1 },
    { items: [{ id: "unusable", updated_at: "2026-09-09T11:00:00Z", status: "unusable" }], total: 1 },
  ], 1, 30);
  assert.equal(result.total, 2);
  assert.deepEqual(result.items.map((item) => item.id), ["unusable", "failed"]);
  assert.equal(mergeSourcePages([
    { items: [{ id: "one" }, { id: "two" }] },
  ], 2, 1).items[0].id, "two");
});

test("岗位列表严格保留合同分页和失败岗位字段", () => {
  const payload = {
    items: [{
      id: "job-1",
      company_id: "company-1",
      title: "算法工程师",
      detail_url: "https://jobs.example.test/job-1",
      capture_status: "failed",
      capture_failure_reason: "详情超时",
      availability_status: "active",
      match_score: null,
    }],
    total: 31,
    page: 2,
    page_size: JOB_PAGE_SIZE,
  };
  const result = normalizeJobs(payload);
  assert.equal(result.total, 31);
  assert.equal(result.page, 2);
  assert.equal(result.items[0].capture_failure_reason, "详情超时");
  assert.equal(captureStatusLabel(result.items[0].capture_status), "抓取失败");
  assert.equal(availabilityStatusLabel(result.items[0].availability_status), "有效");
  assert.equal(formatMatchScore(result.items[0].match_score), "未评分");
  assert.throws(() => normalizeJobs({ items: [] }), /缺少有效总数/);
});

test("匹配分只在确有数值时显示，安全外链仅允许无凭据的http(s)", () => {
  assert.equal(formatMatchScore(0), "0 分");
  assert.equal(formatMatchScore(72.5), "72.5 分");
  assert.equal(formatMatchScore(""), "未评分");
  assert.equal(formatMatchScore(false), "未评分");
  assert.equal(formatMatchScore("not-a-score"), "未评分");
  assert.equal(safeHttpUrl("https://jobs.example.test/detail/1"), "https://jobs.example.test/detail/1");
  assert.equal(safeHttpUrl("https://user:pass@example.test/detail/1"), null);
  assert.equal(safeHttpUrl("https://example.test/detail/1?signature=secret"), null);
});

test("岗位详情路径绑定来源记录并显式分页", () => {
  assert.equal(
    buildJobsPath("source/1", 2, 30),
    "/api/company-sources/source%2F1/jobs?page=2&page_size=30",
  );
});

test("来源分页响应缺失items时不伪造空结果", () => {
  assert.throws(() => normalizeList({ total: 0 }), /来源列表返回格式无效/);
  assert.throws(() => normalizeList({ items: [], total: -1 }), /缺少有效总数/);
});
