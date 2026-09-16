(() => {
  "use strict";

  const protocol = globalThis.RecruitOpsProtocol;
  const actions = globalThis.RecruitOpsActions;
  const applicationRecords = globalThis.RecruitOpsApplicationRecords;
  if (!protocol || globalThis.__RECRUITOPS_CONTENT_SCRIPT_READY__) {
    return;
  }
  globalThis.__RECRUITOPS_CONTENT_SCRIPT_READY__ = true;

  const blockedTags = new Set([
    "BUTTON",
    "FORM",
    "IFRAME",
    "INPUT",
    "OBJECT",
    "OPTION",
    "SCRIPT",
    "SELECT",
    "STYLE",
    "TEXTAREA"
  ]);
  const MAX_TEXT_LENGTH = 20000;
  const ACTION_TICKET_MAX_LENGTH = 128;
  const MAX_LINKS = 200;
  const MAX_NETWORK_REQUESTS = 500;
  const seenActionTickets = new Set();
  const LOGIN_SELECTORS = Object.freeze([
    "input[type='password']",
    "input[autocomplete='username']",
    "[data-recruitops-auth='login']"
  ]);
  const CAPTCHA_SELECTORS = Object.freeze([
    "iframe[src*='captcha']",
    "[data-captcha]",
    "[data-sitekey]",
    "[data-recruitops-auth='captcha']"
  ]);
  const OVERLAY_SELECTORS = Object.freeze([
    "[aria-modal='true']",
    "[role='dialog']",
    "[data-recruitops-blocking-overlay]"
  ]);
  const MAX_SEMANTIC_NODES = 240;
  const MAX_NODE_TEXT_LENGTH = 500;
  const SEMANTIC_SELECTOR = [
    "h1", "h2", "h3", "h4", "p", "li", "td", "th", "article",
    "[role]", "[aria-label]", "[aria-current]", "[aria-selected]", "[aria-checked]",
    "[data-recruitops-application]", "[data-recruitops-application-id]",
    "[data-application-id]", "[data-recruitops-application-status]"
  ].join(", ");

  function isBlockedTextNode(node) {
    let current = node.parentElement;
    while (current) {
      if (blockedTags.has(current.tagName)) {
        return true;
      }
      if (current.getAttribute("contenteditable") === "true") {
        return true;
      }
      current = current.parentElement;
    }
    return false;
  }

  function redactSensitiveText(value) {
    return value
      .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, "[redacted-email]")
      .replace(/(?:\+?86[-\s]?)?1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}/g, "[redacted-phone]")
      .replace(/\b\d{17}[\dXx]\b/g, "[redacted-id]")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, MAX_TEXT_LENGTH);
  }

  function readSanitizedText(root) {
    const container = root && typeof root.nodeType === "number" ? root : document.body;
    if (!container) {
      return "";
    }

    const textNodeFilter = globalThis.NodeFilter ? NodeFilter.SHOW_TEXT : 4;
    const walker = document.createTreeWalker(container, textNodeFilter);
    const chunks = [];
    let totalLength = 0;

    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (isBlockedTextNode(node)) {
        continue;
      }

      const text = typeof node.nodeValue === "string" ? node.nodeValue.trim() : "";
      if (!text) {
        continue;
      }

      chunks.push(text);
      totalLength += text.length;
      if (totalLength >= MAX_TEXT_LENGTH * 2) {
        break;
      }
    }

    return redactSensitiveText(chunks.join(" "));
  }

  function sanitizedHttpUrl(value) {
    try {
      const parsed = new URL(value, location.href);
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
        return null;
      }
      parsed.username = "";
      parsed.password = "";
      parsed.search = "";
      const fragment = parsed.hash.slice(1).split("?", 1)[0];
      parsed.hash = (
        (fragment.startsWith("/") || fragment.startsWith("!/")) &&
        fragment.length <= 1024 && !/[\s#]/.test(fragment)
      ) ? fragment : "";
      return parsed.toString();
    } catch (_error) {
      return null;
    }
  }

  function readSanitizedLinks() {
    const links = [];
    const seen = new Set();
    for (const anchor of document.querySelectorAll("a[href]")) {
      const value = sanitizedHttpUrl(anchor.href);
      if (!value || seen.has(value)) {
        continue;
      }
      seen.add(value);
      links.push(value);
      if (links.length >= MAX_LINKS) {
        break;
      }
    }
    return links;
  }

  function readSanitizedNetworkRequests() {
    if (!globalThis.performance?.getEntriesByType) {
      return [];
    }
    const requests = [];
    for (const entry of performance.getEntriesByType("resource")) {
      const url = sanitizedHttpUrl(entry.name);
      if (!url) {
        continue;
      }
      const responseStatus = Number(entry.responseStatus);
      requests.push({
        method: "UNKNOWN",
        url,
        status_code: responseStatus >= 100 && responseStatus <= 599 ? responseStatus : null,
        resource_type: String(entry.initiatorType || "resource").slice(0, 80)
      });
      if (requests.length >= MAX_NETWORK_REQUESTS) {
        break;
      }
    }
    return requests;
  }

  function createSanitizedSnapshot() {
    return {
      origin: location.origin,
      path: redactSensitiveText(location.pathname),
      title: redactSensitiveText(document.title || ""),
      text: safePageText(),
      links: readSanitizedLinks(),
      networkRequests: readSanitizedNetworkRequests(),
      capturedAt: new Date().toISOString()
    };
  }

  function actionResult(requestId, data) {
    return {
      protocolVersion: protocol.version,
      type: protocol.messageTypes.CONTROLLED_ACTION_RESULT,
      requestId,
      ok: true,
      data
    };
  }

  function actionError(requestId, code) {
    return {
      protocolVersion: protocol.version,
      type: protocol.messageTypes.CONTROLLED_ACTION_RESULT,
      requestId,
      ok: false,
      error: {code}
    };
  }

  function actionPause(requestId, reason, message) {
    const state = protocol.createPauseState(reason, message);
    return {
      protocolVersion: protocol.version,
      type: protocol.messageTypes.PAUSE_STATE,
      requestId,
      ok: false,
      state,
      pause: state
    };
  }

  function safePageText() {
    if (!document.body) {
      return "";
    }
    return redactSensitiveText(document.body.innerText || "");
  }

  function pagePauseReason() {
    if (hasVisibleSelector(CAPTCHA_SELECTORS)) {
      return protocol.pauseReasons.CAPTCHA_REQUIRED;
    }
    if (hasVisibleSelector(LOGIN_SELECTORS)) {
      return protocol.pauseReasons.LOGIN_REQUIRED;
    }
    const text = safePageText();
    const titleAndLead = `${document.title || ""} ${text.slice(0, 800)}`;
    const captchaInput = hasVisibleSelector([
      "input[name*='captcha' i]", "input[id*='captcha' i]", "input[placeholder*='验证码']"
    ]);
    if (
      /(?:captcha|验证码|人机验证|安全验证|滑块验证|verify you are human)/i.test(text) &&
      (captchaInput || /(?:安全检查|完成.{0,8}验证|请.{0,8}验证|security check|verify you are human)/i.test(titleAndLead))
    ) {
      return protocol.pauseReasons.CAPTCHA_REQUIRED;
    }
    if (/(?:请先登录|请登录|登录失效|重新登录|未登录|login required|session expired|sign in to|log in to)/i.test(text)) {
      return protocol.pauseReasons.LOGIN_REQUIRED;
    }
    if (hasVisibleSelector(OVERLAY_SELECTORS)) {
      return protocol.pauseReasons.STATE_UNCLEAR;
    }
    return null;
  }

  function hasVisibleSelector(selectors) {
    for (const selector of selectors) {
      for (const element of document.querySelectorAll(selector)) {
        if (isVisible(element)) {
          return true;
        }
      }
    }
    return false;
  }

  function isVisible(element) {
    if (!element || !element.isConnected) {
      return false;
    }
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) {
      return false;
    }
    const style = globalThis.getComputedStyle ? getComputedStyle(element) : null;
    return !style || (style.visibility !== "hidden" && style.display !== "none");
  }

  function visibleMediaCount() {
    let count = 0;
    for (const element of document.querySelectorAll("canvas, img")) {
      if (!isVisible(element)) continue;
      count += 1;
      if (count >= 100) break;
    }
    return count;
  }

  function safeClassTokens(element) {
    if (!element?.classList) return [];
    return [...element.classList]
      .filter((value) => /^[a-z0-9_-]{1,64}$/i.test(value))
      .slice(0, 12);
  }

  function semanticNode(element) {
    if (!isVisible(element)) return null;
    const text = redactSensitiveText(element.innerText || "").slice(0, MAX_NODE_TEXT_LENGTH);
    const role = redactSensitiveText(element.getAttribute("role") || "").slice(0, 64);
    const ariaLabel = redactSensitiveText(element.getAttribute("aria-label") || "").slice(0, 200);
    if (!text && !role && !ariaLabel) return null;
    const rect = element.getBoundingClientRect();
    const style = globalThis.getComputedStyle ? getComputedStyle(element) : null;
    const attributes = {};
    for (const name of [
      "aria-current", "aria-selected", "aria-checked", "aria-expanded",
      "data-recruitops-application-id", "data-application-id",
      "data-recruitops-application-status"
    ]) {
      const value = element.getAttribute(name);
      if (typeof value === "string" && value.trim()) {
        attributes[name] = redactSensitiveText(value).slice(0, 128);
      }
    }
    return {
      tag: String(element.tagName || "").toLowerCase().slice(0, 24),
      role,
      ariaLabel,
      text,
      classTokens: safeClassTokens(element),
      attributes,
      visual: {
        color: String(style?.color || "").slice(0, 48),
        backgroundColor: String(style?.backgroundColor || "").slice(0, 48),
        fontWeight: String(style?.fontWeight || "").slice(0, 16),
        opacity: String(style?.opacity || "").slice(0, 16)
      },
      rect: {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      }
    };
  }

  function createSemanticObservation(requestId, validation) {
    const root = actions.selectorCandidates(validation.selectorKey)
      .flatMap((selector) => [...document.querySelectorAll(selector)])
      .find(isVisible) || document.body;
    const nodes = [];
    const seen = new Set();
    for (const element of root.querySelectorAll(SEMANTIC_SELECTOR)) {
      if (seen.has(element)) continue;
      seen.add(element);
      const node = semanticNode(element);
      if (node) nodes.push(node);
      if (nodes.length >= MAX_SEMANTIC_NODES) break;
    }
    const extracted = applicationRecords?.extract
      ? applicationRecords.extract(document, {redactText: redactSensitiveText})
      : {records: [], diagnostics: {recordBlockCount: 0, recordCount: 0, mappedStatusCount: 0}};
    const records = Array.isArray(extracted.records) ? extracted.records : [];
    const entries = records.filter((record) => record.status && record.label).map((record) => ({
      status: record.status,
      label: record.label,
      context: record.context,
      evidence: record.evidence,
      confidence: record.confidence
    }));
    return actionResult(requestId, {
      action: validation.action,
      selectorKey: validation.selectorKey,
      page: createSanitizedSnapshot(),
      semanticNodes: nodes,
      applicationRecords: records,
      entries,
      diagnostics: {
        readyState: String(document.readyState || "unknown").slice(0, 32),
        visibleTextLength: safePageText().length,
        visibleMediaCount: visibleMediaCount(),
        semanticNodeCount: nodes.length,
        ...(extracted.diagnostics || {})
      },
      capturedAt: new Date().toISOString()
    });
  }

  function dispatchControlEvents(element) {
    element.dispatchEvent(new Event("input", {bubbles: true}));
    element.dispatchEvent(new Event("change", {bubbles: true}));
  }

  function controlDescriptor(control) {
    const labels = [];
    if (control.id) {
      const explicit = document.querySelector(`label[for="${CSS.escape(control.id)}"]`);
      if (explicit) labels.push(explicit.innerText || explicit.textContent || "");
    }
    const wrappingLabel = control.closest("label");
    if (wrappingLabel) labels.push(wrappingLabel.innerText || wrappingLabel.textContent || "");
    const sibling = control.nextElementSibling;
    if (sibling) labels.push(sibling.innerText || sibling.textContent || "");
    return [control.value, ...labels]
      .map((value) => String(value || "").trim())
      .filter(Boolean)
      .join(" ")
      .slice(0, 300);
  }

  function descriptorMatchesOption(descriptor, option) {
    if (option === "秋招") {
      return descriptor.includes("秋招")
        && !descriptor.includes("秋招提前批")
        && !descriptor.includes("秋招补录");
    }
    return descriptor.includes(option);
  }

  function fixedCheckboxes(name, allowedValues) {
    const controls = [...document.querySelectorAll(`input[name="${name}"]`)];
    if (!controls.length) throw new Error("OC_FILTERS_NOT_FOUND");
    for (const control of controls) {
      const descriptor = controlDescriptor(control);
      const shouldCheck = allowedValues.some((value) => descriptorMatchesOption(descriptor, value));
      if (control.checked !== shouldCheck) {
        control.checked = shouldCheck;
        dispatchControlEvents(control);
      }
    }
    const multiSelects = new Set(controls.map((control) => control.closest(".crt-multi-select")).filter(Boolean));
    for (const multiSelect of multiSelects) {
      const confirm = multiSelect.querySelector(".crt-multi-select-confirm");
      if (confirm) confirm.click();
    }
  }

  function ocFixedFiltersSelected() {
    const checkedControls = (name) => [...document.querySelectorAll(`input[name="${name}"]`)]
      .filter((control) => control.checked);
    const companyTypes = checkedControls("company_type[]");
    const recruitmentTypes = checkedControls("recruitment_type[]");
    const target = document.querySelector("#crt-target-candidates");
    return companyTypes.length === 1
      && descriptorMatchesOption(controlDescriptor(companyTypes[0]), "民企")
      && recruitmentTypes.length === 2
      && recruitmentTypes.some((control) => descriptorMatchesOption(controlDescriptor(control), "秋招"))
      && recruitmentTypes.some((control) => descriptorMatchesOption(controlDescriptor(control), "秋招提前批"))
      && [target?.value, target?.selectedOptions?.[0]?.textContent]
        .some((value) => String(value || "").includes("2027"));
  }

  function ocDiagnostics(rows) {
    const controls = (name) => [...document.querySelectorAll(`input[name="${name}"]`)]
      .map((control) => ({
        checked: Boolean(control.checked),
        descriptor: controlDescriptor(control)
      }));
    const target = document.querySelector("#crt-target-candidates");
    return {
      rowCount: rows.length,
      companyTypeControls: controls("company_type[]"),
      recruitmentTypeControls: controls("recruitment_type[]"),
      targetValue: String(target?.value || "").slice(0, 80),
      targetText: String(target?.selectedOptions?.[0]?.textContent || "").trim().slice(0, 80),
      paginationText: String(document.querySelector("#crt-pagination-container")?.innerText || "")
        .replace(/\s+/g, " ").trim().slice(0, 300),
      sampleRows: rows.slice(0, 3).map((row) => ({
        companyType: ocText(row, "crt-col-type").slice(0, 80),
        recruitmentType: ocText(row, "crt-col-recruitment-type").slice(0, 120),
        recruitmentTarget: ocText(row, "crt-col-target").slice(0, 120)
      }))
    };
  }

  function ocRowsMatchFixedFilters(rows) {
    const dataRows = rows.filter((row) => ocText(row, "crt-col-company"));
    if (!dataRows.length) return false;
    return dataRows.every((row) => {
      const companyType = ocText(row, "crt-col-type");
      const target = ocText(row, "crt-col-target");
      const recruitmentTypes = ocText(row, "crt-col-recruitment-type")
        .split(/[,，、/|;；\n]+/)
        .map((value) => value.trim())
        .filter(Boolean);
      return companyType === "民企"
        && target.includes("2027")
        && recruitmentTypes.length > 0
        && recruitmentTypes.some((value) => value === "秋招" || value === "秋招提前批");
    });
  }

  async function waitForOcRows(previousSignature = "", timeoutMs = 20000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const rows = [...document.querySelectorAll("#crt-companies-tbody tr")];
      const signature = rows.slice(0, 3).map((row) => row.innerText || "").join("|");
      if (rows.length && signature && (!previousSignature || signature !== previousSignature)) {
        return rows;
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    throw new Error("OC_TABLE_TIMEOUT");
  }

  function ocText(row, className) {
    return redactSensitiveText(row.querySelector(`.${className}`)?.innerText || "").slice(0, 1000);
  }

  function parseOcPagination(rowCount) {
    const container = document.querySelector("#crt-pagination-container");
    if (!container) return {totalPages: 1, totalItems: null};
    const values = [];
    for (const element of container.querySelectorAll("*")) {
      const candidates = [element.getAttribute("data-page"), element.textContent];
      for (const candidate of candidates) {
        const text = String(candidate || "").trim();
        if (!/^\d{1,3}$/.test(text)) continue;
        const page = Number.parseInt(text, 10);
        if (Number.isInteger(page) && page >= 1 && page <= 100) values.push(page);
      }
    }
    const paginationText = container.innerText || "";
    const totalMatch = paginationText.match(/(?:共|\/)[^0-9]{0,4}(\d{1,3})\s*页(?:\s|$)/);
    if (totalMatch) values.push(Number.parseInt(totalMatch[1], 10));
    const itemMatch = paginationText.match(/共\s*(\d{1,6})\s*条(?:记录)?/);
    const totalItems = itemMatch ? Number.parseInt(itemMatch[1], 10) : null;
    if (Number.isInteger(totalItems) && totalItems > 0 && rowCount > 0) {
      values.push(Math.ceil(totalItems / rowCount));
    }
    return {
      totalPages: Math.max(1, ...values.filter((value) => value <= 100)),
      totalItems: Number.isInteger(totalItems) && totalItems > 0 ? totalItems : null
    };
  }

  function parseOcRows(rows) {
    return rows.slice(0, 100).map((row) => {
      const linksCell = row.querySelector(".crt-col-links");
      const applyUrls = [];
      for (const anchor of linksCell?.querySelectorAll("a[href]") || []) {
        const value = sanitizedOcApplyUrl(anchor.href);
        if (value && !applyUrls.includes(value)) applyUrls.push(value);
      }
      return {
        company: ocText(row, "crt-col-company").slice(0, 200),
        company_type: ocText(row, "crt-col-type").slice(0, 80),
        industry: ocText(row, "crt-col-industry").slice(0, 500),
        recruitment_type: ocText(row, "crt-col-recruitment-type").slice(0, 200),
        recruitment_target: ocText(row, "crt-col-target").slice(0, 200),
        location: ocText(row, "crt-col-location").slice(0, 500),
        position: ocText(row, "crt-col-position").slice(0, 500),
        status: ocText(row, "crt-col-status").slice(0, 100),
        update_time: ocText(row, "crt-col-update-time").slice(0, 100),
        deadline: ocText(row, "crt-col-deadline").slice(0, 100),
        apply_urls: applyUrls.slice(0, 20),
        notice: ocText(row, "crt-col-notice"),
        exam_info: ocText(row, "crt-col-exam-info"),
        company_size: ocText(row, "crt-col-company-size").slice(0, 100),
        notes: ocText(row, "crt-col-notes")
      };
    }).filter((record) => record.company);
  }

  function ocRecordsRequireLogin(records) {
    if (!Array.isArray(records) || records.length === 0) return false;
    const gatedRows = records.filter((record) =>
      /(?:会员可见|登录后可见|请登录)/.test([
        record.position,
        record.location,
        record.notice,
        record.exam_info,
        record.company_size
      ].join(" "))
    ).length;
    const usableApplyUrl = records.some((record) =>
      (record.apply_urls || []).some((value) => {
        try {
          const parsed = new URL(value);
          return parsed.hostname !== "www.givemeoc.com" || parsed.pathname !== "/" || Boolean(parsed.search);
        } catch (_error) {
          return false;
        }
      })
    );
    return gatedRows >= Math.ceil(records.length / 2) && !usableApplyUrl;
  }

  function sanitizedOcApplyUrl(value) {
    try {
      const parsed = new URL(value, location.href);
      if (!["http:", "https:"].includes(parsed.protocol)) return null;
      parsed.username = "";
      parsed.password = "";
      const transientSignedLink = parsed.protocol === "https:"
        && parsed.hostname === "www.givemeoc.com"
        && parsed.pathname === "/wp-admin/admin-post.php"
        && parsed.searchParams.get("action") === "crt_open_link";
      // OC signatures are required to resolve the public recruitment destination.
      // They stay inside the extension operation and are scrubbed before persistence.
      if (transientSignedLink) {
        return parsed.toString().length <= 4096 ? parsed.toString() : null;
      }
      for (const key of [...parsed.searchParams.keys()]) {
        if (/(?:token|auth|session|signature|password|code|utm_|^aff$)/i.test(key)) {
          parsed.searchParams.delete(key);
        }
      }
      if (parsed.search.length > 1500) parsed.search = "";
      if (parsed.hash.length > 1024 || /[\s#]/.test(parsed.hash.slice(1))) parsed.hash = "";
      return parsed.toString();
    } catch (_error) {
      return null;
    }
  }

  async function captureOcPage(requestId, validation) {
    if (location.origin !== "https://www.givemeoc.com") {
      return actionError(requestId, "SOURCE_NOT_ALLOWED");
    }
    let rows = [...document.querySelectorAll("#crt-companies-tbody tr")];
    const before = rows.slice(0, 3).map((row) => row.innerText || "").join("|");
    if (validation.params.apply_filters) {
      fixedCheckboxes("company_type[]", ["民企"]);
      fixedCheckboxes("recruitment_type[]", ["秋招", "秋招提前批"]);
      const target = document.querySelector("#crt-target-candidates");
      const form = document.querySelector("#crt-filter-form");
      if (!target || !form) throw new Error("OC_FILTERS_NOT_FOUND");
      const targetOption = [...target.options || []].find((option) =>
        [option.value, option.textContent].some((value) => String(value || "").includes("2027"))
      );
      if (!targetOption) throw new Error("OC_TARGET_2027_NOT_FOUND");
      target.value = targetOption.value;
      dispatchControlEvents(target);
      form.dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}));
      return actionResult(requestId, {
        action: validation.action,
        page: 1,
        navigationRequested: true,
        fixedFiltersSelected: ocFixedFiltersSelected(),
        diagnostics: ocDiagnostics(rows),
        capturedAt: new Date().toISOString()
      });
    } else if (validation.params.page > 1) {
      const input = document.querySelector("#crt-pagination-container .crt-page-input");
      const button = document.querySelector("#crt-pagination-container .crt-page-go-btn");
      if (!input || !button) throw new Error("OC_PAGINATION_NOT_FOUND");
      input.value = String(validation.params.page);
      dispatchControlEvents(input);
      button.click();
      return actionResult(requestId, {
        action: validation.action,
        page: validation.params.page,
        navigationRequested: true,
        fixedFiltersSelected: ocFixedFiltersSelected(),
        diagnostics: ocDiagnostics(rows),
        capturedAt: new Date().toISOString()
      });
    } else {
      rows = await waitForOcRows();
    }
    const records = parseOcRows(rows);
    if (ocRecordsRequireLogin(records)) {
      return actionPause(
        requestId,
        protocol.pauseReasons.LOGIN_REQUIRED,
        "Log in to GiveMeOC in the retained Edge tab before capturing companies."
      );
    }
    const pagination = parseOcPagination(records.length);
    return actionResult(requestId, {
      action: validation.action,
      page: validation.params.page,
      totalPages: pagination.totalPages,
      totalItems: pagination.totalItems,
      fixedFiltersSelected: ocFixedFiltersSelected(),
      fixedRowsMatch: ocRowsMatchFixedFilters(rows),
      diagnostics: ocDiagnostics(rows),
      records,
      capturedAt: new Date().toISOString()
    });
  }

  async function executeControlledAction(message, sender) {
    const requestId = typeof message.requestId === "string" ? message.requestId : "";
    if (message.protocolVersion !== protocol.version || message.type !== protocol.messageTypes.EXECUTE_CONTROLLED_ACTION) {
      return actionError(requestId, "ACTION_MESSAGE_INVALID");
    }
    if (message.commandAuthorized !== true) {
      return actionError(requestId, "COMMAND_AUTHORIZATION_REQUIRED");
    }
    const allowedKeys = [
      "protocolVersion",
      "type",
      "requestId",
      "commandAuthorized",
      "authorizedOrigin",
      "tabId",
      "action",
      "selectorKey",
      "params",
      "actionTicket"
    ];
    if (Object.keys(message).some((key) => !allowedKeys.includes(key))) {
      return actionError(requestId, "ACTION_MESSAGE_INVALID");
    }
    // Messages sent from the extension service worker do not expose sender.tab
    // to the receiving content script. tabs.sendMessage already scopes delivery
    // to the commanded tab; verify the extension identity and page origin here.
    if (sender?.id !== chrome.runtime.id) {
      return actionError(requestId, "CURRENT_TAB_REQUIRED");
    }
    if (message.authorizedOrigin !== location.origin) {
      return actionError(requestId, "SOURCE_NOT_ALLOWED");
    }
    if (typeof message.actionTicket !== "string" || !message.actionTicket || message.actionTicket.length > ACTION_TICKET_MAX_LENGTH) {
      return actionError(requestId, "ACTION_TICKET_REQUIRED");
    }
    if (seenActionTickets.has(message.actionTicket)) {
      return actionError(requestId, "ACTION_TICKET_SINGLE_USE");
    }

    const validation = actions.validateActionRequest(
      message.action,
      message.selectorKey,
      message.params
    );
    if (!validation.ok) {
      return actionError(requestId, validation.code);
    }

    seenActionTickets.add(message.actionTicket);
    const pauseReason = pagePauseReason();
    if (pauseReason) {
      return actionPause(requestId, pauseReason);
    }
    if (!document.body) {
      return actionPause(
        requestId,
        protocol.pauseReasons.STATE_UNCLEAR,
        "The page state is unavailable; user confirmation is required."
      );
    }

    if (validation.action === protocol.actionTypes.OBSERVE_APPLICATION_PAGE) {
      return createSemanticObservation(requestId, validation);
    }
    if (validation.action === protocol.actionTypes.CAPTURE_OC_PAGE) {
      try {
        return await captureOcPage(requestId, validation);
      } catch (error) {
        return actionError(requestId, String(error?.message || "OC_CAPTURE_FAILED").slice(0, 80));
      }
    }
    return actionError(requestId, "ACTION_NOT_ALLOWED");
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message?.type !== protocol.messageTypes.READ_SANITIZED_DOM) {
      if (message?.type !== protocol.messageTypes.EXECUTE_CONTROLLED_ACTION) {
        return undefined;
      }
      void executeControlledAction(message, sender).then(sendResponse);
      return true;
    }

    const requestId = typeof message.requestId === "string" ? message.requestId : "";
    const sameExtension = sender?.id === chrome.runtime.id;
    const authorizedOrigin = message.authorizedOrigin === location.origin;
    const userAuthorized = message.authorization === "user_click_current_tab";

    if (!sameExtension || !authorizedOrigin || !userAuthorized) {
      sendResponse({
        protocolVersion: protocol.version,
        type: protocol.messageTypes.DOM_SNAPSHOT,
        requestId,
        ok: false,
        error: {code: "AUTHORIZATION_REQUIRED"}
      });
      return false;
    }

    sendResponse({
      protocolVersion: protocol.version,
      type: protocol.messageTypes.DOM_SNAPSHOT,
      requestId,
      ok: true,
      data: createSanitizedSnapshot()
    });
    return false;
  });
})();
