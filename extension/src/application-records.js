(() => {
  "use strict";

  if (globalThis.RecruitOpsApplicationRecords) return;

  const DATE_PATTERN = /(?:20\d{2}[年./-]\s?\d{1,2}[月./-]\s?\d{1,2}日?(?:\s+\d{1,2}:\d{2})?|\d{1,2}月\d{1,2}日|\d{1,2}[-/.]\d{1,2}\s+\d{1,2}:\d{2}|(?:今天|昨天|前天)(?:\s*(?:[01]?\d|2[0-3]):[0-5]\d)?|\d+\s*(?:分钟|小时|天)前|刚刚|(?<![\d:])(?:[01]?\d|2[0-3]):[0-5]\d(?![\d:]))/;
  const JOB_PATTERN = /工程师|开发|算法|产品|设计|运营|测试|研究|研发|技术|顾问|销售|市场|采购|财务|人力|法务|实习|管培|项目经理|架构|数据|运维|机器人|嵌入式|软件|硬件|视觉|岗位|职位|^[\w\u4e00-\u9fff][\w\u4e00-\u9fff /-]{0,38}岗$|\b(?:engineer|developer|designer|manager|intern|analyst|researcher|builder|architect|scientist|specialist|consultant|lead|director)\b/i;
  const STATUS_PATTERN = /投递|申请|简历|筛选|评估|测评|测试|笔试|面试|初试|复试|终试|洽谈|录用|offer|签约|淘汰|不合适|不匹配|未通过|拒绝|结束|终止|已挂|撤回|等待处理|流程中|applied|assessment|written|interview|rejected|withdrawn/i;
  const ACTION_PATTERN = /^(?:编辑|查看|查看详情|查看\/打印|详情|修改申请|撤回|撤回申请|撤销申请|取消申请|结束流程|变更职位|催促流程)$/;
  const OPERATION_PATTERN = /(?:修改申请|撤回|撤回申请|撤销申请|取消申请|查看\/打印|结束流程|变更职位|催促流程)/;
  const BLOCKED_TITLE_PATTERN = /^(?:首页|个人中心|联系我们|帮助中心|个人信息|我的简历|招聘说明|申请指南|职位搜索|岗位搜索|岗位列表|职位列表|招聘公告|投递记录|我的投递|申请记录|投递历史|已完成的投递|校园招聘|社会招聘|编辑|查看|查看\/打印|修改申请|撤回|撤回申请|取消申请|修改志愿顺序|第\s*[一二三四五六七八九十\d]+\s*(?:志愿|意向)(?:已激活|未激活)?|没有更多了|当前进度.*)$/i;
  const EXPLICIT_TITLE_SELECTOR = "[data-recruitops-job-title], [data-job-title], [class*='job-name'], [class*='jobName'], [class*='job-title'], [class*='jobTitle'], [class*='position-name'], [class*='positionName'], [class*='position-title'], [class~='title']";
  const TITLE_SELECTOR = `${EXPLICIT_TITLE_SELECTOR}, h1, h2, h3, h4`;
  const NAVIGATION_SELECTOR = "nav, [role='navigation'], [role='menu']";
  const VOLUNTEER_PATTERN = /(?:网申\s*)?第\s*([一二三四五六七八九十\d]+)\s*(?:志愿|意向)/;
  const NON_JOB_TITLE_PATTERN = /^(?:(?:已)?(?:推荐|转投|调剂)(?:到|至|其他)|查看(?:岗位|职位)|申请(?:岗位|职位)|岗位推荐|职位推荐|招聘流程|申请流程|投递流程|申请状态|投递状态|筛选阶段|测试阶段|测试中|面试安排|面试通知|笔试通知|申请成功|投递成功|已录用|已拒绝|已结束|初筛$|筛选$|笔试$|面试$|测评$|录用$|签约$|offer$)/i;
  const STEP_LABEL_PATTERN = /^(?:简历投递|投递|申请|网申|投递成功|申请成功|简历筛选|简历评估|筛选|初筛|(?:在线|综合)?测评|(?:岗位|在线)?笔试|机试|(?:专业|综合|技术)?面试|初试|复试|一面|二面|三面|终面|终试(?:洽谈)?|hr面|录用|签约|入职|offer|applied|screening|assessment|written(?: test)?|interview|hired)$/i;
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
      .split(/(\s*(?:-+|—+|–+|→|>|｜|\|)\s*)/)
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

  function submissionDateLabel(text) {
    const pattern = /(?:^|\s)((?:投递时间|申请时间|投递日期|申请日期|投递于|申请于)\s*[:：]?\s*(20\d{2})[年./-]\s?(\d{1,2})[月./-]\s?(\d{1,2})日?(?:[ \t]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?)(?=$|\s|[，。；;])/g;
    for (const match of String(text || "").matchAll(pattern)) {
      const [, label, year, month, day, hour = "0", minute = "0", second = "0"] = match;
      const date = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));
      if (date.getUTCFullYear() === Number(year) && date.getUTCMonth() + 1 === Number(month)
          && date.getUTCDate() === Number(day) && Number(hour) < 24 && Number(minute) < 60 && Number(second) < 60) return label;
    }
    return "";
  }

  function normalizedStatus(rawStatus) {
    const label = String(rawStatus || "").trim();
    if (/^(?:投递时间|申请时间|投递日期|申请日期|投递于|申请于)/.test(label)) {
      return submissionDateLabel(label) === label ? "applied" : "";
    }
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
    if (/一面|第一轮面试|面试|初试|复试/.test(status)) return "interview";
    if (/笔试|机试|编程测试|在线考试|written(?:test)?/.test(status)) return "written";
    if (/测评|assessment/.test(status)) return "applied";
    if (/^(?:筛选阶段|筛选中|测试中|测试阶段|进行中)$/.test(status)) return "applied";
    if (/投递|申请成功|已申请|简历(?:初筛|筛选|评估|待筛)|初筛|待处理|等待处理|处理中/.test(status)) return "applied";
    return "";
  }

  function stepLabels(value) {
    const tokens = String(value || "").replace(/(\d+)[.、:：)]?\s*(?=[\u4e00-\u9fffA-Za-z])/g, "\n$1 ")
      .split(/\n+|\s*[>→｜|]\s*|\s*[-—–]{1,2}\s*|\s+(?=\d+[.、:：)]?\s)|\s+/)
      .map((token) => token.replace(/^\d+[.、:：)]?\s*/, "").trim()).filter(Boolean);
    const labels = tokens.filter((token) => STEP_LABEL_PATTERN.test(token));
    return labels.length >= 2 ? [...new Set(labels)] : [];
  }

  function progressEvidence(root, rawText) {
    const labels = [];
    for (const element of root.querySelectorAll("[class*='step'], [class*='Step'], [class*='timeline'], [class*='Timeline'], [class*='progress'], [class*='Progress'], ol, [role='list']")) {
      labels.push(...stepLabels(textOf(element)));
      const label = textOf(element).replace(/^\d+[.、:：)]?\s*/, "");
      if (STEP_LABEL_PATTERN.test(label)) labels.push(label);
    }
    // Flat numbered/text ladders also occur in otherwise unstructured rows.
    for (const line of linesOf(rawText)) labels.push(...stepLabels(line));
    const run = [];
    for (const line of linesOf(rawText)) {
      const label = line.replace(/^\d+[.、:：)]?\s*/, "");
      if (STEP_LABEL_PATTERN.test(label)) run.push(label);
      else { if (run.length >= 2) labels.push(...run); run.length = 0; }
    }
    if (run.length >= 2) labels.push(...run);
    const unique = [...new Set(labels)].slice(0, 30);
    return {labels: unique.length >= 2 ? unique : [], hasTimeline: unique.length >= 2};
  }

  function explicitStatusFromText(rawText) {
    const lines = linesOf(rawText);
    for (let index = 0; index < lines.length; index += 1) {
      const match = lines[index].match(/^(?:当前进度|申请进度|应聘进度|当前状态|最新状态|状态|status)\s*[:：]?\s*(.*)$/i);
      if (!match) continue;
      const inline = String(match[1] || "").trim();
      if (inline) return {label: normalizeStatusLabel(inline), source: stepLabels(inline).length > 1 ? "progress-timeline" : "explicit-label"};
      const next = String(lines[index + 1] || "").trim();
      if (next && next.length <= 120 && !/^(?:项目|投递时间|申请时间|修改申请|撤回|编辑|查看)\s*[:：]?$/i.test(next)) {
        const following = lines.slice(index + 1).map((line) => line.replace(/^\d+[.、:：)]?\s*/, ""));
        const ladder = STEP_LABEL_PATTERN.test(following[0] || "") && STEP_LABEL_PATTERN.test(following[1] || "");
        return {label: normalizeStatusLabel(next), source: ladder || stepLabels(next).length > 1 ? "progress-timeline" : "explicit-label"};
      }
    }
    for (let index = lines.length - 1; index >= 0; index -= 1) {
      if (/^(?:申请成功|投递成功|已申请|已投递)$/.test(lines[index])) {
        return {label: normalizeStatusLabel(lines[index]), source: "submission-label"};
      }
      if (/^(?:流程终止|流程结束|申请终止|已淘汰|淘汰|不合适|未通过|拒绝|已挂|撤回成功|已撤回)$/.test(lines[index])) {
        return {label: normalizeStatusLabel(lines[index]), source: "terminal-label"};
      }
    }
    return null;
  }

  function activeStatusHint(root) {
    const selectors = [
      "[aria-current='step']", "[aria-current='true']", "[class*='target-view']",
      "[class*='current-step']", "[class*='active-step']", "[class~='current']", "[class~='active']",
      "[class~='is-process']", "[class~='ant-steps-item-process']"
    ];
    const labels = [];
    for (const element of root.querySelectorAll(selectors.join(","))) {
      if (element.closest(NAVIGATION_SELECTOR) || stepLabels(textOf(element)).length > 1) continue;
      if (element.closest("button, a, [role='button'], [role='link']")) continue;
      // An active card/tab wrapper is not an active process node. Require the
      // marker's own bounded text to be a status, not the last line of its body.
      const label = textOf(element).replace(/^\d+[.、:：)]?\s*/, "").trim();
      if (label.length <= 80 && !/\n/.test(label) && STATUS_PATTERN.test(label)
          && !ACTION_PATTERN.test(label) && (STEP_LABEL_PATTERN.test(label) || !JOB_PATTERN.test(label))
          && !DATE_PATTERN.test(label)) labels.push(label);
    }
    return [...new Set(labels)];
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

  function statusHints(root, rawText, progress = progressEvidence(root, rawText)) {
    const hints = [];
    const explicit = explicitStatusFromText(rawText);
    if (explicit?.label && explicit.source !== "progress-timeline"
        && !(progress.hasTimeline && explicit.source === "submission-label")) hints.push(explicit);
    for (const active of activeStatusHint(root)) hints.push({label: normalizeStatusLabel(active), source: "active-step"});
    const selectors = [
      "[data-recruitops-application-status]", "[aria-label='Application status']",
      "[aria-label='Application Status']", "[aria-label='应用状态']", "[aria-label='申请状态']",
      "[class^='status-']", "[class*=' status-']", "[class^='recordStatus']",
      "[class*=' recordStatus']", "[class^='record-status']", "[class*=' record-status']"
    ];
    for (const element of root.querySelectorAll(selectors.join(","))) {
      const label = normalizeStatusLabel(textOf(element));
      const inSteps = element.closest("[class*='step'], [class*='Step'], [class*='timeline'], [class*='Timeline'], ol");
      if (stepLabels(textOf(element)).length > 1 || (progress.hasTimeline && inSteps && progress.labels.includes(label))) continue;
      if (label && STATUS_PATTERN.test(label)) hints.push({label, source: "standalone-status"});
    }
    const fallback = standaloneStatusHint(root);
    if (fallback && !stepLabels(fallback).length && !(progress.hasTimeline && progress.labels.includes(fallback))) hints.push({label: normalizeStatusLabel(fallback), source: "standalone-status"});
    return hints.filter((hint, index, all) =>
      !(hint.source === "submission-label" && all.some((item) => item.source !== "submission-label"))
      && all.findIndex((item) => item.label === hint.label) === index);
  }

  function cleanJobTitle(value) {
    return compactText(value, 200)
      .replace(/\s*(?:[-—–|｜]+\s*)?(?:网申\s*)?第\s*[一二三四五六七八九十\d]+\s*志愿\s*$/i, "")
      .replace(/\s*[(（]?\s*NO\.?\s*[:：]?\s*[A-Z]?\d+\s*[)）]?\s*$/i, "")
      .replace(/^\s*NO\.?\s*[:：]?\s*[A-Z]?\d+\s*[-—–:：]?\s*/i, "")
      .replace(/\s+(?:校园招聘|社会招聘|实习招聘)$/i, "").trim();
  }

  function usableTitle(value) {
    const title = cleanJobTitle(value);
    return title.length >= 2 && title.length <= 90 && !BLOCKED_TITLE_PATTERN.test(title) && !NON_JOB_TITLE_PATTERN.test(title)
      && !STEP_LABEL_PATTERN.test(title) && !stepLabels(title).length
      && !/^(?:岗位|职位|状态|进度|招聘岗位|申请职位|应届生|应聘记录|投递意向|意向岗位|统招|软件产品|硬件产品|(?:软件|硬件|研发|技术|产品|设计|市场|职能)类)$/.test(title)
      && !/\n|查看详情/.test(title) && !OPERATION_PATTERN.test(title)
      && !DATE_PATTERN.test(title) && !/[。！？!?：:]|(?:岗位职责|任职要求|工作内容|请点击|欢迎|了解更多)/.test(title)
      && !/^(?:当前进度|申请进度|应聘进度|当前状态|投递时间|申请时间|状态|待筛选|待处理|已通过|已淘汰|进行中|已投递|投递成功|申请成功|已申请|成功|简历|测评|笔试|面试|offer)(?:$|[\s:：-])/i.test(title);
  }

  function bestJobTitle(rawText) {
    let best = null;
    linesOf(rawText).forEach((line, index) => {
      if (!usableTitle(line)) return;
      if (/^(?:当前进度|申请进度|应聘进度|当前状态|投递时间|申请时间|状态|待筛选|待处理|已通过|已淘汰|进行中|已投递|简历|测评|笔试|面试|offer)/i.test(line)) return;
      let score = JOB_PATTERN.test(cleanJobTitle(line)) ? 10 : 0;
      if (/[（(][A-Za-z]?\d{4,}[）)]/.test(line)) score += 6;
      if (VOLUNTEER_PATTERN.test(line)) score += 3;
      if (index === 0) score += 2;
      if (!best || score > best.score) best = {title: line, score};
    });
    return best && best.score >= 8 ? best.title : "";
  }

  function blockTitle(node) {
    const table = tableRecordParts(node);
    if (table) return {title: table.title, raw_title: table.raw_title, independent: true};
    const titles = (selector) => Array.from(node.querySelectorAll(selector))
      .filter((element) => !element.closest(NAVIGATION_SELECTOR))
      .map((element) => textOf(element)).filter(usableTitle);
    const explicit = titles(EXPLICIT_TITLE_SELECTOR);
    const structured = explicit.length ? explicit : titles("h1, h2, h3, h4");
    // A wrapper containing multiple headings is not an individual application.
    if (new Set(structured.map(cleanJobTitle)).size > 1) return null;
    const readable = !structured.length ? readableRecordParts(node) : null;
    const textTitles = linesOf(textOf(node)).filter((line) => usableTitle(line) && JOB_PATTERN.test(cleanJobTitle(line)));
    if (!structured.length && !readable && new Set(textTitles.map(cleanJobTitle)).size > 1) return null;
    const rawTitle = structured[0] || readable?.raw_title || bestJobTitle(textOf(node));
    return rawTitle ? {title: cleanJobTitle(rawTitle), raw_title: rawTitle,
      independent: Boolean(structured.length || readable)} : null;
  }

  function readableRecordParts(node) {
    const rawText = textOf(node);
    if (!rawText || rawText.length > 1600 || node.closest(NAVIGATION_SELECTOR)
        || node.querySelector(NAVIGATION_SELECTOR)) return null;
    // Nested inline title/status fields may not get their own innerText line.
    const leaves = Array.from(node.querySelectorAll("div, span, p, a, time, h1, h2, h3, h4"))
      .filter((element) => !element.childElementCount).map(textOf);
    const pieces = [...new Set([...linesOf(rawText), ...Array.from(node.children || []).map(textOf), ...leaves])]
      .filter((part) => part.length <= 200);
    const labeled = pieces.map((part) => part.match(/^(?:投递岗位|申请岗位|岗位名称|职位名称|岗位|职位|position|job)\s*[:：]\s*(.+)$/i)?.[1]).filter(usableTitle);
    const titles = labeled.length ? labeled : pieces.filter((part) => usableTitle(part) && JOB_PATTERN.test(cleanJobTitle(part)));
    const unique = [...new Set(titles)];
    if (unique.length !== 1) return null;
    const rawTitle = unique[0];
    const dates = [...new Set(pieces.map((part) => part.match(DATE_PATTERN)?.[0]).filter(Boolean))];
    const explicit = explicitStatusFromText(pieces.join("\n"));
    const standalone = [...new Set(pieces.filter((part) => STEP_LABEL_PATTERN.test(part)))];
    const progress = progressEvidence(node, pieces.join("\n"));
    const label = explicit?.source !== "progress-timeline" && !(progress.hasTimeline && explicit?.source === "submission-label")
      ? explicit?.label || "" : "";
    const hasOperation = OPERATION_PATTERN.test(rawText);
    if (!((dates.length === 1 && (label || standalone.length || progress.hasTimeline || hasOperation))
        || (labeled.length && label) || (hasOperation && (label || progress.hasTimeline)))) return null;
    return {title: cleanJobTitle(rawTitle), raw_title: rawTitle, date: dates.length === 1 ? dates[0] : "",
      status: label || (!progress.hasTimeline && standalone.length === 1 ? standalone[0] : "")};
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
    const title = cleanJobTitle(preferredTitle);
    const status = normalizeStatusLabel(textOf(cells[statusIndex]));
    const date = dateIndex >= 0 && cells[dateIndex]
      ? (textOf(cells[dateIndex]).match(DATE_PATTERN)?.[0] || "")
      : "";
    if (!usableTitle(title) || !status || !STATUS_PATTERN.test(status)) return null;
    return {title, raw_title: preferredTitle, status, date};
  }

  function isRecordLike(text) {
    const hasDate = DATE_PATTERN.test(text);
    const hasVolunteer = VOLUNTEER_PATTERN.test(text);
    const hasOperation = OPERATION_PATTERN.test(text);
    const hasExplicitStatus = /(?:当前进度|申请进度|应聘进度|当前状态|最新状态|状态|status)\s*[:：]|申请成功|投递成功|流程终止|已淘汰|不合适|未通过/i.test(text);
    const hasOfficialDelivery = /官网投递|投递简历/.test(text);
    const hasStandaloneDelivery = /(?:^|\s)投递(?:\s|$)/.test(text);
    return (hasDate && (hasOperation || hasExplicitStatus || hasOfficialDelivery))
      || (hasDate && hasStandaloneDelivery)
      || (hasVolunteer && hasExplicitStatus)
      || (hasOperation && (hasExplicitStatus || stepLabels(text).length > 1));
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
        if (!isRecordLike(text) || !blockTitle(node)) continue;
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
    // Read only bounded, complete rows; no page-wide title/status pairing.
    for (const node of documentValue.querySelectorAll("tr, li, [role='row'], section, div")) {
      if (readableRecordParts(node)) blocks.add(node);
    }
    // Some ATS cards have no semantic card class or actions; start at their title.
    for (const heading of documentValue.querySelectorAll(TITLE_SELECTOR)) {
      if (!usableTitle(textOf(heading)) || heading.closest(NAVIGATION_SELECTOR)) continue;
      let node = heading.parentElement;
      for (let depth = 0; node && node !== documentValue.body && depth < 6; depth += 1, node = node.parentElement) {
        const text = textOf(node);
        if (text.length > 2000) break;
        if (!blockTitle(node)) break;
        if (isRecordLike(text) || /(?:^|\n)(?:申请成功|投递成功|已申请|已投递)(?:\n|$)/.test(text)) {
          blocks.add(node);
          break;
        }
      }
    }
    // Never interpret a multi-application wrapper as another application card.
    const candidates = Array.from(blocks).filter((node) => !node.closest(NAVIGATION_SELECTOR) && blockTitle(node));
    return candidates.filter((node) => !candidates.some((child) => child !== node && node.contains(child) && blockTitle(child)?.independent))
      .filter((node, _index, all) => !all.some((parent) => parent !== node && parent.contains(node)
        && blockTitle(parent)?.title === blockTitle(node)?.title));
  }

  function recordIdentity(node, rawTitle, rawText) {
    const identity = {};
    const allowedId = (value) => /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value || "") ? value : "";
    for (const [key, attributes, parameters] of [
      ["job_id", ["data-job-id", "data-position-id", "data-recruitops-job-id"], ["jobId", "job_id", "positionId", "position_id"]],
      ["application_id", ["data-application-id", "data-apply-id", "data-recruitops-application-id"], ["applicationId", "application_id", "applyId", "apply_id"]]
    ]) {
      const values = new Set();
      for (const element of [node, ...node.querySelectorAll(attributes.map((name) => `[${name}]`).join(","))]) {
        for (const attribute of attributes) {
          const value = allowedId(element.getAttribute(attribute));
          if (value) values.add(value);
        }
      }
      for (const link of node.querySelectorAll("a[href]")) {
        try {
          const url = new URL(link.getAttribute("href"), node.ownerDocument.baseURI);
          if (!/^https?:$/.test(url.protocol)) continue;
          for (const parameter of parameters) {
            const value = allowedId(url.searchParams.get(parameter));
            if (value) values.add(value);
          }
        } catch (_error) { /* Invalid links are not identity evidence. */ }
      }
      if (key === "job_id") {
        const number = rawTitle.match(/\bNO\.?\s*[:：]?\s*([A-Z]?\d+)\b/i)?.[1];
        if (number) values.add(number);
        const jobCode = rawTitle.match(/[（(]\s*(J\d{4,})\s*[）)]\s*$/i)?.[1];
        if (jobCode) values.add(jobCode);
      }
      if (values.size === 1) identity[key] = [...values][0];
    }
    const volunteers = [...rawText.matchAll(new RegExp(VOLUNTEER_PATTERN.source, "g"))].map((match) => match[1]);
    if (new Set(volunteers).size === 1) identity.volunteer_index = volunteers[0];
    return identity;
  }

  function recordFromBlock(node, redactText) {
    // One extra character detects the parser's own cap without retaining more.
    const boundedSourceText = compactText(node?.innerText || node?.textContent || "", 2001);
    const rawText = boundedSourceText.slice(0, 2000);
    const tableParts = tableRecordParts(node);
    const readableParts = tableParts || readableRecordParts(node);
    const progress = progressEvidence(node, rawText);
    const titleParts = blockTitle(node);
    if (!titleParts) return null;
    const {title, raw_title: rawTitle} = titleParts;
    const hints = statusHints(node, rawText, progress);
    if (readableParts?.status && !stepLabels(readableParts.status).length
        && !hints.some((hint) => hint.label === readableParts.status)) {
      hints.unshift({label: readableParts.status, source: "explicit-label"});
    }
    // Dates are historical metadata once a real current status is available.
    // Only an otherwise unselected personal record uses submission as a baseline.
    const submission = submissionDateLabel(rawText);
    if (!hints.length && submission) hints.push({label: submission, source: "submission-date"});
    const mappedHints = hints.map((hint) => ({...hint, status: normalizedStatus(hint.label)}))
      .filter((hint) => hint.status);
    const distinctStatuses = [...new Set(mappedHints.map((hint) => hint.status))];
    const unknownHints = hints.filter((hint) => !normalizedStatus(hint.label));
    const chosen = distinctStatuses.length === 1 && !unknownHints.length ? mappedHints[0] : null;
    const currentIdentified = Boolean(chosen) || (hints.length > 0 && new Set(hints.map((hint) => hint.label)).size === 1);
    const source = unknownHints.length ? "unmapped-status" : chosen?.source || (distinctStatuses.length > 1 ? "conflicting-statuses" : progress.hasTimeline ? "timeline-without-current" : "");
    const label = chosen?.label || unknownHints[0]?.label || hints[0]?.label || "";
    const status = chosen?.status || "";
    const hasOperation = OPERATION_PATTERN.test(rawText);
    const date = readableParts?.date || rawText.match(DATE_PATTERN)?.[0] || "";
    let confidence = 0;
    if (status) {
      confidence = source === "terminal-label" ? 0.99 : source === "explicit-label" ? 0.97 : source === "active-step" ? 0.95 : 0.92;
      const protocolMarked = node.hasAttribute("data-recruitops-application")
        || node.hasAttribute("data-application-id")
        || node.hasAttribute("data-recruitops-application-id");
      if (!protocolMarked && !hasOperation && !date && !VOLUNTEER_PATTERN.test(rawText)) confidence = Math.min(confidence, 0.89);
    }
    const safe = typeof redactText === "function" ? redactText : compactText;
    const safeContext = safe(rawText);
    const context = safeContext.slice(0, 1000);
    return {
      title: safe(title).slice(0, 200),
      raw_title: safe(rawTitle).slice(0, 200),
      ...Object.fromEntries(Object.entries(recordIdentity(node, rawTitle, rawText)).map(([key, value]) => [key, safe(value)])),
      status,
      label: safe(label).slice(0, 200),
      raw_status_labels: hints.map((hint) => safe(hint.label).slice(0, 200)),
      stage_labels: progress.labels.map((label) => safe(label).slice(0, 200)),
      current_step_label: currentIdentified ? safe(chosen?.label || hints[0].label).slice(0, 200) : "",
      context,
      evidence: context,
      confidence,
      applied_at: safe(date).slice(0, 80),
      evidence_source: source || "record-exists-only",
      signals: {
        context_truncated: boundedSourceText.length > rawText.length || safeContext.length > context.length,
        unmapped_status: unknownHints.length > 0,
        has_date: Boolean(date),
        has_operation: hasOperation,
        has_volunteer_index: VOLUNTEER_PATTERN.test(rawText),
        has_explicit_status: hints.some((hint) => hint.source === "explicit-label" || hint.source === "terminal-label"),
        has_active_step: hints.some((hint) => hint.source === "active-step"),
        has_progress_timeline: progress.hasTimeline,
        current_step_identified: currentIdentified,
        conflicting_statuses: distinctStatuses.length > 1
      }
    };
  }

  function extract(documentValue, {redactText} = {}) {
    if (!documentValue?.body) return {records: [], diagnostics: {recordBlockCount: 0, mappedStatusCount: 0}};
    const records = recordBlocks(documentValue).map((node) => recordFromBlock(node, redactText)).filter(Boolean);
    const deduplicated = [];
    for (const record of records) {
      const key = `${record.title}\n${record.label}\n${record.applied_at}\n${record.application_id || ""}\n${record.job_id || ""}\n${record.volunteer_index || ""}`;
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
