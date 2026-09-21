(() => {
  "use strict";

  const PAGE_SIZE = 30;
  const JOB_PAGE_SIZE = 30;
  const SOURCE_FETCH_PAGE_SIZE = 100;
  const MAX_POLL_ATTEMPTS = 6;
  const POLL_INTERVAL_MS = 2000;
  const SOURCE_STATUS_LABELS = {
    pending: "待抓取",
    running: "抓取中",
    complete: "抓取完整",
    partial: "抓取不完整",
    failed: "无法抓取",
    unusable: "无法抓取",
  };
  const LEGACY_STATUS_LABELS = {
    complete: "已完成",
    partial: "部分完成",
    failed: "抓取失败",
    pending: "待抓取",
    running: "抓取中",
  };
  const JOB_CAPTURE_STATUS_LABELS = {
    unknown: "未知",
    pending: "待抓取",
    complete: "抓取完整",
    failed: "抓取失败",
  };
  const AVAILABILITY_STATUS_LABELS = {
    active: "有效",
    inactive: "失效",
  };
  const UNGRABBABLE_SOURCE_STATUSES = new Set(["failed"]);
  const SOURCE_FILTER_OPTIONS = [
    { value: "", label: "全部状态" },
    { value: "pending", label: "待抓取" },
    { value: "running", label: "抓取中" },
    { value: "complete", label: "抓取完整" },
    { value: "partial", label: "抓取不完整" },
    { value: "ungrabbable", label: "无法抓取" },
  ];
  const FAILURE_STAGE_LABELS = {
    entry: "入口",
    list: "列表",
    detail: "详情",
    pagination: "分页",
    persist: "保存",
  };
  const SECRET_QUERY = /^(?:token|signature|sig|secret|key|auth|authorization|access_token|expires|x-amz-|x-goog-)/i;

  function valueOrUnknown(value) {
    return value === null || value === undefined || value === "" ? "未知" : String(value);
  }

  function statusLabel(status) {
    return LEGACY_STATUS_LABELS[status] || valueOrUnknown(status);
  }

  function sourceStatusLabel(status) {
    return SOURCE_STATUS_LABELS[status] || valueOrUnknown(status);
  }

  function captureStatusLabel(status) {
    return JOB_CAPTURE_STATUS_LABELS[status] || valueOrUnknown(status);
  }

  function availabilityStatusLabel(status) {
    return AVAILABILITY_STATUS_LABELS[status] || valueOrUnknown(status);
  }

  function formatMatchScore(value) {
    if (value === null || value === undefined || value === "") return "未评分";
    if (typeof value === "boolean" || (typeof value === "string" && !value.trim())) return "未评分";
    const number = Number(value);
    if (!Number.isFinite(number)) return "未评分";
    return `${Number.isInteger(number) ? number : number.toFixed(1)} 分`;
  }

  function localTime(value) {
    if (!value) return "尚无记录";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "时间未知";
    return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
  }

  function safeHttpUrl(value) {
    const raw = value == null ? "" : String(value).trim();
    if (!/^https?:\/\//i.test(raw)) return null;
    try {
      const url = new URL(raw);
      if (url.protocol !== "http:" && url.protocol !== "https:") return null;
      if (url.username || url.password) return null;
      if ([...url.searchParams.keys()].some((key) => SECRET_QUERY.test(key))) return null;
      return raw;
    } catch (_) { return null; }
  }

  function urlDisplay(value) {
    const raw = value == null ? "" : String(value).trim();
    if (!raw) return "未知";
    if (!/^https?:\/\//i.test(raw)) return "入口不可直接打开";
    try {
      const url = new URL(raw);
      if (url.username || url.password) return "含账号或密码，入口不可直接打开";
      if ([...url.searchParams.keys()].some((key) => SECRET_QUERY.test(key))) return "含敏感参数，入口不可直接打开";
      return raw;
    } catch (_) { return "入口不可直接打开"; }
  }

  function resultTone(status) {
    if (status === "complete") return "success";
    if (status === "partial" || status === "pending") return "warn";
    return "error";
  }

  function normalizeDetail(payload) {
    if (!payload || typeof payload !== "object") return { record: null, attempts: [] };
    return {
      record: payload.record || (payload.id || payload.company_name ? payload : null),
      attempts: Array.isArray(payload.attempts) ? payload.attempts : [],
    };
  }

  function errorMessage(error, fallback = "请求失败") {
    return error?.message || fallback;
  }

  function buildSourceListPath({ page = 1, pageSize = PAGE_SIZE, query = "", status = "" } = {}) {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (query) params.set("q", String(query));
    if (status && status !== "ungrabbable") params.set("status", String(status));
    return `/api/company-sources?${params}`;
  }

  function buildJobsPath(recordId, page = 1, pageSize = JOB_PAGE_SIZE) {
    return `/api/company-sources/${encodeURIComponent(recordId)}/jobs?page=${encodeURIComponent(String(page))}&page_size=${encodeURIComponent(String(pageSize))}`;
  }

  function normalizeList(payload, fallbackPage = 1, fallbackPageSize = PAGE_SIZE) {
    if (!payload || typeof payload !== "object" || !Array.isArray(payload.items)) {
      throw new Error("来源列表返回格式无效");
    }
    const total = Number(payload.total);
    if (!Number.isInteger(total) || total < 0) throw new Error("来源列表缺少有效总数");
    return {
      items: payload.items.filter((item) => item && typeof item === "object"),
      total,
      page: Number.isInteger(Number(payload.page)) && Number(payload.page) > 0 ? Number(payload.page) : fallbackPage,
      page_size: Number.isInteger(Number(payload.page_size)) && Number(payload.page_size) > 0 ? Number(payload.page_size) : fallbackPageSize,
    };
  }

  function normalizeJobs(payload, fallbackPage = 1, fallbackPageSize = JOB_PAGE_SIZE) {
    if (!payload || typeof payload !== "object" || !Array.isArray(payload.items)) {
      throw new Error("岗位列表返回格式无效");
    }
    const total = Number(payload.total);
    if (!Number.isInteger(total) || total < 0) throw new Error("岗位列表缺少有效总数");
    return {
      items: payload.items.filter((item) => item && typeof item === "object"),
      total,
      page: Number.isInteger(Number(payload.page)) && Number(payload.page) > 0 ? Number(payload.page) : fallbackPage,
      page_size: Number.isInteger(Number(payload.page_size)) && Number(payload.page_size) > 0 ? Number(payload.page_size) : fallbackPageSize,
    };
  }

  function mergeSourcePages(pages, page = 1, pageSize = PAGE_SIZE) {
    const records = new Map();
    pages.forEach((sourcePage) => sourcePage.items.forEach((item) => records.set(String(item.id), item)));
    const items = [...records.values()].sort((left, right) => {
      const leftTime = Date.parse(left.updated_at || left.last_attempt_at || "") || 0;
      const rightTime = Date.parse(right.updated_at || right.last_attempt_at || "") || 0;
      return rightTime - leftTime;
    });
    return {
      items: items.slice((page - 1) * pageSize, page * pageSize),
      total: items.length,
      page,
      page_size: pageSize,
    };
  }

  async function requestJson(fetchImpl, path, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const localWriteHeader = method === "GET" || method === "HEAD"
      ? {}
      : { "X-RecruitOps-Local-UI": "1" };
    const response = await fetchImpl(path, {
      ...options,
      headers: { Accept: "application/json", ...localWriteHeader, ...(options.headers || {}) },
    });
    let payload = null;
    try { payload = await response.json(); } catch (_) { /* empty error bodies are valid */ }
    if (!response.ok) {
      const error = new Error(payload?.detail || payload?.message || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return { response, payload };
  }

  async function pollRecord(fetchRecord, wait, options = {}) {
    const maxAttempts = options.maxAttempts ?? MAX_POLL_ATTEMPTS;
    const intervalMs = options.intervalMs ?? POLL_INTERVAL_MS;
    const isActive = options.isActive || (() => true);
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      if (!isActive()) return { cancelled: true, attempts: attempt - 1 };
      await wait(intervalMs);
      if (!isActive()) return { cancelled: true, attempts: attempt - 1 };
      const detail = normalizeDetail(await fetchRecord());
      if (detail.record && detail.record.status !== "running") return { detail, attempts: attempt, done: true };
    }
    return { exhausted: true, attempts: maxAttempts };
  }

  function createBrowserUi() {
    if (typeof document === "undefined") return;
    const view = document.getElementById("companies-view");
    if (!view || document.getElementById("company-sources-section")) return;
    const originalToolbar = view.querySelector(".company-browser-toolbar");
    const originalRanking = view.querySelector('[aria-labelledby="company-ranking-heading"]');

    const toggle = document.createElement("div");
    toggle.className = "company-source-tabs";
    toggle.setAttribute("role", "group");
    toggle.setAttribute("aria-label", "公司视图");
    toggle.innerHTML = '<button class="button button--secondary company-source-view-button" type="button" data-company-source-view="sources" aria-pressed="true">来源状态</button><button class="button button--ghost company-source-view-button" type="button" data-company-source-view="ranking" aria-pressed="false">岗位排行</button>';
    const sharedHeading = view.querySelector(":scope > .section-heading");
    if (sharedHeading) sharedHeading.appendChild(toggle);

    const section = document.createElement("section");
    section.id = "company-sources-section";
    section.className = "workspace-section company-source-section";
    section.setAttribute("aria-labelledby", "company-sources-heading");
    section.innerHTML = [
      '<div class="section-heading section-heading--compact company-source-heading"><div><p class="eyebrow">招聘来源</p><h3 id="company-sources-heading">公司来源</h3></div><button class="button button--ghost company-source-refresh" type="button">刷新</button></div>',
      '<div class="company-source-toolbar"><label class="search-field" for="company-source-search"><span class="sr-only">搜索来源</span><input id="company-source-search" type="search" placeholder="搜索公司或来源" autocomplete="off"></label><label><span class="sr-only">来源状态</span><select id="company-source-status">' + SOURCE_FILTER_OPTIONS.map((option) => `<option value="${option.value}">${option.label}</option>`).join("") + '</select></label></div>',
      '<div class="company-source-message" role="status" aria-live="polite" hidden></div>',
      '<div class="table-scroll"><table class="data-table company-source-table"><thead><tr><th>公司 + 来源</th><th>抓取状态</th><th>当前岗位 / JD 未完成</th><th>最近尝试</th><th>操作</th></tr></thead><tbody id="company-sources-body"><tr><td colspan="5">来源记录加载中</td></tr></tbody></table></div>',
      '<div class="pager company-source-pager"><button class="button button--ghost" data-company-source-page="prev" type="button">上一页</button><span id="company-source-page-info">—</span><button class="button button--ghost" data-company-source-page="next" type="button">下一页</button></div>',
    ].join("");
    view.insertBefore(section, originalToolbar || originalRanking || null);

    const setViewMode = (mode) => {
      const sourcesVisible = mode === "sources";
      section.hidden = !sourcesVisible;
      if (originalToolbar) originalToolbar.hidden = sourcesVisible;
      if (originalRanking) originalRanking.hidden = sourcesVisible;
      toggle.querySelectorAll("[data-company-source-view]").forEach((button) => {
        const selected = button.dataset.companySourceView === mode;
        button.setAttribute("aria-pressed", String(selected));
        button.classList.toggle("button--secondary", selected);
        button.classList.toggle("button--ghost", !selected);
      });
    };
    setViewMode("sources");
    toggle.addEventListener("click", (event) => {
      const button = event.target.closest("[data-company-source-view]");
      if (button) setViewMode(button.dataset.companySourceView);
    });

    const state = {
      page: 1,
      total: 0,
      items: [],
      query: "",
      status: "",
      selectedId: null,
      detail: null,
      jobs: { id: null, page: 1, total: 0, items: [], page_size: JOB_PAGE_SIZE, loading: false, error: "", requestId: 0 },
      requestId: 0,
      pollToken: 0,
      detailToken: 0,
      loading: false,
      error: "",
    };
    const fetchImpl = globalThis.fetch.bind(globalThis);
    const $ = (selector) => section.querySelector(selector);
    const body = $("#company-sources-body");
    const dialog = document.createElement("dialog");
    dialog.className = "job-detail-dialog company-detail-dialog";
    dialog.setAttribute("aria-label", "公司详情");
    const close = document.createElement("button");
    close.type = "button"; close.className = "button button--ghost"; close.textContent = "关闭";
    const detailContent = document.createElement("div");
    dialog.append(close, detailContent); section.appendChild(dialog);
    close.addEventListener("click", () => dialog.close());
    dialog.addEventListener("close", () => {
      cancelPolling(); state.selectedId = null; state.detail = null; resetJobs();
      section.querySelectorAll(".company-source-actions [data-company-source-detail]").forEach((button) => { button.textContent = "详情"; });
    });
    dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });
    const message = $(".company-source-message");
    const setMessage = (text, kind = "error") => { message.textContent = text; message.className = `company-source-message company-source-message--${kind}`; message.hidden = !text; };
    const cancelPolling = () => { state.pollToken += 1; state.detailToken += 1; };
    const active = () => !view.hidden && !section.hidden && document.body.contains(section);
    const fetchSourcePage = async (status, page, pageSize) => {
      const payload = (await requestJson(fetchImpl, buildSourceListPath({ page, pageSize, query: state.query, status }))).payload;
      return normalizeList(payload, page, pageSize);
    };
    const fetchAllSourcePages = async (status) => {
      const first = await fetchSourcePage(status, 1, SOURCE_FETCH_PAGE_SIZE);
      const pageCount = Math.max(1, Math.ceil(first.total / SOURCE_FETCH_PAGE_SIZE));
      if (pageCount === 1) return [first];
      const pages = await Promise.all(
        Array.from({ length: pageCount - 1 }, (_, index) => fetchSourcePage(status, index + 2, SOURCE_FETCH_PAGE_SIZE)),
      );
      return [first, ...pages];
    };
    const fetchList = async () => {
      if (state.status === "ungrabbable") {
        const pages = (await Promise.all([...UNGRABBABLE_SOURCE_STATUSES].map((status) => fetchAllSourcePages(status)))).flat();
        return mergeSourcePages(pages, state.page, PAGE_SIZE);
      }
      return fetchSourcePage(state.status, state.page, PAGE_SIZE);
    };
    const fetchRecord = async (id) => (await requestJson(fetchImpl, `/api/company-sources/${encodeURIComponent(id)}`)).payload;
    const fetchJobs = async (id, page) => normalizeJobs(
      (await requestJson(fetchImpl, buildJobsPath(id, page, JOB_PAGE_SIZE))).payload,
      page,
      JOB_PAGE_SIZE,
    );
    const resetJobs = (id = null) => {
      state.jobs = { id, page: 1, total: 0, items: [], page_size: JOB_PAGE_SIZE, loading: false, error: "", requestId: state.jobs.requestId + 1 };
    };

    const link = (label, url) => {
      const href = safeHttpUrl(url);
      const display = urlDisplay(label);
      if (!href) return document.createTextNode(display);
      const anchor = document.createElement("a"); anchor.className = "company-source-link"; anchor.textContent = display; anchor.href = href; anchor.target = "_blank"; anchor.rel = "noopener noreferrer"; return anchor;
    };
    const jobLink = (url) => {
      const href = safeHttpUrl(url);
      if (!href) return document.createTextNode(url ? urlDisplay(url) : "未知");
      const anchor = document.createElement("a");
      anchor.className = "company-source-link";
      anchor.textContent = "打开详情";
      anchor.title = href;
      anchor.href = href;
      anchor.target = "_blank";
      anchor.rel = "noopener noreferrer";
      return anchor;
    };
    const cellText = (row, content) => { const cell = document.createElement("td"); if (content instanceof Node) cell.appendChild(content); else cell.textContent = valueOrUnknown(content); row.appendChild(cell); return cell; };
    const jobCell = (row, content, className = "") => {
      const cell = document.createElement("td");
      if (className) cell.className = className;
      if (content instanceof Node) cell.appendChild(content); else cell.textContent = valueOrUnknown(content);
      row.appendChild(cell);
      return cell;
    };
    const renderJobs = (jobsState = {}) => {
      const section = document.createElement("section");
      section.className = "company-source-jobs";
      section.setAttribute("aria-labelledby", "company-source-jobs-heading");
      const heading = document.createElement("div");
      heading.className = "company-source-jobs-heading";
      const title = document.createElement("h4");
      title.id = "company-source-jobs-heading";
      title.textContent = "来源岗位";
      const count = document.createElement("span");
      count.className = "muted-label";
      count.textContent = jobsState.loading ? "读取中" : jobsState.error ? "读取失败" : `共 ${valueOrUnknown(jobsState.total)} 条`;
      heading.append(title, count);
      section.appendChild(heading);

      if (jobsState.loading) {
        const loading = document.createElement("p");
        loading.className = "company-source-jobs-state";
        loading.textContent = "岗位列表加载中";
        section.appendChild(loading);
        return section;
      }
      if (jobsState.error) {
        const error = document.createElement("p");
        error.className = "company-source-jobs-state company-source-jobs-state--error";
        error.textContent = `岗位列表读取失败：${jobsState.error}`;
        section.appendChild(error);
        return section;
      }
      if (!jobsState.items?.length) {
        const empty = document.createElement("p");
        empty.className = "company-source-jobs-state";
        empty.textContent = `当前页暂无岗位记录，共 ${valueOrUnknown(jobsState.total)} 条`;
        section.appendChild(empty);
        return section;
      }

      const scroll = document.createElement("div");
      scroll.className = "table-scroll company-source-jobs-scroll";
      const table = document.createElement("table");
      table.className = "data-table company-source-jobs-table";
      const caption = document.createElement("caption");
      caption.className = "sr-only";
      caption.textContent = "来源岗位列表";
      table.appendChild(caption);
      const thead = document.createElement("thead");
      const headerRow = document.createElement("tr");
      ["标题", "详情链接", "抓取状态", "失败原因", "有效性", "匹配分"].forEach((label) => {
        const cell = document.createElement("th");
        cell.scope = "col";
        cell.textContent = label;
        headerRow.appendChild(cell);
      });
      thead.appendChild(headerRow);
      table.appendChild(thead);
      const tbody = document.createElement("tbody");
      jobsState.items.forEach((job) => {
        const row = document.createElement("tr");
        const titleCell = jobCell(row, job.title, "company-source-job-title");
        titleCell.title = valueOrUnknown(job.title);
        jobCell(row, jobLink(job.detail_url), "company-source-job-url");
        const captureStatus = job.capture_status || "unknown";
        const captureBadge = document.createElement("span");
        captureBadge.className = `company-source-job-status company-source-job-status--${captureStatus}`;
        captureBadge.textContent = captureStatusLabel(captureStatus);
        jobCell(row, captureBadge);
        jobCell(row, job.capture_failure_reason, "company-source-job-failure");
        const availability = document.createElement("span");
        availability.className = `company-source-job-availability company-source-job-availability--${job.availability_status || "unknown"}`;
        availability.textContent = availabilityStatusLabel(job.availability_status);
        jobCell(row, availability);
        const score = formatMatchScore(job.match_score);
        const scoreNode = document.createElement("span");
        scoreNode.className = `company-source-job-score${score === "未评分" ? " company-source-job-score--empty" : ""}`;
        scoreNode.textContent = score;
        jobCell(row, scoreNode);
        tbody.appendChild(row);
      });
      table.appendChild(tbody);
      scroll.appendChild(table);
      section.appendChild(scroll);

      const page = Math.max(1, Number(jobsState.page) || 1);
      const pageSize = Math.max(1, Number(jobsState.page_size) || JOB_PAGE_SIZE);
      const pages = Math.max(1, Math.ceil((Number(jobsState.total) || 0) / pageSize));
      const pager = document.createElement("div");
      pager.className = "pager company-source-job-pager";
      const previous = document.createElement("button");
      previous.className = "button button--ghost";
      previous.type = "button";
      previous.dataset.companySourceJobPage = "prev";
      previous.textContent = "上一页";
      previous.disabled = page <= 1;
      const info = document.createElement("span");
      info.className = "muted-label";
      info.textContent = `第 ${page} / ${pages} 页 · 共 ${valueOrUnknown(jobsState.total)} 条`;
      const next = document.createElement("button");
      next.className = "button button--ghost";
      next.type = "button";
      next.dataset.companySourceJobPage = "next";
      next.textContent = "下一页";
      next.disabled = page >= pages;
      pager.append(previous, info, next);
      section.appendChild(pager);
      return section;
    };
    const renderDetail = (record, attempts, jobsState) => {
      const row = document.createElement("tr"); row.className = "company-source-detail-row";
      const cell = document.createElement("td"); cell.colSpan = 5;
      const box = document.createElement("div"); box.className = "company-source-detail";
      const title = document.createElement("strong"); title.textContent = `${valueOrUnknown(record?.company_name)} · 来源详情`; box.appendChild(title);
      const urls = document.createElement("div"); urls.className = "company-source-urls";
      [["原始入口 URL", record?.original_entry_url], ["当前入口 URL", record?.entry_url], ["最终 URL", record?.final_url], ["来源 URL", record?.source_url]].forEach(([label, url]) => { const line = document.createElement("p"); line.className = "company-source-url-line"; line.append(document.createTextNode(`${label}：`), link(url, url)); urls.appendChild(line); });
      box.appendChild(urls);
      const facts = document.createElement("dl"); facts.className = "company-source-facts";
      [["抓取状态", sourceStatusLabel(record?.status)], ["失败阶段", FAILURE_STAGE_LABELS[record?.failure_stage] || record?.failure_stage], ["原因代码", record?.reason_code], ["失败原因", record?.reason || record?.failure_reason], ["分页完成", record?.pagination_complete === true ? "是" : record?.pagination_complete === false ? "否" : "未知"], ["最近成功岗位数", record?.last_success_job_count], ["最近尝试", localTime(record?.last_attempt_at)], ["更新时间", localTime(record?.updated_at)]].forEach(([label, value]) => {
        const group = document.createElement("div"); if (label === "失败原因") group.className = "company-source-fact-wide";
        const term = document.createElement("dt"); term.textContent = label;
        const detail = document.createElement("dd"); detail.textContent = valueOrUnknown(value);
        group.append(term, detail); facts.appendChild(group);
      }); box.appendChild(facts);
      const form = document.createElement("form"); form.className = "company-source-edit"; form.dataset.companySourceEdit = record?.id || "";
      const input = document.createElement("input"); input.type = "url"; input.required = true; input.value = safeHttpUrl(record?.entry_url) ? record.entry_url : ""; input.placeholder = "https://…"; input.className = "company-source-entry-input"; input.setAttribute("aria-label", "编辑入口 URL");
      const save = document.createElement("button"); save.type = "submit"; save.className = "button button--secondary"; save.textContent = "保存入口";
      const retry = document.createElement("button"); retry.type = "button"; retry.className = "button button--ghost"; retry.dataset.companySourceRetry = record?.id || ""; retry.textContent = "重试";
      retry.disabled = record?.status === "running";
      form.append(input, save, retry); box.appendChild(form);
      const history = document.createElement("div"); history.className = "company-source-history";
      const historyTitle = document.createElement("strong"); historyTitle.textContent = "最近尝试"; history.appendChild(historyTitle);
      attempts.slice(0, 5).forEach((attempt) => { const entry = document.createElement("p"); entry.textContent = `${localTime(attempt.attempted_at || attempt.created_at)} · ${statusLabel(attempt.status)} · 成功岗位 ${valueOrUnknown(attempt.success_job_count)}`; history.appendChild(entry); });
      if (!attempts.length) history.appendChild(document.createTextNode("暂无记录")); box.appendChild(history);
      box.appendChild(renderJobs(jobsState));
      cell.appendChild(box); row.appendChild(cell); return row;
    };
    const render = () => {
      while (body.firstChild) body.removeChild(body.firstChild);
      if (state.error || !state.items.length) { const row = document.createElement("tr"); const cell = document.createElement("td"); cell.colSpan = 5; cell.textContent = state.error ? "来源记录读取失败" : "没有匹配的来源记录"; row.appendChild(cell); body.appendChild(row); }
      state.items.forEach((record) => {
        const row = document.createElement("tr"); row.dataset.companySourceId = record.id || "";
        const company = document.createElement("button"); company.type = "button"; company.className = "text-button";
        company.dataset.companySourceDetail = record.id || ""; company.textContent = valueOrUnknown(record.company_name);
        const identity = document.createElement("div"); identity.append(company); cellText(row, identity);
        const status = document.createElement("span"); status.className = `company-source-status company-source-status--${record.status || "unknown"}`; status.textContent = sourceStatusLabel(record.status); cellText(row, status);
        cellText(row, `${valueOrUnknown(record.job_count)} / ${valueOrUnknown(record.jd_pending_count)}`);
        const timeCell = cellText(row, localTime(record.last_attempt_at)); timeCell.title = record.last_attempt_at || "尚无记录";
        const actions = document.createElement("div"); actions.className = "company-source-actions"; const viewButton = document.createElement("button"); viewButton.type = "button"; viewButton.className = "button button--ghost"; viewButton.dataset.companySourceDetail = record.id || ""; viewButton.textContent = state.selectedId === record.id ? "收起" : "详情"; actions.appendChild(viewButton); cellText(row, actions);
        body.appendChild(row);
        if (state.selectedId === record.id && state.detail) {
          const detail = renderDetail(state.detail.record, state.detail.attempts, state.jobs);
          detailContent.replaceChildren(detail.querySelector(".company-source-detail"));
          if (!dialog.open) dialog.showModal();
        }
      });
      const pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE)); $("#company-source-page-info").textContent = state.error ? "读取失败" : `第 ${state.page} / ${pages} 页 · 共 ${valueOrUnknown(state.total)} 条`; $("[data-company-source-page=prev]").disabled = state.error || state.page <= 1; $("[data-company-source-page=next]").disabled = state.error || state.page >= pages;
    };
    const load = async () => {
      const requestId = ++state.requestId; state.loading = true; state.error = ""; setMessage("");
      try { const payload = await fetchList(); if (requestId !== state.requestId) return; state.items = payload.items; state.total = payload.total; state.error = ""; render(); }
      catch (error) { if (requestId !== state.requestId) return; state.items = []; state.total = 0; state.error = errorMessage(error); render(); setMessage(`来源加载失败：${state.error}`); }
      finally { state.loading = false; }
    };
    const loadJobs = async (id, detailToken) => {
      const requestId = state.jobs.requestId + 1;
      const page = state.jobs.page;
      state.jobs = { ...state.jobs, id, loading: true, error: "", requestId };
      render();
      try {
        const jobs = await fetchJobs(id, page);
        if (detailToken !== state.detailToken || state.selectedId !== id || requestId !== state.jobs.requestId) return;
        state.jobs = { ...jobs, id, loading: false, error: "", requestId };
        render();
      } catch (error) {
        if (detailToken !== state.detailToken || state.selectedId !== id || requestId !== state.jobs.requestId) return;
        state.jobs = { ...state.jobs, id, loading: false, error: errorMessage(error), requestId };
        render();
      }
    };
    const openDetail = async (id) => {
      cancelPolling(); const detailToken = state.detailToken; state.selectedId = id; state.detail = null; resetJobs(id); render();
      try {
        const detail = normalizeDetail(await fetchRecord(id));
        if (detailToken !== state.detailToken || state.selectedId !== id) return;
        if (!detail.record) throw new Error("来源详情返回格式无效");
        state.detail = detail; render();
        void loadJobs(id, detailToken);
      } catch (error) {
        if (detailToken === state.detailToken && state.selectedId === id) setMessage(`来源详情加载失败：${errorMessage(error)}`);
      }
    };
    const retry = async (button, id) => {
      if (button.disabled) return; button.disabled = true; setMessage(""); cancelPolling(); const token = state.pollToken;
      try {
        const result = await requestJson(fetchImpl, `/api/company-sources/${encodeURIComponent(id)}/retry`, { method: "POST" });
        if (result.response.status !== 202) throw new Error(`重试请求返回了意外状态（${result.response.status}）`);
        const listItem = state.items.find((item) => String(item.id) === String(id));
        if (listItem) listItem.status = "running";
        if (state.detail?.record && String(state.detail.record.id) === String(id)) state.detail.record.status = "running";
        render();
        setMessage("重试已启动，正在读取状态…", "info");
        const outcome = await pollRecord(() => fetchRecord(id), (ms) => new Promise((resolve) => setTimeout(resolve, ms)), { isActive: () => active() && token === state.pollToken && state.selectedId === id });
        if (outcome.cancelled) return;
        if (outcome.exhausted) { setMessage("重试仍在进行，请稍后手动刷新。", "info"); return; }
        if (outcome.cancelled || token !== state.pollToken || !active() || state.selectedId !== id) return;
        if (!outcome.detail?.record) { setMessage("本次结果：记录缺失，未确认完成。", "error"); return; }
        state.detail = outcome.detail; await load();
        if (token !== state.pollToken || !active() || state.selectedId !== id) return;
        resetJobs(id);
        render();
        await loadJobs(id, state.detailToken);
        if (token !== state.pollToken || !active() || state.selectedId !== id) return;
        render(); setMessage(`本次结果：${sourceStatusLabel(outcome.detail.record.status)}`, resultTone(outcome.detail.record.status));
      } catch (error) { setMessage(`重试失败：${errorMessage(error)}`); }
      finally { button.disabled = state.detail?.record?.status === "running"; }
    };
    section.addEventListener("click", (event) => {
      const jobPageButton = event.target.closest("[data-company-source-job-page]");
      if (jobPageButton && !jobPageButton.disabled && state.selectedId && state.detail) {
        const pageSize = Math.max(1, Number(state.jobs.page_size) || JOB_PAGE_SIZE);
        const pages = Math.max(1, Math.ceil((Number(state.jobs.total) || 0) / pageSize));
        state.jobs.page = jobPageButton.dataset.companySourceJobPage === "prev" ? Math.max(1, state.jobs.page - 1) : Math.min(pages, state.jobs.page + 1);
        void loadJobs(state.selectedId, state.detailToken);
        return;
      }
      const detailButton = event.target.closest("[data-company-source-detail]");
      if (detailButton) {
        const id = detailButton.dataset.companySourceDetail;
        if (state.selectedId === id) {
          dialog.close();
        } else {
          void openDetail(id);
        }
        return;
      }
      const retryButton = event.target.closest("[data-company-source-retry]"); if (retryButton) { void retry(retryButton, retryButton.dataset.companySourceRetry); return; }
      const pageButton = event.target.closest("[data-company-source-page]"); if (pageButton && !pageButton.disabled) { const pages = Math.max(1, Math.ceil(state.total / PAGE_SIZE)); state.page = pageButton.dataset.companySourcePage === "prev" ? Math.max(1, state.page - 1) : Math.min(pages, state.page + 1); state.selectedId = null; state.detail = null; resetJobs(); cancelPolling(); void load(); }
    });
    section.addEventListener("submit", async (event) => {
      const form = event.target.closest("[data-company-source-edit]"); if (!form) return; event.preventDefault(); const input = form.querySelector("input"); const save = form.querySelector("button[type=submit]"); if (save.disabled) return;
      const url = safeHttpUrl(input.value.trim()); if (!url) { setMessage("入口 URL 必须是 http(s) 地址。"); return; }
      const id = form.dataset.companySourceEdit; const detailToken = state.detailToken; const current = state.detail?.record; save.disabled = true;
      try { await requestJson(fetchImpl, `/api/company-sources/${encodeURIComponent(id)}/entry`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ entry_url: input.value.trim(), expected_updated_at: current?.updated_at ?? null }) }); if (detailToken !== state.detailToken || state.selectedId !== id) return; const refreshed = normalizeDetail(await fetchRecord(id)); if (detailToken !== state.detailToken || state.selectedId !== id) return; state.detail = refreshed; await load(); if (detailToken === state.detailToken && state.selectedId === id) { render(); setMessage("入口 URL 已保存。", "success"); } }
      catch (error) { setMessage(`保存入口失败：${errorMessage(error)}`); } finally { save.disabled = false; }
    });
    $(".company-source-refresh").addEventListener("click", () => {
      const selectedId = state.selectedId;
      cancelPolling();
      void (async () => {
        await load();
        if (selectedId && active()) void openDetail(selectedId);
      })();
    });
    const queryInput = $("#company-source-search"); queryInput.addEventListener("input", () => { state.query = queryInput.value.trim(); state.page = 1; state.selectedId = null; state.detail = null; resetJobs(); cancelPolling(); void load(); });
    $("#company-source-status").addEventListener("change", (event) => { state.status = event.target.value; state.page = 1; state.selectedId = null; state.detail = null; resetJobs(); cancelPolling(); void load(); });
    document.addEventListener("click", (event) => { const nav = event.target.closest("[data-view]"); if (nav && nav.dataset.view !== "companies") cancelPolling(); });
    new MutationObserver(() => { if (view.hidden) cancelPolling(); }).observe(view, { attributes: true, attributeFilter: ["hidden"] });
    document.addEventListener("recruitops:business-updated", () => { void load(); });
    void load();
  }

  const helpers = {
    PAGE_SIZE,
    JOB_PAGE_SIZE,
    MAX_POLL_ATTEMPTS,
    POLL_INTERVAL_MS,
    SOURCE_FILTER_OPTIONS,
    localTime,
    statusLabel,
    sourceStatusLabel,
    captureStatusLabel,
    availabilityStatusLabel,
    formatMatchScore,
    resultTone,
    safeHttpUrl,
    urlDisplay,
    normalizeDetail,
    normalizeList,
    normalizeJobs,
    mergeSourcePages,
    buildSourceListPath,
    buildJobsPath,
    requestJson,
    pollRecord,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = helpers;
  if (typeof document !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", createBrowserUi, { once: true }); else createBrowserUi();
  }
})();
