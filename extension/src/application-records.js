(() => {
  "use strict";

  if (globalThis.RecruitOpsApplicationRecords) return;

  const DATE_PATTERN = /(?:20\d{2}[年./-]\s?\d{1,2}[月./-]\s?\d{1,2}日?(?:\s+\d{1,2}:\d{2})?|\d{1,2}月\d{1,2}日|\d{1,2}[-/.]\d{1,2}\s+\d{1,2}:\d{2})/;
  const JOB_PATTERN = /工程师|开发|算法|产品|设计|运营|测试|研究|研发|技术|顾问|销售|市场|采购|财务|人力|法务|实习|管培|项目经理|架构|数据|运维|机器人|嵌入式|软件|硬件|视觉|岗位|职位|\b(?:engineer|developer|designer|manager|intern|analyst|researcher|builder|architect|scientist|specialist|consultant|lead|director)\b/i;
  const STATUS_PATTERN = /投递|申请|简历|筛选|评估|测评|测试|笔试|面试|终试|洽谈|录用|offer|签约|淘汰|不合适|不匹配|未通过|拒绝|结束|终止|已挂|撤回|applied|assessment|written|interview|rejected|withdrawn/i;
  const ACTION_PATTERN = /^(?:编辑|查看|查看\/打印|详情|修改申请|撤回|撤回申请|取消申请)$/;
  const BLOCKED_TITLE_PATTERN = /^(?:投递记录|我的投递|申请记录|投递历史|已完成的投递|校园招聘|社会招聘|编辑|查看|查看\/打印|修改申请|撤回|撤回申请|取消申请|修改志愿顺序|第\s*\d+\s*志愿|没有更多了|当前进度.*)$/i;
  const STRUCTURAL_SELECTOR = [
    "tr", "article", "li[class*='item']", "li[class*='record']", "li[class*='Record']",
    "[class*='card']", "[class*='Card']", "[class*='record']", "[class*='Record']",
    "[class*='apply-item']", "[class*='application']", "[class*='preference']",
    "[data-recruitops-application]", "[data-application-id]", "[data-recruitops-application-id]"
  ].join(",");

  function compactText(value, limit = 2000) {
    return String(value || "").replace(/\r/g, "").replace(/[ \t]+/g, " ").trim().slice(0, limit);
  }

  function linesOf(value) {
    return String(value || "").replace(/\r/g, "").split(/\n+/)
      .map((line) => line.replace(/\s+/g, " ").trim()).filter(Boolean);
  }

  function textOf(element) {
    return compactText(element?.innerText || element?.textContent || "");
  }

  function normalizeStatusLabel(value) {
    const parts = String(value || "").trim()
      .split(/(\s*(?:-|—|–|→|>|｜|\|)\s*)/)
      .filter(Boolean);
    if (parts.length < 3) return parts.join("").trim();
    let result = parts[0];
    let previous = parts[0].replace(/\s+/g, "").toLowerCase();
    for (let index = 1; index + 1 < parts.length; index += 2) {
      const current = parts[index + 1].replace(/\s+/g, "").toLowerCase();
      if (current && current === previous) continue;
      result += parts[index] + parts[index + 1];
      previous = current;
    }
    return result.trim().slice(0, 200);
  }

  function normalizedStatus(rawStatus) {
    const status = String(rawStatus || "").replace(/\s+/g, "").toLowerCase();
    if (!status) return "";
    if (status === "assessment") return "applied";
    if (/^(?:interested|applied|assessment|written|interview|hr|offer|rejected|withdrawn)$/.test(status)) return status;
    if (/撤回成功|已撤回|已取消申请|取消申请成功/.test(status)) return "withdrawn";
    if (/淘汰|不合适|未通过|暂不匹配|不匹配|流程终止|流程结束|申请终止|拒绝|已挂/.test(status)) return "rejected";
    if (/offer|录用|拟录用|签约|待入职|已入职/.test(status)) return "offer";
    if (/hr面|人力面|终面|终试|洽谈/.test(status)) return "hr";
    if (/三面|第三轮面试/.test(status)) return "interview";
    if (/二面|第二轮面试/.test(status)) return "interview";
    if (/一面|第一轮面试|面试/.test(status)) return "interview";
    if (/笔试|机试|编程测试|在线考试|written(?:test)?/.test(status)) return "written";
    if (/测评|assessment/.test(status)) return "applied";
    if (/^(?:筛选阶段|筛选中|测试中|测试阶段|进行中)$/.test(status)) return "applied";
    if (/投递|申请成功|已申请|简历(?:初筛|筛选|评估|待筛)|初筛|待处理|处理中/.test(status)) return "applied";
    return "";
  }

  function explicitStatusFromText(rawText) {
    const lines = linesOf(rawText);
    for (let index = 0; index < lines.length; index += 1) {
      const match = lines[index].match(/^(?:当前进度|申请进度|应聘进度|当前状态|状态|status)\s*[:：]?\s*(.*)$/i);
      if (!match) continue;
      const inline = String(match[1] || "").trim();
      if (inline) return {label: normalizeStatusLabel(inline), source: "explicit-label"};
      const next = String(lines[index + 1] || "").trim();
      if (next && next.length <= 120 && !/^(?:项目|投递时间|申请时间|修改申请|撤回|编辑|查看)\s*[:：]?$/i.test(next)) {
        return {label: normalizeStatusLabel(next), source: "explicit-label"};
      }
    }
    for (let index = lines.length - 1; index >= 0; index -= 1) {
      if (/^(?:流程终止|流程结束|申请终止|已淘汰|淘汰|不合适|未通过|拒绝|已挂|撤回成功|已撤回)$/.test(lines[index])) {
        return {label: normalizeStatusLabel(lines[index]), source: "terminal-label"};
      }
    }
    return null;
  }

  function activeStatusHint(root) {
    const selectors = [
      "[aria-current='step']", "[aria-current='true']", "[class*='target-view']",
      "[class*='current-step']", "[class*='active-step']", "[class~='current']", "[class~='active']"
    ];
    const blocked = /^(?:修改申请|撤回|撤回申请|取消申请|编辑|查看|详情)$/i;
    for (const element of root.querySelectorAll(selectors.join(","))) {
      for (const line of linesOf(element.innerText || element.textContent || "").slice().reverse()) {
        if (line.length <= 50 && STATUS_PATTERN.test(line) && !blocked.test(line)) return line;
      }
    }
    return "";
  }

  function standaloneStatusHint(root) {
    const selectors = [
      "[data-recruitops-application-status]", "[aria-label='Application status']",
      "[aria-label='Application Status']", "[aria-label='应用状态']", "[aria-label='申请状态']",
      "[class^='status-']", "[class*=' status-']", "[class^='recordStatus']",
      "[class*=' recordStatus']", "[class^='record-status']", "[class*=' record-status']"
    ];
    for (const element of root.querySelectorAll(selectors.join(","))) {
      const text = textOf(element);
      if (text && text.length <= 80 && STATUS_PATTERN.test(text)) return text;
    }
    return "";
  }

  function statusHints(root, rawText) {
    const hints = [];
    const explicit = explicitStatusFromText(rawText);
    if (explicit?.label) hints.push(explicit);
    const active = activeStatusHint(root);
    if (active) hints.push({label: normalizeStatusLabel(active), source: "active-step"});
    const selectors = [
      "[data-recruitops-application-status]", "[aria-label='Application status']",
      "[aria-label='Application Status']", "[aria-label='应用状态']", "[aria-label='申请状态']",
      "[class^='status-']", "[class*=' status-']", "[class^='recordStatus']",
      "[class*=' recordStatus']", "[class^='record-status']", "[class*=' record-status']"
    ];
    for (const element of root.querySelectorAll(selectors.join(","))) {
      const label = normalizeStatusLabel(textOf(element));
      if (label && STATUS_PATTERN.test(label)) hints.push({label, source: "standalone-status"});
    }
    const fallback = standaloneStatusHint(root);
    if (fallback) hints.push({label: normalizeStatusLabel(fallback), source: "standalone-status"});
    return hints.filter((hint, index, all) => all.findIndex((item) => item.label === hint.label) === index);
  }

  function bestJobTitle(rawText) {
    let best = null;
    linesOf(rawText).forEach((line, index) => {
      if (line.length < 2 || line.length > 90 || BLOCKED_TITLE_PATTERN.test(line) || DATE_PATTERN.test(line)) return;
      if (/^(?:当前进度|申请进度|应聘进度|当前状态|投递时间|申请时间|状态|待筛选|待处理|已通过|已淘汰|进行中|已投递|简历|测评|笔试|面试|offer)/i.test(line)) return;
      let score = JOB_PATTERN.test(line) ? 10 : 0;
      if (/[（(][A-Za-z]?\d{4,}[）)]/.test(line)) score += 6;
      if (/第\s*\d+\s*志愿/.test(line)) score += 3;
      if (index === 0) score += 2;
      if (!best || score > best.score) best = {title: line, score};
    });
    return best && best.score >= 8 ? best.title : "";
  }

  function tableRecordParts(node) {
    if (String(node?.tagName || "").toLowerCase() !== "tr") return null;
    const table = node.closest("table");
    if (!table) return null;
    const headers = Array.from(table.querySelectorAll("thead th, tr th")).map((header) => textOf(header));
    const cells = Array.from(node.children || []).filter(
      (child) => String(child.tagName || "").toLowerCase() === "td"
    );
    if (!headers.length || !cells.length) return null;
    const indexOf = (pattern) => headers.findIndex((header) => pattern.test(header.replace(/\s+/g, "")));
    const titleIndex = indexOf(/^(?:投递岗位|申请岗位|岗位名称|职位名称|岗位|职位|job|position)$/i);
    const statusIndex = indexOf(/^(?:当前状态|申请状态|投递状态|当前进度|申请进度|应聘进度|状态|进度|status)$/i);
    const dateIndex = indexOf(/^(?:投递日期|申请日期|投递时间|申请时间|日期|date)$/i);
    if (titleIndex < 0 || statusIndex < 0 || !cells[titleIndex] || !cells[statusIndex]) return null;
    const titleCell = textOf(cells[titleIndex]);
    const titleLines = linesOf(titleCell);
    const preferredTitle = titleLines.find((line) => JOB_PATTERN.test(line)) || titleLines[0] || "";
    const title = preferredTitle.replace(/\s+(?:校园招聘|社会招聘|实习招聘)$/i, "").trim();
    const status = normalizeStatusLabel(textOf(cells[statusIndex]));
    const date = dateIndex >= 0 && cells[dateIndex]
      ? (textOf(cells[dateIndex]).match(DATE_PATTERN)?.[0] || "")
      : "";
    if (!title || !JOB_PATTERN.test(title) || !status || !STATUS_PATTERN.test(status)) return null;
    return {title, status, date};
  }

  function isRecordLike(text) {
    const hasDate = DATE_PATTERN.test(text);
    const hasVolunteer = /第\s*\d+\s*志愿/.test(text);
    const hasOperation = /(?:修改申请|撤回|撤回申请|取消申请|查看\/打印)/.test(text);
    const hasExplicitStatus = /(?:当前进度|申请进度|应聘进度|当前状态|状态|status)\s*[:：]|申请成功|投递成功|流程终止|已淘汰|不合适|未通过/i.test(text);
    const hasOfficialDelivery = /官网投递|投递简历/.test(text);
    const hasStandaloneDelivery = /(?:^|\s)投递(?:\s|$)/.test(text);
    return (hasDate && (hasOperation || hasExplicitStatus || hasOfficialDelivery))
      || (hasDate && hasStandaloneDelivery)
      || (hasVolunteer && hasExplicitStatus)
      || (hasOperation && hasExplicitStatus);
  }

  function recordBlocks(documentValue) {
    const blocks = new Set();
    const actions = Array.from(documentValue.querySelectorAll(
      "button, a, [role='button'], [class*='button'], [class*='Button'], [class*='btn'], [class*='Btn']"
    )).filter((element) => ACTION_PATTERN.test(textOf(element)));
    for (const action of actions) {
      let node = action.parentElement;
      for (let depth = 0; node && node !== documentValue.body && depth < 8; depth += 1, node = node.parentElement) {
        const text = textOf(node);
        if (!isRecordLike(text)) continue;
        if (text.length <= 2000) blocks.add(node);
        break;
      }
    }
    for (const node of documentValue.querySelectorAll(STRUCTURAL_SELECTOR)) {
      const text = textOf(node);
      const tableParts = tableRecordParts(node);
      const protocolMarked = node.hasAttribute("data-recruitops-application")
        || node.hasAttribute("data-application-id")
        || node.hasAttribute("data-recruitops-application-id");
      if (text.length <= 2000 && (isRecordLike(text) || protocolMarked || tableParts)) blocks.add(node);
    }
    // Never interpret a multi-application wrapper as another application card.
    const titleOf = (node) => tableRecordParts(node)?.title || bestJobTitle(textOf(node));
    const candidates = Array.from(blocks).filter((node) => titleOf(node));
    return candidates.filter((node) => {
      const childTitles = new Set(candidates
        .filter((child) => child !== node && node.contains(child))
        .map((child) => titleOf(child)));
      return childTitles.size < 2;
    });
  }

  function recordFromBlock(node, redactText) {
    const rawText = textOf(node);
    const tableParts = tableRecordParts(node);
    const title = tableParts?.title || bestJobTitle(rawText);
    if (!title) return null;
    const hints = tableParts?.status
      ? [{label: tableParts.status, source: "explicit-label"}]
      : statusHints(node, rawText);
    const mappedHints = hints.map((hint) => ({...hint, status: normalizedStatus(hint.label)}))
      .filter((hint) => hint.status);
    const distinctStatuses = [...new Set(mappedHints.map((hint) => hint.status))];
    const unknownHints = hints.filter((hint) => !normalizedStatus(hint.label));
    const chosen = distinctStatuses.length === 1 && !unknownHints.length ? mappedHints[0] : null;
    const source = unknownHints.length ? "unmapped-status" : chosen?.source || (distinctStatuses.length > 1 ? "conflicting-statuses" : "");
    const label = chosen?.label || unknownHints[0]?.label || hints[0]?.label || "";
    const status = chosen?.status || "";
    const hasOperation = /(?:修改申请|撤回|撤回申请|取消申请|查看\/打印)/.test(rawText);
    const date = tableParts?.date || rawText.match(DATE_PATTERN)?.[0] || "";
    let confidence = 0;
    if (status) {
      confidence = source === "terminal-label" ? 0.99 : source === "explicit-label" ? 0.97 : source === "active-step" ? 0.95 : 0.92;
      const protocolMarked = node.hasAttribute("data-recruitops-application")
        || node.hasAttribute("data-application-id")
        || node.hasAttribute("data-recruitops-application-id");
      if (!protocolMarked && !hasOperation && !date && !/第\s*\d+\s*志愿/.test(rawText)) confidence = Math.min(confidence, 0.89);
    }
    const safe = typeof redactText === "function" ? redactText : compactText;
    const context = safe(rawText).slice(0, 1000);
    return {
      title: safe(title).slice(0, 200),
      status,
      label: safe(label).slice(0, 200),
      raw_status_labels: hints.map((hint) => safe(hint.label).slice(0, 200)),
      context,
      evidence: context,
      confidence,
      applied_at: safe(date).slice(0, 80),
      evidence_source: source || "record-exists-only",
      signals: {
        unmapped_status: unknownHints.length > 0,
        has_date: Boolean(date),
        has_operation: hasOperation,
        has_volunteer_index: /第\s*\d+\s*志愿/.test(rawText),
        has_explicit_status: hints.some((hint) => hint.source === "explicit-label" || hint.source === "terminal-label"),
        has_active_step: hints.some((hint) => hint.source === "active-step"),
        conflicting_statuses: distinctStatuses.length > 1
      }
    };
  }

  function extract(documentValue, {redactText} = {}) {
    if (!documentValue?.body) return {records: [], diagnostics: {recordBlockCount: 0, mappedStatusCount: 0}};
    const records = recordBlocks(documentValue).map((node) => recordFromBlock(node, redactText)).filter(Boolean);
    const deduplicated = [];
    for (const record of records) {
      const key = `${record.title}\n${record.label}\n${record.applied_at}`;
      const existing = deduplicated.findIndex((item) => item.key === key);
      if (existing < 0) deduplicated.push({key, record});
      else if (record.confidence > deduplicated[existing].record.confidence) deduplicated[existing] = {key, record};
    }
    const result = deduplicated.map((item) => item.record).slice(0, 100);
    return {
      records: result,
      diagnostics: {
        recordBlockCount: records.length,
        recordCount: result.length,
        mappedStatusCount: result.filter((record) => record.status).length
      }
    };
  }

  globalThis.RecruitOpsApplicationRecords = Object.freeze({
    extract,
    normalizedStatus,
    normalizeStatusLabel,
    explicitStatusFromText,
    bestJobTitle
  });
})();
