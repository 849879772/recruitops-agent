(() => {
  "use strict";

  const STORAGE_KEYS = {
    codexThread: "recruitops.assistant.codex_thread_id",
    recent: "recruitops.assistant.recent.v1",
  };
  const STORAGE_SECRET_KEYS = /^(api_token|authorization|bearer_token|access_token|client_secret|password|secret)$/i;
  const STORAGE_BULKY_KEYS = /^(jd_raw|raw_html|page_text|body_html)$/i;

  function readStorage(key) {
    try { return globalThis.localStorage?.getItem(key) || ""; } catch (_) { return ""; }
  }

  function writeStorage(key, value) {
    try { globalThis.localStorage?.setItem(key, value); } catch (_) { /* private browsing can disable storage */ }
  }

  function redactStorageValue(value, key = "") {
    if (STORAGE_SECRET_KEYS.test(key)) return undefined;
    if (STORAGE_BULKY_KEYS.test(key)) return "[未保存到本地会话]";
    if (typeof value === "string") return value.length > 2000 ? `${value.slice(0, 2000)}…` : value;
    if (Array.isArray(value)) return value.slice(0, 20).map((item) => redactStorageValue(item)).filter((item) => item !== undefined);
    if (value && typeof value === "object") {
      return Object.fromEntries(
        Object.entries(value)
          .map(([childKey, childValue]) => [childKey, redactStorageValue(childValue, childKey)])
          .filter(([, childValue]) => childValue !== undefined),
      );
    }
    return value;
  }

  function loadStoredConversation() {
    let recent = {};
    try { recent = JSON.parse(readStorage(STORAGE_KEYS.recent) || "{}"); } catch (_) { recent = {}; }
    const codexThreadId = readStorage(STORAGE_KEYS.codexThread) || (typeof recent.codexThreadId === "string" ? recent.codexThreadId : "");
    if (codexThreadId) writeStorage(STORAGE_KEYS.codexThread, codexThreadId);
    return {
      codexThreadId,
      messages: codexThreadId && Array.isArray(recent.messages) ? recent.messages : [],
      tasks: codexThreadId && Array.isArray(recent.tasks) ? recent.tasks : [],
    };
  }

  const storedConversation = loadStoredConversation();
  const state = {
    tasks: storedConversation.tasks,
    messages: storedConversation.messages,
    traces: [],
    approvals: [],
    companies: [],
    jobs: [],
    jobBrowseItems: [],
    featuredJobs: [],
    jobFacets: { companies: [], categories: {}, platforms: [] },
    companySummaries: [],
    jobBrowse: { mode: "all", page: 1, pageSize: 50, total: 0, allTotal: 0, todayTotal: 0, requestId: 0 },
    companyPage: 1,
    companyPageSize: 30,
    applications: [],
    applicationsRequestId: 0,
    applicationBrowse: { pageSize: 50, total: 0, columns: {}, query: "" },
    applicationRequestController: null,
    jobRequestController: null,
    jobSummaryMode: null,
    jobSummaryAt: 0,
    schedules: [],
    allSchedules: [],
    scheduleRequestId: 0,
    scheduleView: "todo",
    scheduleStatus: "pending",
    scheduleDateFilter: "",
    scheduleMonth: "",
    scheduleEditingId: "",
    automations: [],
    mails: [],
    mailFreshness: { status: "unknown", synced_at: null, error_type: null },
    mailSync: { status: "idle", fetched: 0, inserted: 0, reused: 0, error: "" },
    conversations: [],
    assistantContext: {},
    apiToken: "",
    activeAssistantController: null,
    codexHealth: null,
    codexEnabled: null,
    codexReady: false,
    assistantConfiguration: null,
    assistantHealthRequestId: 0,
    codexThreadId: storedConversation.codexThreadId || "",
    codexThreadMetadata: null,
    codexThreadCursor: "",
    codexThreadListLoading: false,
    codexThreadListError: "",
    codexHistoryLoading: false,
    codexHistoryError: "",
    codexHistoryRequestId: 0,
    codexPendingThreadId: "",
    codexTurnId: "",
    codexTurnReady: null,
    codexStopRequested: false,
    codexEventState: {},
    operationalReport: null,
    jobTotal: 0,
    selectedTask: storedConversation.tasks[storedConversation.tasks.length - 1] || null,
  };

  const $ = (id) => document.getElementById(id);
  const localDate = (value = new Date()) => {
    const pad = (part) => String(part).padStart(2, "0");
    return `${value.getFullYear()}-${pad(value.getMonth() + 1)}-${pad(value.getDate())}`;
  };
  const today = localDate();
  const LOCAL_SCHEDULE_EVENTS_URL = "/api/local-ui/events";

  const TASK_LABELS = {
    conversation: "直接回答",
    capabilities: "能力说明",
    today_schedule: "今日安排",
    today_new_jobs: "今日岗位",
    company_coverage: "公司接入",
    knowledge_search: "接入知识检索",
    application_query: "投递状态",
    application_status_review: "投递进度复核",
    recommendation_explanation: "岗位匹配解释",
    application_preview: "投递预览",
    recruitment_mail_search: "招聘邮件",
    recruitment_mail_detail: "邮件详情",
    recruitment_mail_review: "邮件关联复核",
    recruitment_mail_process: "处理邮件信息",
    full_recruitment_sync: "全量爬取",
    operation_run: "手动运营任务",
    automation_plan: "定时计划预览",
  };

  const STATUS_LABELS = {
    success: "已完成",
    succeeded: "已完成",
    no_results: "没有结果",
    ambiguous: "需要确认",
    failure: "执行失败",
    safe_stop: "已安全停止",
    stopped: "已停止",
    paused: "已暂停，可继续",
    retrying: "重试中",
    planned: "已规划",
    tool_completed: "工具已返回",
    preview: "只读预览",
  };

  const APPLICATION_STAGE_LABELS = {
    interested: "关注",
    applied: "已投递",
    assessment: "测评",
    written: "笔试",
    interview1: "一面",
    interview2: "二面",
    interview3: "三面",
    hr: "HR 面",
    offer: "Offer",
    rejected: "已结束",
    withdrawn: "已撤回",
  };

  const APPLICATION_COLUMNS = [
    { key: "applied", label: "已投递", stages: ["interested", "applied"] },
    { key: "written", label: "笔试", stages: ["assessment", "written"] },
    { key: "interview", label: "面试", stages: ["interview1", "interview2", "interview3", "hr"] },
    { key: "offer", label: "Offer", stages: ["offer"] },
    { key: "closed", label: "已挂", stages: ["rejected", "withdrawn"] },
  ];

  const CODEX_STREAM_MAX_RECONNECTS = 2;

  // This is deliberately deterministic: the UI chooses an existing API task before making a request.
  const INTENT_RULES = [
    { taskType: "automation_plan", label: "定时计划预览", terms: ["制定定时任务", "定时计划", "安排每天", "每天凌晨", "每日凌晨", "每天更新", "每日更新"] },
    { taskType: "full_recruitment_sync", label: "全量爬取", terms: ["全量爬取", "全量抓取", "运行每日抓取", "运行每日招聘情报", "执行每日招聘情报"] },
    { taskType: "operation_run", label: "手动运营任务", terms: ["运行爬虫健康", "同步招聘邮箱", "邮箱同步", "运行投递进度任务", "执行投递复核任务"] },
    {
      taskType: "recommendation_explanation",
      label: "岗位匹配解释",
      terms: ["为什么匹配", "匹配原因", "适合我", "岗位匹配", "解释岗位", "推荐理由"],
    },
    { taskType: "application_status_review", label: "投递进度复核", terms: ["复核投递", "检查投递", "同步投递", "更新投递", "更新申请", "岗位状态", "官网状态"] },
    { taskType: "recruitment_mail_process", label: "处理邮件信息", terms: ["处理邮件信息", "处理招聘邮件", "整理招聘邮件", "处理全部待处理邮件"] },
    { taskType: "recruitment_mail_review", label: "邮件关联复核", terms: ["复核邮件", "关联邮件", "邮件关联"] },
    { taskType: "recruitment_mail_search", label: "招聘邮件", terms: ["招聘邮件", "邮箱消息", "企业邮件", "offer 邮件"] },
    { taskType: "application_query", label: "投递状态", terms: ["投递", "申请进度", "投递进度", "申请状态", "已投", "投了哪些"] },
    { taskType: "knowledge_search", label: "接入知识检索", terms: ["爬虫经验", "接入经验", "历史方案", "ats"] },
    { taskType: "company_coverage", label: "公司接入", terms: ["接入", "招聘来源", "招聘网站", "爬虫", "覆盖哪些公司"] },
    { taskType: "today_schedule", label: "今日安排", terms: ["安排", "日程", "面试", "笔试", "截止", "今天有什么事"] },
    { taskType: "today_new_jobs", label: "今日岗位", terms: ["岗位", "职位", "招聘", "机会", "值得看", "新增", "推荐岗位"] },
  ];

  const DEFAULT_QUESTIONS = {
    today_schedule: "查看今天的笔试和面试安排",
    today_new_jobs: "查看今日新增岗位",
    company_coverage: "哪些公司还没有接入？",
    application_query: "查询我的投递进度",
    application_status_review: "复核官网投递状态",
    recommendation_explanation: "请解释这个岗位为什么匹配我",
    full_recruitment_sync: "立即在后台执行一次全量岗位爬取，包括公司发现、岗位抓取、职位详情补全和匹配评分",
    recruitment_mail_process: "处理全部待处理的招聘邮件，并按照安全规则更新投递进度和日程",
  };

  const api = async (path, options = {}) => {
    const response = await fetch(path, { headers: { Accept: "application/json" }, ...options });
    let payload = null;
    try { payload = await response.json(); } catch (_) { payload = null; }
    if (!response.ok) throw new Error(payload?.detail || `请求失败（${response.status}）`);
    return payload;
  };

  const text = (value, fallback = "—") => String(value ?? fallback);
  const localizeCodexRuntimeMessage = (value) => {
    const raw = text(value, "");
    return raw
      .replace(
        /Codex turn interrupted after reaching the runtime time limit\.?/gi,
        "本次对话回合达到运行时限并已结束；已经启动的后台任务不会因此自动取消，可以直接询问最近任务的进度。中断前已经完成的写入会保留。",
      )
      .replace(
        /Codex turn interrupted after reaching the tool-call budget\.?/gi,
        "本次任务达到工具调用次数上限，已被运行时中断；中断前已经完成的写入会保留。",
      )
      .replace(
        /Codex turn interrupted after repeated events without progress\.?/gi,
        "本次任务连续多次没有产生新进展，已被运行时中断；中断前已经完成的写入会保留。",
      );
  };
  const RUNTIME_STAGE_LABELS = {
    starting: "准备任务",
    scope_pending: "准备抓取范围",
    scope_frozen: "抓取范围已确认",
    discovery: "公司发现",
    reconciliation: "公司整理",
    companies: "公司岗位列表抓取",
    details: "职位详情补全",
    crawl: "岗位抓取",
    matching: "岗位评分",
    offline_reconciliation: "岗位状态整理",
    reporting: "生成结果",
  };
  function friendlyRuntimeProgress(value) {
    const raw = text(value, "").trim();
    if (!raw) return "处理中";
    const [stage, detail = ""] = raw.split(":", 2);
    const label = RUNTIME_STAGE_LABELS[stage];
    if (!label) return raw;
    const count = detail.match(/^\d+\/\d+$/)?.[0];
    return count ? `${label} ${stage === "companies" ? "已处理 " : "本轮 "}${count}` : label;
  }
  const DAILY_STAGE_NAMES = {
    discovery: "公司发现", reconciliation: "公司整理", crawl: "岗位抓取",
    offline_reconciliation: "岗位状态整理", reporting: "生成结果",
    companies: "公司岗位列表抓取", jd: "职位详情补全", matching: "岗位评分",
  };
  const DAILY_STATUS_NAMES = {
    accepted: "准备中", pausing: "正在安全暂停", cancelling: "正在安全取消",
    running: "运行中", succeeded: "已完成", success: "已完成", failed: "失败",
    paused: "已暂停", stopped: "已中断", pending: "等待中",
    awaiting_continuation: "等待助理继续下一批",
  };
  const DAILY_MODE_NAMES = {
    full: "后台全量爬取", crawl_only: "后台岗位抓取",
    score_only: "后台岗位评分", resume: "后台任务续跑",
  };

  const ACTIVE_TASK_STATUSES = new Set(["accepted", "running", "pausing", "cancelling", "awaiting_continuation"]);
  function renderDailyProgress(payload) {
    const card = $("assistant-task-progress");
    if (!card) return;
    const runs = (Array.isArray(payload?.runs) ? payload.runs : payload?.run ? [payload.run] : [])
      .filter(item => ACTIVE_TASK_STATUSES.has(item.status));
    const run = runs[0];
    const extra = $("assistant-more-task-progress");
    clear(extra);
    card.hidden = !run;
    if (!run) { const bar = $("assistant-task-progress-bar"); if (bar) bar.hidden = true; return; }
    renderTaskProgressCard(card, run, true);
    runs.slice(1).forEach(item => {
      if (!extra) return;
      const other = element("section", "assistant-task-progress");
      extra.appendChild(other);
      renderTaskProgressCard(other, item, false);
    });
  }

  function renderTaskProgressCard(card, run, primary) {
    const heading = primary ? null : element("div", "assistant-task-progress-head");
    if (heading) card.appendChild(heading);
    const node = (suffix, tag, className = "") => {
      if (primary && $(`assistant-task-progress-${suffix}`)) return $(`assistant-task-progress-${suffix}`);
      const child = element(tag, className);
      (heading && ["title", "state"].includes(suffix) ? heading : card).appendChild(child);
      return child;
    };
    const titleNode = node("title", "strong");
    const stateNode = node("state", "span");
    const detailNode = node("detail", "p");
    const bar = node("bar", "progress");
    const stagesNode = node("stages", "small");
    const actions = node("actions", "div", "inline-actions"); clear(actions);
    const stage = run.progress?.stage || run.phase;
    const taskNames = { application_review: "官网投递状态复核", recruitment_mail: "处理招聘邮件" };
    const stageNames = { preparing: "准备任务", sync: "同步邮件", syncing: "同步邮件", analysis: "分析与处理", processing: "分析与处理", reviewing: "核查官网状态", application_review: "核查官网状态" };
    const label = DAILY_STAGE_NAMES[stage] || stageNames[stage] || "准备任务";
    const progress = run.progress || {};
    let completed = run.completed ?? null;
    let total = run.total ?? null;
    let detail = label;
    if (taskNames[run.task_kind]) {
      detail += Number.isInteger(total) && total > 0 ? ` · 已处理 ${completed ?? 0} / ${total} ${run.unit || "条"}` : " · 正在确认处理范围";
      if (run.task_kind === "application_review" && Number.isInteger(run.remaining)) detail += `，待完成 ${run.remaining}`;
      if (run.retry_pending > 0) detail += `（含待重试 ${run.retry_pending}）`;
      if (run.failed > 0) detail += `，失败 ${run.failed}${run.retry_pending > 0 ? "（含待重试）" : ""}`;
      if (run.blocked > 0) detail += `，待确认 ${run.blocked}`;
    } else if (stage === "discovery") {
      completed = progress.pages_fetched; total = progress.pages_total;
      detail += ` · 已读取 ${completed ?? 0} 页${Number.isInteger(total) && total > 0 ? ` / ${total} 页` : ""}，发现 ${progress.records_seen ?? 0} 条来源`;
    } else if (stage === "companies") {
      completed = progress.attempted_unique ?? run.completed ?? progress.confirmed_complete;
      total = progress.scope_total ?? run.total;
      detail += ` · 已处理 ${completed ?? 0}${Number.isInteger(total) && total > 0 ? ` / 总计 ${total}` : ""} 家`;
    } else if (stage === "jd" || stage === "matching") {
      const durable = stage === "matching" && Number.isInteger(progress.scope_total);
      completed = durable ? progress.confirmed_complete : progress.run_completed;
      total = durable ? progress.scope_total : progress.run_total;
      detail += ` · ${durable ? "已确认完成" : "本轮"} ${completed ?? 0}${Number.isInteger(total) && total > 0 ? ` / ${total}` : ""}`;
      if (progress.retry_pending > 0) detail += `，待重试 ${progress.retry_pending}`;
    }
    titleNode.textContent = `${taskNames[run.task_kind] || DAILY_MODE_NAMES[run.mode] || "后台任务"} · ${label}`;
    stateNode.textContent = DAILY_STATUS_NAMES[run.status] || "状态未知";
    detailNode.textContent = detail;
    const measurable = Number.isInteger(completed) && Number.isInteger(total) && total > 0;
    bar.hidden = !measurable;
    if (measurable) { bar.max = 100; bar.value = Math.min(100, Math.max(0, completed / total * 100)); }
    const stages = ["discovery", "reconciliation", "crawl", "offline_reconciliation", "reporting"];
    const updated = new Date(run.progress_updated_at || run.updated_at || "");
    const timestamp = Number.isNaN(updated.getTime()) ? "" : ` · 更新于 ${updated.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    stagesNode.textContent = (taskNames[run.task_kind] ? "已处理不等于全部成功，异常项单独统计" : stages.map(key => `${DAILY_STAGE_NAMES[key]}：${DAILY_STATUS_NAMES[run.stages?.[key]] || "待执行"}`).join(" · ")) + timestamp;
    for (const action of ["pause", "cancel"]) {
      if (!run.run_id || !(run.actions?.includes(action) || run[`can_${action}`])) continue;
      const button = element("button", "button button--ghost", action === "pause" ? "暂停" : "取消任务");
      button.type = "button";
      button.addEventListener("click", () => void controlBackgroundTask(run, action, button));
      actions.appendChild(button);
    }
  }

  async function controlBackgroundTask(run, action, button) {
    if (action === "cancel" && !window.confirm("取消当前任务？已保存的数据会保留；正在处理的步骤会安全停止。")) return;
    if (button) button.disabled = true;
    try {
      const result = await api(`/api/local-ui/tasks/${encodeURIComponent(run.run_id)}/control`, {
        method: "POST", headers: authHeaders("控制后台任务", { "Content-Type": "application/json" }),
        body: JSON.stringify({ task_kind: run.task_kind || "daily", action }),
      });
      if (result?.success === false) throw new Error(result.error_message || result.message || "暂时无法操作该任务");
      showToast(action === "pause" ? "已请求安全暂停" : "已请求安全取消", "success");
      await refreshDailyProgress();
    } catch (error) { showToast(error.message, "error"); }
    finally { if (button) button.disabled = false; }
  }

  let taskProgressRequestId = 0;
  let taskProgressController = null;
  async function refreshDailyProgress() {
    const requestId = ++taskProgressRequestId;
    taskProgressController?.abort();
    taskProgressController = new AbortController();
    try {
      const result = await api("/api/local-ui/tasks/progress", { headers: authHeaders("daily-progress"), signal: taskProgressController.signal });
      if (requestId !== taskProgressRequestId) return;
      renderDailyProgress(result);
      // Approval proposals may arrive after the assistant has ended its reply.
      try {
        const approvals = await api("/api/approvals");
        if (requestId !== taskProgressRequestId) return;
        state.approvals = normalizeApprovals(approvals);
        renderMailBindingApprovals();
      } catch (_) { /* A failed approval refresh never hides real task progress. */ }
    } catch (error) {
      if (requestId !== taskProgressRequestId || error.name === "AbortError") return;
      const card = $("assistant-task-progress");
      if (card && !card.hidden) setText("assistant-task-progress-state", "暂时无法刷新");
    }
  }
  const setText = (id, value) => { const node = $(id); if (node) node.textContent = text(value); };
  const clear = (node) => { while (node?.firstChild) node.removeChild(node.firstChild); };
  const SAFE_LINK_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);


  function appendInlineMarkdown(node, value) {
    const source = text(value, "");
    const pattern = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\))/g;
    let cursor = 0;
    for (const match of source.matchAll(pattern)) {
      if (match.index > cursor) node.appendChild(document.createTextNode(source.slice(cursor, match.index)));
      if (match[2] !== undefined) {
        const strong = document.createElement("strong");
        strong.textContent = match[2];
        node.appendChild(strong);
      } else if (match[3] !== undefined) {
        const code = document.createElement("code");
        code.textContent = match[3];
        node.appendChild(code);
      } else {
        let target = null;
        try { target = new URL(match[5], window.location.href); } catch (_) { target = null; }
        if (target && SAFE_LINK_PROTOCOLS.has(target.protocol)) {
          const link = document.createElement("a");
          link.textContent = match[4];
          link.href = target.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          node.appendChild(link);
        } else {
          node.appendChild(document.createTextNode(match[0]));
        }
      }
      cursor = match.index + match[0].length;
    }
    if (cursor < source.length) node.appendChild(document.createTextNode(source.slice(cursor)));
  }

  function splitMarkdownTableRow(value) {
    let source = text(value, "").trim();
    if (source.startsWith("|")) source = source.slice(1);
    if (source.endsWith("|")) source = source.slice(0, -1);
    const cells = [];
    let current = "";
    for (let index = 0; index < source.length; index += 1) {
      const char = source[index];
      if (char === "\\" && source[index + 1] === "|") {
        current += "|";
        index += 1;
      } else if (char === "|") {
        cells.push(current.trim());
        current = "";
      } else {
        current += char;
      }
    }
    cells.push(current.trim());
    return cells;
  }

  function markdownTableAlignment(value) {
    const cell = text(value, "").trim();
    if (/^:-{3,}:$/.test(cell)) return "center";
    if (/^-{3,}:$/.test(cell)) return "right";
    return "left";
  }

  function isMarkdownTable(lines, index) {
    if (index + 1 >= lines.length || !lines[index].includes("|")) return false;
    const headers = splitMarkdownTableRow(lines[index]);
    const separators = splitMarkdownTableRow(lines[index + 1]);
    return headers.length > 1
      && headers.length === separators.length
      && separators.every((cell) => /^:?-{3,}:?$/.test(cell));
  }

  function appendMarkdownTable(node, lines, startIndex) {
    const headers = splitMarkdownTableRow(lines[startIndex]);
    const separators = splitMarkdownTableRow(lines[startIndex + 1]);
    const wrapper = document.createElement("div");
    wrapper.className = "message-table-wrap";
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    headers.forEach((cell, index) => {
      const element = document.createElement("th");
      element.style.textAlign = markdownTableAlignment(separators[index]);
      appendInlineMarkdown(element, cell);
      headRow.appendChild(element);
    });
    head.appendChild(headRow);
    table.appendChild(head);

    const body = document.createElement("tbody");
    let index = startIndex + 2;
    while (index < lines.length && lines[index].trim() && lines[index].includes("|")) {
      const row = document.createElement("tr");
      const cells = splitMarkdownTableRow(lines[index]);
      headers.forEach((_, cellIndex) => {
        const element = document.createElement("td");
        element.style.textAlign = markdownTableAlignment(separators[cellIndex]);
        appendInlineMarkdown(element, cells[cellIndex] || "");
        row.appendChild(element);
      });
      body.appendChild(row);
      index += 1;
    }
    table.appendChild(body);
    wrapper.appendChild(table);
    node.appendChild(wrapper);
    return index - 1;
  }

  function renderMarkdown(node, value) {
    clear(node);
    const lines = text(value, "").replace(/\r\n?/g, "\n").split("\n");
    let paragraph = [];
    let list = null;
    let listType = "";
    let codeBlock = null;

    const flushParagraph = () => {
      if (!paragraph.length) return;
      const element = document.createElement("p");
      appendInlineMarkdown(element, paragraph.join("\n"));
      node.appendChild(element);
      paragraph = [];
    };
    const resetList = () => { list = null; listType = ""; };

    for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
      const line = lines[lineIndex];
      if (line.trim().startsWith("```")) {
        flushParagraph(); resetList();
        if (codeBlock) {
          node.appendChild(codeBlock);
          codeBlock = null;
        } else {
          codeBlock = document.createElement("pre");
          codeBlock.appendChild(document.createElement("code"));
        }
        continue;
      }
      if (codeBlock) {
        const code = codeBlock.firstChild;
        code.textContent += `${code.textContent ? "\n" : ""}${line}`;
        continue;
      }
      if (!line.trim()) {
        flushParagraph(); resetList();
        continue;
      }
      if (isMarkdownTable(lines, lineIndex)) {
        flushParagraph(); resetList();
        lineIndex = appendMarkdownTable(node, lines, lineIndex);
        continue;
      }
      const heading = line.match(/^(#{1,4})\s+(.+)$/);
      if (heading) {
        flushParagraph(); resetList();
        const element = document.createElement(`h${Math.min(heading[1].length + 2, 6)}`);
        appendInlineMarkdown(element, heading[2]);
        node.appendChild(element);
        continue;
      }
      if (/^\s*(?:---+|___+)\s*$/.test(line)) {
        flushParagraph(); resetList(); node.appendChild(document.createElement("hr"));
        continue;
      }
      const unordered = line.match(/^\s*[-+*]\s+(.+)$/);
      const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (unordered || ordered) {
        flushParagraph();
        const nextType = ordered ? "ol" : "ul";
        if (!list || listType !== nextType) {
          list = document.createElement(nextType);
          listType = nextType;
          node.appendChild(list);
        }
        const item = document.createElement("li");
        appendInlineMarkdown(item, (ordered || unordered)[1]);
        list.appendChild(item);
        continue;
      }
      const quote = line.match(/^\s*>\s?(.+)$/);
      if (quote) {
        flushParagraph(); resetList();
        const element = document.createElement("blockquote");
        appendInlineMarkdown(element, quote[1]);
        node.appendChild(element);
        continue;
      }
      resetList();
      paragraph.push(line);
    }
    flushParagraph();
    if (codeBlock) node.appendChild(codeBlock);
  }
  const safeJson = (value) => {
    try { return JSON.stringify(value, null, 2); } catch (_) { return text(value); }
  };
  function persistConversation() {
    if (state.codexThreadId) writeStorage(STORAGE_KEYS.codexThread, state.codexThreadId);
    writeStorage(STORAGE_KEYS.recent, JSON.stringify({
      version: 1,
      codexThreadId: state.codexThreadId || null,
      messages: redactStorageValue(state.messages.slice(-60)),
      tasks: redactStorageValue(state.tasks.slice(-30)),
    }));
  }

  const compactResultValue = (value, depth = 0) => {
    if (depth > 3) return "[已折叠]";
    if (typeof value === "string") {
      return value.length > 1200 ? `${value.slice(0, 1200)}\n…（已截断 ${value.length - 1200} 字）` : value;
    }
    if (Array.isArray(value)) {
      const visible = value.slice(0, 12).map((item) => compactResultValue(item, depth + 1));
      if (value.length > visible.length) visible.push(`…（其余 ${value.length - visible.length} 项已折叠）`);
      return visible;
    }
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, compactResultValue(item, depth + 1)]));
    }
    return value;
  };
  const normalizeApprovals = (items) => (items || []).map((item) => (
    item?.token ? { ...item.token, preview: item.preview || null } : item
  ));

  function authHeaders(reason, extra = {}) {
    return { ...extra, "X-RecruitOps-Local-UI": "1" };
  }

  function showToast(message, kind = "info") {
    const region = $("toast-region");
    if (!region) return;
    const item = document.createElement("div");
    item.className = `toast toast--${kind}`;
    item.textContent = message;
    region.appendChild(item);
    window.setTimeout(() => item.remove(), 4200);
  }

  function setStatus(kind, message) {
    const target = $("global-status");
    if (!target) return;
    target.dataset.state = kind;
    const statusText = target.querySelector(".status-text");
    if (statusText) statusText.textContent = message;
  }

  function setAssistantStatus(message, kind = "ok") {
    const target = $("assistant-message-status");
    if (target) target.textContent = message;
    const intent = $("assistant-intent");
    if (!intent) return;
    const dot = intent.querySelector(".status-dot");
    if (dot) dot.className = `status-dot status-dot--${kind === "error" ? "error" : kind === "warn" ? "warn" : "ok"}`;
  }

  const CODEX_STATUS_KEYS = ["thread", "turn", "item", "tool", "progress", "error"];
  const CODEX_STATUS_LABELS = { thread: "会话", turn: "轮次", item: "步骤", tool: "工具", progress: "进度", error: "错误" };

  function setCodexEventStatus(key, value, kind = "") {
    if (!CODEX_STATUS_KEYS.includes(key)) return;
    const nextValue = text(value, "等待");
    state.codexEventState[key] = { value: nextValue, kind };
    const target = document.querySelector(`[data-codex-status="${key}"]`);
    if (!target) return;
    target.textContent = `${CODEX_STATUS_LABELS[key]} · ${nextValue}`;
    target.dataset.state = kind;
    target.title = nextValue;
  }

  function resetCodexEventStatuses() {
    state.codexEventState = {};
    setCodexEventStatus("thread", state.codexThreadId || "未绑定", state.codexThreadId ? "ok" : "warn");
    setCodexEventStatus("turn", "等待", "");
    setCodexEventStatus("item", "等待", "");
    setCodexEventStatus("tool", "未报告", "");
    setCodexEventStatus("progress", "等待", "");
    setCodexEventStatus("error", "无", "");
  }

  function assistantAvailability() {
    const status = state.assistantConfiguration?.status;
    const messages = {
      missing_model: "尚未配置模型连接。请填写服务地址、模型名称和 API 密钥并保存；无需先上传简历或填写岗位关键词。",
      disabled: "求职助理已在高级配置中关闭。可在模型连接的高级设置中改为随模型启用。",
      restart_required: "模型配置已保存，重启桌面后生效。请先保存当前工作；不会自动重启。",
    };
    if (messages[status]) return {status, message: messages[status]};
    if (state.codexEnabled === true && state.codexReady === true) return {status: "ready", message: "求职助理已就绪"};
    if (["failed", "error", "unavailable"].includes(state.codexHealth?.state)) {
      return {status: "runtime_failed", message: "本地助理服务连接失败。请重新检查；若仍失败，请查看桌面服务诊断。无需重复填写简历或模型密钥。"};
    }
    return {status: "checking", message: "正在等待助理配置和本地服务就绪，可重新检查或查看模型连接。"};
  }

  function codexUnavailableMessage() {
    return assistantAvailability().message;
  }

  async function refreshAssistantAvailability() {
    const requestId = ++state.assistantHealthRequestId;
    let health;
    try { health = await api("/api/codex/health"); }
    catch (_) { health = {enabled: false, ready: false, state: "unavailable"}; }
    if (requestId !== state.assistantHealthRequestId) return;
    state.codexHealth = health;
    state.codexEnabled = health?.enabled === true;
    state.codexReady = state.codexEnabled && health?.ready === true;
    renderCodexRuntimeStatus();
    if (!state.messages.length) renderConversation();
  }

  function setCodexControlsEnabled(enabled) {
    [
      "assistant-message",
      "run-task-button",
      "new-conversation-button",
      "clear-conversation-button",
    ].forEach((id) => {
      const control = $(id);
      if (control) control.disabled = !enabled;
    });
  }

  function renderCodexRuntimeStatus() {
    const strip = $("assistant-codex-event-strip");
    const contextPolicy = $("assistant-context-policy");
    const diagnostic = assistantAvailability();
    const available = diagnostic.status === "ready";
    // Raw thread, turn, item and tool identifiers remain available in task traces.
    // They are diagnostic metadata, not useful status for ordinary assistant users.
    if (strip) strip.hidden = true;
    if ($("assistant-thread-label")) $("assistant-thread-label").hidden = !available;
    const availability = $("assistant-availability");
    if (availability) { availability.hidden = available; availability.dataset.state = diagnostic.status; }
    setText("assistant-availability-detail", diagnostic.message);
    setText("assistant-open-configuration", diagnostic.status === "missing_model" ? "配置模型连接" : "查看模型配置");
    if (contextPolicy) contextPolicy.hidden = !available;
    if (contextPolicy) {
      const context = state.codexHealth?.context_management || {};
      const compactLimit = Number(context.auto_compact_token_limit);
      const contextWindow = Number(context.context_window_tokens);
      const compactLabel = Number.isFinite(compactLimit) && compactLimit > 0
        ? `${Math.round(compactLimit / 1000)}k 自动压缩`
        : "上下文压缩已启用";
      contextPolicy.textContent = compactLabel;
      contextPolicy.title = Number.isFinite(compactLimit) && compactLimit > 0 && Number.isFinite(contextWindow) && contextWindow > 0
        ? `长对话达到约 ${compactLimit.toLocaleString("zh-CN")} tokens 后由 Codex Harness 自动压缩；模型上下文窗口约 ${contextWindow.toLocaleString("zh-CN")} tokens。`
        : "长对话达到阈值后由 Codex Harness 自动压缩。";
      contextPolicy.dataset.state = available ? "ready" : "unavailable";
    }
    setCodexControlsEnabled(available);
    if (!available) {
      const detail = codexUnavailableMessage();
      setCodexEventStatus("thread", detail, "warn");
      setCodexEventStatus("error", detail, "error");
      setAssistantStatus(({missing_model: "待配置模型", disabled: "助理已关闭", restart_required: "配置待重启", runtime_failed: "本地服务异常"})[diagnostic.status] || "正在检查助理", "error");
      return;
    }
    setAssistantStatus("助理已就绪", "success");
    if (!Object.keys(state.codexEventState).length) resetCodexEventStatuses();
    if (!state.codexReady) {
      setCodexEventStatus("thread", state.codexHealth?.detail || "运行时未就绪", "warn");
      setCodexEventStatus("error", state.codexHealth?.detail || "运行时未就绪", "warn");
    }
  }

  function codexThreadPayload(response) {
    if (response?.thread && typeof response.thread === "object") return response.thread;
    return response && typeof response === "object" ? response : {};
  }

  function codexThreadIdOf(value) {
    if (typeof value === "string") return value.trim();
    return text(value?.id || value?.thread_id || value?.threadId, "").trim();
  }

  function codexTimestamp(value) {
    if (value === null || value === undefined || value === "") return new Date().toISOString();
    const numeric = Number(value);
    if (Number.isFinite(numeric)) {
      const date = new Date(numeric < 1e12 ? numeric * 1000 : numeric);
      if (!Number.isNaN(date.getTime())) return date.toISOString();
    }
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? new Date().toISOString() : date.toISOString();
  }

  function codexContentText(value) {
    if (typeof value === "string") return value;
    if (Array.isArray(value)) {
      return value.map((part) => {
        if (typeof part === "string") return part;
        if (!part || typeof part !== "object") return "";
        return codexContentText(part.text ?? part.content ?? part.value);
      }).filter(Boolean).join("");
    }
    if (value && typeof value === "object") return codexContentText(value.text ?? value.content ?? value.value);
    return "";
  }

  function codexHistoryMessages(thread) {
    const threadId = codexThreadIdOf(thread) || state.codexThreadId || "thread";
    const records = [];
    const add = (role, body, key, timestamp) => {
      const normalizedBody = codexContentText(body).trim();
      if (!normalizedBody) return;
      records.push({
        id: `codex-${threadId}-${key}`,
        role,
        body: normalizedBody,
        created_at: codexTimestamp(timestamp),
        task_id: null,
        result: null,
        streaming: false,
      });
    };
    const turns = Array.isArray(thread?.turns) ? thread.turns : [];
    turns.forEach((turn, turnIndex) => {
      const turnId = text(turn?.id, `turn-${turnIndex}`);
      const timestamp = turn?.createdAt ?? turn?.created_at ?? thread?.updatedAt ?? thread?.updated_at;
      (Array.isArray(turn?.items) ? turn.items : []).forEach((item, itemIndex) => {
        const kind = text(item?.type, "").replace(/[-_]/g, "").toLowerCase();
        const itemId = text(item?.id, `item-${itemIndex}`);
        if (kind === "usermessage" || kind === "user") {
          add("user", item.content ?? item.text, `${turnId}-${itemId}`, item?.createdAt ?? item?.created_at ?? timestamp);
        } else if (kind === "agentmessage" || kind === "assistant") {
          add("assistant", item.text ?? item.content, `${turnId}-${itemId}`, item?.createdAt ?? item?.created_at ?? timestamp);
        }
      });
    });
    if (!records.length && Array.isArray(thread?.messages)) {
      thread.messages.forEach((message, index) => {
        const role = text(message?.role, "").toLowerCase() === "user" ? "user" : "assistant";
        add(role, message?.body ?? message?.text ?? message?.content, `message-${text(message?.id, index)}`, message?.createdAt ?? message?.created_at);
      });
    }
    return records;
  }

  async function createCodexThread() {
    const response = await api("/api/codex/threads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const thread = codexThreadPayload(response);
    const threadId = codexThreadIdOf(thread);
    if (!threadId) throw new Error("Codex 创建会话后没有返回 thread id。");
    state.codexThreadId = threadId;
    state.codexThreadMetadata = thread;
    state.codexHistoryError = "";
    writeStorage(STORAGE_KEYS.codexThread, threadId);
    resetCodexEventStatuses();
    persistConversation();
    return threadId;
  }

  async function initializeCodex(health) {
    state.codexHealth = health || {};
    state.codexEnabled = health?.enabled === true;
    state.codexReady = state.codexEnabled && health?.ready === true;
    const storedThreadId = readStorage(STORAGE_KEYS.codexThread).trim() || state.codexThreadId;
    state.codexThreadId = storedThreadId;
    state.codexThreadMetadata = null;
    if (!state.codexReady) {
      renderCodexRuntimeStatus();
      return;
    }
    if (storedThreadId) {
      state.codexThreadId = storedThreadId;
      resetCodexEventStatuses();
    } else {
      await createCodexThread();
    }
    renderCodexRuntimeStatus();
  }

  function empty(node, title, detail) {
    if (!node) return;
    clear(node); node.dataset.state = "empty";
    const box = document.createElement("div"); box.className = "empty-state empty-state--inline";
    const strong = document.createElement("strong"); strong.textContent = title;
    const small = document.createElement("span"); small.textContent = detail;
    box.append(strong, small); node.appendChild(box);
  }

  function errorState(node, title, detail) {
    if (!node) return;
    clear(node); node.dataset.state = "error";
    const box = document.createElement("div"); box.className = "error-state";
    const strong = document.createElement("strong"); strong.textContent = title;
    const small = document.createElement("span"); small.textContent = detail;
    box.append(strong, small); node.appendChild(box);
  }

  function loading(node) {
    if (!node) return;
    clear(node); node.dataset.state = "loading";
    const rows = document.createElement("div"); rows.className = "loading-rows";
    for (let i = 0; i < 3; i += 1) rows.appendChild(document.createElement("span"));
    node.appendChild(rows);
  }

  function row(label, value, href = null) {
    const item = document.createElement("div"); item.className = "data-row";
    const left = document.createElement("span"); left.className = "row-time"; left.textContent = label;
    const middle = document.createElement("span"); middle.className = "row-title"; middle.textContent = text(value);
    item.append(left, middle);
    if (href) {
      const link = document.createElement("a"); link.className = "row-link"; link.textContent = "查看";
      link.href = href; link.target = "_blank"; link.rel = "noreferrer"; item.appendChild(link);
    }
    return item;
  }

  function renderSchedule(events) {
    const node = $("today-schedule"); clear(node);
    if (!events.length) return empty(node, "今天没有已记录安排", "日程会从投递记录中读取。");
    events.slice(0, 6).forEach((event) => {
      const time = scheduleTimeLabel(event);
      const title = `${text(event.event_type)} · ${text(event.company_name)} · ${text(event.job_title)}`;
      const location = text(event.location_or_link, "").trim();
      const href = safeScheduleHref(location);
      node.appendChild(row(time, location && !href ? `${title} · ${location}` : title, href));
    });
    node.dataset.state = "ready";
  }

  function batchLabel(batch) {
    return { formal: "正式批", early: "提前批" }[batch] || text(batch, "批次待确认");
  }

  function createJobAction(label, action, job) {
    const button = document.createElement("button");
    button.className = `button job-action job-action--${action}`;
    button.type = "button";
    button.textContent = label;
    button.dataset.jobAction = action;
    button.dataset.jobId = text(job.id, "");
    button.setAttribute("aria-label", `${label}：${text(job.title)}`);
    return button;
  }

  function visibleJobs(items) {
    const excluded = new Set(["excluded", "direction_out", "doctorate_only", "internship", "cohort_unconfirmed"]);
    return Array.isArray(items) ? items.filter((job) => job && !excluded.has(job.analysis_status)) : [];
  }

  function renderJobs(items) {
    const node = $("today-new-jobs"); clear(node);
    state.jobs = visibleJobs(items);
    if (!state.jobs.length) return empty(node, "今天没有新的确认岗位", "只显示已确认的 2027 校招岗位。");

    state.jobs.slice(0, 8).forEach((job) => {
      const card = document.createElement("article"); card.className = "job-card";
      const heading = document.createElement("div"); heading.className = "job-card-heading";
      const title = document.createElement("h4"); title.className = "job-card-title"; title.textContent = text(job.title);
      const company = document.createElement("span"); company.className = "job-card-company"; company.textContent = text(job.company_id);
      heading.append(title, company);

      const meta = document.createElement("div"); meta.className = "job-card-meta";
      [text(job.city, "地点待确认"), batchLabel(job.batch), `匹配 ${text(job.match_score, "未评估")}${job.match_score == null ? "" : " 分"}`].forEach((value) => {
        const item = document.createElement("span"); item.textContent = value; meta.appendChild(item);
      });

      const details = document.createElement("div"); details.className = "job-card-details";
      const id = document.createElement("span"); id.textContent = `岗位 ID：${text(job.id)}`;
      details.appendChild(id);
      if (job.detail_url) {
        const link = document.createElement("a"); link.className = "job-detail-link"; link.textContent = "岗位详情";
        link.href = job.detail_url; link.target = "_blank"; link.rel = "noreferrer"; details.appendChild(link);
      }

      const actions = document.createElement("div"); actions.className = "job-card-actions";
      actions.append(
        createJobAction("询问助理", "ask", job),
        createJobAction("准备投递", "prepare", job),
      );
      card.append(heading, meta, details, actions); node.appendChild(card);
    });
    node.dataset.state = "ready";
  }

  function element(tag, className = "", value = "") {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== "") node.textContent = text(value, "");
    return node;
  }

  function appendUiIcon(node, name) {
    const paths = {
      external: "M14 3h7v7M21 3 10 14M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5",
      more: "M5 12h.01M12 12h.01M19 12h.01",
      close: "m6 6 12 12M18 6 6 18",
      applied: "M4 7h16v13H4zM9 7V4h6v3M4 12h16M10 12v3h4v-3",
      written: "M8 4H5v17h14V4h-3M8 3h8v4H8zM8 12h8M8 16h5",
      interview: "M4 4h16v12H9l-5 4zM8 8h8M8 12h5",
      offer: "m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9z",
      list: "M5 6h14M5 12h14M5 18h14",
      calendar: "M3 5h18v16H3zM8 3v4M16 3v4M3 10h18",
      plus: "M12 5v14M5 12h14",
      check: "M5 12l4 4L19 6",
      ignore: "M7 7l10 10M17 7 7 17",
      restore: "M4 12a8 8 0 1 0 2.3-5.7L4 8.6M4 4v4.6h4.6",
      edit: "m4 16.5-.5 3.5 3.5-.5L18.7 7.3a2.4 2.4 0 0 0-3.4-3.4zM13.8 5.8l3.4 3.4",
      mail: "M3 5h18v14H3zM3 7l9 6 9-6",
      "chevron-left": "m15 18-6-6 6-6",
      "chevron-right": "m9 18 6-6-6-6",
    };
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    Object.entries({ class: "ui-icon", viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": "1.75", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true", focusable: "false" }).forEach(([key, value]) => svg.setAttribute(key, value));
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", paths[name]);
    svg.appendChild(path);
    node.appendChild(svg);
    return node;
  }

  function scoreBadge(score, status = null) {
    if (status && status !== "complete") score = null;
    const label = element("span", "score-badge");
    if (score == null) {
      label.classList.add("score-badge--empty");
      const labels = {
        eligible: "待评分", jd_incomplete: "待补全 JD", direction_out: "方向不符",
        doctorate_only: "博士限定", internship: "实习岗位", cohort_unconfirmed: "届别待核查",
        master_only: "硕士限定",
        not_target_track: "非目标批次",
        failed: "评分失败", refused: "未能评分",
      };
      label.textContent = labels[status] || "未评分";
      return label;
    }
    label.textContent = String(score);
    label.classList.add(score >= 80 ? "score-badge--high" : score >= 60 ? "score-badge--medium" : "score-badge--low");
    return label;
  }

  function applicationStageLabel(stage) {
    return APPLICATION_STAGE_LABELS[stage] || text(stage, "未投递");
  }

  function compactDate(value, fallback = "—") {
    if (!value) return fallback;
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return fallback;
    return parsed.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
  }

  const SCHEDULE_STATUS_LABELS = {
    pending: "待处理",
    completed: "已完成",
    ignored: "已忽略",
  };

  const SCHEDULE_TIME_KIND_LABELS = {
    appointment: "约定事项",
    deadline: "截止提醒",
    unspecified: "时间未定",
  };

  const SCHEDULE_DUE_GROUPS = [
    { key: "overdue", label: "逾期" },
    { key: "soon", label: "即将到期" },
    { key: "other", label: "其他" },
  ];

  function normalizeScheduleStatus(value) {
    return Object.prototype.hasOwnProperty.call(SCHEDULE_STATUS_LABELS, value) ? value : "pending";
  }

  function normalizeScheduleTimeKind(value) {
    return Object.prototype.hasOwnProperty.call(SCHEDULE_TIME_KIND_LABELS, value) ? value : "appointment";
  }

  function parseScheduleDate(value) {
    const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (!match) return null;
    const parsed = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    return parsed.getFullYear() === Number(match[1])
      && parsed.getMonth() === Number(match[2]) - 1
      && parsed.getDate() === Number(match[3])
      ? parsed
      : null;
  }

  function scheduleDateKey(value) {
    const parsed = value instanceof Date ? value : parseScheduleDate(value);
    if (!parsed) return "";
    const pad = (part) => String(part).padStart(2, "0");
    return `${parsed.getFullYear()}-${pad(parsed.getMonth() + 1)}-${pad(parsed.getDate())}`;
  }

  function scheduleMonthKey(value) {
    if (value instanceof Date && !Number.isNaN(value.getTime())) {
      return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}`;
    }
    const match = String(value || today).match(/^(\d{4})-(\d{2})/);
    if (!match) return today.slice(0, 7);
    return `${match[1]}-${match[2]}`;
  }

  function scheduleMonthStart(value = state.scheduleMonth || today) {
    const [year, month] = scheduleMonthKey(value).split("-").map(Number);
    return new Date(year, month - 1, 1);
  }

  function addScheduleDays(value, amount) {
    const parsed = parseScheduleDate(value) || new Date();
    parsed.setDate(parsed.getDate() + amount);
    return scheduleDateKey(parsed);
  }

  function scheduleDateLabel(value, fallback = "日期待定") {
    const parsed = parseScheduleDate(value);
    if (!parsed) return fallback;
    return parsed.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
  }

  function scheduleEventTitle(event = {}) {
    return text(event.title || [event.company_name, event.event_type].filter(Boolean).join(" · "), "未命名事项");
  }

  function scheduleTimeLabel(event = {}) {
    if (event.event_time) return text(event.event_time, "").slice(0, 5);
    if (!event.event_date) return "时间待定";
    if (normalizeScheduleTimeKind(event.time_kind) === "deadline") return "截止提醒";
    return "时间待定";
  }

  function scheduleCalendarTimeLabel(event = {}) {
    const eventTime = event.event_time ? text(event.event_time, "").slice(0, 5) : "";
    if (normalizeScheduleTimeKind(event.time_kind) === "deadline") return eventTime ? `截止 ${eventTime}` : "截止";
    return eventTime || "未定时段";
  }

  function scheduleTimeSeconds(value) {
    const match = text(value, "").trim().match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?$/);
    if (!match) return null;
    const hours = Number(match[1]);
    const minutes = Number(match[2]);
    const seconds = Number(match[3] || 0);
    if (hours > 23 || minutes > 59 || seconds > 59) return null;
    return hours * 3600 + minutes * 60 + seconds;
  }

  function scheduleSortValue(event = {}) {
    return `${event.event_date || "9999-99-99"}T${event.event_time || "99:99:99"}T${text(event.id, "")}`;
  }

  function scheduleDueGroup(event = {}, now = new Date()) {
    const eventDate = event.event_date;
    if (normalizeScheduleStatus(event.status) !== "pending" || !eventDate) return "other";
    const currentDate = localDate(now);
    if (eventDate < currentDate) return "overdue";
    if (eventDate === currentDate && event.event_time) {
      const eventSeconds = scheduleTimeSeconds(event.event_time);
      const currentSeconds = now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds();
      if (eventSeconds !== null && eventSeconds < currentSeconds) return "overdue";
    }
    if (eventDate <= addScheduleDays(currentDate, 7)) return "soon";
    return "other";
  }

  function scheduleEventsForCurrentFilter() {
    const status = state.scheduleStatus || $("schedule-status-filter")?.value || "pending";
    const date = state.scheduleDateFilter || $("schedule-date-filter")?.value || "";
    return state.allSchedules
      .filter((event) => status === "all" || normalizeScheduleStatus(event.status) === status)
      .filter((event) => !date || event.event_date === date)
      .slice()
      .sort((left, right) => scheduleSortValue(left).localeCompare(scheduleSortValue(right)));
  }

  function replaceSelectOptions(select, options, placeholder) {
    if (!select) return;
    const selected = select.value;
    clear(select);
    const initial = document.createElement("option");
    initial.value = "";
    initial.textContent = placeholder;
    select.appendChild(initial);
    options.forEach(({ value, label }) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    });
    if (options.some((option) => option.value === selected)) select.value = selected;
  }

  function renderJobFacets(facets = {}) {
    state.jobFacets = {
      companies: Array.isArray(facets.companies) ? facets.companies : [],
      categories: facets.categories && typeof facets.categories === "object" ? facets.categories : {},
      platforms: Array.isArray(facets.platforms) ? facets.platforms : [],
    };
    replaceSelectOptions(
      $("job-company-filter"),
      state.jobFacets.companies.map((company) => ({ value: company.key, label: `${company.name} (${company.job_count})` })),
      "全部公司",
    );
    replaceSelectOptions(
      $("job-category-filter"),
      Object.entries(state.jobFacets.categories).map(([value, label]) => ({ value, label })),
      "全部岗位类型",
    );
    replaceSelectOptions(
      $("job-platform-filter"),
      state.jobFacets.platforms.map((value) => ({ value, label: value })),
      "全部平台",
    );
    state.companySummaries = state.jobFacets.companies;
    renderCompanyRanking();
  }

  async function loadCompanyRanking() {
    try {
      const payload = await api("/api/jobs/browse?limit=1&offset=0&sort=score");
      renderJobFacets(payload.facets);
    } catch (error) {
      state.companySummaries = [];
      const body = $("company-ranking-body");
      clear(body);
      const row = document.createElement("tr");
      const cell = element("td", "table-empty", `公司排行加载失败：${error.message}`);
      cell.colSpan = 6;
      row.appendChild(cell);
      body.appendChild(row);
      setText("company-ranking-count", "加载失败");
    }
  }

  function jobBrowseQuery() {
    const params = new URLSearchParams({
      limit: String(state.jobBrowse.pageSize),
      offset: String((state.jobBrowse.page - 1) * state.jobBrowse.pageSize),
      sort: $("job-sort")?.value || "score",
    });
    if (state.jobBrowse.mode === "today") params.set("first_seen_on", today);
    const fields = [
      ["query", $("job-search")?.value.trim()],
      ["company", $("job-company-filter")?.value],
      ["category", $("job-category-filter")?.value],
      ["platform", $("job-platform-filter")?.value],
      ["score_band", $("job-score-filter")?.value],
    ];
    fields.forEach(([key, value]) => { if (value) params.set(key, value); });
    const evaluation = $("job-evaluation-filter")?.value;
    if (["scored", "unscored", "pending", "jd_incomplete"].includes(evaluation)) params.set("evaluation", evaluation);
    return params;
  }

  function renderJobStats(stats = {}) {
    setText("jobs-total", stats.jobs ?? 0);
    setText("jobs-company-total", stats.companies ?? 0);
    setText("jobs-high-total", stats.high_match ?? 0);
    setText("jobs-unscored-total", stats.pending ?? 0);
    setText("jobs-unscored-detail", `待补全 JD ${stats.jd_incomplete ?? 0}`);
    if (state.jobBrowse.mode === "today") {
      state.jobBrowse.todayTotal = stats.jobs ?? 0;
      setText("nav-today-count", state.jobBrowse.todayTotal);
    } else {
      state.jobBrowse.allTotal = stats.jobs ?? 0;
      setText("nav-job-count", state.jobBrowse.allTotal);
    }
  }

  function renderJobModePresentation() {
    const isToday = state.jobBrowse.mode === "today";
    setText("jobs-heading", isToday ? "今日新增岗位" : "27届校招岗位");
    setText(
      "jobs-page-description",
      isToday ? "今天首次发现并确认的 27 届正式批和提前批岗位。" : "已确认的正式批和提前批岗位，高匹配岗位优先展示。",
    );
  }

  function createJobTableRow(job) {
    const tr = document.createElement("tr");
    const companyCell = document.createElement("td");
    const companyButton = element("button", "table-link", job.company_name);
    companyButton.type = "button";
    companyButton.dataset.companyJobs = job.organization_id || job.company_id;
    companyCell.appendChild(companyButton);

    const titleCell = document.createElement("td");
    const titleButton = element("button", "job-title-button", job.title);
    titleButton.type = "button";
    titleButton.dataset.jobDetails = job.id;
    titleCell.appendChild(titleButton);
    const titleMeta = element("div", "job-table-meta");
    const category = element("span", "table-tag", job.category_label);
    titleMeta.appendChild(category);
    (job.matched_directions || []).slice(0, 2).forEach((direction) => titleMeta.appendChild(element("span", "table-tag table-tag--muted", direction)));
    if (job.application_stage) titleMeta.appendChild(element("span", "application-badge", applicationStageLabel(job.application_stage)));
    titleCell.appendChild(titleMeta);

    const scoreCell = document.createElement("td");
    scoreCell.appendChild(scoreBadge(job.match_score, job.analysis_status));
    const cityCell = element("td", "cell-muted", job.city || "地点待确认");
    const platformCell = document.createElement("td");
    platformCell.appendChild(element("span", "platform-badge", job.platform));
    const actionCell = document.createElement("td");
    actionCell.className = "job-table-actions";
    const actionGroup = element("div", "job-table-action-group");
    const detailButton = appendUiIcon(element("button", "icon-button icon-button--small"), "external");
    detailButton.type = "button";
    detailButton.dataset.jobDetails = job.id;
    detailButton.title = "查看岗位详情";
    detailButton.setAttribute("aria-label", `查看岗位详情：${job.title}`);
    const askButton = element("button", "button button--ghost button--small", "询问助理");
    askButton.type = "button";
    askButton.dataset.jobAction = "ask";
    askButton.dataset.jobId = job.id;
    const recordButton = element("button", "button button--secondary button--small", job.application_stage ? "查看投递" : "记录投递");
    recordButton.type = "button";
    recordButton.dataset.jobAction = job.application_stage ? "application" : "record";
    recordButton.dataset.jobId = job.id;
    actionGroup.append(detailButton, askButton, recordButton);
    actionCell.appendChild(actionGroup);
    tr.append(companyCell, titleCell, scoreCell, cityCell, platformCell, actionCell);
    return tr;
  }

  function renderJobTable(payload = {}) {
    state.jobBrowseItems = visibleJobs(payload.items);
    state.jobBrowse.total = payload.total || 0;
    const body = $("jobs-table-body");
    clear(body);
    if (!state.jobBrowseItems.length) {
      const row = document.createElement("tr");
      const cell = element("td", "table-empty", "没有符合当前条件的岗位");
      cell.colSpan = 6;
      row.appendChild(cell);
      body.appendChild(row);
    } else {
      state.jobBrowseItems.forEach((job) => body.appendChild(createJobTableRow(job)));
    }
    const pages = Math.max(1, Math.ceil(state.jobBrowse.total / state.jobBrowse.pageSize));
    if (state.jobBrowse.page > pages) state.jobBrowse.page = pages;
    setText("jobs-result-count", `${state.jobBrowse.total} 个岗位`);
    setText("jobs-page-info", `第 ${state.jobBrowse.page} / ${pages} 页 · 共 ${state.jobBrowse.total} 条`);
    $("jobs-prev-button").disabled = state.jobBrowse.page <= 1;
    $("jobs-next-button").disabled = state.jobBrowse.page >= pages;
    $("jobs-table-wrap").dataset.state = "ready";
  }

  function renderFeaturedJobs(items = []) {
    const node = $("featured-job-list");
    clear(node);
    state.featuredJobs = visibleJobs(items);
    setText("featured-jobs-count", `${state.featuredJobs.length} 个岗位`);
    if (!state.featuredJobs.length) return empty(node, "暂无高匹配岗位", "当前范围内没有 70 分及以上的岗位。");
    state.featuredJobs.forEach((job) => {
      const card = document.createElement("article");
      card.className = "featured-job-card";
      const header = element("div", "featured-job-header");
      const identity = element("div", "featured-job-identity");
      const company = element("button", "table-link", job.company_name);
      company.type = "button";
      company.dataset.companyJobs = job.organization_id || job.company_id;
      const title = element("h4", "featured-job-title", job.title);
      const meta = element("div", "featured-job-meta", `${job.city || "地点待确认"} · ${job.category_label} · ${job.platform}`);
      identity.append(company, title, meta);
      const score = scoreBadge(job.match_score, job.analysis_status);
      score.classList.add("score-badge--large");
      header.append(identity, score);
      const summary = element("p", "featured-job-summary", job.summary || "该岗位尚无分析摘要，可打开详情查看完整 JD。");
      const comparison = element("div", "featured-job-comparison");
      const advantages = element("div", "job-analysis-block");
      advantages.appendChild(element("strong", "analysis-block-title analysis-block-title--good", "我的优势"));
      const advantageList = document.createElement("ul");
      (job.advantages || []).slice(0, 3).forEach((value) => advantageList.appendChild(element("li", "", value)));
      if (!advantageList.children.length) advantageList.appendChild(element("li", "is-muted", "暂无结构化优势"));
      advantages.appendChild(advantageList);
      const gaps = element("div", "job-analysis-block");
      gaps.appendChild(element("strong", "analysis-block-title analysis-block-title--warn", "需要补足"));
      const gapList = document.createElement("ul");
      (job.gaps || []).slice(0, 3).forEach((value) => gapList.appendChild(element("li", "", value)));
      if (!gapList.children.length) gapList.appendChild(element("li", "is-muted", "未发现明确短板"));
      gaps.appendChild(gapList);
      comparison.append(advantages, gaps);
      const actions = element("div", "featured-job-actions");
      const details = element("button", "button button--ghost", "查看详情");
      details.type = "button";
      details.dataset.jobDetails = job.id;
      const ask = element("button", "button button--ghost", "询问助理");
      ask.type = "button";
      ask.dataset.jobAction = "ask";
      ask.dataset.jobId = job.id;
      const apply = element("a", "button button--primary", "打开招聘页");
      apply.href = job.detail_url;
      apply.target = "_blank";
      apply.rel = "noreferrer";
      actions.append(details, ask, apply);
      if (job.application_stage) actions.prepend(element("span", "application-badge", applicationStageLabel(job.application_stage)));
      const analysisDisclosure = element("details", "featured-job-disclosure");
      analysisDisclosure.append(element("summary", "", "展开匹配分析 · 优势与不足"), summary, comparison);
      card.append(header, actions, analysisDisclosure);
      node.appendChild(card);
    });
    node.dataset.state = "ready";
  }

  async function loadJobBrowser({ refreshFeatured = false } = {}) {
    const requestId = state.jobBrowse.requestId + 1;
    state.jobBrowse.requestId = requestId;
    state.jobRequestController?.abort();
    const controller = new AbortController();
    state.jobRequestController = controller;
    $("jobs-table-wrap").dataset.state = "loading";
    try {
      const params = jobBrowseQuery();
      const includeSummary = refreshFeatured || state.jobSummaryMode !== state.jobBrowse.mode || Date.now() - state.jobSummaryAt > 15000;
      params.set("include_summary", String(includeSummary));
      const payload = await api(`/api/jobs/browse?${params.toString()}`, { signal: controller.signal });
      if (requestId !== state.jobBrowse.requestId) return;
      if (payload.stats) renderJobStats(payload.stats);
      if (state.jobBrowse.mode === "all" && payload.facets) renderJobFacets(payload.facets);
      if (payload.summary_included !== false && payload.stats) {
        state.jobSummaryMode = state.jobBrowse.mode;
        state.jobSummaryAt = Date.now();
      }
      renderJobTable(payload);
      if (payload.summary_included !== false && (refreshFeatured || !state.featuredJobs.length)) renderFeaturedJobs(payload.featured || []);
    } catch (error) {
      if (requestId !== state.jobBrowse.requestId || error.name === "AbortError") return;
      const body = $("jobs-table-body");
      clear(body);
      const row = document.createElement("tr");
      const cell = element("td", "table-empty", `岗位加载失败：${error.message}`);
      cell.colSpan = 6;
      row.appendChild(cell);
      body.appendChild(row);
      $("jobs-table-wrap").dataset.state = "error";
      if (refreshFeatured) empty($("featured-job-list"), "高匹配岗位加载失败", error.message);
      showToast(`岗位加载失败：${error.message}`, "error");
    }
  }

  function findBrowseJob(jobId) {
    return [...state.jobBrowseItems, ...state.featuredJobs, ...state.jobs]
      .find((item) => text(item.id, "") === text(jobId, ""));
  }

  function renderJobDetail(payload) {
    const job = payload.job || {};
    const analysis = payload.analysis || null;
    const browseJob = findBrowseJob(job.id) || {};
    setText("job-detail-company", browseJob.company_name || job.company_id || "岗位详情");
    setText("job-detail-title", job.title || "岗位详情");
    const content = $("job-detail-content");
    clear(content);

    const meta = element("div", "job-detail-meta");
    [job.city || "地点待确认", batchLabel(job.batch), browseJob.platform || "招聘官网", compactDate(job.first_seen_at, "发现日期待确认")]
      .forEach((value) => meta.appendChild(element("span", "table-tag", value)));
    meta.appendChild(scoreBadge(job.match_score ?? analysis?.match_score, analysis?.analysis_status));
    content.appendChild(meta);

    if (analysis) {
      const analysisSection = element("section", "job-detail-section");
      analysisSection.appendChild(element("h3", "", "岗位匹配"));
      if (analysis.summary) analysisSection.appendChild(element("p", "job-detail-summary", analysis.summary));
      const grid = element("div", "job-detail-analysis-grid");
      [["我的优势", analysis.advantages || [], "good"], ["需要补足", analysis.gaps || [], "warn"]].forEach(([label, values, tone]) => {
        const block = element("div", "job-analysis-block");
        block.appendChild(element("strong", `analysis-block-title analysis-block-title--${tone}`, label));
        const list = document.createElement("ul");
        values.forEach((value) => list.appendChild(element("li", "", value)));
        if (!list.children.length) list.appendChild(element("li", "is-muted", "暂无结构化信息"));
        block.appendChild(list);
        grid.appendChild(block);
      });
      analysisSection.appendChild(grid);
      content.appendChild(analysisSection);
    }

    const jdSection = element("section", "job-detail-section");
    jdSection.appendChild(element("h3", "", "岗位说明"));
    jdSection.appendChild(element("div", "job-jd", job.jd_raw || "招聘页暂未提供完整岗位说明。"));
    content.appendChild(jdSection);

    const actions = element("div", "job-detail-actions");
    const ask = element("button", "button button--ghost", "询问助理");
    ask.type = "button";
    ask.dataset.jobAction = "ask";
    ask.dataset.jobId = job.id;
    const record = element("button", "button button--secondary", browseJob.application_stage ? "查看投递" : "记录投递");
    record.type = "button";
    record.dataset.jobAction = browseJob.application_stage ? "application" : "record";
    record.dataset.jobId = job.id;
    const source = element("a", "button button--primary", "打开招聘页");
    source.href = job.detail_url;
    source.target = "_blank";
    source.rel = "noreferrer";
    actions.append(ask, record, source);
    content.appendChild(actions);
    content.dataset.state = "ready";
  }

  async function openJobDetail(jobId) {
    const dialog = $("job-detail-dialog");
    setText("job-detail-company", "岗位详情");
    setText("job-detail-title", "正在加载");
    loading($("job-detail-content"));
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    try {
      renderJobDetail(await api(`/api/jobs/${encodeURIComponent(jobId)}`));
    } catch (error) {
      empty($("job-detail-content"), "岗位详情加载失败", error.message);
    }
  }

  function renderCompanies(companies) {
    const node = $("company-list"); clear(node);
    if (!companies.length) return empty(node, "暂无公司配置", "请先同步主项目配置。");
    companies.slice(0, 80).forEach((company) => {
      const item = document.createElement("div"); item.className = "company-row";
      const main = document.createElement("div"); main.className = "company-row-main";
      const mark = document.createElement("span"); mark.className = `company-status-mark${company.integration_status === "connected" ? " is-connected" : ""}`;
      mark.textContent = company.integration_status === "connected" ? "✓" : "?"; mark.setAttribute("aria-hidden", "true");
      const name = company.campus_url ? document.createElement("a") : document.createElement("strong");
      name.className = "company-name"; name.textContent = text(company.name);
      if (company.campus_url) { name.href = company.campus_url; name.target = "_blank"; name.rel = "noreferrer"; }
      const meta = document.createElement("span"); meta.className = "company-meta"; meta.textContent = `${text(company.integration_status)} · ${text(company.crawler_key, "无 crawler")}`;
      main.append(mark, name, meta);
      item.appendChild(main); node.appendChild(item);
    });
    setText("company-list-count", `${companies.length} 家`);
    setText("connected-company-count", companies.filter((item) => item.integration_status === "connected").length);
    setText("unconnected-company-count", companies.filter((item) => item.integration_status !== "connected").length);
    setText("total-company-count", companies.length);
    node.dataset.state = "ready";
  }

  function sortedCompanySummaries() {
    const query = ($("company-search")?.value || "").trim().toLocaleLowerCase("zh-CN");
    const sort = $("company-sort")?.value || "score";
    const rows = state.companySummaries.filter((company) => !query || company.name.toLowerCase().includes(query));
    rows.sort((left, right) => {
      if (sort === "jobs") return right.job_count - left.job_count || left.name.localeCompare(right.name, "zh-CN");
      if (sort === "name") return left.name.localeCompare(right.name, "zh-CN");
      const leftScore = left.average_score == null ? -1 : left.average_score;
      const rightScore = right.average_score == null ? -1 : right.average_score;
      return rightScore - leftScore || right.job_count - left.job_count || left.name.localeCompare(right.name, "zh-CN");
    });
    return rows;
  }

  function renderCompanyRanking() {
    const body = $("company-ranking-body");
    if (!body) return;
    const rows = sortedCompanySummaries();
    const pages = Math.max(1, Math.ceil(rows.length / state.companyPageSize));
    if (state.companyPage > pages) state.companyPage = pages;
    const pageRows = rows.slice((state.companyPage - 1) * state.companyPageSize, state.companyPage * state.companyPageSize);
    clear(body);
    if (!pageRows.length) {
      const row = document.createElement("tr");
      const cell = element("td", "table-empty", "没有匹配的公司");
      cell.colSpan = 6;
      row.appendChild(cell);
      body.appendChild(row);
    } else {
      pageRows.forEach((company) => {
        const row = document.createElement("tr");
        row.className = "company-ranking-row";
        row.dataset.companyJobs = company.key;
        const nameCell = document.createElement("td");
        nameCell.appendChild(element("strong", "", company.name));
        if (company.recruitment_units?.length) nameCell.appendChild(element("small", "company-units", company.recruitment_units.join("、")));
        const countCell = element("td", "numeric-cell", company.job_count);
        const averageCell = document.createElement("td");
        averageCell.appendChild(scoreBadge(company.average_score == null ? null : Math.round(company.average_score)));
        const topScoreCell = document.createElement("td");
        topScoreCell.appendChild(scoreBadge(company.top_score));
        const topJobCell = element("td", "cell-muted", company.top_job || "尚未评分");
        const sourceCell = document.createElement("td");
        if (company.campus_url) {
          const link = appendUiIcon(element("a", "icon-button icon-button--small"), "external");
          link.href = company.campus_url;
          link.target = "_blank";
          link.rel = "noreferrer";
          link.title = "打开校招官网";
          link.setAttribute("aria-label", `打开 ${company.name} 校招官网`);
          sourceCell.appendChild(link);
        } else {
          sourceCell.textContent = "—";
        }
        row.append(nameCell, countCell, averageCell, topScoreCell, topJobCell, sourceCell);
        body.appendChild(row);
      });
    }
    setText("company-ranking-count", `${rows.length} 家公司`);
    setText("companies-page-info", `第 ${state.companyPage} / ${pages} 页 · 共 ${rows.length} 家`);
    $("companies-prev-button").disabled = state.companyPage <= 1;
    $("companies-next-button").disabled = state.companyPage >= pages;
  }

  function applicationHistorySource(entry = {}) {
    const explicitLabel = entry.source_label || entry.source_name;
    if (explicitLabel) {
      const label = String(explicitLabel);
      if (/mail|邮箱|邮件/i.test(label)) return { key: "mail", label: "招聘邮件" };
      if (/edge|官网|browser/i.test(label)) return { key: "edge", label: "Edge 官网" };
      return { key: "record", label };
    }
    const source = [entry.source, entry.source_type, entry.source_ref].filter(Boolean).join(" ").toLowerCase();
    if (/recruitment[_ -]?mail|mail:|imap|邮件|邮箱/.test(source)) return { key: "mail", label: "招聘邮件" };
    if (/edge|browser|application[_ -]?status[_ -]?review|官网/.test(source)) return { key: "edge", label: "Edge 官网" };
    return { key: "record", label: "历史记录" };
  }

  function renderApplications(payload = {}) {
    state.applications = Array.isArray(payload.items) ? payload.items : [];
    state.applicationBrowse.total = payload.total ?? state.applications.length;
    const summary = $("application-summary");
    const kanban = $("application-kanban");
    clear(summary);
    clear(kanban);
    const filtered = Boolean($("application-search")?.value.trim());
    setText("application-page-description", `${filtered ? "匹配" : "共"} ${state.applicationBrowse.total} 条 · 已显示 ${state.applications.length} 条`);
    APPLICATION_COLUMNS.forEach((column) => {
      const applications = state.applications.filter((application) => column.stages.includes(application.stage));
      const count = payload.stage_counts && Object.keys(payload.stage_counts).length
        ? column.stages.reduce((sum, stage) => sum + (payload.stage_counts[stage] || 0), 0) : applications.length;
      const summaryItem = element("div", `application-summary-item application-summary-item--${column.key}`);
      const summaryIcon = appendUiIcon(element("span", "application-summary-icon"), column.key === "closed" ? "close" : column.key);
      const summaryCopy = element("div", "application-summary-copy");
      summaryCopy.append(element("span", "", column.label), element("strong", "", count), element("small", "", "搜索范围内记录"));
      summaryItem.append(summaryIcon, summaryCopy);
      summary.appendChild(summaryItem);

      const section = element("section", `kanban-column kanban-column--${column.key}`);
      const header = element("header", "kanban-column-header");
      header.append(element("h3", "", column.label), element("span", "kanban-count", count));
      section.appendChild(header);
      const list = element("div", "kanban-list");
      if (!applications.length) {
        list.appendChild(element("div", "kanban-empty", "暂无记录"));
      } else {
        applications.forEach((application) => {
          const card = element("article", "application-card");
          const heading = element("div", "application-card-heading");
          const companyName = text(application.company_name, "公司待确认");
          const avatarName = companyName.replace(/^加入/, "").replace(/^(深圳市|北京市|上海市|杭州市)/, "").replace(/(科技|集团|股份|有限|公司).*$/, "") || companyName;
          const avatar = element("span", "application-avatar", avatarName.slice(0, 2));
          const tone = Array.from(companyName).reduce((total, char) => total + char.charCodeAt(0), 0) % 5;
          avatar.dataset.tone = String(tone);
          const identity = element("div", "application-card-identity");
          identity.append(element("strong", "", application.job_title), element("p", "application-company", companyName));
          const menu = appendUiIcon(element("button", "application-card-menu"), "more");
          menu.type = "button";
          menu.title = "展开编辑";
          menu.setAttribute("aria-label", `编辑 ${companyName} 的投递记录`);
          menu.setAttribute("aria-expanded", "false");
          const editor = applicationEditor(application);
          menu.addEventListener("click", (event) => {
            event.stopPropagation();
            editor.hidden = !editor.hidden;
            menu.setAttribute("aria-expanded", String(!editor.hidden));
            menu.title = editor.hidden ? "展开编辑" : "收起编辑";
          });
          heading.append(avatar, identity, menu);

          const historyItems = Array.isArray(application.stage_history) ? application.stage_history : [];
          const latestHistory = historyItems[historyItems.length - 1] || {};
          const fallbackResult = ["rejected", "withdrawn"].includes(application.stage) ? "已结束" : "进行中";
          const status = element("div", "application-card-status");
          status.append(
            element("span", "application-result", latestHistory.result || fallbackResult),
            element("span", "application-updated", `更新于 ${compactDate(application.updated_at)}`),
          );

          const actions = element("div", "application-card-actions");
          const links = element("div", "application-card-links");
          if (application.record_url) {
            const record = element("a", "text-action", "查看投递进度 ›");
            record.href = application.record_url;
            record.target = "_blank";
            record.rel = "noreferrer";
            links.appendChild(record);
          }
          if (application.job_id) {
            const job = element("button", "text-action", "岗位详情");
            job.type = "button";
            job.dataset.jobDetails = application.job_id;
            links.appendChild(job);
          }
          card.append(heading, status);
          if (application.note) card.appendChild(element("p", "application-note", application.note));
          if (historyItems.length) {
            const history = document.createElement("details");
            history.className = "application-history";
            const historySummary = element("summary", "", `阶段历史 (${historyItems.length})`);
            history.appendChild(historySummary);
            const historyList = document.createElement("ol");
            historyItems.slice().reverse().forEach((entry) => {
              const stage = applicationStageLabel(entry.stage || entry.to_stage || entry.status);
              const parts = [stage];
              if (entry.result) parts.push(entry.result);
              parts.push(compactDate(entry.changed_at || entry.at || entry.created_at || entry.date));
              const historyItem = element("li", "application-history-entry");
              historyItem.append(
                element("span", "application-history-copy", parts.join(" · ")),
                element("span", `history-source history-source--${applicationHistorySource(entry).key}`, `来源：${applicationHistorySource(entry).label}`),
              );
              historyList.appendChild(historyItem);
            });
            history.appendChild(historyList);
            links.appendChild(history);
          }
          actions.appendChild(links);
          card.appendChild(actions);
          card.appendChild(editor);
          list.appendChild(card);
        });
      }
      section.appendChild(list);
      const browse = state.applicationBrowse.columns[column.key];
      if (browse && browse.offset < browse.total) {
        const more = element("button", "button button--ghost", `加载更多（已显示 ${applications.length} / ${browse.total}）`);
        more.type = "button";
        more.dataset.applicationMore = column.key;
        more.setAttribute("aria-label", `加载更多${column.label}记录`);
        more.addEventListener("click", async () => {
          if (more.disabled) return;
          more.disabled = true;
          more.textContent = "加载中…";
          try { await loadApplications({ moreColumn: column.key }); }
          finally { more.disabled = false; more.textContent = `加载更多（已显示 ${applications.length} / ${browse.total}）`; }
        });
        section.appendChild(more);
      }
      kanban.appendChild(section);
    });
    setText("nav-application-count", payload.unfiltered_total ?? payload.total ?? state.applications.length);
    if (!state.applications.length) empty(kanban, "没有匹配的投递记录", filtered ? "试试其他公司或岗位名称，或清除筛选条件。" : "可以手动添加投递，或在招聘官网记录投递。");
    kanban.dataset.state = "ready";
  }

  function applicationEditor(application) {
    const editor = element("div", "application-editor");
    editor.hidden = true;
    const field = (form, label, control) => {
      const wrapper = element("label", "application-editor-field", label);
      wrapper.appendChild(control);
      form.appendChild(wrapper);
      return control;
    };
    const select = (choices, value) => {
      const node = document.createElement("select");
      choices.forEach(([key, label]) => {
        const option = element("option", "", label);
        option.value = key;
        node.appendChild(option);
      });
      node.value = value;
      return node;
    };
    const submit = (form, label, action) => {
      const button = element("button", "button button--secondary", label);
      button.type = "submit";
      form.appendChild(button);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (button.disabled) return;
        button.disabled = true;
        try { await action(); } catch (error) { showToast(error.message, "error"); }
        finally { button.disabled = false; }
      });
    };
    const url = `/api/local-ui/applications/${encodeURIComponent(application.id)}`;
    const request = (method, data, suffix = "") => api(url + suffix, {
      method, headers: authHeaders("编辑投递", { "Content-Type": "application/json" }),
      body: JSON.stringify(data),
    });
    const form = document.createElement("form");
    const companyName = field(form, "公司名称", document.createElement("input"));
    companyName.type = "text";
    companyName.required = true;
    companyName.maxLength = 255;
    companyName.value = application.company_name || "";
    const jobTitle = field(form, "岗位名称", document.createElement("input"));
    jobTitle.type = "text";
    jobTitle.required = true;
    jobTitle.maxLength = 512;
    jobTitle.value = application.job_title || "";
    const stage = field(form, "阶段", select(Object.entries(APPLICATION_STAGE_LABELS), application.stage));
    const previous = (application.stage_history || []).slice(-1)[0] || {};
    const result = field(form, "结果", select(["待", "进行中", "通过", "淘汰", "放弃"].map(x => [x, x]), previous.result || "进行中"));
    const note = field(form, "备注", document.createElement("textarea"));
    note.value = application.note || "";
    note.maxLength = 4000;
    submit(form, "更新", async () => {
      if (!companyName.value.trim() || !jobTitle.value.trim()) throw new Error("公司和岗位名称不能为空");
      await request("PATCH", { company_name: companyName.value.trim(), job_title: jobTitle.value.trim(),
        stage: stage.value, result: result.value, note: note.value, expected_updated_at: application.updated_at });
      showToast("投递记录已更新", "success");
      await loadApplications();
    });
    editor.appendChild(form);
    const linkForm = document.createElement("form");
    const recordUrl = field(linkForm, "官网投递进度页地址", document.createElement("input"));
    recordUrl.type = "url";
    recordUrl.required = true;
    recordUrl.maxLength = 2048;
    recordUrl.value = application.record_url || "";
    submit(linkForm, "保存进度页链接", async () => {
      await request("PATCH", { record_url: recordUrl.value.trim(), expected_updated_at: application.updated_at }, "/record-url");
      showToast("进度页链接已保存，投递阶段未改变", "success");
      await loadApplications();
    });
    editor.appendChild(linkForm);
    const eventForm = document.createElement("form");
    const kind = field(eventForm, "日程", select(["测评", "笔试", "面试", "其他"].map(x => [x, x]), "面试"));
    const day = field(eventForm, "日期", document.createElement("input"));
    day.type = "date";
    const time = field(eventForm, "时间", document.createElement("input"));
    time.type = "time";
    const eventNote = field(eventForm, "日程备注", document.createElement("input"));
    eventNote.maxLength = 4000;
    submit(eventForm, "添加日程", async () => {
      if (!day.value && time.value) throw new Error("未填写日期时不能填写时间。");
      await api(LOCAL_SCHEDULE_EVENTS_URL, {
        method: "POST",
        headers: authHeaders("添加日程", { "Content-Type": "application/json" }),
        body: JSON.stringify({
          title: `${application.company_name} · ${kind.value}`,
          event_type: kind.value,
          event_date: day.value || null,
          event_time: time.value || null,
          company_name: application.company_name,
          job_title: application.job_title,
          application_id: application.id,
          location_or_link: null,
          note: eventNote.value,
          time_kind: "appointment",
        }),
      });
      showToast("日程已添加", "success");
      eventForm.reset();
      await loadFullSchedule();
    });
    editor.appendChild(eventForm);
    const deleteForm = document.createElement("form");
    const deleteConfirmation = element("div", "application-delete-confirmation");
    deleteConfirmation.hidden = true;
    deleteConfirmation.setAttribute("role", "group");
    deleteConfirmation.setAttribute("aria-label", "确认删除投递记录");
    deleteConfirmation.append(
      element("strong", "", `${application.company_name} · ${application.job_title}`),
      element("p", "", "将删除这条投递记录及关联日程；邮件保留，仅解除关联。"),
    );
    const deleteError = element("p", "inline-error application-delete-error");
    deleteError.hidden = true;
    deleteError.setAttribute("role", "alert");
    const deleteActions = element("div", "application-delete-actions");
    const cancelDelete = element("button", "button button--secondary", "取消");
    cancelDelete.type = "button";
    const confirmDelete = element("button", "button application-delete-submit", "确认删除");
    confirmDelete.type = "button";
    cancelDelete.addEventListener("click", () => {
      deleteConfirmation.hidden = true;
      deleteError.hidden = true;
      deleteForm.querySelector('button[type="submit"]').focus();
    });
    confirmDelete.addEventListener("click", async () => {
      if (confirmDelete.disabled) return;
      confirmDelete.disabled = true;
      cancelDelete.disabled = true;
      confirmDelete.textContent = "正在删除…";
      deleteError.hidden = true;
      try {
        await request("DELETE", { expected_updated_at: application.updated_at });
        showToast("投递记录已删除", "success");
        deleteConfirmation.hidden = true;
        await Promise.all([loadApplications(), loadFullSchedule()]);
      } catch (error) {
        deleteError.textContent = `删除未完成：${error.message}`;
        deleteError.hidden = false;
      } finally {
        confirmDelete.disabled = false;
        cancelDelete.disabled = false;
        confirmDelete.textContent = "确认删除";
      }
    });
    deleteActions.append(cancelDelete, confirmDelete);
    deleteConfirmation.append(deleteError, deleteActions);
    submit(deleteForm, "删除投递记录", async () => {
      deleteConfirmation.hidden = false;
      deleteError.hidden = true;
      cancelDelete.focus();
    });
    deleteForm.appendChild(deleteConfirmation);
    editor.appendChild(deleteForm);
    return editor;
  }

  function applicationBrowseQuery(column, offset = 0) {
    const params = new URLSearchParams({ limit: String(state.applicationBrowse.pageSize), offset: String(offset) });
    const query = $("application-search")?.value.trim();
    if (query) params.set("query", query);
    column.stages.forEach(stage => params.append("stages", stage));
    return params;
  }

  async function loadApplications({ moreColumn = null } = {}) {
    const query = $("application-search")?.value.trim() || "";
    if (query !== state.applicationBrowse.query) moreColumn = null;
    const columns = moreColumn ? APPLICATION_COLUMNS.filter(column => column.key === moreColumn) : APPLICATION_COLUMNS;
    const requestId = ++state.applicationsRequestId;
    state.applicationRequestController?.abort();
    const controller = new AbortController();
    state.applicationRequestController = controller;
    try {
      const pages = await Promise.all(columns.map(async column => {
        const offset = moreColumn ? state.applicationBrowse.columns[column.key]?.offset || 0 : 0;
        const payload = await api(`/api/applications/page?${applicationBrowseQuery(column, offset)}`, { signal: controller.signal });
        return { column, offset, payload };
      }));
      if (requestId !== state.applicationsRequestId) return false;
      if (!moreColumn) state.applicationBrowse.columns = {};
      state.applicationBrowse.query = query;
      for (const { column, offset, payload } of pages) {
        const previous = moreColumn ? state.applicationBrowse.columns[column.key]?.items || [] : [];
        const items = Array.isArray(payload.items) ? payload.items : [];
        state.applicationBrowse.columns[column.key] = {
          items: [...new Map([...previous, ...items].map(item => [item.id, item])).values()],
          offset: offset + items.length, total: payload.total || 0,
        };
      }
      const payload = pages[0].payload;
      const stageCounts = payload.stage_counts || {};
      renderApplications({ ...payload, stage_counts: stageCounts,
        total: Object.keys(stageCounts).length ? Object.values(stageCounts).reduce((sum, count) => sum + count, 0)
          : Object.values(state.applicationBrowse.columns).reduce((sum, column) => sum + column.total, 0),
        items: [...new Map(Object.values(state.applicationBrowse.columns).flatMap(column => column.items).map(item => [item.id, item])).values()],
      });
      return true;
    } catch (error) {
      if (requestId !== state.applicationsRequestId || error.name === "AbortError") return false;
      if (!moreColumn) empty($("application-kanban"), "投递记录加载失败", error.message);
      showToast(`投递记录加载失败：${error.message}`, "error");
      return false;
    }
  }

  function safeScheduleHref(value) {
    const candidate = text(value, "").trim();
    if (!/^https?:\/\//i.test(candidate)) return null;
    try {
      const url = new URL(candidate);
      return ["http:", "https:"].includes(url.protocol) ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  function scheduleActionButton(action, event, icon, label) {
    const button = element("button", "icon-button icon-button--small");
    button.type = "button";
    button.title = label;
    button.setAttribute("aria-label", `${label}：${scheduleEventTitle(event)}`);
    button.dataset.scheduleAction = action;
    button.dataset.scheduleId = text(event.id, "");
    appendUiIcon(button, icon);
    return button;
  }

  function scheduleSourceButton(event) {
    if (event.source !== "recruitment_mail_schedule" || !event.source_ref) return null;
    const button = element("button", "icon-button icon-button--small");
    button.type = "button";
    button.title = "查看来源邮件";
    button.setAttribute("aria-label", `查看来源邮件：${scheduleEventTitle(event)}`);
    button.dataset.mailOpenId = text(event.source_ref, "");
    appendUiIcon(button, "mail");
    return button;
  }

  function renderScheduleItem(event, dueGroup = scheduleDueGroup(event)) {
    const status = normalizeScheduleStatus(event.status);
    const locationValue = text(event.location_or_link, "").trim();
    const href = safeScheduleHref(locationValue);
    const item = element("article", `schedule-item schedule-item--${status} schedule-item--${dueGroup}`);
    const timing = element("div", "schedule-item-timing");
    timing.append(
      element("span", "schedule-item-date", scheduleDateLabel(event.event_date)),
      element("time", "schedule-event-time", scheduleTimeLabel(event)),
    );

    const copy = element("div", "schedule-item-copy");
    copy.appendChild(element("strong", "schedule-item-title", scheduleEventTitle(event)));
    const meta = element("div", "schedule-item-meta");
    [event.event_type, event.company_name, event.job_title, SCHEDULE_TIME_KIND_LABELS[normalizeScheduleTimeKind(event.time_kind)]]
      .filter(Boolean)
      .forEach((value) => meta.appendChild(element("span", "schedule-meta-tag", value)));
    copy.appendChild(meta);
    if (event.note) copy.appendChild(element("p", "schedule-item-note", event.note));
    if (locationValue && !href) copy.appendChild(element("p", "schedule-item-location", locationValue));

    const statusTag = element("span", `schedule-status-tag schedule-status-tag--${status}`, SCHEDULE_STATUS_LABELS[status]);
    statusTag.dataset.status = status;
    copy.appendChild(statusTag);

    const actions = element("div", "schedule-item-actions");
    if (status === "pending") {
      actions.append(
        scheduleActionButton("complete", event, "check", "完成"),
        scheduleActionButton("ignore", event, "ignore", "忽略"),
      );
    } else {
      actions.appendChild(scheduleActionButton("restore", event, "restore", "恢复"));
    }
    actions.appendChild(scheduleActionButton("edit", event, "edit", "编辑"));
    const source = scheduleSourceButton(event);
    if (source) actions.appendChild(source);
    if (href) {
      const link = element("a", "icon-button icon-button--small");
      link.href = href;
      link.target = "_blank";
      link.rel = "noreferrer";
      link.title = "打开位置或链接";
      link.setAttribute("aria-label", `打开位置或链接：${scheduleEventTitle(event)}`);
      appendUiIcon(link, "external");
      actions.appendChild(link);
    }
    item.append(timing, copy, actions);
    return item;
  }

  function renderScheduleTodo(events) {
    const node = $("schedule-todo-view");
    clear(node);
    if (!events.length) {
      empty(node, state.scheduleStatus === "pending" ? "暂无待办日程" : "没有符合条件的日程", "可以添加一个新的求职安排，或调整状态筛选。");
      return;
    }
    SCHEDULE_DUE_GROUPS.forEach(({ key, label }) => {
      const grouped = events.filter((event) => scheduleDueGroup(event) === key);
      if (!grouped.length) return;
      const section = element("section", `schedule-group schedule-group--${key}`);
      const heading = element("div", "schedule-group-heading");
      heading.append(element("h3", "", label), element("span", "muted-label", `${grouped.length} 项`));
      const list = element("div", "schedule-group-list");
      grouped.forEach((event) => list.appendChild(renderScheduleItem(event, key)));
      section.append(heading, list);
      node.appendChild(section);
    });
    node.dataset.state = "ready";
  }

  function renderScheduleCalendar(events) {
    const month = scheduleMonthStart(state.scheduleMonth || today);
    state.scheduleMonth = scheduleMonthKey(month);
    setText("schedule-calendar-month", month.toLocaleDateString("zh-CN", { year: "numeric", month: "long" }));
    const grid = $("schedule-calendar-grid");
    clear(grid);
    const firstDayOffset = (month.getDay() + 6) % 7;
    const start = new Date(month.getFullYear(), month.getMonth(), 1 - firstDayOffset);
    for (let index = 0; index < 42; index += 1) {
      const day = new Date(start);
      day.setDate(start.getDate() + index);
      const dateKey = scheduleDateKey(day);
      const outside = day.getMonth() !== month.getMonth();
      const cell = element("article", `schedule-calendar-day${outside ? " is-outside" : ""}${dateKey === today ? " is-today" : ""}`);
      cell.dataset.date = dateKey;
      cell.setAttribute("aria-label", dateKey);
      const heading = element("div", "schedule-calendar-day-heading");
      heading.appendChild(element("span", "schedule-calendar-day-number", String(day.getDate())));
      if (dateKey === today) heading.appendChild(element("span", "schedule-calendar-today", "今天"));
      cell.appendChild(heading);
      events.filter((event) => event.event_date === dateKey).forEach((event) => {
        const status = normalizeScheduleStatus(event.status);
        const chip = element("button", `schedule-calendar-event schedule-calendar-event--${status}${normalizeScheduleTimeKind(event.time_kind) === "deadline" ? " schedule-calendar-event--deadline" : ""}`);
        chip.type = "button";
        chip.dataset.scheduleEditId = text(event.id, "");
        chip.title = `编辑：${scheduleEventTitle(event)}`;
        chip.setAttribute("aria-label", `编辑${scheduleEventTitle(event)}，${scheduleDateLabel(event.event_date)}，${scheduleCalendarTimeLabel(event)}`);
        chip.append(element("span", "schedule-calendar-event-time", scheduleCalendarTimeLabel(event)), element("span", "schedule-calendar-event-title", scheduleEventTitle(event)));
        cell.appendChild(chip);
      });
      grid.appendChild(cell);
    }
    grid.dataset.state = "ready";

    const undated = $("schedule-calendar-undated");
    clear(undated);
    const undatedEvents = events.filter((event) => !event.event_date);
    if (!undatedEvents.length) {
      undated.hidden = true;
      return;
    }
    undated.hidden = false;
    const notice = element("div", "schedule-calendar-undated-note");
    notice.appendChild(element("span", "", `${undatedEvents.length} 项未定日期事项`));
    const backToTodo = element("button", "button button--ghost", "切回待办");
    backToTodo.type = "button";
    backToTodo.addEventListener("click", () => setScheduleView("todo"));
    notice.appendChild(backToTodo);
    undated.appendChild(notice);
  }

  function setScheduleView(view) {
    state.scheduleView = view === "calendar" ? "calendar" : "todo";
    document.querySelectorAll("[data-schedule-view]").forEach((button) => {
      const active = button.dataset.scheduleView === state.scheduleView;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", String(active));
    });
    const todo = $("schedule-todo-view");
    const calendar = $("schedule-calendar-view");
    if (todo) todo.hidden = state.scheduleView !== "todo";
    if (calendar) calendar.hidden = state.scheduleView !== "calendar";
    if (state.scheduleView === "calendar") renderScheduleCalendar(scheduleEventsForCurrentFilter());
  }

  function renderFullSchedule() {
    const events = scheduleEventsForCurrentFilter();
    setText("schedule-result-count", `${events.length} 项`);
    renderScheduleTodo(events);
    renderScheduleCalendar(events);
    setScheduleView(state.scheduleView);
    setText("nav-schedule-count", state.allSchedules.filter((event) => normalizeScheduleStatus(event.status) === "pending").length);
  }

  async function loadFullSchedule() {
    const requestId = ++state.scheduleRequestId;
    try {
      const payload = await api("/api/schedule");
      if (requestId !== state.scheduleRequestId) return false;
      state.allSchedules = Array.isArray(payload) ? payload : [];
      renderFullSchedule();
      state.schedules = state.allSchedules.filter((event) => event.event_date === localDate() && normalizeScheduleStatus(event.status) === "pending");
      renderSchedule(state.schedules);
      setText("metric-schedule", state.schedules.length);
      renderDashboardTodos();
      return true;
    } catch (error) {
      if (requestId !== state.scheduleRequestId) return false;
      errorState($("schedule-todo-view"), "日程加载失败", error.message);
      errorState($("schedule-calendar-grid"), "日程加载失败", error.message);
      showToast(`日程加载失败：${error.message}`, "error");
      return false;
    }
  }

  async function updateScheduleStatus(eventId, status) {
    const event = state.allSchedules.find((item) => text(item.id, "") === text(eventId, ""));
    if (!event || !SCHEDULE_STATUS_LABELS[status]) return;
    try {
      await api(`${LOCAL_SCHEDULE_EVENTS_URL}/${encodeURIComponent(eventId)}`, {
        method: "PATCH",
        headers: authHeaders(`更新日程为${SCHEDULE_STATUS_LABELS[status]}`, { "Content-Type": "application/json" }),
        body: JSON.stringify({ status, expected_updated_at: event.updated_at }),
      });
      await loadFullSchedule();
      showToast(`日程已${SCHEDULE_STATUS_LABELS[status]}`, "success");
    } catch (error) {
      showToast(`日程状态更新失败：${error.message}`, "error");
    }
  }

  function closeScheduleEditor() {
    const dialog = $("schedule-editor-dialog");
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
    state.scheduleEditingId = "";
  }

  function populateScheduleApplicationOptions(selectedId = "") {
    const select = $("schedule-application-id");
    if (!select) return;
    clear(select);
    const none = element("option", "", "无关联");
    none.value = "";
    select.appendChild(none);
    state.applications.forEach((application) => {
      const option = element("option", "", `${text(application.company_name, "公司待确认")} · ${text(application.job_title, "岗位待确认")}`);
      option.value = text(application.id, "");
      select.appendChild(option);
    });
    if (selectedId && !state.applications.some((application) => text(application.id, "") === text(selectedId, ""))) {
      const option = element("option", "", `当前绑定：${selectedId}`);
      option.value = selectedId;
      select.appendChild(option);
    }
    select.value = selectedId || "";
  }

  function openScheduleEditor(event = null) {
    state.scheduleEditingId = event ? text(event.id, "") : "";
    setText("schedule-editor-heading", event ? "编辑日程" : "添加日程");
    setText("schedule-editor-kicker", event ? "调整当前安排" : "手动记录安排");
    populateScheduleApplicationOptions(event?.application_id || "");
    const values = {
      "schedule-title": event ? scheduleEventTitle(event) : "",
      "schedule-event-type": event?.event_type || "",
      "schedule-event-date": event?.event_date || "",
      "schedule-event-time": event?.event_time ? text(event.event_time, "").slice(0, 5) : "",
      "schedule-time-kind": normalizeScheduleTimeKind(event?.time_kind),
      "schedule-company-name": event?.company_name || "",
      "schedule-job-title": event?.job_title || "",
      "schedule-application-id": event?.application_id || "",
      "schedule-location": event?.location_or_link || "",
      "schedule-note": event?.note || "",
    };
    Object.entries(values).forEach(([id, value]) => { if ($(id)) $(id).value = value; });
    setText("schedule-editor-submit", event ? "保存修改" : "添加日程");
    const dialog = $("schedule-editor-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function scheduleFormPayload() {
    const value = (id) => text($(id)?.value, "").trim();
    const title = value("schedule-title");
    if (!title) throw new Error("请填写日程标题。");
    const eventDate = value("schedule-event-date") || null;
    const eventTime = value("schedule-event-time") || null;
    const companyName = value("schedule-company-name");
    const jobTitle = value("schedule-job-title");
    if (!companyName) throw new Error("请填写公司。");
    if (!eventDate && eventTime) throw new Error("未填写日期时不能填写时间。");
    return {
      title,
      event_date: eventDate,
      event_time: eventTime,
      event_type: value("schedule-event-type") || "其他",
      company_name: companyName,
      job_title: jobTitle,
      application_id: value("schedule-application-id") || null,
      location_or_link: value("schedule-location") || null,
      note: value("schedule-note") || null,
      time_kind: normalizeScheduleTimeKind(value("schedule-time-kind")),
    };
  }

  async function submitScheduleForm(domEvent) {
    domEvent.preventDefault();
    const form = $("schedule-editor-form");
    if (typeof form.reportValidity === "function" && !form.reportValidity()) return;
    const submit = $("schedule-editor-submit");
    if (submit.disabled) return;
    submit.disabled = true;
    try {
      const payload = scheduleFormPayload();
      const existing = state.allSchedules.find((event) => text(event.id, "") === state.scheduleEditingId);
      if (existing) {
        payload.status = normalizeScheduleStatus(existing.status);
        payload.expected_updated_at = existing.updated_at;
      }
      const path = existing
        ? `${LOCAL_SCHEDULE_EVENTS_URL}/${encodeURIComponent(existing.id)}`
        : LOCAL_SCHEDULE_EVENTS_URL;
      await api(path, {
        method: existing ? "PATCH" : "POST",
        headers: authHeaders(existing ? "编辑日程" : "添加日程", { "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      closeScheduleEditor();
      await loadFullSchedule();
      showToast(existing ? "日程已更新" : "日程已添加", "success");
    } catch (error) {
      showToast(`日程保存失败：${error.message}`, "error");
    } finally {
      submit.disabled = false;
    }
  }

  function renderMailDetail(payload, recordId) {
    const summary = state.mails.find((mail) => text(mail.id, "") === text(recordId, "")) || {};
    const message = payload?.message || payload?.data?.message || payload || {};
    const subject = message.subject || summary.subject || "招聘邮件";
    setText("job-detail-company", `招聘邮件 · ${text(recordId, "邮件来源")}`);
    setText("job-detail-title", subject);
    const content = $("job-detail-content");
    clear(content);
    const meta = element("div", "job-detail-meta mail-detail-meta");
    [message.sender || summary.sender, mailDateTime(message.received_at || summary.received_at), mailProcessingLabel(payload?.processing_status || summary.processing_status)]
      .filter(Boolean)
      .forEach((value) => meta.appendChild(element("span", "table-tag", value)));
    content.appendChild(meta);
    const target = [message.company_candidates?.[0]?.value || summary.company_name, message.job_candidates?.[0]?.value || summary.job_title]
      .filter(Boolean)
      .join(" · ");
    if (target) content.appendChild(element("p", "mail-detail-target", `投递目标：${target}`));
    content.appendChild(element("pre", "mail-detail-body", message.body_text || message.text || "邮件正文未提供。"));
    const actions = element("div", "job-detail-actions");
    const openMailbox = element("button", "button button--ghost", "打开招聘邮箱");
    openMailbox.type = "button";
    openMailbox.addEventListener("click", () => {
      $("job-detail-dialog").close();
      switchView("mail");
    });
    actions.appendChild(openMailbox);
    const bindMail = element("button", "button button--secondary", "选择或修改关联投递");
    bindMail.type = "button";
    bindMail.addEventListener("click", () => void openMailBinding(recordId));
    actions.appendChild(bindMail);
    content.appendChild(actions);
    content.dataset.state = "ready";
  }

  async function openRecruitmentMail(recordId) {
    const dialog = $("job-detail-dialog");
    setText("job-detail-company", "招聘邮件");
    setText("job-detail-title", "正在加载");
    loading($("job-detail-content"));
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    try {
      renderMailDetail(await api(`/api/recruitment-mails/${encodeURIComponent(recordId)}?refresh=false`), recordId);
    } catch (error) {
      const summary = state.mails.find((mail) => text(mail.id, "") === text(recordId, ""));
      if (summary) {
        renderMailDetail(summary, recordId);
        showToast("邮件详情暂不可用，当前显示列表摘要", "info");
      } else {
        empty($("job-detail-content"), "邮件详情加载失败", error.message);
      }
    }
  }

  function mailBindingCard(approval) {
    const preview = approval.preview || {};
    const after = preview.after || {};
    const card = element("article", "approval-card");
    card.appendChild(element("strong", "", after.action === "unbind" ? "确认解除这封邮件的关联" : "确认邮件关联"));
    card.appendChild(element("p", "", `邮件：${preview.before?.subject || "招聘邮件"}`));
    card.appendChild(element("p", "", `发件人：${preview.before?.sender || "待核验"}`));
    card.appendChild(element("p", "", after.action === "unbind" ? "解除关联，保留邮件与已有业务记录。" : `关联到：${after.company_name || "—"} · ${after.job_title || "—"}`));
    card.appendChild(element("small", "", "仅确认本封邮件与投递的对应关系；不会发送邮件，也不会直接更改投递阶段。后续处理仍检查发件人和事件证据。"));
    const actions = element("div", "inline-actions");
    const confirm = element("button", "button button--primary", after.action === "unbind" ? "确认解除" : "确认关联");
    confirm.type = "button";
    confirm.addEventListener("click", () => void confirmMailBinding(approval, confirm));
    const reject = element("button", "button button--ghost", "拒绝"); reject.type = "button";
    reject.addEventListener("click", () => void decideApproval(approval.token_id, "reject"));
    actions.append(confirm, reject); card.appendChild(actions);
    return card;
  }

  function renderMailBindingApprovals() {
    const target = $("assistant-mail-binding-approvals");
    if (!target) return;
    clear(target);
    state.approvals.filter(item => item.operation === "recruitment_mail_binding" && ["pending", "approved"].includes(item.status))
      .forEach(item => target.appendChild(mailBindingCard(item)));
  }

  async function confirmMailBinding(approval, button) {
    if (button?.disabled) return;
    if (button) button.disabled = true;
    try {
      if (approval.status !== "approved") {
        const decision = await api(`/api/approvals/${encodeURIComponent(approval.token_id)}/approve`, {
          method: "POST", headers: authHeaders("确认邮件关联"),
        });
        if (!decision.allowed || decision.status !== "approved") throw new Error("预览已失效，请重新选择并确认");
      }
      const audit = await api(`/api/approvals/${encodeURIComponent(approval.token_id)}/execute`, {
        method: "POST", headers: authHeaders("保存邮件关联", { "Content-Type": "application/json" }),
        body: JSON.stringify({ operator: "local-ui-user" }),
      });
      if (!audit.success) throw new Error("关联未保存，邮件或投递可能已变化，请重新确认");
      state.approvals = normalizeApprovals(await api("/api/approvals"));
      renderApprovals();
      await Promise.all([loadRecruitmentMails({ showLoading: false }), loadFullSchedule()]);
      showToast(approval.preview?.after?.action === "unbind" ? "关联已解除，邮件及已有记录已保留" : "关联已保存；需要更新阶段或日程时，可让助理继续处理邮件", "success");
      $("job-detail-dialog")?.close?.();
    } catch (error) { showToast(error.message, "error"); }
    finally { if (button) button.disabled = false; }
  }

  async function openMailBinding(recordId) {
    const dialog = $("job-detail-dialog");
    setText("job-detail-company", "邮件关联"); setText("job-detail-title", "选择对应的投递记录");
    const content = $("job-detail-content"); clear(content);
    const input = element("input", ""); input.type = "search"; input.placeholder = "搜索公司或岗位名称";
    input.setAttribute("aria-label", "搜索要关联的投递记录");
    const search = element("button", "button button--secondary", "搜索"); search.type = "submit";
    const form = element("form", "application-filter-bar"); form.append(input, search);
    const candidates = element("div", ""); const previewNode = element("div", "");
    content.append(element("p", "", "名称不一致时可搜索实际投递。选择后请核对邮件主题、发件人与岗位，再确认关联。"), form, candidates, previewNode);
    if (!dialog.open) { if (dialog.showModal) dialog.showModal(); else dialog.setAttribute("open", ""); }
    let sequence = 0;
    const load = async () => {
      const request = ++sequence; clear(previewNode); loading(candidates);
      try {
        const result = await api(`/api/recruitment-mails/${encodeURIComponent(recordId)}/binding-candidates?query=${encodeURIComponent(input.value || "")}`);
        if (request !== sequence || content.firstChild == null) return;
        clear(candidates);
        const propose = async (applicationId, action, button) => {
          button.disabled = true;
          try {
            const response = await api(`/api/recruitment-mails/${encodeURIComponent(recordId)}/binding-proposals`, {
              method: "POST", headers: authHeaders("提出邮件关联", { "Content-Type": "application/json" }),
              body: JSON.stringify({ record_id: recordId, application_id: applicationId, action,
                content_digest: result.content_digest, binding_revision: result.binding_revision }),
            });
            if (!response.success || !response.data?.approval_id) throw new Error("无法生成确认预览");
            clear(previewNode);
            previewNode.appendChild(mailBindingCard({ token_id: response.data.approval_id, status: response.data.approval_status,
              operation: "recruitment_mail_binding", preview: response.data.preview }));
          } catch (error) { showToast(error.message, "error"); }
          finally { button.disabled = false; }
        };
        for (const item of result.candidates || []) {
          const row = element("div", "approval-card");
          row.appendChild(element("p", "", `${item.company_name} · ${item.job_title}`));
          const button = element("button", "button button--ghost", "选择并查看确认预览"); button.type = "button";
          button.addEventListener("click", () => void propose(item.application_id, result.current_application_id ? "correct" : "bind", button));
          row.appendChild(button); candidates.appendChild(row);
        }
        if (!result.candidates?.length) empty(candidates, "暂无匹配投递", "输入公司或岗位名称搜索，也可以暂不关联。");
        if (result.current_application_id) {
          const unbind = element("button", "button button--ghost", "解除当前关联"); unbind.type = "button";
          unbind.addEventListener("click", () => void propose(null, "unbind", unbind)); candidates.appendChild(unbind);
        }
      } catch (error) { if (request === sequence) empty(candidates, "读取失败", error.message); }
    };
    form.addEventListener("submit", event => { event.preventDefault(); void load(); });
    await load();
  }

  const AUTOMATION_STATUS_LABELS = {
    running: "执行中",
    succeeded: "最近成功",
    failed: "最近失败",
    blocked: "需要处理",
  };

  function automationDateTime(value, fallback = "尚未执行") {
    if (!value) return fallback;
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return fallback;
    return parsed.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  function automationDuration(startedAt, completedAt) {
    if (!startedAt || !completedAt) return "—";
    const started = new Date(startedAt);
    const completed = new Date(completedAt);
    const seconds = Math.max(0, Math.round((completed.getTime() - started.getTime()) / 1000));
    if (!Number.isFinite(seconds)) return "—";
    if (seconds < 60) return `${seconds} 秒`;
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    return remainder ? `${minutes} 分 ${remainder} 秒` : `${minutes} 分钟`;
  }

  function automationExplanationPrompt(automation) {
    const latest = automation?.latest_execution || {};
    const status = text(latest.status || automation?.last_status, "未知");
    const prompt = [
      `请解释定时任务“${text(automation?.task_label, automation?.task_id || "未命名任务")}”最近一次执行为什么显示“${AUTOMATION_STATUS_LABELS[status] || status}”。`,
      `执行 ID：${text(latest.id, "无")}`,
      `执行状态：${status}`,
      `开始时间：${automationDateTime(latest.started_at)}`,
      `结束时间：${automationDateTime(latest.completed_at)}`,
      `系统错误：${text(latest.error || automation?.last_error, "无")}`,
      `执行摘要：\n${text(latest.result_summary, "无")}`,
      "只根据上述这一次执行记录解释，不要重新运行任务。请用中文说明直接原因、实际完成情况、影响范围和下一步操作，并区分任务失败与部分记录需要人工处理。",
    ].join("\n");
    return prompt.slice(0, 7000);
  }

  function openAutomationDetail(scheduleId) {
    const automation = state.automations.find((item) => text(item.id, "") === text(scheduleId, ""));
    if (!automation) return;
    const latest = automation.latest_execution || null;
    const content = $("automation-detail-content");
    clear(content);
    setText("automation-detail-title", text(automation.task_label, "任务详情"));

    const facts = element("dl", "automation-detail-facts");
    const addFact = (label, value) => {
      const group = element("div", "automation-detail-fact");
      group.append(element("dt", "", label), element("dd", "", value));
      facts.appendChild(group);
    };
    const status = text(latest?.status || automation.last_status, automation.active ? "active" : "inactive");
    addFact("执行状态", AUTOMATION_STATUS_LABELS[status] || (automation.active ? "已启用" : "已停用"));
    addFact("执行目标", text(automation.target_label, automation.target_kind === "all" ? "全部目标" : "目标待确认"));
    addFact("计划时间", `${automation.frequency === "daily" ? "每天" : text(automation.frequency)} ${text(automation.start_time, "--:--")} · ${text(automation.timezone, "Asia/Shanghai")}`);
    addFact("计划触发", automationDateTime(latest?.scheduled_for));
    addFact("开始时间", automationDateTime(latest?.started_at));
    addFact("结束时间", automationDateTime(latest?.completed_at));
    addFact("执行耗时", automationDuration(latest?.started_at, latest?.completed_at));
    addFact("执行 ID", text(latest?.id, "尚未执行"));
    content.appendChild(facts);

    const error = text(latest?.error || automation.last_error, "");
    if (error) {
      const alert = element("section", "automation-detail-alert");
      alert.append(element("h3", "", "错误信息"), element("p", "", error));
      content.appendChild(alert);
    }

    const result = element("section", "automation-detail-result");
    result.append(
      element("h3", "", "执行结果"),
      element("pre", "", text(latest?.result_summary, latest ? "本次执行没有返回摘要。" : "该任务尚未执行。")),
    );
    content.appendChild(result);

    const explain = $("automation-explain-button");
    const abnormal = ["failed", "blocked"].includes(status);
    explain.hidden = !abnormal;
    explain.dataset.automationExplain = abnormal ? automation.id : "";
    const dialog = $("automation-detail-dialog");
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
  }

  function renderAutomations(payload = {}) {
    const node = $("automation-list");
    if (!node) return;
    state.automations = Array.isArray(payload.items) ? payload.items : [];
    const activeCount = state.automations.filter((item) => item.active).length;
    const failedCount = state.automations.filter((item) => ["failed", "blocked"].includes(item.last_status)).length;
    setText("automation-total-count", payload.total ?? state.automations.length);
    setText("automation-active-count", activeCount);
    setText("automation-failed-count", failedCount);
    setText("automation-list-count", `${state.automations.length} 项`);
    setText("nav-automation-count", activeCount);
    clear(node);
    if (!state.automations.length) {
      empty(node, "暂无定时任务", "可以让求职助理创建每天执行的本地任务。");
      return;
    }
    state.automations.forEach((automation) => {
      const item = element("article", "automation-item");
      const heading = element("div", "automation-item-heading");
      const titleBlock = element("div", "automation-item-title");
      titleBlock.append(
        element("strong", "", text(automation.task_label, "未命名任务")),
        element("span", "", text(automation.target_label, automation.target_kind === "all" ? "全部目标" : "目标待确认")),
      );
      const status = element("span", "automation-status");
      status.dataset.state = automation.active ? text(automation.last_status, "active") : "inactive";
      status.textContent = automation.active
        ? AUTOMATION_STATUS_LABELS[automation.last_status] || "已启用"
        : "已停用";
      heading.append(titleBlock, status);

      const timing = element("div", "automation-timing");
      timing.append(
        element("span", "", `${automation.frequency === "daily" ? "每天" : text(automation.frequency)} ${text(automation.start_time, "--:--")} · ${text(automation.timezone, "Asia/Shanghai")}`),
        element("span", "", automation.active ? `下次 ${automationDateTime(automation.next_run_at, "待计算")}` : "不再自动执行"),
        element("span", "", `上次 ${automationDateTime(automation.last_run_at)}`),
      );

      const latest = automation.latest_execution || {};
      const detailText = text(latest.error || automation.last_error || latest.result_summary, "尚无执行结果");
      const detail = element("p", "automation-result", detailText.length > 240 ? `${detailText.slice(0, 240)}…` : detailText);
      const actions = element("div", "automation-actions");
      const inspect = element("button", "button button--secondary", "详情");
      inspect.type = "button";
      inspect.dataset.automationDetail = automation.id;
      inspect.setAttribute("aria-label", `查看定时任务详情：${text(automation.task_label)}`);
      const edit = element("button", "button button--ghost", "交给助理调整");
      edit.type = "button";
      edit.dataset.assistantPrompt = `调整定时任务“${text(automation.task_label, automation.task_id)}”，当前为每天 ${text(automation.start_time)} 执行。请先说明准备如何修改。`;
      actions.append(inspect, edit);
      if (automation.active) {
        const disable = element("button", "button button--secondary", "停用");
        disable.type = "button";
        disable.dataset.automationDisable = automation.id;
        disable.setAttribute("aria-label", `停用定时任务：${text(automation.task_label)}`);
        actions.appendChild(disable);
      }
      item.append(heading, timing, detail, actions);
      node.appendChild(item);
    });
    node.dataset.state = "ready";
  }

  async function loadAutomations() {
    const node = $("automation-list");
    if (node) loading(node);
    try {
      renderAutomations(await api("/api/automations"));
      return true;
    } catch (error) {
      errorState(node, "定时任务加载失败", error.message);
      setText("automation-list-count", "加载失败");
      showToast(`定时任务加载失败：${error.message}`, "error");
      return false;
    }
  }

  async function disableAutomation(scheduleId) {
    const automation = state.automations.find((item) => item.id === scheduleId);
    if (!automation) return;
    if (!window.confirm(`确定停用“${text(automation.task_label, "这个定时任务")}”吗？`)) return;
    try {
      await api(`/api/automations/${encodeURIComponent(scheduleId)}/disable`, {
        method: "POST",
        headers: authHeaders("停用定时任务"),
      });
      await loadAutomations();
      showToast("定时任务已停用", "success");
    } catch (error) {
      showToast(`停用失败：${error.message}`, "error");
    }
  }

  const MAIL_CATEGORY_LABELS = {
    application_confirmation: "投递确认",
    assessment: "测评",
    written_test: "笔试",
    interview: "面试",
    offer: "Offer",
    rejection: "拒信",
    other: "其他",
  };

  const MAIL_PROCESSING_STATUS_LABELS = {
    ambiguous_application: "多个候选，待确认",
    failed_terminal: "处理失败，需检查",
    needs_auth_metadata: "发件人验证信息不足",
    pending: "待处理",
    processed_updated: "已更新投递阶段",
    processed_unchanged: "已核对，无变化",
    processed: "已处理",
    irrelevant: "非招聘邮件",
    pending_association: "待关联",
    failed: "处理失败",
    needs_confirmation: "待人工确认",
    linked: "已关联",
    ignored: "已忽略",
  };

  const MAIL_FRESHNESS_STATUSES = new Set(["synced", "cached", "failed", "disabled"]);

  function normalizeMailStatus(value) {
    return String(value || "").trim().toLowerCase();
  }

  function mailProcessingLabel(status) {
    const normalized = normalizeMailStatus(status);
    return MAIL_PROCESSING_STATUS_LABELS[normalized] || "待处理";
  }

  function mailProcessingTone(status) {
    const normalized = normalizeMailStatus(status);
    if (["processed_updated", "processed_unchanged", "processed", "linked"].includes(normalized)) return "success";
    if (["failed"].includes(normalized)) return "error";
    if (["irrelevant", "ignored"].includes(normalized)) return "muted";
    return "pending";
  }

  function mailAssociationState(mail) {
    if (mail?.binding_state === "stale") return { key: "review", label: "原关联需重新确认" };
    if (mail?.binding_state === "unbound") return { key: "muted", label: "用户已解除" };
    if (mail?.binding_state === "confirmed") return { key: "linked", label: "用户已确认" };
    if (mail?.association_required === false) return { key: "muted", label: "无需关联" };
    if (mail?.application_id) return { key: "linked", label: "已关联" };
    const processing = normalizeMailStatus(mail?.processing_status);
    if (processing === "pending_association") return { key: "pending", label: "待关联" };
    if (["irrelevant", "ignored"].includes(processing)) return { key: "muted", label: "不适用" };
    if (processing === "failed") return { key: "error", label: "关联失败" };
    if (mail?.requires_confirmation || processing === "needs_confirmation") return { key: "review", label: "待确认" };
    return { key: "pending", label: "待关联" };
  }

  function mailFreshnessStatus(freshness, unavailable = false) {
    const status = normalizeMailStatus(freshness?.status);
    if (MAIL_FRESHNESS_STATUSES.has(status)) return status;
    return unavailable ? "disabled" : "unknown";
  }

  function mailDateTime(value, fallback = "时间待确认") {
    if (!value) return fallback;
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return fallback;
    return parsed.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
  }

  function mailFreshnessError(freshness) {
    return text(freshness?.error_type || freshness?.error || freshness?.reason, "同步失败");
  }

  function renderMailFreshness(freshness = {}, unavailable = false) {
    const status = mailFreshnessStatus(freshness, unavailable);
    state.mailFreshness = {
      ...freshness,
      status,
      synced_at: freshness?.synced_at || null,
      error_type: freshness?.error_type || null,
    };
    const node = $("mail-freshness-note");
    if (!node) return;
    node.dataset.state = status;
    node.hidden = status === "unknown";
    if (status === "synced") {
      setText("mail-freshness-title", "邮箱数据已同步");
      setText("mail-freshness-detail", freshness.synced_at ? `最近同步于 ${mailDateTime(freshness.synced_at)}` : "已完成最近一次邮箱同步。");
    } else if (status === "cached") {
      setText("mail-freshness-title", "当前显示本地缓存");
      setText("mail-freshness-detail", freshness.synced_at ? `最近一次同步于 ${mailDateTime(freshness.synced_at)}，尚未确认最新邮件。` : "尚未确认最新邮件，当前列表来自本地缓存。");
    } else if (status === "failed") {
      setText("mail-freshness-title", "同步失败，当前显示缓存");
      setText("mail-freshness-detail", state.mails.length ? `${mailFreshnessError(freshness)}；邮件列表可能不是最新。` : `${mailFreshnessError(freshness)}；暂无可用的本地邮件缓存。`);
    } else if (status === "disabled") {
      setText("mail-freshness-title", "邮箱同步未启用");
      setText("mail-freshness-detail", state.mails.length ? "当前显示本地已保存的邮件，请先启用只读邮箱连接。" : "请在本机配置只读邮箱连接后再同步。");
    }
  }

  function setMailSyncFeedback(status, title, detail, extra = {}) {
    state.mailSync = {
      ...state.mailSync,
      ...extra,
      status,
      error: extra.error || "",
    };
    const node = $("mail-sync-status");
    if (!node) return;
    node.dataset.state = status;
    node.hidden = status === "idle";
    setText("mail-sync-status-title", title);
    setText("mail-sync-status-detail", detail);
  }

  function mailSyncCounts(sync = {}) {
    const value = (key) => {
      const count = Number(sync?.[key]);
      return Number.isFinite(count) && count >= 0 ? count : 0;
    };
    return { fetched: value("fetched"), inserted: value("inserted"), reused: value("reused") };
  }

  function mailFreshnessFrom(payload) {
    return payload?.freshness || payload?.data?.freshness || {};
  }

  function renderMails(items, unavailable = false, freshness = null) {
    const node = $("mail-list"); clear(node);
    state.mails = Array.isArray(items) ? items : [];
    renderMailFreshness(freshness || (unavailable ? { status: "disabled" } : {}), unavailable);
    setText("nav-mail-count", state.mails.length);
    setText("mail-total-count", state.mails.length);
    setText("mail-confirm-count", state.mails.filter((item) => item.requires_confirmation).length);
    setText("mail-linked-count", state.mails.filter((item) => item.application_id).length);
    setText("mail-list-count", `${state.mails.length} 封`);
    if (unavailable && !state.mails.length) {
      if (state.mailFreshness.status === "failed") return errorState(node, "招聘邮件读取失败", "无法确认最新邮件，当前没有可用的本地缓存。");
      return empty(node, "招聘邮箱尚未连接", "请在本机配置只读邮箱连接并执行同步任务。");
    }
    if (!state.mails.length) return empty(node, "暂无招聘邮件", "同步后会在这里显示分类和关联状态。");
    state.mails.forEach((mail) => {
      const item = document.createElement("article"); item.className = "mail-row";
      const main = document.createElement("div"); main.className = "mail-row-main";
      const title = document.createElement("strong"); title.className = "mail-subject"; title.textContent = text(mail.subject, "无主题");
      const meta = document.createElement("div"); meta.className = "mail-tags";
      meta.appendChild(element("span", "mail-tag mail-tag--category", MAIL_CATEGORY_LABELS[mail.category] || text(mail.category, "其他")));
      const processingStatus = normalizeMailStatus(mail.processing_status) || "pending";
      const processing = element("span", `mail-tag mail-tag--${mailProcessingTone(processingStatus)}`, `处理：${mail.processing_label || mailProcessingLabel(processingStatus)}`);
      processing.dataset.status = processingStatus;
      meta.appendChild(processing);
      const association = mailAssociationState(mail);
      const associationTag = element("span", `mail-tag mail-tag--${association.key}`, `关联：${association.label}`);
      associationTag.dataset.status = association.key;
      meta.appendChild(associationTag);
      const sender = document.createElement("span"); sender.className = "mail-sender"; sender.textContent = text(mail.sender, "发件人待确认");
      main.append(title, meta, sender);
      const target = [mail.company_name, mail.job_title].filter(Boolean).join(" · ");
      if (target) main.appendChild(element("span", "mail-target", `投递目标：${target}`));
      const actions = element("div", "mail-row-actions");
      const open = element("button", "button button--ghost", "查看邮件");
      open.type = "button"; open.dataset.mailOpenId = text(mail.id, "");
      const review = element("button", "button button--secondary", "生成关联预览");
      review.type = "button"; review.dataset.mailReviewId = text(mail.id, "");
      const bindMail = element("button", "button button--ghost", mail.application_id ? "修改关联" : "关联投递");
      bindMail.type = "button"; bindMail.addEventListener("click", () => void openMailBinding(mail.id));
      actions.append(open, bindMail, review);
      item.append(main, actions); node.appendChild(item);
    });
    node.dataset.state = "ready";
  }

  async function loadRecruitmentMails({ showLoading = true } = {}) {
    if (showLoading) loading($("mail-list"));
    try {
      const response = await api("/api/recruitment-mails?limit=50&refresh=false");
      renderMails(response.items || [], Boolean(response.unavailable), mailFreshnessFrom(response));
      return response;
    } catch (error) {
      const cachedItems = state.mails.slice();
      const errorType = error.name && error.name !== "Error" ? error.name : error.message;
      const freshness = { status: "failed", error_type: errorType || "mail_list_unavailable" };
      renderMails(cachedItems, false, freshness);
      return { items: cachedItems, total: cachedItems.length, freshness };
    }
  }

  async function syncRecruitmentMails() {
    if (state.mailSync.status === "syncing") return;
    const button = $("mail-refresh-button");
    if (button) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.dataset.previousLabel = button.textContent;
      button.textContent = "同步中...";
    }
    setMailSyncFeedback("syncing", "正在同步招聘邮件", "正在从只读邮箱读取新邮件，请稍候。");
    let outcome = null;
    try {
      const result = await api("/api/recruitment-mails/sync?limit=100", { method: "POST" });
      const sync = result?.sync || result?.data?.sync || {};
      const counts = mailSyncCounts(sync);
      const status = normalizeMailStatus(result?.status || result?.data?.status);
      if (!["synced", "success", "succeeded"].includes(status)) {
        const failure = mailFreshnessError(result) || text(result?.reason, "邮箱同步未完成");
        outcome = {
          status: "failed",
          title: status === "disabled" ? "邮箱同步未启用" : "邮箱同步失败",
          detail: `${failure}；当前列表可能不是最新。`,
          extra: { ...counts, error: failure },
          toast: `邮箱同步失败：${failure}`,
        };
      } else if (counts.inserted > 0) {
        outcome = {
          status: "success",
          title: "邮箱同步完成",
          detail: `读取 ${counts.fetched} 封 · 新增 ${counts.inserted} 封 · 已存在 ${counts.reused} 封`,
          extra: counts,
          toast: `邮箱已同步：新增 ${counts.inserted} 封`,
        };
      } else {
        outcome = {
          status: "no-new",
          title: "邮箱已是最新",
          detail: `读取 ${counts.fetched} 封 · 没有新邮件${counts.reused ? ` · 已存在 ${counts.reused} 封` : ""}`,
          extra: counts,
          toast: "邮箱已是最新，没有新邮件",
        };
      }
    } catch (error) {
      const failure = error.message || "邮箱同步请求失败";
      outcome = {
        status: "failed",
        title: "邮箱同步失败",
        detail: `${failure}；当前列表可能不是最新。`,
        extra: { error: failure },
        toast: `邮箱同步失败：${failure}`,
      };
    }
    await loadRecruitmentMails({ showLoading: false });
    if (outcome) {
      setMailSyncFeedback(outcome.status, outcome.title, outcome.detail, outcome.extra);
      showToast(outcome.toast, outcome.status === "failed" ? "error" : (outcome.status === "success" ? "success" : "info"));
    }
    if (button) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = button.dataset.previousLabel || "同步邮件";
      delete button.dataset.previousLabel;
    }
  }

  async function reviewRecruitmentMail(recordId) {
    const node = $("mail-preview"); loading(node);
    try {
      const result = await api(`/api/recruitment-mails/${encodeURIComponent(recordId)}/review`, {
        method: "POST",
        headers: authHeaders("生成邮件关联预览", { "Content-Type": "application/json" }),
        body: JSON.stringify({ minimum_confidence: 0.65, minimum_margin: 0.15 }),
      });
      clear(node); node.dataset.state = "ready";
      const summary = document.createElement("p"); summary.className = "section-description";
      summary.textContent = `关联状态：${text(result.association?.status)} · 待审批草稿 ${result.approval_previews?.length || 0} 项`;
      const preview = document.createElement("pre"); preview.className = "approval-preview";
      preview.textContent = safeJson(compactResultValue(result));
      node.append(summary, preview);
      showToast("邮件关联预览已生成，尚未写入", "success");
    } catch (error) {
      empty(node, "关联预览未生成", error.message);
      showToast(error.message, "error");
    }
  }

  function createTodoCard(todo) {
    const card = document.createElement("article"); card.className = "todo-card";
    const head = document.createElement("div"); head.className = "todo-card-head";
    const status = document.createElement("span"); status.className = `status-label status-label--${todo.kind}`; status.textContent = todo.status;
    const kind = document.createElement("span"); kind.className = "todo-kind"; kind.textContent = todo.kindLabel;
    head.append(status, kind);
    const title = document.createElement("strong"); title.className = "todo-title"; title.textContent = todo.title;
    const detail = document.createElement("p"); detail.className = "todo-detail"; detail.textContent = todo.detail;
    const action = document.createElement("button"); action.className = "text-button todo-action"; action.type = "button"; action.textContent = todo.actionLabel;
    if (todo.taskType) { action.dataset.task = todo.taskType; action.dataset.question = todo.question || DEFAULT_QUESTIONS[todo.taskType]; }
    if (todo.view) action.dataset.openView = todo.view;
    card.append(head, title, detail, action);
    return card;
  }

  function renderDashboardTodos() {
    const node = $("today-todos"); clear(node);
    const pendingApprovals = state.approvals.filter((item) => item.status === "pending").length;
    const unconnected = state.companies.filter((item) => item.integration_status !== "connected").length;
    const todos = [
      {
        kind: state.schedules.length ? "pending" : "success",
        kindLabel: "日程",
        status: state.schedules.length ? `${state.schedules.length} 项待看` : "暂无",
        title: state.schedules.length ? "核对今天的笔试、面试和截止事项" : "今天没有已记录日程",
        detail: state.schedules.length ? "按时间查看下一项安排。" : "可以继续查询岗位或投递状态。",
        actionLabel: state.schedules.length ? "查看日程" : "询问助理",
        taskType: "today_schedule",
      },
      {
        kind: state.jobs.length ? "pending" : "success",
        kindLabel: "岗位",
        status: state.jobs.length ? `${state.jobTotal || state.jobs.length} 个新增` : "暂无",
        title: state.jobs.length ? "筛选今天新增的确认岗位" : "等待新的确认岗位",
        detail: "仅纳入 2027 校招且届别已确认的岗位。",
        actionLabel: "交给助理筛选",
        taskType: "today_new_jobs",
      },
      {
        kind: pendingApprovals ? "pending" : "success",
        kindLabel: "审批",
        status: pendingApprovals ? `${pendingApprovals} 项待确认` : "已清空",
        title: pendingApprovals ? "核对需要人工确认的操作预览" : "暂无待审批操作",
        detail: "所有写入都必须经过明确的人工确认。",
        actionLabel: pendingApprovals ? "打开审批中心" : "查看审批中心",
        view: "approvals",
      },
      {
        kind: unconnected ? "warning" : "success",
        kindLabel: "接入",
        status: unconnected ? `${unconnected} 家待确认` : "正常",
        title: unconnected ? "检查未完成的公司接入" : "当前公司接入状态正常",
        detail: "这里只读取配置，不会修改公司来源。",
        actionLabel: "查看接入",
        view: "companies",
      },
    ];
    todos.forEach((todo) => node.appendChild(createTodoCard(todo)));
    node.dataset.state = "ready";
    setText("today-todo-count", `${todos.filter((todo) => todo.kind !== "success").length} 项需关注`);
  }

  function renderDashboardLists() {
    const anomalies = $("today-anomalies"); clear(anomalies);
    const unconnected = state.companies.filter((item) => item.integration_status !== "connected");
    const repairCandidates = state.operationalReport?.repair_candidates || [];
    if (!unconnected.length && !repairCandidates.length) empty(anomalies, "暂无运营异常", "公司接入和最近一次运行报告均正常。");
    else {
      unconnected.slice(0, 4).forEach((company) => anomalies.appendChild(row(company.name, "待确认接入")));
      repairCandidates.slice(0, 6 - Math.min(unconnected.length, 4)).forEach((issue) => {
        const label = issue.company || issue.crawler_key || issue.type || "运行报告";
        const detail = issue.recommendation || issue.message || "需要人工复核";
        anomalies.appendChild(row(label, detail));
      });
    }

    const pending = $("pending-approvals"); clear(pending);
    const approvals = state.approvals.filter((item) => item.status === "pending");
    if (!approvals.length) empty(pending, "暂无待审批操作", "只读任务不会自动生成写操作。");
    else approvals.slice(0, 6).forEach((approval) => pending.appendChild(row(approval.operation, approval.id)));
  }

  function renderApprovals() {
    renderMailBindingApprovals();
    const node = $("approval-list"); clear(node);
    const pending = state.approvals.filter((item) => item.status === "pending");
    setText("approval-summary-count", pending.length); setText("approval-list-count", `${state.approvals.length} 条`);
    setText("nav-approval-count", pending.length);
    if (!state.approvals.length) return empty(node, "暂无审批请求", "只读任务不会自动生成写操作。");
    state.approvals.forEach((approval) => {
      if (approval.operation === "recruitment_mail_binding" && ["pending", "approved"].includes(approval.status)) {
        node.appendChild(mailBindingCard(approval)); return;
      }
      const card = document.createElement("article"); card.className = "approval-card";
      const title = document.createElement("strong"); title.textContent = `${text(approval.operation)} · ${text(approval.status)}`;
      const detail = document.createElement("p"); detail.textContent = `任务 ${text(approval.task_id)} · 幂等键 ${text(approval.idempotency_key)}`;
      card.append(title, detail);
      if (approval.preview) {
        const evidence = document.createElement("p"); evidence.className = "approval-evidence";
        evidence.textContent = `证据：${text(approval.preview.evidence_summary, "未提供")}`;
        const preview = document.createElement("pre"); preview.className = "approval-preview";
        preview.textContent = safeJson({
          target_id: approval.preview.target_id,
          before: approval.preview.before,
          after: approval.preview.after,
          payload: approval.preview.payload,
        });
        card.append(evidence, preview);
      }
      if (approval.status === "pending") {
        const actions = document.createElement("div"); actions.className = "inline-actions";
        ["approve", "reject"].forEach((decision) => {
          const button = document.createElement("button"); button.className = `button button--${decision === "approve" ? "primary" : "ghost"}`;
          button.type = "button"; button.textContent = decision === "approve" ? "批准预览" : "拒绝";
          button.addEventListener("click", () => decideApproval(approval.token_id, decision)); actions.appendChild(button);
        }); card.appendChild(actions);
      } else if (approval.status === "approved" && approval.operation === "browser_action") {
        const actions = document.createElement("div"); actions.className = "inline-actions";
        const pageUrl = approval.preview?.payload?.page_url;
        if (typeof pageUrl === "string" && /^https?:\/\//i.test(pageUrl)) {
          const link = document.createElement("a"); link.className = "button button--ghost";
          link.href = pageUrl; link.target = "_blank"; link.rel = "noopener noreferrer";
          link.textContent = "打开待复核页面"; actions.appendChild(link);
        }
        const copy = document.createElement("button"); copy.className = "button button--primary";
        copy.type = "button"; copy.textContent = "复制单次令牌";
        copy.addEventListener("click", () => copyApprovalToken(approval.token_id));
        actions.appendChild(copy); card.appendChild(actions);
      } else if (approval.status === "approved") {
        const actions = document.createElement("div"); actions.className = "inline-actions";
        const button = document.createElement("button"); button.className = "button button--primary";
        button.type = "button"; button.textContent = "执行已批准操作";
        button.addEventListener("click", () => executeApproval(approval.token_id));
        actions.appendChild(button); card.appendChild(actions);
      }
      node.appendChild(card);
    });
    node.dataset.state = "ready";
  }

  async function copyApprovalToken(tokenId) {
    if (!tokenId) return showToast("审批令牌不存在", "error");
    try {
      await navigator.clipboard.writeText(tokenId);
      showToast("单次令牌已复制，请在目标页面完成读取", "success");
    } catch (_error) {
      window.prompt("请复制单次审批令牌", tokenId);
    }
  }

  function renderTraces() {
    const node = $("trace-list"); clear(node); setText("trace-count", `${state.traces.length} 次调用`);
    if (!state.traces.length) return empty(node, "暂无执行轨迹", "执行一次助理任务后会显示工具调用。");
    state.traces.slice().reverse().slice(0, 40).forEach((trace) => {
      const item = document.createElement("div"); item.className = "trace-row";
      const main = document.createElement("div"); main.className = "trace-main";
      const title = document.createElement("strong"); title.className = "trace-name";
      title.textContent = `${text(trace.event_type || trace.kind)} · ${text(trace.method || trace.name)}`;
      const meta = document.createElement("span"); meta.className = "trace-timing";
      meta.textContent = `${trace.success === false ? "停止" : "成功"} · ${text(trace.latency_ms ?? trace.elapsed_ms, 0)} ms`;
      main.append(title, meta); item.appendChild(main);
      const detail = document.createElement("span"); detail.className = "trace-meta";
      const reason = trace.error_code
        ? `停止原因：${text(trace.error_code)}`
        : text(trace.phase || (trace.metadata?.read_only ? "只读工具" : "服务端记录"));
      detail.textContent = `${text(trace.thread_id, "未关联线程")} · ${reason}`; item.appendChild(detail); node.appendChild(item);
    });
    node.dataset.state = "ready";
  }

  function statusLabel(value) {
    return STATUS_LABELS[value] || text(value, "未知状态");
  }

  function statusClass(result, response) {
    if (result.status === "preview" || response.status === "preview") return "pending";
    if (result.status === "paused" || response.status === "paused") return "pending";
    if (result.status === "succeeded" || result.status === "success" || response.success === true) return "success";
    return "error";
  }

  function resultBlock(title) {
    const block = document.createElement("section"); block.className = "result-block";
    const heading = document.createElement("h4"); heading.className = "result-block-title"; heading.textContent = title;
    block.appendChild(heading); return block;
  }

  function renderPlan(node, plan) {
    if (!Array.isArray(plan) || !plan.length) return;
    const block = resultBlock("任务计划");
    const list = document.createElement("ol"); list.className = "plan-list";
    plan.forEach((step, index) => {
      const item = document.createElement("li"); item.className = "plan-item";
      const number = document.createElement("span"); number.className = "plan-index"; number.textContent = `${index + 1}.`;
      const value = document.createElement("span"); value.textContent = text(step);
      item.append(number, value); list.appendChild(item);
    });
    block.appendChild(list); node.appendChild(block);
  }

  function summarizeRecord(record) {
    if (!record || typeof record !== "object") return text(record);
    const preferred = [record.title, record.job_title, record.company_name, record.company_id, record.city, record.stage, record.status, record.match_score == null ? null : `匹配 ${record.match_score} 分`];
    const values = preferred.filter((value) => value !== null && value !== undefined && value !== "");
    if (values.length) return values.map((value) => text(value)).join(" · ");
    return Object.entries(record).slice(0, 3).map(([key, value]) => `${key}: ${text(value)}`).join(" · ");
  }

  function companyName(companyId) {
    return state.companies.find((item) => text(item.id, "") === text(companyId, ""))?.name || companyId;
  }

  function resultRecordKind(record) {
    if (record?.detail_url && record?.title && record?.company_id) return "job";
    if (record?.event_type && (record?.event_date || record?.event_time || record?.time_kind)) return "schedule";
    if (record?.job_title && record?.stage) return "application";
    if (record?.subject && record?.category) return "mail";
    if (record?.integration_status && record?.name) return "company";
    return "generic";
  }

  function renderBusinessRecord(record) {
    const kind = resultRecordKind(record);
    const item = document.createElement("li"); item.className = `business-result-row business-result-row--${kind}`;
    const content = document.createElement("div"); content.className = "business-result-copy";
    const title = document.createElement("strong");
    const meta = document.createElement("span");
    const actions = document.createElement("div"); actions.className = "business-result-actions";
    if (kind === "job") {
      title.textContent = text(record.title);
      meta.textContent = [companyName(record.company_id), record.city, record.match_score == null ? null : `匹配 ${record.match_score} 分`].filter(Boolean).join(" · ");
      const ask = document.createElement("button"); ask.type = "button"; ask.className = "text-action"; ask.textContent = "解释匹配";
      ask.addEventListener("click", () => void handleJobAction("ask", record)); actions.appendChild(ask);
      if (/^https?:\/\//i.test(record.detail_url || "")) {
        const link = document.createElement("a"); link.className = "text-action"; link.href = record.detail_url; link.target = "_blank"; link.rel = "noreferrer"; link.textContent = "查看岗位"; actions.appendChild(link);
      }
    } else if (kind === "schedule") {
      title.textContent = `${scheduleTimeLabel(record)} · ${text(record.title)}`;
      meta.textContent = [record.company_name, record.job_title, record.event_type].filter(Boolean).join(" · ");
      if (/^https?:\/\//i.test(record.location_or_link || "")) {
        const link = document.createElement("a"); link.className = "text-action"; link.href = record.location_or_link; link.target = "_blank"; link.rel = "noreferrer"; link.textContent = "打开安排"; actions.appendChild(link);
      }
    } else if (kind === "application") {
      title.textContent = `${text(record.company_name)} · ${text(record.job_title)}`;
      meta.textContent = `当前阶段：${text(record.stage)}${record.source_status ? ` · 官网：${record.source_status}` : ""}`;
      if (/^https?:\/\//i.test(record.record_url || "")) {
        const link = document.createElement("a"); link.className = "text-action"; link.href = record.record_url; link.target = "_blank"; link.rel = "noreferrer"; link.textContent = "查看投递"; actions.appendChild(link);
      }
    } else if (kind === "mail") {
      title.textContent = text(record.subject);
      meta.textContent = [record.sender, record.category, record.processing_status].filter(Boolean).join(" · ");
    } else if (kind === "company") {
      title.textContent = text(record.name);
      meta.textContent = [record.integration_status, record.crawler_key].filter(Boolean).join(" · ");
      if (/^https?:\/\//i.test(record.campus_url || "")) {
        const link = document.createElement("a"); link.className = "text-action"; link.href = record.campus_url; link.target = "_blank"; link.rel = "noreferrer"; link.textContent = "校招官网"; actions.appendChild(link);
      }
    } else {
      title.textContent = summarizeRecord(record);
    }
    content.append(title, meta); item.append(content, actions); return item;
  }

  function renderResultData(node, data) {
    if (data == null) return;
    const block = resultBlock("工具结果");
    const collection = ["items", "events", "companies", "matches"].find((key) => Array.isArray(data[key]));
    if (collection) {
      const list = document.createElement("ul"); list.className = "result-list business-result-list";
      const pageSize = 5;
      let visible = 0;
      const appendPage = () => {
        data[collection].slice(visible, visible + pageSize).forEach((record) => {
          list.appendChild(renderBusinessRecord(record));
        });
        visible = Math.min(visible + pageSize, data[collection].length);
      };
      appendPage();
      block.appendChild(list);
      const total = Number(data.total ?? data[collection].length);
      if (data[collection].length > visible) {
        const more = document.createElement("button");
        more.type = "button";
        more.className = "button button--ghost result-load-more";
        more.textContent = `再显示 ${Math.min(pageSize, data[collection].length - visible)} 条`;
        more.addEventListener("click", () => {
          appendPage();
          if (visible >= data[collection].length) more.remove();
          else more.textContent = `再显示 ${Math.min(pageSize, data[collection].length - visible)} 条`;
        });
        block.appendChild(more);
      }
      if (total > data[collection].length) {
        const note = document.createElement("p"); note.className = "result-copy";
        note.textContent = `本次返回 ${data[collection].length} 条，共 ${total} 条。可缩小查询范围查看更多细节。`;
        block.appendChild(note);
      }
    } else if (data.job || data.application || data.payload) {
      const summary = document.createElement("p"); summary.className = "result-copy";
      summary.textContent = summarizeRecord(data.job || data.application || data.payload);
      block.appendChild(summary);
    }
    const details = document.createElement("details"); details.className = "result-details";
    const summary = document.createElement("summary"); summary.textContent = "查看结构化结果";
    const pre = document.createElement("pre"); pre.className = "result-json";
    pre.textContent = safeJson(compactResultValue(data));
    details.append(summary, pre); block.appendChild(details);
    node.appendChild(block);

    if (data.preview_only === true || data.send_attempted === false || data.write_attempted === false) {
      const note = document.createElement("p"); note.className = "preview-safety-note";
      note.textContent = "这是只读预览：未写入投递记录，未发送外部操作。"; node.appendChild(note);
    }
  }

  function renderCitations(node, citations, title) {
    if (!Array.isArray(citations) || !citations.length) return;
    const block = resultBlock(title);
    const list = document.createElement("ul"); list.className = "citation-list";
    citations.forEach((citation) => {
      const item = document.createElement("li");
      const score = citation.metadata?.retrieval_score;
      const scoreText = score == null ? "" : ` · 相关度 ${Number(score).toFixed(2)}`;
      item.textContent = `${text(citation.source, "来源")} · ${text(citation.source_ref, "未提供引用定位")}${scoreText}`;
      list.appendChild(item);
    });
    block.appendChild(list); node.appendChild(block);
  }

  function renderCandidateProfile(node, profile) {
    if (!profile) return;
    const block = resultBlock("匹配依据");
    const directions = document.createElement("p");
    directions.className = "result-copy";
    directions.textContent = `目标方向：${(profile.target_directions || []).join("、") || "未配置"}`;
    block.appendChild(directions);

    const evidence = Array.isArray(profile.evidence) ? profile.evidence : [];
    if (evidence.length) {
      const list = document.createElement("ul");
      list.className = "citation-list";
      evidence.slice(0, 12).forEach((item) => {
        const row = document.createElement("li");
        row.textContent = `${text(item.text, "未提供能力描述")} · ${text(item.source_ref, profile.source_ref)}`;
        list.appendChild(row);
      });
      block.appendChild(list);
    }
    node.appendChild(block);
  }

  function executionStages(result) {
    const explicit = [
      ...(Array.isArray(result?.stages) ? result.stages : []),
      ...(Array.isArray(result?.events) ? result.events : []),
      ...(Array.isArray(result?.codex_events) ? result.codex_events : []),
      ...(Array.isArray(result?.execution_events) ? result.execution_events : []),
    ];
    if (explicit.length) return explicit;
    if (Array.isArray(result?.plan) && result.plan.length) {
      return result.plan.map((label) => ({ phase: "plan", label, status: result.status === "safe_stop" ? "stopped" : "completed" }));
    }
    return [];
  }

  function renderExecutionStages(node, result) {
    const stages = executionStages(result);
    if (!stages.length) return false;
    const block = document.createElement("section"); block.className = "execution-stages";
    const heading = document.createElement("h4"); heading.className = "result-block-title"; heading.textContent = "执行阶段";
    const list = document.createElement("ol"); list.className = "stage-list";
    stages.slice(0, 12).forEach((stage, index) => {
      const item = document.createElement("li"); item.className = `stage-item stage-item--${text(stage.status, "completed")}`;
      const marker = document.createElement("span"); marker.className = "stage-marker"; marker.textContent = `${index + 1}`;
      const copy = document.createElement("span"); copy.className = "stage-copy";
      const label = document.createElement("strong"); label.textContent = text(stage.label || stage.phase, "任务阶段");
      const detail = document.createElement("small"); detail.textContent = [text(stage.phase, "阶段"), stage.detail].filter(Boolean).join(" · ");
      copy.append(label, detail); item.append(marker, copy); list.appendChild(item);
    });
    block.append(heading, list); node.appendChild(block);
    return true;
  }

  function approvalForTask(task) {
    const approvals = new Map();
    const add = (approval) => {
      const token = approval?.token || approval?.decision?.token;
      const tokenId = approval?.token_id || approval?.decision?.token_id || token?.token_id;
      if (tokenId && !approvals.has(tokenId)) {
        approvals.set(tokenId, { ...token, ...approval, token_id: tokenId });
      }
    };
    const taskId = task?.task_id;
    if (taskId) {
      state.approvals
        .filter((approval) => approval.task_id === taskId || approval.preview?.task_id === taskId)
        .forEach(add);
    }
    (task.approvals || []).forEach(add);
    return [...approvals.values()];
  }

  function approvalPreviewsForTask(task) {
    const previews = [];
    const add = (value) => {
      if (value && typeof value === "object" && value.operation && value.idempotency_key) previews.push(value);
    };
    (task?.tool_response?.data?.approval_previews || []).forEach(add);
    const registered = new Set(state.approvals.map((item) => item.idempotency_key));
    return previews.filter((item, index) => (
      !registered.has(item.idempotency_key)
      && previews.findIndex((other) => other.idempotency_key === item.idempotency_key) === index
    ));
  }

  async function createApprovalFromPreview(preview) {
    try {
      await api("/api/approvals", {
        method: "POST",
        headers: authHeaders("登记审批预览", { "Content-Type": "application/json" }),
        body: JSON.stringify(preview),
      });
      state.approvals = normalizeApprovals(await api("/api/approvals"));
      renderApprovals(); renderDashboardLists(); renderDashboardTodos(); renderConversation();
      showToast("审批已登记，等待你的批准或拒绝", "success");
    } catch (error) { showToast(error.message, "error"); }
  }

  function renderInlineApprovals(node, task) {
    if (!task) return;
    const approvals = approvalForTask(task).filter((approval) => !(
      approval.operation === "browser_action"
      && approval.preview?.payload?.command_authorized === true
      && ["approved", "consumed"].includes(approval.status)
    ));
    const previews = approvalPreviewsForTask(task);
    if (!approvals.length && !previews.length) return;
    const section = document.createElement("section"); section.className = "inline-approvals";
    const heading = document.createElement("div"); heading.className = "inline-approval-heading";
    const title = document.createElement("strong"); title.textContent = "需要人工确认";
    const count = document.createElement("span"); count.textContent = `${approvals.length + previews.length} 项`;
    heading.append(title, count); section.appendChild(heading);
    approvals.forEach((approval) => {
      const card = document.createElement("article"); card.className = "inline-approval-card";
      card.dataset.approvalId = text(approval.token_id, "");
      card.setAttribute("data-approval-id", text(approval.token_id, ""));
      const cardTitle = document.createElement("strong"); cardTitle.textContent = `${text(approval.operation, "操作")} · ${text(approval.status, "待审批")}`;
      const meta = document.createElement("span"); meta.className = "inline-approval-meta";
      meta.textContent = `幂等键：${text(approval.idempotency_key, "未提供")}`;
      card.append(cardTitle, meta);
      if (approval.preview) {
        const evidence = document.createElement("p"); evidence.className = "inline-approval-evidence";
        evidence.textContent = `证据：${text(approval.preview.evidence_summary, "未提供")}`;
        card.appendChild(evidence);
        const preview = document.createElement("pre"); preview.className = "approval-preview";
        preview.textContent = safeJson({ target_id: approval.preview.target_id, before: approval.preview.before, after: approval.preview.after, payload: approval.preview.payload });
        card.appendChild(preview);
      }
      const actions = document.createElement("div"); actions.className = "inline-actions inline-approval-actions";
      if (approval.status === "pending") {
        ["approve", "reject"].forEach((decision) => {
          const button = document.createElement("button"); button.className = `button button--${decision === "approve" ? "approve" : "reject"}`;
          button.type = "button"; button.textContent = decision === "approve" ? "批准" : "拒绝";
          button.addEventListener("click", () => void decideApproval(approval.token_id, decision)); actions.appendChild(button);
        });
      } else if (approval.status === "approved" && approval.operation === "browser_action") {
        const pageUrl = approval.preview?.payload?.page_url;
        if (typeof pageUrl === "string" && /^https?:\/\//i.test(pageUrl)) {
          const link = document.createElement("a"); link.className = "button button--ghost"; link.href = pageUrl; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = "打开页面"; actions.appendChild(link);
        }
        const button = document.createElement("button"); button.className = "button button--primary"; button.type = "button"; button.textContent = "复制令牌";
        button.addEventListener("click", () => void copyApprovalToken(approval.token_id)); actions.appendChild(button);
      } else if (approval.status === "approved") {
        const button = document.createElement("button"); button.className = "button button--primary"; button.type = "button"; button.textContent = "执行";
        button.addEventListener("click", () => void executeApproval(approval.token_id)); actions.appendChild(button);
      }
      if (actions.childElementCount) card.appendChild(actions);
      section.appendChild(card);
    });
    previews.forEach((preview) => {
      const card = document.createElement("article"); card.className = "inline-approval-card";
      const cardTitle = document.createElement("strong"); cardTitle.textContent = `${text(preview.operation, "操作")} · 审批预览`;
      const evidence = document.createElement("p"); evidence.className = "inline-approval-evidence";
      evidence.textContent = `证据：${text(preview.evidence_summary, "未提供")}`;
      const detail = document.createElement("pre"); detail.className = "approval-preview";
      detail.textContent = safeJson({ target_id: preview.target_id, before: preview.before, after: preview.after, payload: preview.payload });
      const actions = document.createElement("div"); actions.className = "inline-actions inline-approval-actions";
      const button = document.createElement("button"); button.className = "button button--primary"; button.type = "button"; button.textContent = "创建审批";
      button.addEventListener("click", () => void createApprovalFromPreview(preview));
      actions.appendChild(button); card.append(cardTitle, evidence, detail, actions); section.appendChild(card);
    });
    node.appendChild(section);
  }

  function renderTaskResult(result, targetId) {
    const node = $(targetId); if (!node) return;
    clear(node); node.dataset.state = "ready";
    const response = result.tool_response || {};
    const header = document.createElement("div"); header.className = "result-header";
    const statusLine = document.createElement("div"); statusLine.className = "result-status-line";
    const badge = document.createElement("span"); badge.className = `status-label status-label--${statusClass(result, response)}`;
    badge.textContent = statusLabel(response.status || result.status);
    const title = document.createElement("strong"); title.textContent = `${result.display_label || TASK_LABELS[result.task_type] || text(result.task_type, "助理任务")} · ${text(result.steps, 0)} 步`;
    statusLine.append(badge, title);
    const meta = document.createElement("span"); meta.className = "result-meta";
    meta.textContent = [response.tool_name, response.elapsed_ms == null ? null : `${response.elapsed_ms} ms`, result.current_step ? `当前：${friendlyRuntimeProgress(result.current_step)}` : null].filter(Boolean).join(" · ");
    header.append(statusLine, meta); node.appendChild(header);

    if (result.answer) {
      const answer = resultBlock("助理回答");
      const copy = document.createElement("p"); copy.className = "result-copy"; copy.textContent = text(result.answer, "");
      answer.appendChild(copy); node.appendChild(answer);
    }
    const noResults = response.status === "no_results";
    const reasons = noResults
      ? [result.error && result.error !== "no_results" ? result.error : null].filter(Boolean)
      : [result.error, response.error_message, response.error_code && `错误码 ${response.error_code}`, response.timed_out && "工具已超时"].filter(Boolean);
    if (reasons.length) {
      const reason = document.createElement("div"); reason.className = "result-reason";
      const label = document.createElement("strong"); label.textContent = "失败或停止原因";
      const detail = document.createElement("p"); detail.textContent = [...new Set(reasons.map((item) => localizeCodexRuntimeMessage(item)))].join(" · ");
      reason.append(label, detail); node.appendChild(reason);
    }

    if (!renderExecutionStages(node, result) && result.plan?.length) renderPlan(node, result.plan);
    if (response.data !== undefined && response.data !== null) renderResultData(node, response.data);
    renderCitations(node, response.evidence, "工具引用");
    if (result.grounded_evidence) {
      if (result.grounded_evidence.answerable === false) {
        const refusal = resultBlock("引用状态");
        const copy = document.createElement("p"); copy.className = "result-copy";
        copy.textContent = `证据不足：${text(result.grounded_evidence.refusal_reason, "没有达到可信阈值的引用")}`;
        refusal.appendChild(copy); node.appendChild(refusal);
      }
      renderCitations(node, result.grounded_evidence.citations, "知识库引用");
    }
    renderCandidateProfile(node, result.candidate_profile);
    renderInlineApprovals(node, result);
    if (!response.data && !response.error_message && !reasons.length && !result.plan?.length) {
      const copy = document.createElement("p"); copy.className = "result-copy"; copy.textContent = "任务已返回，但没有可展开的结果字段。"; node.appendChild(copy);
    }
  }

  function renderTaskHistory() {
    const node = $("task-history"); clear(node); setText("task-history-count", `${state.tasks.length} 条`);
    setText("nav-task-count", state.tasks.length);
    if (!state.tasks.length) return empty(node, "暂无任务记录", "执行一次助理任务后会显示在这里。");
    state.tasks.slice().reverse().forEach((task) => {
      const button = document.createElement("button"); button.type = "button"; button.className = "history-row";
      button.setAttribute("aria-pressed", String(state.selectedTask === task));
      const title = document.createElement("strong"); title.className = "task-title"; title.textContent = task.user_request || task.display_label || TASK_LABELS[task.task_type] || text(task.task_type);
      const meta = document.createElement("span"); meta.className = "task-meta"; meta.textContent = `${task.display_label || TASK_LABELS[task.task_type] || text(task.task_type)} · ${statusLabel(task.status)} · ${text(task.steps, 0)} 步${task.local_preview ? " · 本地预览" : ""}`;
      button.append(title, meta);
      button.addEventListener("click", () => {
        state.selectedTask = task; renderTaskHistory(); renderTaskResult(task, "selected-task-result");
      });
      node.appendChild(button);
    });
    node.dataset.state = "ready";
  }

  function renderWelcomeMessage(node) {
    const article = document.createElement("article"); article.className = "message message--assistant";
    const meta = document.createElement("div"); meta.className = "message-meta";
    const author = document.createElement("strong"); author.textContent = "求职助理";
    const time = document.createElement("span"); time.textContent = "待命";
    meta.append(author, time);
    const copy = document.createElement("p");
    copy.textContent = assistantAvailability().status !== "ready"
      ? codexUnavailableMessage()
      : "发送第一条问题开始对话。简历和岗位关键词可稍后配置。";
    article.append(meta, copy); node.appendChild(article);
  }

  function renderMessageRecord(record, node, expandResult = false) {
    const article = document.createElement("article"); article.className = `message message--${record.role === "user" ? "user" : "assistant"}`;
    article.dataset.messageId = record.id || "";
    if (record.streaming) article.dataset.streaming = "true";
    const meta = document.createElement("div"); meta.className = "message-meta";
    const author = document.createElement("strong"); author.textContent = record.role === "user" ? "你" : "求职助理";
    const time = document.createElement("span"); time.textContent = record.created_at ? new Date(record.created_at).toLocaleTimeString() : "刚刚";
    meta.append(author, time);
    const copy = document.createElement("div"); copy.className = "message-copy";
    if (record.role === "assistant") renderMarkdown(copy, localizeCodexRuntimeMessage(record.body));
    else {
      const paragraph = document.createElement("p"); paragraph.textContent = text(record.body, "");
      copy.appendChild(paragraph);
    }
    article.append(meta, copy);
    const result = record.task_id
      ? state.tasks.find((task) => task.local_task_id === record.task_id || task.task_id === record.task_id)
      : record.result;
    if (result) {
      const disclosure = document.createElement("details");
      disclosure.className = "message-result-details";
      disclosure.open = expandResult;
      const summary = document.createElement("summary");
      summary.textContent = "查看执行过程与结构化结果";
      const details = document.createElement("div"); details.className = "message-result";
      const key = `message-result-${record.id || Date.now()}-${Math.random().toString(16).slice(2)}`;
      details.id = key;
      let rendered = false;
      const ensureRendered = () => {
        if (rendered) return;
        rendered = true;
        renderTaskResult(result, key);
      };
      disclosure.addEventListener("toggle", () => {
        if (disclosure.open) ensureRendered();
      }, { once: true });
      disclosure.append(summary, details);
      article.appendChild(disclosure);
      node.appendChild(article);
      if (expandResult) ensureRendered();
    } else {
      node.appendChild(article);
    }
  }

  const CONTEXT_LABELS = {
    job_id: "岗位",
    job_title: "岗位名称",
    company_id: "公司",
    company_name: "公司",
    city: "城市",
    min_score: "最低匹配",
    query: "筛选",
    last_task_type: "最近任务",
    application_id: "投递记录",
  };

  function renderAssistantContext(context = state.assistantContext) {
    state.assistantContext = context && typeof context === "object" ? { ...context } : {};
    const entries = Object.entries(state.assistantContext).filter(([, value]) => value !== null && value !== undefined && value !== "");
    const chips = $("assistant-context-chips");
    clear(chips);
    entries.slice(0, 4).forEach(([key, value]) => {
      const chip = document.createElement("span"); chip.className = "context-chip";
      chip.textContent = `${CONTEXT_LABELS[key] || key}：${text(value)}`;
      chips.appendChild(chip);
    });

    const panel = $("assistant-context-panel"); clear(panel);
    if (!entries.length) {
      const row = document.createElement("div");
      const term = document.createElement("dt"); term.textContent = "状态";
      const value = document.createElement("dd"); value.textContent = "尚未选择岗位或公司";
      row.append(term, value); panel.appendChild(row);
    } else {
      entries.slice(0, 8).forEach(([key, raw]) => {
        const row = document.createElement("div");
        const term = document.createElement("dt"); term.textContent = CONTEXT_LABELS[key] || key;
        const value = document.createElement("dd"); value.textContent = text(raw);
        row.append(term, value); panel.appendChild(row);
      });
    }
    setText("context-field-count", entries.length);
  }

  function codexThreadTitle(thread) {
    return text(thread?.preview || thread?.name || thread?.title, "新 Codex 会话");
  }

  function codexThreadUpdatedAt(thread) {
    const value = thread?.updatedAt ?? thread?.updated_at ?? thread?.recencyAt ?? thread?.recency_at;
    if (value === null || value === undefined || value === "") return "刚刚";
    return new Date(codexTimestamp(value)).toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function renderConversationList() {
    const node = $("conversation-list");
    if (!node) return;
    clear(node);
    const activeThreadId = state.codexPendingThreadId || state.codexThreadId;
    const controls = $("conversation-list-controls");
    const status = $("conversation-list-status");
    const loadMore = $("conversation-load-more-button");
    if (controls) controls.hidden = false;

    if (!state.conversations.length) {
      if (state.codexThreadListLoading) loading(node);
      else if (state.codexThreadListError) errorState(node, "会话列表暂不可用", state.codexThreadListError);
      else empty(node, "暂无历史会话", "还没有可恢复的 Codex 会话。");
    } else {
      state.conversations.forEach((conversation) => {
        const item = element("div", "conversation-list-item");
        const button = document.createElement("button"); button.type = "button";
        const threadId = codexThreadIdOf(conversation);
        if (!threadId) return;
        button.className = `conversation-item${threadId === activeThreadId ? " is-active" : ""}`;
        button.dataset.threadId = threadId || "";
        const title = document.createElement("strong");
        title.textContent = codexThreadTitle(conversation);
        const meta = document.createElement("span");
        const turnCount = conversation.turn_count ?? conversation.turnCount ?? (Array.isArray(conversation.turns) ? conversation.turns.length : null);
        const countLabel = turnCount === null || turnCount === undefined ? "Codex 线程" : `${turnCount} 轮`;
        meta.textContent = `${countLabel} · ${codexThreadUpdatedAt(conversation)}`;
        button.title = codexThreadTitle(conversation);
        button.append(title, meta);
        button.addEventListener("click", () => void loadConversation(threadId));
        const remove = appendUiIcon(element("button", "conversation-delete-button"), "close");
        remove.type = "button";
        remove.title = "删除会话";
        remove.setAttribute("aria-label", `删除会话：${codexThreadTitle(conversation)}`);
        remove.addEventListener("click", (event) => {
          event.stopPropagation();
          void deleteConversation(threadId);
        });
        item.append(button, remove);
        node.appendChild(item);
      });
      node.dataset.state = state.codexThreadListLoading ? "loading" : "ready";
    }
    node.setAttribute("aria-busy", String(state.codexThreadListLoading));
    if (status) {
      status.textContent = state.codexThreadListLoading
        ? "正在加载会话…"
        : state.codexThreadListError
          ? state.codexThreadListError
          : state.codexThreadCursor
            ? "还有历史会话"
            : state.conversations.length
              ? "已加载全部会话"
              : "";
      status.dataset.state = state.codexThreadListError ? "error" : "ready";
    }
    if (loadMore) {
      loadMore.hidden = !state.codexThreadCursor;
      loadMore.disabled = state.codexThreadListLoading;
      loadMore.textContent = state.codexThreadListLoading ? "加载中…" : "加载更多";
    }
  }

  async function refreshConversationList(options = {}) {
    if (state.codexEnabled !== true || state.codexReady !== true) {
      state.codexThreadListError = codexUnavailableMessage();
      renderConversationList();
      return false;
    }
    const append = options.append === true;
    if (state.codexThreadListLoading) return false;
    const cursor = append ? state.codexThreadCursor : "";
    if (append && !cursor) return true;
    state.codexThreadListLoading = true;
    if (!append) {
      state.conversations = [];
      state.codexThreadCursor = "";
      state.codexThreadListError = "";
    }
    renderConversationList();
    try {
      const path = cursor
        ? `/api/codex/threads?limit=20&cursor=${encodeURIComponent(cursor)}`
        : "/api/codex/threads?limit=20";
      const response = await api(path);
      const rows = Array.isArray(response?.data)
        ? response.data
        : Array.isArray(response?.threads)
          ? response.threads
          : [];
      state.conversations = append ? state.conversations.concat(rows) : rows;
      const nextCursor = response?.next_cursor ?? response?.nextCursor;
      state.codexThreadCursor = typeof nextCursor === "string" ? nextCursor.trim() : text(nextCursor, "").trim();
      state.codexThreadListError = "";
      return true;
    } catch (error) {
      state.codexThreadListError = error.message;
      if (!append) state.conversations = [];
      return false;
    } finally {
      state.codexThreadListLoading = false;
      renderConversationList();
    }
  }

  function useEmptyCodexThread(threadId) {
    state.codexThreadId = threadId;
    state.codexThreadMetadata = null;
    state.messages = [];
    state.tasks = [];
    state.selectedTask = null;
    state.codexHistoryError = "";
    writeStorage(STORAGE_KEYS.codexThread, threadId);
    persistConversation();
    renderAssistantContext({});
    renderConversation();
    renderTaskHistory();
    setAssistantStatus("新会话等待首次提问", "ok");
  }

  async function loadConversation(threadId) {
    if (state.codexEnabled !== true || state.codexReady !== true) {
      setAssistantStatus("Codex 运行时不可用", "error");
      return false;
    }
    const selectedThreadId = codexThreadIdOf(threadId);
    if (!selectedThreadId) return false;
    const requestId = state.codexHistoryRequestId + 1;
    state.codexHistoryRequestId = requestId;
    state.codexPendingThreadId = selectedThreadId;
    state.codexHistoryLoading = true;
    state.codexHistoryError = "";
    loading($("assistant-messages"));
    setAssistantStatus("正在恢复 Codex 会话", "warn");
    renderConversationList();
    try {
      const detailResponse = await api(`/api/codex/threads/${encodeURIComponent(selectedThreadId)}`);
      if (requestId !== state.codexHistoryRequestId) return false;
      const detailThread = codexThreadPayload(detailResponse);
      if (!Array.isArray(detailThread.turns) || !detailThread.turns.length) {
        useEmptyCodexThread(selectedThreadId);
        return true;
      }
      const resumeResponse = await api(`/api/codex/threads/${encodeURIComponent(selectedThreadId)}/resume`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (requestId !== state.codexHistoryRequestId) return false;
      const resumedThread = codexThreadPayload(resumeResponse);
      const historyThread = !Array.isArray(detailThread.turns) && Array.isArray(resumedThread.turns)
        ? resumedThread
        : detailThread;
      state.codexThreadId = selectedThreadId;
      state.codexThreadMetadata = historyThread;
      state.messages = codexHistoryMessages(historyThread);
      state.tasks = [];
      state.selectedTask = null;
      state.assistantContext = {};
      state.codexHistoryError = "";
      writeStorage(STORAGE_KEYS.codexThread, selectedThreadId);
      resetCodexEventStatuses();
      persistConversation();
      renderAssistantContext({});
      renderConversation();
      renderTaskHistory();
      renderConversationList();
      setAssistantStatus(state.messages.length ? "已恢复 Codex 会话" : "已恢复空会话", "ok");
      return true;
    } catch (error) {
      if (requestId !== state.codexHistoryRequestId) return false;
      if (/not materialized|includeTurns is unavailable/i.test(error.message || "")) {
        useEmptyCodexThread(selectedThreadId);
        return true;
      }
      state.codexHistoryError = error.message;
      errorState($("assistant-messages"), "会话恢复失败", error.message);
      setAssistantStatus(`会话恢复失败：${error.message}`, "error");
      if (!$("assistant-view")?.hidden) showToast(`会话读取失败：${error.message}`, "error");
      return false;
    } finally {
      if (requestId === state.codexHistoryRequestId) {
        state.codexHistoryLoading = false;
        state.codexPendingThreadId = "";
        renderConversationList();
      }
    }
  }

  async function loadConversationWorkspace() {
    renderCodexRuntimeStatus();
    if (state.codexEnabled !== true || state.codexReady !== true) {
      renderConversationList();
      renderAssistantContext({});
      renderConversation();
      renderTaskHistory();
      return;
    }
    const serverAvailable = await refreshConversationList();
    if (!serverAvailable) {
      renderAssistantContext(state.assistantContext);
      renderConversation();
      renderTaskHistory();
      return;
    }
    const currentThreadId = state.codexThreadId;
    const firstThreadId = state.conversations.length ? codexThreadIdOf(state.conversations[0]) : "";
    const targetThreadId = currentThreadId || firstThreadId;
    const targetThread = state.conversations.find((thread) => codexThreadIdOf(thread) === targetThreadId);
    const targetTurnCount = targetThread?.turn_count ?? targetThread?.turnCount;
    if (targetThreadId && Number(targetTurnCount) === 0) useEmptyCodexThread(targetThreadId);
    else if (targetThreadId) {
      const loaded = await loadConversation(targetThreadId);
      if (!loaded && firstThreadId && firstThreadId !== targetThreadId) {
        await loadConversation(firstThreadId);
      }
    } else {
      state.messages = [];
      state.tasks = [];
      state.selectedTask = null;
      renderAssistantContext({});
      persistConversation();
      renderConversation();
      renderTaskHistory();
    }
  }

  function renderConversation(shouldScroll = false) {
    const node = $("assistant-messages"); if (!node) return;
    clear(node);
    if (!state.messages.length) renderWelcomeMessage(node);
    else {
      const visibleMessages = state.messages.slice(-50);
      const hiddenCount = state.messages.length - visibleMessages.length;
      if (hiddenCount > 0) {
        const note = document.createElement("p");
        note.className = "conversation-window-note";
        note.textContent = `为保持页面流畅，较早的 ${hiddenCount} 条消息未在当前窗口渲染。`;
        node.appendChild(note);
      }
      visibleMessages.forEach((record) => renderMessageRecord(record, node, false));
    }
    node.dataset.state = state.messages.length ? "ready" : "empty";
    setText("assistant-thread-label", `当前会话 · ${state.messages.length} 条`);
    if (shouldScroll) node.scrollTop = node.scrollHeight;
  }

  function appendMessage(role, body, result = null, options = {}) {
    const record = {
      id: `message-${Date.now()}-${Math.random().toString(16).slice(2)}`,
      role,
      body,
      created_at: new Date().toISOString(),
      task_id: result?.local_task_id || result?.task_id || null,
      result,
      streaming: Boolean(options.streaming),
    };
    state.messages.push(record);
    state.messages = state.messages.slice(-60);
    persistConversation();
    renderConversation(true);
    return record;
  }

  function updateMessage(recordId, body, result = undefined, streaming = undefined) {
    const record = state.messages.find((item) => item.id === recordId);
    if (!record) return;
    record.body = body;
    if (result !== undefined) {
      record.result = result;
      record.task_id = result?.local_task_id || result?.task_id || null;
    }
    if (streaming !== undefined) record.streaming = Boolean(streaming);
    persistConversation();
    const article = Array.from($("assistant-messages")?.querySelectorAll(".message") || [])
      .find((item) => item.dataset.messageId === recordId);
    if (!article) {
      renderConversation(true);
      return;
    }
    if (record.streaming) article.dataset.streaming = "true";
    else delete article.dataset.streaming;
    const copy = article.querySelector(".message-copy");
    if (copy) renderMarkdown(copy, body);
    const messageList = $("assistant-messages");
    if (messageList) messageList.scrollTop = messageList.scrollHeight;
  }

  function mapIntent(message) {
    const normalized = text(message, "").trim().toLowerCase();
    const match = INTENT_RULES.find((rule) => rule.terms.some((term) => normalized.includes(term)));
    if (match) return { taskType: match.taskType, label: match.label, matched: true };
    return { taskType: "today_new_jobs", label: "今日岗位", matched: false };
  }

  function updateIntentHint(message = $("assistant-message")?.value || "") {
    const target = $("assistant-intent"); if (!target) return;
    const copy = target.querySelector("span:last-child"); if (!copy) return;
    const intent = mapIntent(message);
    copy.textContent = message.trim() ? `识别为：${intent.label}${intent.matched ? "" : "（默认岗位查询）"}` : "等待识别问题";
  }

  function rememberTask(task) {
    const remembered = { ...task, local_task_id: task.local_task_id || `task-${Date.now()}-${Math.random().toString(16).slice(2)}` };
    state.tasks.push(remembered);
    state.tasks = state.tasks.slice(-30);
    state.selectedTask = remembered;
    persistConversation();
    renderTaskHistory();
    return remembered;
  }

  async function refreshTraces() {
    try {
      state.traces = await api("/api/codex/traces"); renderTraces();
    } catch (error) {
      showToast(`任务已完成，但轨迹刷新失败：${error.message}`, "error");
    }
  }

  async function runCodexAssistantQuery(message, jobId = "", streamingMessageId = "") {
    if (state.codexEnabled === false) throw new Error(codexUnavailableMessage());
    if (state.codexEnabled !== true || state.codexReady !== true) throw new Error(codexUnavailableMessage());
    if (!state.codexThreadId) await createCodexThread();
    const controller = new AbortController();
    state.activeAssistantController = controller;
    state.codexStopRequested = false;
    state.codexTurnId = "";
    let resolveTurnReady = null;
    state.codexTurnReady = new Promise((resolve) => { resolveTurnReady = resolve; });
    const cleanupCodexRun = () => {
      resolveTurnReady?.(state.codexTurnId || "");
      state.codexTurnReady = null;
      state.activeAssistantController = null;
    };
    resetCodexEventStatuses();
    setCodexEventStatus("thread", state.codexThreadId, "ok");
    setCodexEventStatus("error", "无", "");
    const streamPath = `/api/codex/threads/${encodeURIComponent(state.codexThreadId)}/turns/stream`;
    const eventPath = `/api/codex/threads/${encodeURIComponent(state.codexThreadId)}/events`;
    const requestBody = jobId ? {
      body: JSON.stringify({ text: message, job_id: jobId }),
    } : { body: JSON.stringify({ text: message }) };
    let response;
    try {
      response = await fetch(streamPath, {
        method: "POST",
        headers: {
          Accept: "text/event-stream",
          "Content-Type": "application/json",
        },
        signal: controller.signal,
        ...requestBody,
      });
    } catch (error) {
      cleanupCodexRun();
      throw error;
    }
    if (!response.ok || !response.body || typeof response.body.getReader !== "function") {
      let detail = `Codex 请求失败（${response.status}）`;
      try { detail = (await response.json())?.detail || detail; } catch (_) { /* stream may not be JSON */ }
      setCodexEventStatus("error", detail, "error");
      cleanupCodexRun();
      throw new Error(detail);
    }

    let streamedAnswer = "";
    let streamError = null;
    let turnCompleted = false;
    const eventStages = [];
    const seenEventIds = new Set();
    let lastEventId = "";
    let lastEventFingerprint = "";
    let reconnectReplayFence = false;
    let reconnectAttempts = 0;
    let activeTurnId = "";
    const live = $("assistant-live-run");
    live.hidden = false;

    const pick = (...values) => values.find((value) => typeof value === "string" && value.trim())?.trim() || "";
    const objectValue = (value) => value && typeof value === "object" ? value : {};
    const eventDetail = (payload, data, fallback = "状态已更新") => pick(
      payload?.text,
      payload?.message,
      data?.message,
      typeof data?.error === "string" ? data.error : "",
      data?.error?.message,
      data?.error?.detail,
      payload?.error?.message,
      payload?.error?.detail,
      data?.detail,
      data?.status,
      data?.phase,
    ) || fallback;
    const friendlyRuntimeError = (value) => {
      const raw = localizeCodexRuntimeMessage(text(value, "模型运行时返回了未说明的错误。"));
      if (/\b402\b.*insufficient balance|insufficient balance.*\b402\b/i.test(raw)) {
        return "Agent 专用 DeepSeek API 余额不足，请充值或更换 Key。";
      }
      return raw;
    };

    const cancelledError = () => {
      const error = new Error("Codex 执行已取消。");
      error.name = "AbortError";
      return error;
    };
    const isCancelled = () => controller.signal.aborted || state.codexStopRequested;
    const eventIdOf = (event) => {
      const data = objectValue(event?.payload);
      const value = [
        event?.event_id,
        event?.eventId,
        data.event_id,
        data.eventId,
        event?.sequence,
        event?.seq,
        data.sequence,
        data.seq,
      ].find((candidate) => (typeof candidate === "string" && candidate.trim()) || Number.isFinite(candidate));
      return value === undefined || value === null ? "" : String(value);
    };
    const eventFingerprintOf = (eventName, event) => safeJson({
      eventName,
      event_type: event?.event_type,
      method: event?.method,
      thread_id: event?.thread_id,
      turn_id: event?.turn_id,
      item_id: event?.item_id,
      text: event?.text,
      payload: event?.payload,
    });
    const shouldSkipDuplicate = (eventName, event) => {
      const eventId = eventIdOf(event);
      if (eventId) {
        const key = `id:${eventId}`;
        if (seenEventIds.has(key)) return true;
        seenEventIds.add(key);
        lastEventId = eventId;
        return false;
      }
      const fingerprint = eventFingerprintOf(eventName, event);
      if (reconnectReplayFence && fingerprint === lastEventFingerprint) {
        reconnectReplayFence = false;
        return true;
      }
      reconnectReplayFence = false;
      lastEventFingerprint = fingerprint;
      return false;
    };

    const appendEventStage = (stage) => {
      if (!stage.label || stage.event_type === "text_delta") return;
      const previous = eventStages[eventStages.length - 1];
      if (previous && previous.event_type === stage.event_type && previous.label === stage.label) return;
      eventStages.push(stage);
    };

    const handleEvent = (eventName, payload) => {
      if (isCancelled() || turnCompleted) return;
      const event = objectValue(payload);
      const kind = text(event.event_type, eventName).toLowerCase();
      const data = objectValue(event.payload);
      const method = text(event.method, eventName);
      const signal = `${kind} ${eventName} ${method} ${text(data.type, "")} ${text(data.item_type, "")}`.toLowerCase();
      const threadId = pick(event.thread_id, event.threadId, data.thread_id, data.threadId);
      const turnId = pick(event.turn_id, event.turnId, data.turn_id, data.turnId, kind === "turn" ? event.id : "");
      const itemId = pick(event.item_id, event.itemId, data.item_id, data.itemId, data.item?.id);
      if (activeTurnId && turnId && turnId !== activeTurnId) return;
      if (turnId && !activeTurnId) activeTurnId = turnId;
      if (shouldSkipDuplicate(eventName, event)) return;
      if (threadId) setCodexEventStatus("thread", threadId, "ok");
      if (turnId) {
        state.codexTurnId = turnId;
        resolveTurnReady?.(turnId);
        resolveTurnReady = null;
        setCodexEventStatus("turn", turnId, kind === "turn_completed" ? "ok" : "active");
      }
      if (itemId) setCodexEventStatus("item", itemId, kind === "item_completed" ? "ok" : "active");

      const toolName = pick(
        data.tool_name,
        data.toolName,
        data.name,
        data.item?.name,
        data.item?.type,
      );
      if (toolName || /tool|mcp|command|shell|function/.test(signal)) {
        setCodexEventStatus("tool", toolName || eventDetail(event, data), kind.includes("completed") ? "ok" : "active");
      }
      const progressValue = data.progress && typeof data.progress === "object"
        ? pick(data.progress.message, data.progress.status, data.progress.phase)
        : pick(data.progress, data.message, data.status, data.phase);
      if (progressValue || /progress/.test(signal)) {
        setCodexEventStatus("progress", friendlyRuntimeProgress(progressValue || eventDetail(event, data)), "active");
      }

      const phase = /tool|mcp|command|shell|function/.test(signal)
        ? "tool"
        : kind.includes("thread")
          ? "thread"
          : kind.includes("turn") || kind === "turn"
            ? "turn"
            : kind.includes("item")
              ? "item"
              : "event";
      const label = phase === "tool"
        ? toolName || progressValue || eventDetail(event, data)
        : kind === "turn"
          ? "Codex turn 已建立"
          : kind === "turn_started"
            ? "Codex turn 已开始"
            : kind === "turn_completed"
              ? "Codex turn 已完成"
              : kind === "thread_started"
                ? "Codex thread 已连接"
                : kind === "item_started"
                  ? "执行项已开始"
                  : kind === "item_completed"
                    ? "执行项已完成"
                    : eventDetail(event, data);
      appendEventStage({
        phase,
        label,
        detail: progressValue || threadId || turnId || itemId || "",
        status: kind === "error" || /error|failed/.test(signal)
          ? "failed"
          : kind.includes("completed")
            ? "completed"
            : kind.includes("started") || kind === "turn"
              ? "active"
              : "updated",
        event_type: kind,
        thread_id: threadId || state.codexThreadId,
        turn_id: turnId || state.codexTurnId,
        item_id: itemId || "",
      });

      if (kind === "text_delta") {
        const delta = typeof event.text === "string" ? event.text : "";
        if (delta) {
          streamedAnswer += delta;
          if (streamingMessageId) updateMessage(streamingMessageId, streamedAnswer, undefined, true);
          setText("assistant-live-label", "正在生成回答");
          setText("assistant-live-detail", "正在整理结果");
        }
        return;
      }
      if (kind === "thread_started" || kind === "thread_updated") {
        setText("assistant-live-label", "求职助理已连接");
        setText("assistant-live-detail", "正在准备本次任务");
      } else if (kind === "turn" || kind === "turn_started") {
        setText("assistant-live-label", "求职助理正在处理");
        setText("assistant-live-detail", "正在分析请求");
      } else if (kind === "item_started") {
        setText("assistant-live-label", "正在执行当前步骤");
        setText("assistant-live-detail", "请稍候");
      } else if (kind === "turn_completed") {
        turnCompleted = true;
        setCodexEventStatus("progress", "已完成", "ok");
        setText("assistant-live-label", "执行完成");
        setText("assistant-live-detail", "正在显示结果");
      }
      if (kind === "error" || /error|failed/.test(signal)) {
        const detail = friendlyRuntimeError(
          eventDetail(event, data, "模型运行时返回了未说明的错误。")
        );
        streamError = detail;
        setCodexEventStatus("error", detail, "error");
        setText("assistant-live-label", "求职助理执行失败");
        setText("assistant-live-detail", detail);
      }
    };

    const consumeBlock = (block) => {
      if (!block.trim() || block.trim().startsWith(":")) return;
      let eventName = "message";
      const dataLines = [];
      block.split(/\r?\n/).forEach((line) => {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
      });
      if (!dataLines.length) return;
      try { handleEvent(eventName, JSON.parse(dataLines.join("\n"))); }
      catch (_) {
        streamError = "Codex 返回了无法解析的流式事件。";
        setCodexEventStatus("error", streamError, "error");
      }
    };

    const ensureStreamResponse = async (streamResponse) => {
      if (streamResponse?.ok && streamResponse.body && typeof streamResponse.body.getReader === "function") return;
      let detail = `Codex 流连接失败（${streamResponse?.status || 0}）`;
      try { detail = (await streamResponse.json())?.detail || detail; } catch (_) { /* stream may not be JSON */ }
      throw new Error(detail);
    };
    const consumeResponse = async (streamResponse) => {
      await ensureStreamResponse(streamResponse);
      const reader = streamResponse.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const blocks = buffer.split(/\r?\n\r?\n/);
        buffer = blocks.pop() || "";
        blocks.forEach(consumeBlock);
        if (turnCompleted || isCancelled()) {
          if (turnCompleted && typeof reader.cancel === "function") {
            try { await reader.cancel(); } catch (_) { /* the server may already have closed */ }
          }
          break;
        }
        if (done) break;
      }
      if (buffer.trim()) consumeBlock(buffer);
      if (isCancelled()) throw cancelledError();
    };

    let streamResponse = response;
    try {
      while (!turnCompleted) {
        try {
          await consumeResponse(streamResponse);
          if (turnCompleted) break;
          if (isCancelled()) throw cancelledError();
          throw new Error("Codex 流连接已断开。");
        } catch (error) {
          if (error.name === "AbortError" || isCancelled()) throw cancelledError();
          if (streamError) throw new Error(streamError);
          if (reconnectAttempts >= CODEX_STREAM_MAX_RECONNECTS) throw error;
          reconnectAttempts += 1;
          reconnectReplayFence = true;
          setAssistantStatus(`Codex 连接中断，正在重连（${reconnectAttempts}/${CODEX_STREAM_MAX_RECONNECTS}）`, "warn");
          setText("assistant-live-label", "Codex 流连接中断");
          setText("assistant-live-detail", `正在重连（${reconnectAttempts}/${CODEX_STREAM_MAX_RECONNECTS}）`);
          setCodexEventStatus("progress", `连接中断，正在重连（${reconnectAttempts}/${CODEX_STREAM_MAX_RECONNECTS}）`, "active");
          const headers = {
            Accept: "text/event-stream",
            ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
          };
          streamResponse = await fetch(eventPath, {
            method: "GET",
            headers,
            signal: controller.signal,
          });
        }
      }
    } finally {
      cleanupCodexRun();
    }
    if (streamError) throw new Error(streamError);
    if (!turnCompleted) throw new Error("Codex 流已结束，但没有收到 turn_completed 事件。");
    return rememberTask({
      task_id: `codex-${state.codexTurnId || Date.now()}`,
      task_type: "conversation",
      user_request: message,
      status: "succeeded",
      steps: eventStages.length,
      answer: streamedAnswer || "Codex 未返回文本。",
      error: null,
      thread_id: state.codexThreadId,
      turn_id: state.codexTurnId,
      stages: eventStages,
      codex_events: eventStages,
      job_id: jobId,
    });
  }

  function openAssistantDraft(message, jobId = "") {
    switchView("assistant");
    $("assistant-message").value = message;
    $("assistant-job-id").value = jobId;
    window.dispatchEvent?.(new CustomEvent("recruitops:assistant-job", { detail: { id: jobId } }));
    $("assistant-form-error").hidden = true;
    updateIntentHint(message);
    setAssistantStatus("已准备问题");
  }

  async function stopAssistantExecution() {
    const controller = state.activeAssistantController;
    if (!controller) return;
    state.codexStopRequested = true;
    controller.abort();
    setAssistantStatus("正在停止 Codex", "warn");
    if (state.codexThreadId) {
      if (!state.codexTurnId && state.codexTurnReady) {
        await Promise.race([
          state.codexTurnReady,
          new Promise((resolve) => window.setTimeout(resolve, 1000)),
        ]);
      }
      const turnId = state.codexTurnId;
      if (turnId) {
        try {
          await api(`/api/codex/threads/${encodeURIComponent(state.codexThreadId)}/interrupt`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ turn_id: turnId }),
          });
        } catch (error) {
          setCodexEventStatus("error", `停止失败：${error.message}`, "error");
          showToast(`Codex 停止请求失败：${error.message}`, "error");
        }
      }
    }
  }

  async function resetConversation(newThread = false) {
    if (state.codexEnabled !== true || state.codexReady !== true) {
      showToast(codexUnavailableMessage(), "error");
      setAssistantStatus("Codex 运行时不可用", "error");
      return;
    }
    if (state.activeAssistantController) await stopAssistantExecution();
    if (newThread) {
      try {
        await createCodexThread();
      } catch (error) {
        showToast(`新 Codex 会话创建失败：${error.message}`, "error");
        return;
      }
    }
    state.messages = [];
    state.tasks = [];
    state.selectedTask = null;
    persistConversation();
    renderAssistantContext({}); renderConversation(); renderTaskHistory();
    $("assistant-message").value = "";
    $("assistant-job-id").value = "";
    window.dispatchEvent?.(new CustomEvent("recruitops:assistant-job", { detail: { id: "" } }));
    $("assistant-form-error").hidden = true;
    resetCodexEventStatuses();
    updateIntentHint(""); setAssistantStatus(newThread ? "已新建 Codex 会话" : "已清空当前会话", "ok");
    await refreshConversationList();
    showToast(newThread ? "已新建会话" : "已清空当前会话", "success");
  }

  async function deleteConversation(threadId) {
    const selectedThreadId = codexThreadIdOf(threadId);
    if (!selectedThreadId) return false;
    const wasActive = selectedThreadId === state.codexThreadId;
    if (wasActive && state.activeAssistantController) await stopAssistantExecution();
    try {
      await api(`/api/codex/threads/${encodeURIComponent(selectedThreadId)}`, { method: "DELETE" });
      state.conversations = state.conversations.filter((item) => codexThreadIdOf(item) !== selectedThreadId);
      if (wasActive) {
        state.codexThreadId = "";
        state.codexThreadMetadata = null;
        state.messages = [];
        state.tasks = [];
        state.selectedTask = null;
        state.assistantContext = {};
        writeStorage(STORAGE_KEYS.codexThread, "");
        persistConversation();
        const nextThreadId = state.conversations.length ? codexThreadIdOf(state.conversations[0]) : "";
        if (nextThreadId) await loadConversation(nextThreadId);
        else {
          const createdThreadId = await createCodexThread();
          useEmptyCodexThread(createdThreadId);
        }
      }
      await refreshConversationList();
      renderConversationList();
      showToast("会话已删除", "success");
      return true;
    } catch (error) {
      showToast(`会话删除失败：${error.message}`, "error");
      return false;
    }
  }

  async function handleJobAction(action, job) {
    if (!job) return;
    if (action === "details") {
      await openJobDetail(job.id);
      return;
    }
    if (action === "ask") {
      openAssistantDraft(`请解释“${text(job.title)}”为什么匹配我，并指出优势和缺口。`, text(job.id));
      $("assistant-message")?.focus();
      return;
    }
    if (action === "prepare") {
      const message = `准备投递：${text(job.title)}，请先给我一个只读预览，不要写入或发送。`;
      openAssistantDraft(message, text(job.id));
      void submitAssistantQuestion(message, text(job.id));
      return;
    }
    if (action === "record") {
      const buttons = [...document.querySelectorAll('[data-job-action="record"]')].filter(button => button.dataset.jobId === String(job.id));
      if (buttons.some(button => button.disabled)) return;
      buttons.forEach(button => { button.disabled = true; });
      try {
        const result = await api("/api/local-ui/applications", {
          method: "POST", headers: authHeaders("记录投递", { "Content-Type": "application/json" }),
          body: JSON.stringify({ job_id: job.id }),
        });
        job.application_stage = result.stage;
        buttons.forEach(button => { button.dataset.jobAction = "application"; button.textContent = "查看投递"; });
        showToast(result.created ? "已记录投递" : "已有投递记录，保留原阶段", "success");
        await Promise.all([loadApplications(), loadJobBrowser()]);
      } catch (error) { showToast(`记录投递失败：${error.message}`, "error"); }
      finally { buttons.forEach(button => { button.disabled = false; }); }
      return;
    }
    if (action === "application") {
      switchView("applications");
    }
  }

  async function submitAssistantQuestion(message, explicitJobId = null) {
    const normalized = text(message, "").trim();
    if (!normalized) {
      $("assistant-form-error").textContent = "请先输入一个问题。";
      $("assistant-form-error").hidden = false;
      setAssistantStatus("等待提问", "warn");
      return null;
    }
    if (state.codexEnabled !== true || state.codexReady !== true) {
      const unavailable = codexUnavailableMessage();
      $("assistant-form-error").textContent = unavailable;
      $("assistant-form-error").hidden = false;
      setAssistantStatus("Codex 运行时不可用", "error");
      showToast(unavailable, "error");
      return null;
    }
    const intent = mapIntent(normalized);
    const jobId = text(explicitJobId ?? $("assistant-job-id")?.value, "").trim();
    $("assistant-message").value = "";
    $("assistant-job-id").value = "";
    window.dispatchEvent?.(new CustomEvent("recruitops:assistant-job", { detail: { id: "" } }));
    updateIntentHint("");
    $("assistant-form-error").hidden = true;
    $("run-task-button").disabled = true;
    $("assistant-stop-button").hidden = false;
    $("assistant-live-run").hidden = false;
    setAssistantStatus(`正在执行：${intent.label}`);
    appendMessage("user", normalized);
    const streamingMessage = appendMessage("assistant", "", null, { streaming: true });
    try {
      const task = await runCodexAssistantQuery(normalized, jobId, streamingMessage.id);
      const taskLabel = task.display_label || TASK_LABELS[task.task_type] || intent.label || "任务";
      const answer = task.error
          ? `任务未完成：${localizeCodexRuntimeMessage(task.error)}`
          : task.answer
            ? text(task.answer)
            : `已完成${taskLabel}，结果和引用已记录。`;
      updateMessage(streamingMessage.id, answer, task, false);
      await refreshConversationList();
      try {
        state.approvals = normalizeApprovals(await api("/api/approvals"));
        renderApprovals(); renderDashboardLists(); renderDashboardTodos();
      } catch (error) {
        showToast(`审批状态刷新失败：${error.message}`, "error");
      }
      setAssistantStatus(
        task.error
          ? `已停止：${localizeCodexRuntimeMessage(task.error)}`
          : `已完成：${taskLabel}`,
        task.error ? "warn" : "ok"
      );
      showToast(
        task.error
          ? `任务已停止：${localizeCodexRuntimeMessage(task.error)}`
          : `任务完成：${taskLabel}`,
        task.error ? "error" : "success"
      );
      return task;
    } catch (error) {
      if (error.name === "AbortError") {
        const stopped = "已按你的要求停止本次执行。服务端会保留停止前已经完成的步骤。";
        if (streamingMessage) updateMessage(streamingMessage.id, stopped, null, false);
        else appendMessage("assistant", stopped, null);
        setAssistantStatus("已停止", "warn");
        showToast("已停止当前任务", "info");
        return null;
      }
      const localizedError = localizeCodexRuntimeMessage(error.message);
      const failed = rememberTask({ task_id: `failed-${Date.now()}`, task_type: intent.taskType, status: "safe_stop", steps: 0, error: localizedError, user_request: normalized });
      if (streamingMessage) updateMessage(streamingMessage.id, `任务未完成：${localizedError}`, failed, false);
      else appendMessage("assistant", `任务未完成：${localizedError}`, failed);
      $("assistant-form-error").textContent = localizedError;
      $("assistant-form-error").hidden = false;
      setAssistantStatus(`未完成：${localizedError}`, "error");
      showToast(localizedError, "error");
      return null;
    } finally {
      state.activeAssistantController = null;
      state.codexStopRequested = false;
      $("run-task-button").disabled = false;
      $("assistant-stop-button").hidden = true;
      $("assistant-live-run").hidden = true;
      await refreshAssistantBusinessData();
    }
  }

  async function refreshAssistantBusinessData() {
    // A failed or interrupted turn may already have committed business changes.
    // Refresh data only: keep conversation, filters, pagination and navigation intact.
    await Promise.allSettled([
      loadApplications(), loadFullSchedule(), loadAutomations(),
      loadRecruitmentMails({ showLoading: false }),
      loadJobBrowser({ refreshFeatured: true }),
      api("/api/approvals").then((approvals) => {
        state.approvals = normalizeApprovals(approvals);
        renderApprovals();
        renderDashboardLists();
      }),
      api(`/api/jobs/browse?first_seen_on=${localDate()}&limit=8&sort=newest&include_summary=false`).then((jobs) => {
        state.jobTotal = jobs.total || 0;
        renderJobs(jobs.items || []);
        setText("metric-new-jobs", state.jobTotal);
      }),
    ]);
    renderDashboardTodos();
    if (typeof CustomEvent === "function") document.dispatchEvent(new CustomEvent("recruitops:business-updated"));
  }

  async function loadCore() {
    setStatus("loading", "连接中");
    ["today-schedule", "today-new-jobs", "today-todos", "today-anomalies", "pending-approvals", "company-list", "approval-list", "trace-list", "featured-job-list", "application-kanban", "schedule-todo-view", "schedule-calendar-grid", "automation-list"].forEach((id) => loading($(id)));
    const section = (promise, ids) => promise.catch(error => {
      ids.forEach(id => { if ($(id)) empty($(id), "暂时无法读取", error.message); });
    });
    // Each section renders as soon as its own data is ready. Mail/model startup
    // or a slow operational report must not block the current job page.
    await Promise.allSettled([
      api("/health").then(health => {
        setStatus("ready", "已连接");
        setText("dashboard-status-summary", `服务 ${health.status} · ${health.mode}`);
      }).catch(error => { setStatus("error", "连接失败"); showToast(error.message, "error"); }),
      api("/api/codex/health").catch(error => ({ enabled: null, ready: false, detail: error.message }))
        .then(async codexHealth => {
          try { await initializeCodex(codexHealth); await loadConversationWorkspace(); }
          catch (error) {
            state.codexEnabled = codexHealth?.enabled === true;
            state.codexReady = codexHealth?.ready === true;
            renderCodexRuntimeStatus();
            showToast(`助理初始化失败：${error.message}`, "error");
          }
        }),
      loadJobBrowser({ refreshFeatured: true }),
      ...(state.jobBrowse.mode !== "all" ? [loadCompanyRanking()] : []),
      section(api(`/api/jobs/browse?first_seen_on=${today}&limit=8&sort=newest&include_summary=false`).then(jobs => {
        state.jobTotal = jobs.total || 0;
        renderJobs(jobs.items || []);
        setText("nav-today-count", state.jobTotal); setText("metric-new-jobs", state.jobTotal);
        setText("metric-new-jobs-detail", "已确认 2027 校招");
      }), ["today-new-jobs"]),
      section(api(`/api/schedule?on=${today}`).then(schedule => {
        state.schedules = schedule; renderSchedule(schedule); renderDashboardTodos();
        setText("metric-schedule", schedule.length); setText("metric-schedule-detail", schedule.length ? "有安排" : "暂无安排");
      }), ["today-schedule"]),
      section(api("/api/companies").then(companies => { state.companies = companies; renderCompanies(companies); }), ["company-list"]),
      section(api("/api/approvals").then(approvals => {
        state.approvals = normalizeApprovals(approvals); renderApprovals(); renderDashboardLists(); renderDashboardTodos();
        setText("metric-approvals", state.approvals.filter(item => item.status === "pending").length);
        setText("metric-approvals-detail", "等待人工确认");
      }), ["approval-list", "pending-approvals"]),
      section(api("/api/codex/traces").then(traces => { state.traces = traces; renderTraces(); }), ["trace-list"]),
      loadRecruitmentMails({ showLoading: false }),
      section(api(`/api/reports/operational?on=${today}`).then(report => {
        state.operationalReport = report; renderDashboardLists(); renderDashboardTodos();
        setText("metric-anomalies", report?.counts?.crawler_issues ?? 0); setText("metric-anomalies-detail", "运营报告异常");
      }), ["today-anomalies"]),
      loadApplications(), loadFullSchedule(), loadAutomations(),
    ]);
    setText("today-label", today); setText("last-sync-label", `读取于 ${new Date().toLocaleTimeString()}`);
  }

  async function decideApproval(id, decision) {
    try { await api(`/api/approvals/${encodeURIComponent(id)}/${decision}`, { method: "POST", headers: authHeaders("确认审批决定") }); showToast("审批状态已更新", "success"); state.approvals = normalizeApprovals(await api("/api/approvals")); renderApprovals(); renderDashboardLists(); renderDashboardTodos(); renderConversation(); }
    catch (error) { showToast(error.message, "error"); }
  }

  async function executeApproval(id) {
    const operator = window.prompt("请输入本次操作人名称");
    if (!operator?.trim()) return;
    try {
      await api(`/api/approvals/${encodeURIComponent(id)}/execute`, {
        method: "POST",
        headers: authHeaders("执行已批准操作", { "Content-Type": "application/json" }),
        body: JSON.stringify({ operator: operator.trim() }),
      });
      showToast("已执行获批操作，并生成审计记录", "success");
      state.approvals = normalizeApprovals(await api("/api/approvals")); renderApprovals(); renderDashboardLists(); renderDashboardTodos(); renderConversation();
      await Promise.all([loadApplications(), loadFullSchedule(), loadJobBrowser()]);
    } catch (error) { showToast(error.message, "error"); }
  }

  let activeView = null;
  const viewScroll = new Map();
  const jobBrowseHistory = [];

  function saveJobBrowseLocation() {
    return {
      view: activeView || "jobs", scrollY: window.scrollY,
      page: state.jobBrowse.page, mode: state.jobBrowse.mode,
      filters: [...$("job-filter-form").querySelectorAll("input[id], select[id]")]
        .map(input => ({ id: input.id, value: input.value,
          label: input.selectedOptions?.[0]?.textContent })),
    };
  }

  async function returnToJobBrowseLocation() {
    const previous = jobBrowseHistory.pop();
    if (!previous) return;
    state.jobBrowse.page = previous.page;
    state.jobBrowse.mode = previous.mode;
    previous.filters.forEach(({ id, value, label }) => {
      const input = $(id);
      if (input.tagName === "SELECT" && ![...input.options].some(option => option.value === value)) {
        input.add(new Option(label || value, value));
      }
      input.value = value;
    });
    $("jobs-back-button").hidden = jobBrowseHistory.length === 0;
    document.querySelectorAll("[data-job-mode]").forEach(button =>
      button.classList.toggle("is-active", button.dataset.jobMode === previous.mode));
    renderJobModePresentation();
    await loadJobBrowser({ refreshFeatured: true });
    switchView(previous.view);
    window.scrollTo(0, previous.scrollY);
  }

  function switchView(view) {
    if (activeView && activeView !== view) viewScroll.set(activeView, window.scrollY);
    const changed = activeView !== view;
    activeView = view;
    try { sessionStorage.setItem("recruitops.activeView", view); } catch (_) { /* Storage may be unavailable. */ }
    document.querySelectorAll("[data-view-panel]").forEach((panel) => { panel.hidden = panel.dataset.viewPanel !== view; panel.classList.toggle("is-visible", panel.dataset.viewPanel === view); });
    document.querySelectorAll("[data-view]").forEach((item) => {
      let isActive = item.dataset.view === view;
      if (isActive && view === "jobs" && item.dataset.jobNavMode) {
        isActive = item.dataset.jobNavMode === state.jobBrowse.mode;
      }
      item.classList.toggle("is-active", isActive);
    });
    const titles = {
      dashboard: ["工作台 / 今日", "今日工作台"],
      assistant: ["任务入口", "求职助理"],
      jobs: ["岗位 / 2027 届", state.jobBrowse.mode === "today" ? "今日新增" : "27届校招"],
      companies: ["来源与岗位覆盖", "公司"],
      applications: ["我的进度", "投递记录"],
      schedule: ["我的安排", "日程安排"],
      automations: ["我的安排", "定时任务"],
      mail: ["本地招聘情报", "招聘邮箱"],
      tasks: ["系统 / 可追溯执行", "运行记录"],
      approvals: ["系统 / 人工确认", "待确认事项"],
      integrations: ["系统 / 招聘情报", "数据接入"],
      configuration: ["个人设置", "配置"],
    };
    if (titles[view]) { setText("page-eyebrow", titles[view][0]); setText("page-title", titles[view][1]); }
    const systemNav = document.querySelector(".system-nav");
    if (["tasks", "approvals", "integrations"].includes(view) && systemNav) systemNav.open = true;
    $("sidebar")?.classList.remove("is-open"); $("mobile-menu-button")?.setAttribute("aria-expanded", "false");
    if (changed) window.scrollTo(0, viewScroll.get(view) || 0);
    if (changed && view === "schedule") void loadFullSchedule();
    if (changed && view === "applications") void loadApplications();
    if (view === "assistant") void refreshDailyProgress();
  }

  function normalizeApplicationDraft(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("投递草稿格式无效");
    const limits = { company_name: 255, job_title: 512, record_url: 2048, note: 4000 };
    if (Object.keys(value).some((key) => !Object.hasOwn(limits, key))) throw new Error("投递草稿包含不支持的字段");
    const draft = {};
    for (const [key, limit] of Object.entries(limits)) {
      const raw = value[key] ?? "";
      if (typeof raw !== "string" || raw.length > limit || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(raw)) throw new Error("投递草稿字段无效");
      draft[key] = raw.trim();
    }
    if (!draft.job_title) throw new Error("投递草稿缺少岗位");
    if (draft.record_url) {
      const url = new URL(draft.record_url);
      if (!["https:", "http:"].includes(url.protocol) || url.username || url.password || url.hash.startsWith("#/job/")) throw new Error("投递草稿进度页地址无效");
    }
    return draft;
  }

  function openManualApplicationDraft(value = null) {
    const dialog = $("application-create-dialog");
    if (dialog.open || $("application-create-submit").disabled) {
      showToast("请先保存或取消当前投递草稿，再重新采集。", "error");
      return false;
    }
    let draft;
    try { draft = value === null ? {} : normalizeApplicationDraft(value); }
    catch (error) { showToast(error.message, "error"); return false; }
    const form = $("application-create-form");
    form.reset();
    form.elements.stage.value = "applied";
    for (const [key, text] of Object.entries(draft)) form.elements.namedItem(key).value = text;
    $("application-create-error").hidden = true;
    switchView("applications");
    dialog.showModal();
    return true;
  }

  function bind() {
    let jobRequest = 0;
    window.addEventListener("recruitops:assistant-job", async (event) => {
      const id = event.detail.id, request = ++jobRequest;
      $("assistant-selected-job").hidden = $("assistant-clear-job").hidden = !id;
      $("assistant-selected-job").textContent = id ? "当前岗位" : "";
      if (!id) return;
      try {
        const response = await fetch(`/api/jobs/${encodeURIComponent(id)}`);
        if (!response.ok) throw new Error();
        const value = await response.json(), job = value.job || value;
        if (request === jobRequest) $("assistant-selected-job").textContent = job.title || id;
      } catch (_) { if (request === jobRequest) $("assistant-selected-job").textContent = "岗位详情暂不可用"; }
    });
    $("assistant-clear-job").addEventListener("click", () => {
      $("assistant-job-id").value = "";
      window.dispatchEvent(new CustomEvent("recruitops:assistant-job", {detail: {id: ""}}));
    });
    document.addEventListener("recruitops:assistant-configuration", (event) => {
      state.assistantConfiguration = {status: event.detail?.status || "unknown"};
      renderCodexRuntimeStatus();
      void refreshAssistantAvailability();
    });
    $("assistant-open-configuration").addEventListener("click", () => document.querySelector('[data-view="configuration"]').click());
    $("assistant-recheck").addEventListener("click", () => document.dispatchEvent(new CustomEvent("recruitops:configuration-reload")));
    document.querySelectorAll("[data-view]").forEach((item) => item.addEventListener("click", () => {
      if (item.dataset.jobNavMode && item.dataset.jobNavMode !== state.jobBrowse.mode) {
        state.jobBrowse.mode = item.dataset.jobNavMode;
        state.jobBrowse.page = 1;
        document.querySelectorAll("[data-job-mode]").forEach((button) => button.classList.toggle("is-active", button.dataset.jobMode === state.jobBrowse.mode));
        renderJobModePresentation();
        void loadJobBrowser({ refreshFeatured: true });
      }
      switchView(item.dataset.view);
    }));
    document.addEventListener("click", (event) => {
      const taskButton = event.target.closest("[data-task]");
      if (taskButton) {
        event.preventDefault();
        const message = taskButton.dataset.question || DEFAULT_QUESTIONS[taskButton.dataset.task] || "请查询今天的岗位。";
        switchView("assistant"); openAssistantDraft(message, taskButton.dataset.jobId || "");
        void submitAssistantQuestion(message, taskButton.dataset.jobId || null);
        return;
      }
      const viewButton = event.target.closest("[data-open-view]");
      if (viewButton) {
        event.preventDefault();
        switchView(viewButton.dataset.openView);
        return;
      }
      const promptButton = event.target.closest("[data-assistant-prompt]");
      if (promptButton) {
        event.preventDefault();
        switchView("assistant");
        openAssistantDraft(promptButton.dataset.assistantPrompt || "");
        $("assistant-message")?.focus();
        return;
      }
      const mailOpenButton = event.target.closest("[data-mail-open-id]");
      if (mailOpenButton) {
        event.preventDefault();
        void openRecruitmentMail(mailOpenButton.dataset.mailOpenId);
        return;
      }
      const scheduleButton = event.target.closest("[data-schedule-action]");
      if (scheduleButton) {
        event.preventDefault();
        const action = scheduleButton.dataset.scheduleAction;
        if (action === "edit") {
          const scheduleEvent = state.allSchedules.find((item) => text(item.id, "") === text(scheduleButton.dataset.scheduleId, ""));
          if (scheduleEvent) openScheduleEditor(scheduleEvent);
        } else {
          const nextStatus = { complete: "completed", ignore: "ignored", restore: "pending" }[action];
          if (nextStatus) void updateScheduleStatus(scheduleButton.dataset.scheduleId, nextStatus);
        }
        return;
      }
      const scheduleEditButton = event.target.closest("[data-schedule-edit-id]");
      if (scheduleEditButton) {
        event.preventDefault();
        const scheduleEvent = state.allSchedules.find((item) => text(item.id, "") === text(scheduleEditButton.dataset.scheduleEditId, ""));
        if (scheduleEvent) openScheduleEditor(scheduleEvent);
        return;
      }
      const detailButton = event.target.closest("[data-job-details]");
      if (detailButton) {
        event.preventDefault();
        void openJobDetail(detailButton.dataset.jobDetails);
        return;
      }
      const companyButton = event.target.closest("[data-company-jobs]");
      if (companyButton && !event.target.closest("a")) {
        event.preventDefault();
        jobBrowseHistory.push(saveJobBrowseLocation());
        state.jobBrowse.mode = "all";
        document.querySelectorAll("[data-job-mode]").forEach((item) => item.classList.toggle("is-active", item.dataset.jobMode === "all"));
        switchView("jobs");
        $("job-company-filter").value = companyButton.dataset.companyJobs || "";
        $("jobs-back-button").hidden = false;
        state.jobBrowse.page = 1;
        renderJobModePresentation();
        void loadJobBrowser();
        return;
      }
      const automationButton = event.target.closest("[data-automation-disable]");
      if (automationButton) { event.preventDefault(); void disableAutomation(automationButton.dataset.automationDisable); return; }
      const automationDetailButton = event.target.closest("[data-automation-detail]");
      if (automationDetailButton) {
        event.preventDefault();
        openAutomationDetail(automationDetailButton.dataset.automationDetail);
        return;
      }
      const automationExplainButton = event.target.closest("[data-automation-explain]");
      if (automationExplainButton) {
        event.preventDefault();
        const automation = state.automations.find((item) => text(item.id, "") === text(automationExplainButton.dataset.automationExplain, ""));
        if (!automation) return;
        $("automation-detail-dialog")?.close();
        const prompt = automationExplanationPrompt(automation);
        openAssistantDraft(prompt);
        void submitAssistantQuestion(prompt, null);
        return;
      }
      const button = event.target.closest("[data-job-action]");
      if (!button) return;
      const job = findBrowseJob(button.dataset.jobId);
      void handleJobAction(button.dataset.jobAction, job);
    });
    $("jobs-back-button").addEventListener("click", () => void returnToJobBrowseLocation());
    $("refresh-button").addEventListener("click", loadCore);
    const createDialog = $("application-create-dialog");
    const createForm = $("application-create-form");
    const createSubmit = $("application-create-submit");
    const createError = $("application-create-error");
    Object.entries(APPLICATION_STAGE_LABELS).forEach(([value, label]) => {
      createForm.elements.stage.add(new Option(label, value));
    });
    $("application-add-button").addEventListener("click", () => openManualApplicationDraft());
    // Only the isolated workbench preload supplies this one-way subscription.
    // Remote pages have no preload; never listen for cross-window messages.
    if (typeof window.recruitopsDesktop?.onApplicationDraft === "function") {
      window.recruitopsDesktop.onApplicationDraft((draft) => queueMicrotask(() => openManualApplicationDraft(draft)));
    }
    if (typeof window.recruitopsDesktop?.onDataChanged === "function") {
      window.recruitopsDesktop.onDataChanged((event) => queueMicrotask(async () => {
        if (!event || typeof event !== "object" || event.applications !== true) return;
        const scroll = window.scrollY;
        await loadApplications();
        if (event.preservePosition === true) window.scrollTo(0, scroll);
      }));
    }
    const closeCreate = () => { if (!createSubmit.disabled) createDialog.close(); };
    $("application-create-close").addEventListener("click", closeCreate);
    $("application-create-cancel").addEventListener("click", closeCreate);
    createDialog.addEventListener("cancel", (event) => { if (createSubmit.disabled) event.preventDefault(); });
    createForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (createSubmit.disabled) return;
      createSubmit.disabled = true;
      createError.hidden = true;
      const body = Object.fromEntries(new FormData(createForm));
      Object.keys(body).forEach(key => { body[key] = body[key].trim(); });
      body.record_url ||= null;
      try {
        const result = await api("/api/local-ui/applications/manual", {
          method: "POST", headers: authHeaders("手动添加投递", {"Content-Type": "application/json"}),
          body: JSON.stringify(body),
        });
        createDialog.close();
        showToast(result.created ? "投递已添加" : "已有相同公司和岗位的投递，保留原记录", "success");
        await loadApplications();
      } catch (error) {
        createError.textContent = `保存失败：${error.message}`;
        createError.hidden = false;
      } finally { createSubmit.disabled = false; }
    });
    $("applications-refresh-button").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      if (button.disabled) return;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      try {
        if (await loadApplications()) showToast("投递记录已刷新", "success");
      } finally {
        button.disabled = false;
        button.removeAttribute("aria-busy");
      }
    });
    let applicationSearchTimer;
    $("application-filter-form")?.addEventListener("submit", event => event.preventDefault());
    $("application-search")?.addEventListener("input", () => {
      ++state.applicationsRequestId;
      state.applicationRequestController?.abort();
      clearTimeout(applicationSearchTimer);
      applicationSearchTimer = setTimeout(() => void loadApplications(), 250);
    });
    $("application-filter-form")?.addEventListener("reset", () => {
      clearTimeout(applicationSearchTimer);
      ++state.applicationsRequestId;
      state.applicationRequestController?.abort();
      setTimeout(() => { void loadApplications(); }, 0);
    });
    $("approvals-refresh-button").addEventListener("click", async () => { try { state.approvals = normalizeApprovals(await api("/api/approvals")); renderApprovals(); renderDashboardLists(); renderDashboardTodos(); renderConversation(); } catch (error) { showToast(error.message, "error"); } });
    $("traces-refresh-button").addEventListener("click", refreshTraces);
    $("mail-refresh-button").addEventListener("click", syncRecruitmentMails);
    $("automations-refresh-button").addEventListener("click", loadAutomations);
    $("mobile-menu-button").addEventListener("click", () => { const sidebar = $("sidebar"); const open = sidebar.classList.toggle("is-open"); $("mobile-menu-button").setAttribute("aria-expanded", String(open)); });
    $("new-conversation-button").addEventListener("click", () => void resetConversation(true));
    $("clear-conversation-button").addEventListener("click", () => {
      if (state.codexThreadId) void deleteConversation(state.codexThreadId);
    });
    $("conversation-load-more-button")?.addEventListener("click", () => void refreshConversationList({ append: true }));
    $("assistant-message").addEventListener("input", (event) => updateIntentHint(event.target.value));
    $("assistant-message").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        $("assistant-form").requestSubmit();
      }
    });
    $("assistant-form").addEventListener("submit", (event) => { event.preventDefault(); void submitAssistantQuestion($("assistant-message").value, $("assistant-job-id").value); });
    $("assistant-stop-button").addEventListener("click", () => void stopAssistantExecution());
    let jobSearchTimer;
    $("job-filter-form").addEventListener("input", (event) => {
      if (event.target.id !== "job-search") return;
      state.jobBrowse.page = 1;
      clearTimeout(jobSearchTimer);
      jobSearchTimer = setTimeout(() => void loadJobBrowser(), 220);
    });
    $("job-filter-form").addEventListener("change", () => {
      state.jobBrowse.page = 1;
      void loadJobBrowser();
    });
    $("job-filter-form").addEventListener("reset", () => {
      setTimeout(() => {
        state.jobBrowse.page = 1;
        void loadJobBrowser();
      }, 0);
    });
    document.querySelectorAll("[data-job-mode]").forEach((button) => button.addEventListener("click", () => {
      state.jobBrowse.mode = button.dataset.jobMode || "all";
      state.jobBrowse.page = 1;
      document.querySelectorAll("[data-job-mode]").forEach((item) => item.classList.toggle("is-active", item === button));
      renderJobModePresentation();
      switchView("jobs");
      void loadJobBrowser({ refreshFeatured: true });
    }));
    $("jobs-refresh-button").addEventListener("click", () => void loadJobBrowser({ refreshFeatured: true }));
    $("jobs-prev-button").addEventListener("click", () => {
      if (state.jobBrowse.page <= 1) return;
      state.jobBrowse.page -= 1;
      void loadJobBrowser();
    });
    $("jobs-next-button").addEventListener("click", () => {
      const pages = Math.max(1, Math.ceil(state.jobBrowse.total / state.jobBrowse.pageSize));
      if (state.jobBrowse.page >= pages) return;
      state.jobBrowse.page += 1;
      void loadJobBrowser();
    });
    $("company-search").addEventListener("input", () => { state.companyPage = 1; renderCompanyRanking(); });
    $("company-sort").addEventListener("change", () => { state.companyPage = 1; renderCompanyRanking(); });
    $("companies-prev-button").addEventListener("click", () => { if (state.companyPage > 1) { state.companyPage -= 1; renderCompanyRanking(); } });
    $("companies-next-button").addEventListener("click", () => {
      const pages = Math.max(1, Math.ceil(sortedCompanySummaries().length / state.companyPageSize));
      if (state.companyPage < pages) { state.companyPage += 1; renderCompanyRanking(); }
    });
    document.querySelectorAll("[data-schedule-view]").forEach((button) => button.addEventListener("click", () => setScheduleView(button.dataset.scheduleView)));
    $("schedule-status-filter").addEventListener("change", (event) => { state.scheduleStatus = event.target.value; renderFullSchedule(); });
    $("schedule-date-filter").addEventListener("change", (event) => { state.scheduleDateFilter = event.target.value; renderFullSchedule(); });
    $("schedule-today-button").addEventListener("click", () => {
      $("schedule-date-filter").value = today;
      state.scheduleDateFilter = today;
      state.scheduleMonth = scheduleMonthKey(today);
      renderFullSchedule();
    });
    $("schedule-all-button").addEventListener("click", () => {
      $("schedule-date-filter").value = "";
      state.scheduleDateFilter = "";
      renderFullSchedule();
    });
    $("schedule-month-previous").addEventListener("click", () => {
      const month = scheduleMonthStart(state.scheduleMonth || today);
      month.setMonth(month.getMonth() - 1);
      state.scheduleMonth = scheduleMonthKey(month);
      renderScheduleCalendar(scheduleEventsForCurrentFilter());
    });
    $("schedule-month-next").addEventListener("click", () => {
      const month = scheduleMonthStart(state.scheduleMonth || today);
      month.setMonth(month.getMonth() + 1);
      state.scheduleMonth = scheduleMonthKey(month);
      renderScheduleCalendar(scheduleEventsForCurrentFilter());
    });
    $("schedule-add-button").addEventListener("click", () => openScheduleEditor());
    $("schedule-application-id").addEventListener("change", (event) => {
      const application = state.applications.find((item) => text(item.id, "") === text(event.target.value, ""));
      if (!application) return;
      $("schedule-company-name").value = application.company_name || "";
      $("schedule-job-title").value = application.job_title || "";
    });
    $("schedule-editor-close").addEventListener("click", closeScheduleEditor);
    $("schedule-editor-cancel").addEventListener("click", closeScheduleEditor);
    $("schedule-editor-form").addEventListener("submit", (event) => { void submitScheduleForm(event); });
    $("schedule-editor-dialog").addEventListener("click", (event) => {
      if (event.target === $("schedule-editor-dialog")) closeScheduleEditor();
    });
    $("job-detail-close").addEventListener("click", () => $("job-detail-dialog").close());
    $("job-detail-dialog").addEventListener("click", (event) => {
      if (event.target === $("job-detail-dialog")) $("job-detail-dialog").close();
    });
    $("automation-detail-close").addEventListener("click", () => $("automation-detail-dialog").close());
    $("automation-detail-dismiss").addEventListener("click", () => $("automation-detail-dialog").close());
    $("automation-detail-dialog").addEventListener("click", (event) => {
      if (event.target === $("automation-detail-dialog")) $("automation-detail-dialog").close();
    });
    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-mail-review-id]");
      if (button) void reviewRecruitmentMail(button.dataset.mailReviewId);
    });
  }

  const testHooks = {
    renderMarkdown,
    renderCodexRuntimeStatus,
    assistantAvailability,
    renderConversation,
    renderMails,
    renderSchedule,
    renderFullSchedule,
    renderScheduleCalendar,
    renderScheduleTodo,
    scheduleDueGroup,
    safeScheduleHref,
    updateScheduleStatus,
    openScheduleEditor,
    openRecruitmentMail,
    renderAutomations,
    openAutomationDetail,
    automationExplanationPrompt,
    scheduleFormPayload,
    renderApplications,
    loadApplications,
    applicationBrowseQuery,
    loadJobBrowser,
    openMailBinding,
    confirmMailBinding,
    renderMailBindingApprovals,
    controlBackgroundTask,
    loadCore,
    loadFullSchedule,
    normalizeApplicationDraft,
    syncRecruitmentMails,
    runCodexAssistantQuery,
    stopAssistantExecution,
    submitAssistantQuestion,
    appendMessage,
    friendlyRuntimeProgress,
    renderDailyProgress,
    refreshDailyProgress,
    deleteConversation,
    state,
  };
  if (globalThis.__RECRUITOPS_TEST_MODE__) {
    globalThis.__RECRUITOPS_TEST_HOOKS__ = testHooks;
  } else {
    let initialView = "jobs";
    try {
      const savedView = sessionStorage.getItem("recruitops.activeView");
      if ([...document.querySelectorAll("[data-view-panel]")].some(panel => panel.dataset.viewPanel === savedView)) initialView = savedView;
    } catch (_) { /* Start at jobs when storage is unavailable. */ }
    bind(); switchView(initialView); updateIntentHint(); renderConversation(); renderTaskHistory(); renderDashboardTodos(); loadCore();
    window.setInterval(() => { if (activeView === "assistant") void refreshDailyProgress(); }, 5000);
  }
})();
