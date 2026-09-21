importScripts("./protocol.js", "./allowlist.js", "./config.js", "./actions.js");

(() => {
  "use strict";

  const protocol = globalThis.RecruitOpsProtocol;
  const allowlist = globalThis.RecruitOpsAllowlist;
  const config = globalThis.RecruitOpsConfig;
  const actions = globalThis.RecruitOpsActions;
  const bridgeInitialRetryMs = 500;
  const bridgeMaxRetryMs = 30000;
  const bridgeHeartbeatMs = 20000;
  const ocPageSettleTimeoutMs = 45000;
  const bridgeAuthDomain = "recruitops-browser-bridge-v1";
  let bridgeSocket = null;
  let bridgeConnectPromise = null;
  let bridgeRetryTimer = null;
  let bridgeRetryAttempt = 0;
  let bridgeHeartbeatTimer = null;
  let bridgeDeviceId = null;
  let bridgeProgressCounter = 0;
  const bridgeOperations = new Map();

  async function recordBridgeStatus(state, details = {}) {
    const value = {
      state,
      checkedAt: new Date().toISOString(),
      ...details
    };
    try {
      await chrome.storage.local.set({[config.BRIDGE_STATUS_KEY]: value});
    } catch (_error) { /* diagnostics must not interrupt browser work */ }
    return value;
  }

  function errorResponse(requestId, code) {
    return {
      protocolVersion: protocol.version,
      type: protocol.messageTypes.DOM_SNAPSHOT,
      requestId,
      ok: false,
      error: {code}
    };
  }

  async function getSettings() {
    const stored = await chrome.storage.local.get(config.DEFAULT_SETTINGS);
    return config.normalizeSettings(stored);
  }

  function isTrustedExtensionPage(sender) {
    if (sender?.id !== chrome.runtime.id || typeof sender?.url !== "string") {
      return false;
    }
    try {
      const url = new URL(sender.url);
      return url.protocol === "chrome-extension:" && url.hostname === chrome.runtime.id;
    } catch (_error) {
      return false;
    }
  }

  async function hasPageAccess(pageUrl) {
    const origin = allowlist.getHttpOrigin(pageUrl);
    return Boolean(
      origin && await chrome.permissions.contains({origins: [`${origin}/*`]})
    );
  }

  function configResponse(requestType, requestId, settings) {
    return {
      protocolVersion: protocol.version,
      type: requestType,
      requestId,
      ok: true,
      settings
    };
  }

  function configError(requestType, requestId, code) {
    return {
      protocolVersion: protocol.version,
      type: requestType,
      requestId,
      ok: false,
      error: {code}
    };
  }

  function observationError(requestId, code) {
    return {
      protocolVersion: protocol.version,
      type: protocol.messageTypes.OBSERVATION_SUBMITTED,
      requestId,
      ok: false,
      error: {code}
    };
  }

  function applicationCaptureResponse(requestId, ok, data, code) {
    const response = {
      protocolVersion: protocol.version,
      type: protocol.messageTypes.APPLICATION_CAPTURE_SUBMITTED,
      requestId,
      ok
    };
    if (data !== undefined) {
      response.data = data;
    }
    if (code) {
      response.error = {code};
    }
    return response;
  }

  function createActionTicket() {
    if (globalThis.crypto?.randomUUID) {
      return globalThis.crypto.randomUUID();
    }
    return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  function normalizeResponseApplicationIds(value) {
    if (!Array.isArray(value) || value.length === 0 || value.length > 500) {
      return null;
    }
    const ids = value.map((item) => (
      typeof item === "string" && item.length > 0 && item.length <= 128 ? item : null
    ));
    if (ids.some((item) => item === null) || new Set(ids).size !== ids.length) {
      return null;
    }
    return ids;
  }

  function localApiErrorCode(body, status) {
    if (body && typeof body.error_code === "string" && /^[a-z0-9_]{1,80}$/i.test(body.error_code)) {
      return body.error_code;
    }
    return `API_HTTP_${status}`;
  }

  async function waitForTabComplete(tabId, timeoutMs = 30000) {
    const current = await chrome.tabs.get(tabId);
    if (current.status === "complete") return current;
    return new Promise((resolve, reject) => {
      const listener = (updatedTabId, changeInfo, tab) => {
        if (updatedTabId !== tabId || changeInfo.status !== "complete") return;
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve(tab);
      };
      const timer = setTimeout(() => {
        chrome.tabs.onUpdated.removeListener(listener);
        reject(new Error("PAGE_LOAD_TIMEOUT"));
      }, timeoutMs);
      chrome.tabs.onUpdated.addListener(listener);
    });
  }

  function delay(milliseconds) {
    return new Promise((resolve) => setTimeout(resolve, milliseconds));
  }

  function boundedPromise(promise, timeoutMs, errorCode) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        reject(new Error(errorCode));
      }, timeoutMs);
      Promise.resolve(promise).then((value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      }).catch((error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(error);
      });
    });
  }

  function usableSemanticObservation(response) {
    if (!response?.ok || !response.data) return false;
    const text = typeof response.data.page?.text === "string"
      ? response.data.page.text.trim()
      : "";
    const nodes = Array.isArray(response.data.semanticNodes)
      ? response.data.semanticNodes
      : [];
    const records = Array.isArray(response.data.applicationRecords)
      ? response.data.applicationRecords
      : [];
    return records.length > 0 || text.length >= 40 || nodes.length >= 2;
  }

  function semanticObservationScore(response) {
    if (!response?.ok || !response.data) return -1;
    const textLength = typeof response.data.page?.text === "string"
      ? response.data.page.text.trim().length
      : 0;
    const nodeCount = Array.isArray(response.data.semanticNodes)
      ? response.data.semanticNodes.length
      : 0;
    const recordCount = Array.isArray(response.data.applicationRecords)
      ? response.data.applicationRecords.length
      : 0;
    const mappedCount = Array.isArray(response.data.entries)
      ? response.data.entries.length
      : 0;
    return textLength + nodeCount * 120 + recordCount * 100000 + mappedCount * 1000000;
  }

  async function requestSemanticObservation(tabId, requestId, origin, validation, frameId = 0, timeoutMs = 30000) {
    const message = {
      protocolVersion: protocol.version,
      type: protocol.messageTypes.EXECUTE_CONTROLLED_ACTION,
      requestId,
      commandAuthorized: true,
      authorizedOrigin: origin,
      tabId,
      action: validation.action,
      selectorKey: validation.selectorKey,
      params: validation.params,
      actionTicket: createActionTicket()
    };
    return new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        reject(new Error("EDGE_PAGE_COMMAND_TIMEOUT"));
      }, timeoutMs);
      chrome.tabs.sendMessage(tabId, message, {frameId}).then((response) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(response);
      }).catch((error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(error);
      });
    });
  }

  function aggregateSemanticObservations(responses, validation) {
    const successful = responses.filter((item) => item?.response?.ok && item.response.data);
    if (!successful.length) {
      const paused = responses.find((item) => item?.response?.pause?.reason === protocol.pauseReasons.CAPTCHA_REQUIRED)
        || responses.find((item) => item?.response?.pause?.reason === protocol.pauseReasons.LOGIN_REQUIRED)
        || responses.find((item) => item?.response?.pause);
      return paused?.response || null;
    }
    const ranked = successful.slice().sort((left, right) =>
      semanticObservationScore(right.response) - semanticObservationScore(left.response)
      || Number(left.frameId !== 0) - Number(right.frameId !== 0)
    );
    const best = ranked[0];
    const semanticNodes = [];
    const applicationRecords = [];
    const entries = [];
    let visibleMediaCount = 0;
    for (const item of successful) {
      for (const node of item.response.data.semanticNodes || []) {
        if (semanticNodes.length >= 240) break;
        semanticNodes.push({...node, frameId: item.frameId, frameUrl: item.frameUrl});
      }
      for (const record of item.response.data.applicationRecords || []) {
        if (applicationRecords.length >= 100) break;
        applicationRecords.push({...record, frameId: item.frameId, frameUrl: item.frameUrl});
      }
      for (const entry of item.response.data.entries || []) {
        if (entries.length >= 100) break;
        entries.push({...entry, frameId: item.frameId, frameUrl: item.frameUrl});
      }
      const frameMediaCount = Number(item.response.data.diagnostics?.visibleMediaCount);
      if (Number.isFinite(frameMediaCount) && frameMediaCount > 0) {
        visibleMediaCount = Math.min(100, visibleMediaCount + frameMediaCount);
      }
    }
    return {
      ...best.response,
      data: {
        ...best.response.data,
        semanticNodes,
        applicationRecords,
        entries,
        diagnostics: {
          ...(best.response.data.diagnostics || {}),
          frameCount: responses.length,
          successfulFrameCount: successful.length,
          applicationRecordCount: applicationRecords.length,
          mappedStatusCount: entries.length,
          visibleMediaCount,
          frames: successful.slice(0, 32).map((item) => ({
            frameId: item.frameId,
            frameUrl: item.frameUrl,
            recordCount: item.response.data.applicationRecords?.length || 0,
            mappedStatusCount: item.response.data.entries?.length || 0
          }))
        }
      }
    };
  }

  async function waitForSemanticObservation(tabId, requestId, origin, validation, frameContexts = []) {
    let lastResponse = null;
    let bestResponse = null;
    let bestScore = -1;
    let stableAttempts = 0;
    const contexts = frameContexts.length ? frameContexts : [{frameId: 0, frameUrl: "", origin}];
    for (let attempt = 1; attempt <= 22; attempt += 1) {
      const settled = await Promise.allSettled(contexts.map((frame) => requestSemanticObservation(
        tabId,
        `${requestId}-${frame.frameId}-${attempt}`.slice(0, 128),
        frame.origin,
        validation,
        frame.frameId
      )));
      lastResponse = aggregateSemanticObservations(settled.map((item, index) => ({
        frameId: contexts[index].frameId,
        frameUrl: contexts[index].frameUrl,
        response: item.status === "fulfilled" ? item.value : null
      })), validation);
      const pauseReason = lastResponse?.pause?.reason;
      if (
        pauseReason === protocol.pauseReasons.LOGIN_REQUIRED ||
        pauseReason === protocol.pauseReasons.CAPTCHA_REQUIRED
      ) {
        return lastResponse;
      }
      const score = semanticObservationScore(lastResponse);
      if (score > bestScore) {
        bestResponse = lastResponse;
        bestScore = score;
        stableAttempts = 0;
      } else if (score === bestScore && score >= 0) {
        stableAttempts += 1;
      }
      if (
        attempt >= 6 &&
        stableAttempts >= 2 &&
        usableSemanticObservation(bestResponse)
      ) {
        return bestResponse;
      }
      if (attempt < 22) await delay(1200);
    }
    return bestResponse || lastResponse;
  }

  function bridgeId(value, maxLength = 128) {
    return typeof value === "string" && value.trim()
      ? value.trim().slice(0, maxLength)
      : null;
  }

  function bridgeSocketOpen(socket = bridgeSocket) {
    return Boolean(socket && socket.readyState === 1);
  }

  async function readBridgeStatus() {
    const key = config.BRIDGE_STATUS_KEY;
    const stored = await chrome.storage.local.get({[key]: null});
    const value = stored[key];
    return value && typeof value === "object" ? value : null;
  }

  async function waitForBridgeAuthentication(socket, timeoutMs = 8000) {
    const deadline = Date.now() + timeoutMs;
    while (socket === bridgeSocket && Date.now() < deadline) {
      const status = await readBridgeStatus();
      if (["connected", "connection_error", "disconnected"].includes(status?.state)) {
        return status;
      }
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    return null;
  }

  async function getBridgeDeviceId() {
    if (bridgeDeviceId) {
      return bridgeDeviceId;
    }
    const key = config.BRIDGE_DEVICE_ID_KEY;
    const stored = await chrome.storage.local.get({[key]: ""});
    const existing = bridgeId(stored[key]);
    if (existing && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(existing)) {
      bridgeDeviceId = existing;
      return bridgeDeviceId;
    }
    bridgeDeviceId = globalThis.crypto?.randomUUID
      ? globalThis.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    await chrome.storage.local.set({[key]: bridgeDeviceId});
    return bridgeDeviceId;
  }

  function bridgeAuthMessage(challenge, deviceId) {
    return `${bridgeAuthDomain}\n${deviceId}\n${challenge}`;
  }

  async function signBridgeChallenge(apiToken, challenge, deviceId) {
    const key = await globalThis.crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(apiToken),
      {name: "HMAC", hash: "SHA-256"},
      false,
      ["sign"]
    );
    const signature = await globalThis.crypto.subtle.sign(
      "HMAC",
      key,
      new TextEncoder().encode(bridgeAuthMessage(challenge, deviceId))
    );
    return [...new Uint8Array(signature)]
      .map((item) => item.toString(16).padStart(2, "0"))
      .join("");
  }

  function sendBridgeMessage(message, socket = bridgeSocket) {
    if (!bridgeSocketOpen(socket)) {
      return false;
    }
    try {
      socket.send(JSON.stringify(message));
      return true;
    } catch (_error) {
      return false;
    }
  }

  function bridgeEventId(operationId, label) {
    return `${label}-${operationId}`.slice(0, 128);
  }

  async function reportBridgeProgress(socket, operationId, status, detail) {
    bridgeProgressCounter += 1;
    sendBridgeMessage({
      protocol_version: protocol.bridgeProtocol.version,
      type: "progress",
      operation_id: operationId,
      event_id: `${operationId}-${Date.now()}-${bridgeProgressCounter}`.slice(0, 128),
      status,
      payload: {stage: status, detail}
    }, socket);
  }

  function bridgeWorkFromDispatch(message) {
    const validation = protocol.validateBridgeDispatch(message);
    const payload = validation.payload || {};
    const command = validation.command || {};
    return {
      operation_id: bridgeId(message.operation_id || payload.operation_id),
      operation: payload.operation,
      commandComplete: validation.ok,
      commandMissingFields: validation.missingFields || [],
      action: command.action,
      selector_key: command.selector_key,
      params: command.params,
      page_url: command.page_url,
      origin: command.origin,
      application_id: command.application_id,
      application_ids: command.application_ids
    };
  }

  function bridgeFailure(errorCode = "STATE_UNCLEAR", result = {}) {
    const status = ["LOGIN_REQUIRED", "CAPTCHA_REQUIRED", "STATE_UNCLEAR"].includes(errorCode)
      ? "STATE_UNCLEAR"
      : "FAILED";
    return {
      ok: false,
      status,
      errorCode,
      result
    };
  }

  function canRequestVision(response, validation, work) {
    if (!response?.ok || validation.params.include_vision !== true) return false;
    const applicationIds = normalizeResponseApplicationIds(work?.application_ids);
    const primaryApplicationId = bridgeId(work?.application_id);
    if (!applicationIds || applicationIds.length !== 1 || applicationIds[0] !== primaryApplicationId) {
      return false;
    }
    const data = response.data;
    const page = data?.page && typeof data.page === "object" ? data.page : {};
    const pageText = typeof page.text === "string" ? page.text.trim() : "";
    const gateText = `${page.title || ""} ${pageText}`.toLowerCase();
    const entries = Array.isArray(data?.entries) ? data.entries : [];
    const records = Array.isArray(data?.applicationRecords) ? data.applicationRecords : [];
    if (
      entries.length > 0 ||
      /captcha|验证码|人机验证|安全验证|滑块验证|security check|verify you are human/.test(gateText) ||
      /请先登录|请登录|登录失效|重新登录|未登录|login required|session expired|sign in|log in/.test(gateText)
    ) return false;
    if (records.length > 1) return false;
    if (records.length === 1) {
      const record = records[0];
      if (!record || typeof record !== "object") return false;
      const signals = record.signals && typeof record.signals === "object"
        ? record.signals
        : {};
      return typeof record.title === "string" &&
        record.title.trim().length > 0 &&
        signals.conflicting_statuses !== true;
    }
    const visibleMediaCount = Number(data?.diagnostics?.visibleMediaCount);
    return Boolean(pageText) || (Number.isFinite(visibleMediaCount) && visibleMediaCount > 0);
  }

  function pageIdentity(rawUrl) {
    const sanitized = sanitizedTabUrl(rawUrl);
    if (!sanitized) return null;
    try {
      const url = new URL(sanitized);
      url.pathname = url.pathname.replace(/\/+$/, "") || "/";
      if ((url.protocol === "http:" && url.port === "80") ||
          (url.protocol === "https:" && url.port === "443")) {
        url.port = "";
      }
      return url.toString();
    } catch (_error) {
      return null;
    }
  }

  async function assertVisibleCaptureTarget(tab, expectedPageUrl) {
    const current = await chrome.tabs.get(tab.id);
    const expectedIdentity = pageIdentity(expectedPageUrl);
    const currentIdentity = pageIdentity(current?.url);
    if (
      current?.id !== tab.id ||
      current?.windowId !== tab.windowId ||
      !expectedIdentity ||
      currentIdentity !== expectedIdentity
    ) {
      throw new Error("CAPTURE_TAB_CHANGED");
    }
    const [activeTab] = await chrome.tabs.query({active: true, windowId: tab.windowId});
    if (activeTab?.id !== tab.id || pageIdentity(activeTab.url) !== expectedIdentity) {
      throw new Error("CAPTURE_TAB_NOT_ACTIVE");
    }
    return currentIdentity;
  }

  function normalizeVisionResponse(body) {
    if (!body || typeof body !== "object" || Array.isArray(body)) {
      throw new Error("VISION_RESPONSE_INVALID");
    }
    if (
      typeof body.text !== "string" ||
      typeof body.confidence !== "number" ||
      !Number.isFinite(body.confidence) ||
      body.confidence < 0 ||
      body.confidence > 1 ||
      typeof body.model !== "string" ||
      typeof body.image_sha256 !== "string" ||
      !body.image_sha256
    ) {
      throw new Error("VISION_RESPONSE_INVALID");
    }
    return {
      text: body.text.slice(0, 20_000),
      confidence: body.confidence,
      model: body.model.slice(0, 200),
      image_sha256: body.image_sha256.slice(0, 128),
      usage: body.usage === undefined ? null : body.usage
    };
  }

  async function captureVisiblePageVision(settings, tab, pageUrl, operationId) {
    if (typeof tab?.id !== "number" || typeof tab?.windowId !== "number") {
      throw new Error("TAB_UNAVAILABLE");
    }
    const canCapture = await chrome.permissions.contains({origins: ["<all_urls>"]});
    if (!canCapture) {
      throw new Error("SCREEN_CAPTURE_PERMISSION_REQUIRED");
    }
    const expectedPageIdentity = pageIdentity(pageUrl);
    if (!expectedPageIdentity) throw new Error("VISION_PAGE_URL_INVALID");
    await assertVisibleCaptureTarget(tab, pageUrl).catch(async (error) => {
      if (error?.message !== "CAPTURE_TAB_NOT_ACTIVE") throw error;
      await chrome.tabs.update(tab.id, {active: true});
      try { await chrome.windows.update(tab.windowId, {focused: true}); } catch (_focusError) { /* best effort */ }
    });
    try { await chrome.windows.update(tab.windowId, {focused: true}); } catch (_error) { /* best effort */ }
    const beforeCaptureIdentity = await assertVisibleCaptureTarget(tab, pageUrl);
    const imageDataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, {
      format: "jpeg",
      quality: 82
    });
    const afterCaptureIdentity = await assertVisibleCaptureTarget(tab, pageUrl);
    if (beforeCaptureIdentity !== afterCaptureIdentity || afterCaptureIdentity !== expectedPageIdentity) {
      throw new Error("CAPTURE_TAB_CHANGED");
    }
    const apiBaseUrl = config.normalizeApiBaseUrl(settings.apiBaseUrl);
    if (!apiBaseUrl) throw new Error("LOCAL_API_ONLY");
    const headers = {"Content-Type": "application/json"};
    if (settings.apiToken) headers.Authorization = `Bearer ${settings.apiToken}`;
    const response = await fetch(`${apiBaseUrl}/api/browser/vision`, {
      method: "POST",
      headers,
      credentials: "omit",
      body: JSON.stringify({
        image_data_url: imageDataUrl,
        page_url: pageUrl,
        operation_id: operationId
      })
    });
    let body = null;
    try { body = await response.json(); } catch (_error) { body = null; }
    if (!response.ok || !body) {
      throw new Error(localApiErrorCode(body, response.status));
    }
    return normalizeVisionResponse(body);
  }

  let visionQueue = Promise.resolve();
  function captureVisiblePageVisionSerial(settings, tab, pageUrl, operationId) {
    const current = visionQueue.catch(() => null).then(
      () => captureVisiblePageVision(settings, tab, pageUrl, operationId)
    );
    visionQueue = current.catch(() => null);
    return current;
  }

  async function persistOcSnapshot(settings, payload) {
    const apiBaseUrl = config.normalizeApiBaseUrl(settings.apiBaseUrl);
    if (!apiBaseUrl) throw new Error("LOCAL_API_ONLY");
    const headers = {"Content-Type": "application/json"};
    if (settings.apiToken) headers.Authorization = `Bearer ${settings.apiToken}`;
    const response = await fetch(`${apiBaseUrl}/api/browser/oc-snapshots`, {
      method: "POST",
      headers,
      credentials: "omit",
      body: JSON.stringify(payload)
    });
    let body = null;
    try { body = await response.json(); } catch (_error) { body = null; }
    if (!response.ok || !body) throw new Error(localApiErrorCode(body, response.status));
    return body;
  }

  const ocCandidateIndustryKeywords = Object.freeze([
    "软件", "科技", "互联网", "机器人", "人工智能", "游戏", "新能源", "车企"
  ]);

  function ocRecordNeedsResolvedLinks(record) {
    const recruitmentTypes = String(record?.recruitment_type || "")
      .split(/[,，、/|;；\n]+/)
      .map((value) => value.trim())
      .filter(Boolean);
    return String(record?.company_type || "").trim() === "民企"
      && String(record?.recruitment_target || "").includes("2027")
      && recruitmentTypes.some((value) => value === "秋招" || value === "秋招提前批")
      && ocCandidateIndustryKeywords.some((value) => String(record?.industry || "").includes(value));
  }

  function isOcSignedApplyUrl(value) {
    try {
      const parsed = new URL(value);
      return parsed.protocol === "https:"
        && parsed.hostname === "www.givemeoc.com"
        && parsed.pathname === "/wp-admin/admin-post.php"
        && parsed.searchParams.get("action") === "crt_open_link";
    } catch (_error) {
      return false;
    }
  }

  function isUsableOcDestinationUrl(value) {
    try {
      const parsed = new URL(value);
      return (parsed.protocol === "https:" || parsed.protocol === "http:")
        && parsed.hostname !== "www.givemeoc.com"
        && parsed.hostname !== "givemeoc.com"
        && !parsed.username
        && !parsed.password;
    } catch (_error) {
      return false;
    }
  }

  function classifyOcDestinationUrl(value) {
    try {
      const parsed = new URL(value);
      const hostname = parsed.hostname.toLowerCase().replace(/\.$/, "");
      const path = parsed.pathname.toLowerCase().replace(/\/$/, "");
      const location = `${path}#${parsed.hash.slice(1).toLowerCase()}`;
      if (hostname === "wj.qq.com" || (hostname === "docs.qq.com" && path.startsWith("/form"))) {
        return {kind: "form", reason: "Tencent questionnaire/form is not a reusable recruitment job list."};
      }
      const isHost = (domain) => hostname === domain || hostname.endsWith(`.${domain}`);
      if (isHost("wjx.cn") || isHost("wjx.top") || [
        "jinshuju.net", "www.jinshuju.net",
        "jinshuju.com", "www.jinshuju.com", "forms.office.com", "f.wps.cn"
      ].includes(hostname) || (hostname === "docs.google.com" && path.startsWith("/forms"))) {
        return {kind: "form", reason: "Third-party form is not a reusable recruitment job list."};
      }
      if (hostname === "alidocs.dingtalk.com" && path.includes("/notable/share/form")) {
        return {kind: "form", reason: "DingTalk form is not a reusable recruitment job list."};
      }
      if (hostname === "mp.weixin.qq.com") {
        return {kind: "article", reason: "WeChat article is a notice, not a reusable recruitment job list."};
      }
      if ([
        "candidatehome/applications", "application-record", "application_record",
        "delivery-record", "delivery_record"
      ].some((marker) => location.includes(marker))) {
        return {kind: "application_record", reason: "Application record/personal center cannot enumerate jobs."};
      }
      if (["apply-success", "apply_success", "application-success"].some((marker) => location.includes(marker))) {
        return {kind: "success_page", reason: "Application success page cannot enumerate jobs."};
      }
      return null;
    } catch (_error) {
      return null;
    }
  }

  function isOcLoginFallbackUrl(value) {
    try {
      const parsed = new URL(value);
      return ["www.givemeoc.com", "givemeoc.com"].includes(parsed.hostname)
        && parsed.pathname === "/780.html";
    } catch (_error) {
      return false;
    }
  }

  function sanitizedOcDestinationUrl(rawUrl) {
    try {
      const url = new URL(rawUrl);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
        return null;
      }
      for (const key of [...url.searchParams.keys()]) {
        if (/(?:token|auth|session|signature|password|code|utm_|^aff$)/i.test(key)) {
          url.searchParams.delete(key);
        }
      }
      if (url.search.length > 1500) url.search = "";
      if (
        url.hash.length > 1024
        || /\s/.test(url.hash)
        || /(?:token|auth|session|signature|password|code)=/i.test(url.hash)
      ) {
        url.hash = "";
      }
      return url.toString();
    } catch (_error) {
      return null;
    }
  }

  async function resolveOcSignedApplyUrl(value, state) {
    if (!isOcSignedApplyUrl(value)) return {destination: null, loginRequired: false};
    let tab = null;
    let quietTimer = null;
    try {
      tab = await chrome.tabs.create({url: "about:blank", active: false});
      return await new Promise((resolve) => {
        let settled = false;
        let latest = null;
        const finish = (result) => {
          if (settled) return;
          settled = true;
          if (quietTimer !== null) clearTimeout(quietTimer);
          clearTimeout(deadline);
          chrome.tabs.onUpdated.removeListener(onUpdated);
          resolve(result);
        };
        const acceptUrl = (candidate) => {
          const sanitized = sanitizedOcDestinationUrl(candidate);
          if (!sanitized) return;
          if (isOcLoginFallbackUrl(sanitized)) {
            finish({destination: null, loginRequired: true});
            return;
          }
          if (!isUsableOcDestinationUrl(sanitized)) return;
          latest = sanitized;
          if (quietTimer !== null) clearTimeout(quietTimer);
          quietTimer = setTimeout(
            () => finish({destination: latest, loginRequired: false}),
            500
          );
        };
        const onUpdated = (tabId, changeInfo, updatedTab) => {
          if (tabId !== tab.id) return;
          if (state.cancelled) {
            finish({destination: null, loginRequired: false});
            return;
          }
          acceptUrl(changeInfo.url || updatedTab?.url || "");
          if (changeInfo.status === "complete" && latest) {
            finish({destination: latest, loginRequired: false});
          }
        };
        const deadline = setTimeout(
          () => finish({destination: latest, loginRequired: false}),
          10000
        );
        chrome.tabs.onUpdated.addListener(onUpdated);
        chrome.tabs.update(tab.id, {url: value}).then(
          (updatedTab) => acceptUrl(updatedTab?.url || ""),
          () => finish({destination: null, loginRequired: false})
        );
      });
    } finally {
      if (typeof tab?.id === "number") {
        try { await chrome.tabs.remove(tab.id); } catch (_error) { /* already closed */ }
      }
    }
  }

  async function resolveOcCandidateLinks(records, state, socket, operationId) {
    const targets = [];
    const seen = new Set();
    for (const record of records) {
      if (!ocRecordNeedsResolvedLinks(record)) continue;
      for (const value of record.apply_urls || []) {
        if (isOcSignedApplyUrl(value) && !seen.has(value)) {
          seen.add(value);
          targets.push(value);
        }
      }
    }
    const resolved = new Map();
    let loginRequired = false;
    let loginFallbacks = 0;
    let cursor = 0;
    let completed = 0;
    const loginProbeSize = Math.min(12, targets.length);
    const worker = async () => {
      while (cursor < targets.length && !state.cancelled && !loginRequired) {
        const index = cursor;
        cursor += 1;
        const sourceUrl = targets[index];
        let outcome = {destination: null, loginRequired: false};
        try {
          outcome = await resolveOcSignedApplyUrl(sourceUrl, state);
        } catch (_error) {
          outcome = {destination: null, loginRequired: false};
        }
        if (outcome.loginRequired) loginFallbacks += 1;
        const destination = outcome.destination;
        if (destination && isUsableOcDestinationUrl(destination)) {
          resolved.set(sourceUrl, {
            destination,
            exclusion: classifyOcDestinationUrl(destination)
          });
        }
        completed += 1;
        // A lone stale/removed OC link can also fall back to /780.html. Treat it as
        // a global login failure only when the initial probe is entirely made of
        // login fallbacks and no public destination has resolved.
        if (
          completed >= loginProbeSize
          && resolved.size === 0
          && loginFallbacks === completed
        ) {
          loginRequired = true;
        }
        if (completed === targets.length || completed % 25 === 0) {
          await reportBridgeProgress(
            socket,
            operationId,
            "EXTRACTING",
            `Resolved ${completed} of ${targets.length} OC recruitment links.`
          );
        }
      }
    };
    await Promise.all(Array.from({length: Math.min(6, targets.length)}, () => worker()));
    for (const record of records) {
      if (!ocRecordNeedsResolvedLinks(record)) {
        record.resolved_apply_urls = [];
        record.link_resolution = "not_requested";
        continue;
      }
      const destinations = [];
      const excluded = [];
      for (const value of record.apply_urls || []) {
        const outcome = isUsableOcDestinationUrl(value)
          ? {destination: value, exclusion: classifyOcDestinationUrl(value)}
          : resolved.get(value);
        const destination = outcome?.destination;
        if (!destination) continue;
        if (outcome.exclusion) {
          if (!excluded.some((item) => item.url === destination)) {
            excluded.push({url: destination, ...outcome.exclusion});
          }
        } else if (!destinations.includes(destination)) {
          destinations.push(destination);
        }
      }
      record.resolved_apply_urls = destinations.slice(0, 20);
      record.excluded_apply_urls = excluded.slice(0, 20);
      record.link_resolution = destinations.length > 0
        ? "resolved"
        : excluded.length > 0
        ? "excluded_non_job_entry"
        : "unresolved";
    }
    const outcomes = [...resolved.values()];
    const excludedCount = outcomes.filter((item) => item.exclusion).length;
    return {
      requested: targets.length,
      resolved: resolved.size,
      unresolved: Math.max(0, targets.length - resolved.size),
      login_fallbacks: loginFallbacks,
      login_required: loginRequired,
      addressable: resolved.size - excludedCount,
      excluded: excludedCount
    };
  }

  function scrubOcTransientApplyUrls(records) {
    for (const record of records) {
      const publicUrls = [];
      for (const value of record.apply_urls || []) {
        if (!isUsableOcDestinationUrl(value)) continue;
        const sanitized = sanitizedOcDestinationUrl(value);
        if (sanitized && !publicUrls.includes(sanitized)) publicUrls.push(sanitized);
      }
      record.apply_urls = publicUrls.slice(0, 20);
    }
  }

  async function executeOcSnapshotCapture(settings, work, state, socket, validation) {
    const pageUrl = sanitizedTabUrl(work.page_url);
    const origin = pageUrl ? allowlist.getHttpOrigin(pageUrl) : null;
    if (origin !== "https://www.givemeoc.com" || work.origin !== origin) {
      return bridgeFailure("SOURCE_NOT_ALLOWED");
    }
    if (!await chrome.permissions.contains({origins: [`${origin}/*`]})) {
      return bridgeFailure("SOURCE_NOT_ALLOWED");
    }
    const operationId = work.operation_id;
    const requestId = `bridge-${operationId || createActionTicket()}`.slice(0, 100);
    let tab = null;
    let keepTabOpen = false;
    const injectOcContentScript = async () => boundedPromise(
      chrome.scripting.executeScript({
        target: {tabId: tab.id},
        files: ["src/protocol.js", "src/actions.js", "src/content-script.js"]
      }),
      10000,
      "EDGE_SCRIPT_INJECTION_TIMEOUT"
    );
    const captureCurrentPage = async (requestIdSuffix) => {
      const currentValidation = {...validation, params: {page: 1, apply_filters: false}};
      try {
        return await requestSemanticObservation(
          tab.id,
          `${requestId}-${requestIdSuffix}`.slice(0, 128),
          origin,
          currentValidation
        );
      } catch (_error) {
        try { tab = await waitForTabComplete(tab.id, 5000); } catch (_waitError) {
          tab = await chrome.tabs.get(tab.id);
        }
        await injectOcContentScript();
        return requestSemanticObservation(
          tab.id,
          `${requestId}-${requestIdSuffix}-retry`.slice(0, 128),
          origin,
          currentValidation
        );
      }
    };
    const waitForOcPage = async (page, previousSignature = "", previousTotalItems = null) => {
      const deadline = Date.now() + ocPageSettleTimeoutMs;
      let lastResponse = null;
      while (Date.now() < deadline) {
        if (state.cancelled) {
          return {ok: false, error: {code: "CANCELLED"}};
        }
        try {
          try { tab = await waitForTabComplete(tab.id, 5000); } catch (_waitError) {
            tab = await chrome.tabs.get(tab.id);
          }
          await injectOcContentScript();
          lastResponse = await captureCurrentPage(`settled-${page}-${Date.now()}`);
        } catch (_error) {
          lastResponse = null;
        }
        const reason = lastResponse?.pause?.reason;
        if (reason === protocol.pauseReasons.LOGIN_REQUIRED || reason === protocol.pauseReasons.CAPTCHA_REQUIRED) {
          return lastResponse;
        }
        const pageRecords = Array.isArray(lastResponse?.data?.records)
          ? lastResponse.data.records
          : [];
        const currentSignature = pageSignature(pageRecords);
        const currentTotalItems = Number(lastResponse?.data?.totalItems);
        const resultChanged = !previousSignature
          || currentSignature !== previousSignature
          || (Number.isInteger(previousTotalItems) && currentTotalItems !== previousTotalItems);
        if (
          lastResponse?.ok &&
          pageRecords.length > 0 &&
          lastResponse.data?.fixedFiltersSelected === true &&
          lastResponse.data?.fixedRowsMatch === true &&
          resultChanged
        ) {
          // The OC table is replaced in jQuery's success callback. Let its complete
          // callback clear the in-flight guard before requesting the next page.
          await delay(350);
          return lastResponse;
        }
        await delay(500);
      }
      return lastResponse?.pause
        ? lastResponse
        : {
            ok: false,
            error: {code: "OC_TABLE_TIMEOUT"},
            data: lastResponse?.data || null
          };
    };
    const requestOcPage = async (page, applyFilters, previousSignature = "", previousTotalItems = null) => {
      const pageValidation = {...validation, params: {page, apply_filters: applyFilters}};
      try {
        const response = await requestSemanticObservation(
          tab.id,
          `${requestId}-${page}`.slice(0, 128),
          origin,
          pageValidation,
          15000
        );
        if (response?.data?.navigationRequested !== true && response?.error?.code !== "OC_TABLE_TIMEOUT") {
          return response;
        }
      } catch (_error) {
        // Navigation may replace the page before the content script can reply.
      }
      return waitForOcPage(page, previousSignature, previousTotalItems);
    };
    const pageSignature = (records) => JSON.stringify((records || []).map((record) => [
      record.company,
      record.recruitment_type,
      record.recruitment_target,
      record.update_time,
      record.apply_urls
    ]));
    try {
      const extensionVersion = chrome.runtime.getManifest().version;
      await reportBridgeProgress(socket, operationId, "NAVIGATING", `Opening GiveMeOC with Edge extension ${extensionVersion}.`);
      tab = await chrome.tabs.create({url: pageUrl, active: false});
      tab = await waitForTabComplete(tab.id);
      if (allowlist.getHttpOrigin(tab.url || "") !== origin) return bridgeFailure("SOURCE_NOT_ALLOWED");
      await injectOcContentScript();
      const beforeFilter = await captureCurrentPage("before-filter");
      const beforeFilterRecords = Array.isArray(beforeFilter?.data?.records) ? beforeFilter.data.records : [];
      const beforeFilterSignature = pageSignature(beforeFilterRecords);
      const beforeFilterTotalItems = Number(beforeFilter?.data?.totalItems);
      await reportBridgeProgress(socket, operationId, "EXTRACTING", "Applying the fixed private-company, 2027, autumn filters.");
      const firstValidation = {
        ...validation,
        params: {page: 1, apply_filters: true}
      };
      const first = await requestOcPage(1, true, beforeFilterSignature, beforeFilterTotalItems);
      if (!first?.ok) {
        const reason = first?.pause?.reason;
        const errorCode = reason === protocol.pauseReasons.LOGIN_REQUIRED
          ? "LOGIN_REQUIRED"
          : reason === protocol.pauseReasons.CAPTCHA_REQUIRED
          ? "CAPTCHA_REQUIRED"
          : first?.error?.code || "STATE_UNCLEAR";
        keepTabOpen = true;
        await chrome.tabs.update(tab.id, {active: true});
        if (typeof tab.windowId === "number") {
          try { await chrome.windows.update(tab.windowId, {focused: true}); } catch (_error) { /* best effort */ }
        }
        return bridgeFailure(errorCode, {
          tab_retained: true,
          page_url: sanitizedTabUrl(tab.url),
          diagnostics: first?.data?.diagnostics || null
        });
      }
      const totalPages = Math.min(100, Math.max(1, Number(first.data?.totalPages) || 1));
      const totalItems = Number(first.data?.totalItems);
      if (!Number.isInteger(totalItems) || totalItems < 1) {
        return bridgeFailure("OC_TOTAL_ITEMS_UNAVAILABLE");
      }
      const records = Array.isArray(first.data?.records) ? [...first.data.records] : [];
      if (first.data?.fixedFiltersSelected !== true) {
        return bridgeFailure("OC_FILTERS_NOT_APPLIED");
      }
      if (first.data?.fixedRowsMatch !== true) {
        return bridgeFailure("OC_FILTER_RESULTS_INVALID");
      }
      await reportBridgeProgress(
        socket,
        operationId,
        "EXTRACTING",
        `GiveMeOC filters verified: ${totalItems} records across ${totalPages} pages.`
      );
      const pageCounts = [records.length];
      const seenPageSignatures = new Set([pageSignature(records)]);
      for (let page = 2; page <= totalPages; page += 1) {
        if (state.cancelled) return {ok: false, status: "CANCELLED", result: {reason: "cancelled"}};
        await reportBridgeProgress(socket, operationId, "EXTRACTING", `Collecting GiveMeOC page ${page} of ${totalPages}.`);
        const previousSignature = pageSignature(records.slice(-pageCounts[pageCounts.length - 1]));
        const response = await requestOcPage(page, false, previousSignature);
        if (!response?.ok || !Array.isArray(response.data?.records)) {
          const reason = response?.pause?.reason;
          const errorCode = reason === protocol.pauseReasons.LOGIN_REQUIRED
            ? "LOGIN_REQUIRED"
            : reason === protocol.pauseReasons.CAPTCHA_REQUIRED
            ? "CAPTCHA_REQUIRED"
            : response?.error?.code || "OC_PAGE_CAPTURE_FAILED";
          if (reason) {
            keepTabOpen = true;
            await chrome.tabs.update(tab.id, {active: true});
            if (typeof tab.windowId === "number") {
              try { await chrome.windows.update(tab.windowId, {focused: true}); } catch (_error) { /* best effort */ }
            }
          }
          return bridgeFailure(errorCode, {
            failed_page: page,
            total_pages: totalPages,
            diagnostics: response?.data?.diagnostics || null,
            ...(reason ? {tab_retained: true, page_url: sanitizedTabUrl(tab.url)} : {})
          });
        }
        if (Number(response.data?.totalItems) !== totalItems) {
          return bridgeFailure("OC_TOTAL_ITEMS_CHANGED", {
            page,
            expected_total_items: totalItems,
            observed_total_items: response.data?.totalItems
          });
        }
        if (response.data?.fixedFiltersSelected !== true || response.data?.fixedRowsMatch !== true) {
          return bridgeFailure("OC_FILTER_RESULTS_INVALID", {page, total_pages: totalPages});
        }
        const signature = pageSignature(response.data.records);
        if (seenPageSignatures.has(signature)) {
          return bridgeFailure("OC_DUPLICATE_PAGE", {page, total_pages: totalPages});
        }
        seenPageSignatures.add(signature);
        pageCounts.push(response.data.records.length);
        records.push(...response.data.records);
      }
      if (records.length !== totalItems || pageCounts.length !== totalPages) {
        return bridgeFailure("OC_PAGINATION_INCOMPLETE", {
          expected_total_items: totalItems,
          observed_records: records.length,
          expected_total_pages: totalPages,
          observed_pages: pageCounts.length
        });
      }
      await reportBridgeProgress(socket, operationId, "EXTRACTING", "Resolving eligible OC recruitment links with the retained Edge login session.");
      const linkResolution = await resolveOcCandidateLinks(records, state, socket, operationId);
      if (state.cancelled) return {ok: false, status: "CANCELLED", result: {reason: "cancelled"}};
      if (linkResolution.login_required) {
        keepTabOpen = true;
        await chrome.tabs.update(tab.id, {active: true});
        if (typeof tab.windowId === "number") {
          try { await chrome.windows.update(tab.windowId, {focused: true}); } catch (_error) { /* best effort */ }
        }
        return bridgeFailure("LOGIN_REQUIRED", {
          tab_retained: true,
          page_url: sanitizedTabUrl(tab.url),
          link_resolution: linkResolution
        });
      }
      scrubOcTransientApplyUrls(records);
      await reportBridgeProgress(socket, operationId, "VALIDATING", "Persisting the sanitized GiveMeOC snapshot locally.");
      const saved = await persistOcSnapshot(settings, {
        source_url: "https://www.givemeoc.com/",
        captured_at: new Date().toISOString(),
        total_pages: totalPages,
        total_items: totalItems,
        page_counts: pageCounts,
        records,
        link_resolution: linkResolution
      });
      return {
        ok: true,
        status: "SUCCEEDED",
        result: {
          evidence_only: true,
          database_updated: false,
          source: "givemeoc",
          ...saved
        }
      };
    } catch (error) {
      const detail = typeof error?.message === "string"
        ? error.message.replace(/[\r\n\t]+/g, " ").slice(0, 300)
        : "Unknown OC capture error.";
      return bridgeFailure("ACTION_EXECUTION_FAILED", {detail});
    } finally {
      if (!keepTabOpen && typeof tab?.id === "number") {
        try { await chrome.tabs.remove(tab.id); } catch (_error) { /* already closed */ }
      }
    }
  }

  async function executeCommandAuthorizedAction(settings, work, state, socket) {
    if (state.cancelled) {
      return {ok: false, status: "CANCELLED", result: {reason: "cancelled"}};
    }
    if (!work.commandComplete) {
      return bridgeFailure("COMMAND_INVALID", {
        missing_fields: work.commandMissingFields
      });
    }
    const validation = actions.validateActionRequest(work.action, work.selector_key, work.params);
    if (!validation.ok) {
      return bridgeFailure("ACTION_NOT_ALLOWED");
    }
    if (
      work.operation === protocol.commandTypes.CAPTURE_OC_SNAPSHOT &&
      validation.action === protocol.actionTypes.CAPTURE_OC_PAGE
    ) {
      return executeOcSnapshotCapture(settings, work, state, socket, validation);
    }
    if (
      work.operation !== protocol.commandTypes.OBSERVE_APPLICATION_STATUS_PAGE ||
      validation.action !== protocol.actionTypes.OBSERVE_APPLICATION_PAGE
    ) return bridgeFailure("ACTION_NOT_ALLOWED");
    const pageUrl = sanitizedTabUrl(work.page_url);
    const origin = pageUrl ? allowlist.getHttpOrigin(pageUrl) : null;
    if (!pageUrl || !origin || work.origin !== origin) {
      return bridgeFailure("SOURCE_NOT_ALLOWED");
    }
    if (!await chrome.permissions.contains({origins: [`${origin}/*`]})) {
      return bridgeFailure("SOURCE_NOT_ALLOWED");
    }
    let tab = null;
    let keepTabOpen = false;
    const operationId = work.operation_id;
    const requestId = `bridge-${operationId || createActionTicket()}`.slice(0, 128);
    try {
      await reportBridgeProgress(socket, operationId, "NAVIGATING", "Opening the commanded application page.");
      tab = await chrome.tabs.create({url: pageUrl, active: false});
      tab = await waitForTabComplete(tab.id);
      if (typeof tab.url !== "string" || allowlist.getHttpOrigin(tab.url) !== origin) {
        return bridgeFailure("SOURCE_NOT_ALLOWED");
      }
      if (state.cancelled) {
        return {ok: false, status: "CANCELLED", result: {reason: "cancelled"}};
      }
      await reportBridgeProgress(socket, operationId, "EXTRACTING", "Collecting bounded semantic page evidence.");
      await chrome.scripting.executeScript({
        target: {tabId: tab.id, allFrames: true},
        files: ["src/protocol.js", "src/actions.js", "src/application-records.js", "src/content-script.js"]
      });
      const frameResults = await chrome.scripting.executeScript({
        target: {tabId: tab.id, allFrames: true},
        func: () => ({frameUrl: location.href, origin: location.origin})
      });
      const frameContexts = frameResults.map((item) => ({
        frameId: item.frameId,
        frameUrl: sanitizedTabUrl(item.result?.frameUrl) || "",
        origin: typeof item.result?.origin === "string" ? item.result.origin : ""
      })).filter((item) => /^https?:\/\//i.test(item.origin));
      const response = await waitForSemanticObservation(
        tab.id,
        requestId,
        origin,
        validation,
        frameContexts
      );
      if (response?.ok) {
        const structuredEntries = Array.isArray(response?.data?.entries) ? response.data.entries : [];
        let vision = null;
        if (canRequestVision(response, validation, work)) {
          await reportBridgeProgress(socket, operationId, "EXTRACTING", "Capturing the visible page for model analysis.");
          vision = await captureVisiblePageVisionSerial(
            settings,
            tab,
            pageUrl,
            operationId
          );
        }
        await reportBridgeProgress(socket, operationId, "VALIDATING", "Returning sanitized evidence to the model.");
        const applicationIds = normalizeResponseApplicationIds(work.application_ids)
          || (bridgeId(work.application_id) ? [bridgeId(work.application_id)] : null);
        if (!applicationIds) {
          return bridgeFailure("APPLICATION_BINDING_INVALID");
        }
        return {
          ok: true,
          status: "SUCCEEDED",
          result: {
            evidence_only: true,
            database_updated: false,
            application_id: applicationIds[0],
            application_ids: applicationIds,
            page_url: sanitizedTabUrl(tab.url),
            captured_at: response?.data?.capturedAt || new Date().toISOString(),
            page: response?.data?.page || {},
            semantic_nodes: Array.isArray(response?.data?.semanticNodes)
              ? response.data.semanticNodes
              : [],
            application_records: Array.isArray(response?.data?.applicationRecords)
              ? response.data.applicationRecords
              : [],
            entries: structuredEntries,
            diagnostics: response?.data?.diagnostics || {},
            ...(vision ? {vision} : {})
          }
        };
      }
      const errorCode = {
        [protocol.pauseReasons.LOGIN_REQUIRED]: "LOGIN_REQUIRED",
        [protocol.pauseReasons.CAPTCHA_REQUIRED]: "CAPTCHA_REQUIRED",
        [protocol.pauseReasons.STATE_UNCLEAR]: "STATE_UNCLEAR"
      }[response?.pause?.reason] || "STATE_UNCLEAR";

      const waitStage = errorCode === "LOGIN_REQUIRED" || errorCode === "CAPTCHA_REQUIRED"
        ? "WAITING_FOR_LOGIN"
        : "VALIDATING";
      const pause = response?.pause || protocol.createPauseState(
        protocol.pauseReasons.STATE_UNCLEAR,
        "Inspect the opened Edge tab, resolve any login or page overlay, then retry."
      );
      keepTabOpen = validation.params.retain_on_pause === true;
      if (keepTabOpen) {
        await chrome.tabs.update(tab.id, {active: true});
        if (typeof tab.windowId === "number") {
          try { await chrome.windows.update(tab.windowId, {focused: true}); } catch (_error) { /* best effort */ }
        }
      }
      await reportBridgeProgress(
        socket,
        operationId,
        waitStage,
        "User action is required in the opened Edge tab before the review can continue."
      );
      return bridgeFailure(errorCode, {
        pause,
        page_url: sanitizedTabUrl(tab.url),
        tab_retained: keepTabOpen
      });
    } catch (error) {
      const detail = typeof error?.message === "string"
        ? error.message.replace(/[\r\n\t]+/g, " ").slice(0, 300)
        : "Unknown extension execution error.";
      return bridgeFailure("ACTION_EXECUTION_FAILED", {detail});
    } finally {
      if (!keepTabOpen && typeof tab?.id === "number") {
        try { await chrome.tabs.remove(tab.id); } catch (_error) { /* already closed */ }
      }
    }
  }

  async function sendBridgeResult(socket, operationId, outcome) {
    if (outcome.status === "CANCELLED") {
      return;
    }
    sendBridgeMessage({
      protocol_version: protocol.bridgeProtocol.version,
      type: "result",
      operation_id: operationId,
      event_id: bridgeEventId(operationId, "result"),
      status: outcome.status,
      result: outcome.result || {},
      ...(outcome.errorCode ? {error_code: outcome.errorCode} : {})
    }, socket);
  }

  async function handleBridgeDispatch(message, socket) {
    const work = bridgeWorkFromDispatch(message);
    const sequence = message.sequence;
    if (!work || !work.operation_id || !Number.isInteger(sequence) || sequence <= 0) {
      return;
    }
    if (bridgeOperations.has(work.operation_id)) {
      sendBridgeMessage({
        protocol_version: protocol.bridgeProtocol.version,
        type: "ack",
        sequence,
        operation_id: work.operation_id,
        ack_id: `ack-${sequence}`
      }, socket);
      return;
    }
    const state = {cancelled: false};
    bridgeOperations.set(work.operation_id, state);
    if (!sendBridgeMessage({
      protocol_version: protocol.bridgeProtocol.version,
      type: "ack",
      sequence,
      operation_id: work.operation_id,
      ack_id: `ack-${sequence}`
    }, socket)) {
      bridgeOperations.delete(work.operation_id);
      return;
    }
    await recordBridgeStatus("operation_acknowledged", {operationId: work.operation_id});
    void (async () => {
      let outcome;
      try {
        const settings = await getSettings();
        outcome = await executeCommandAuthorizedAction(settings, work, state, socket);
      } catch (_error) {
        outcome = bridgeFailure("ACTION_EXECUTION_FAILED");
      }
      if (!state.cancelled) {
        await sendBridgeResult(socket, work.operation_id, outcome);
        await recordBridgeStatus("operation_completed", {operationId: work.operation_id});
      }
      bridgeOperations.delete(work.operation_id);
    })();
  }

  async function handleBridgeCancel(message, socket) {
    const payload = message.payload && typeof message.payload === "object" ? message.payload : {};
    const operationId = bridgeId(message.operation_id || payload.operation_id);
    const sequence = message.sequence;
    if (!operationId || !Number.isInteger(sequence) || sequence <= 0) {
      return;
    }
    const state = bridgeOperations.get(operationId);
    if (state) {
      state.cancelled = true;
    }
    sendBridgeMessage({
      protocol_version: protocol.bridgeProtocol.version,
      type: "cancel",
      sequence,
      operation_id: operationId,
      ack_id: `cancel-${sequence}`
    }, socket);
    await recordBridgeStatus("operation_cancelled", {operationId});
  }

  async function handleBridgeMessage(event, socket, settings, deviceId) {
    if (socket !== bridgeSocket) {
      return;
    }
    let message;
    try {
      message = typeof event.data === "string" ? JSON.parse(event.data) : null;
    } catch (_error) {
      return;
    }
    if (!message || typeof message !== "object" || Array.isArray(message)) {
      return;
    }
    if (message.type === "challenge") {
      if (!settings.apiToken || typeof message.challenge !== "string") {
        return;
      }
      try {
        const signature = await signBridgeChallenge(settings.apiToken, message.challenge, deviceId);
        sendBridgeMessage({
          protocol_version: protocol.bridgeProtocol.version,
          type: "auth",
          device_id: deviceId,
          challenge: message.challenge,
          signature
        }, socket);
        await recordBridgeStatus("authenticating");
        sendBridgeMessage({protocol_version: protocol.bridgeProtocol.version, type: "heartbeat"}, socket);
      } catch (_error) {
        return;
      }
      return;
    }
    if (message.type === "operation.dispatch") {
      await handleBridgeDispatch(message, socket);
      return;
    }
    if (message.type === "operation.cancel") {
      await handleBridgeCancel(message, socket);
      return;
    }
    if (message.type === "heartbeat") {
      if (message.ack === true) {
        bridgeRetryAttempt = 0;
        await recordBridgeStatus("connected", {deviceId});
      } else {
        sendBridgeMessage({protocol_version: protocol.bridgeProtocol.version, type: "heartbeat", ack: true}, socket);
      }
    }
  }

  function clearBridgeHeartbeat() {
    if (bridgeHeartbeatTimer !== null) {
      clearInterval(bridgeHeartbeatTimer);
      bridgeHeartbeatTimer = null;
    }
  }

  function scheduleBridgeReconnect() {
    if (bridgeRetryTimer !== null) {
      return;
    }
    const delay = Math.min(
      bridgeMaxRetryMs,
      bridgeInitialRetryMs * (2 ** Math.min(bridgeRetryAttempt, 10))
    );
    bridgeRetryAttempt += 1;
    bridgeRetryTimer = setTimeout(() => {
      bridgeRetryTimer = null;
      void connectBrowserBridge();
    }, delay);
    void recordBridgeStatus("retry_scheduled", {retryInMs: delay});
  }

  function stopBridgeSocket() {
    clearBridgeHeartbeat();
    const socket = bridgeSocket;
    bridgeSocket = null;
    if (socket) {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      try { socket.close(); } catch (_error) { /* already closed */ }
    }
  }

  async function connectBrowserBridge() {
    if (bridgeSocket && (bridgeSocket.readyState === 0 || bridgeSocketOpen())) {
      const current = await readBridgeStatus();
      if (bridgeSocketOpen() && current?.state === "connected") {
        return current;
      }
      const outcome = await waitForBridgeAuthentication(bridgeSocket);
      return outcome || recordBridgeStatus("connection_error", {reason: "authentication_timeout"});
    }
    if (bridgeConnectPromise) {
      return bridgeConnectPromise;
    }
    bridgeConnectPromise = (async () => {
      const settings = await getSettings();
      const bridgeUrl = config.browserBridgeUrl(settings.apiBaseUrl);
      if (!bridgeUrl || !settings.apiToken) {
        return recordBridgeStatus("config_missing");
      }
      const deviceId = await getBridgeDeviceId();
      await recordBridgeStatus("connecting", {deviceId});
      let socket;
      try {
        socket = new WebSocket(bridgeUrl);
      } catch (_error) {
        await recordBridgeStatus("connection_error");
        scheduleBridgeReconnect();
        return recordBridgeStatus("connection_error");
      }
      bridgeSocket = socket;
      socket.onopen = () => {
        if (socket !== bridgeSocket) return;
        clearBridgeHeartbeat();
        bridgeHeartbeatTimer = setInterval(() => {
          sendBridgeMessage({protocol_version: protocol.bridgeProtocol.version, type: "heartbeat"}, socket);
        }, bridgeHeartbeatMs);
        void recordBridgeStatus("authenticating", {deviceId});
      };
      socket.onmessage = (event) => {
        void handleBridgeMessage(event, socket, settings, deviceId);
      };
      socket.onerror = () => {
        if (socket === bridgeSocket) {
          void recordBridgeStatus("connection_error");
        }
      };
      socket.onclose = () => {
        if (socket !== bridgeSocket) return;
        bridgeSocket = null;
        clearBridgeHeartbeat();
        void recordBridgeStatus("disconnected");
        scheduleBridgeReconnect();
      };
      const outcome = await waitForBridgeAuthentication(socket);
      if (outcome) {
        return outcome;
      }
      if (socket === bridgeSocket) {
        try { socket.close(); } catch (_error) { /* already closed */ }
      }
      return recordBridgeStatus("connection_error", {
        deviceId,
        reason: "authentication_timeout"
      });
    })().catch(async () => {
      await recordBridgeStatus("connection_error");
      scheduleBridgeReconnect();
      return recordBridgeStatus("connection_error");
    }).finally(() => {
      bridgeConnectPromise = null;
    });
    return bridgeConnectPromise;
  }

  async function restartBrowserBridge() {
    if (bridgeRetryTimer !== null) {
      clearTimeout(bridgeRetryTimer);
      bridgeRetryTimer = null;
    }
    bridgeRetryAttempt = 0;
    stopBridgeSocket();
    return connectBrowserBridge();
  }

  function sanitizedTabUrl(rawUrl) {
    try {
      const url = new URL(rawUrl);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
        return null;
      }
      url.search = "";
      const fragment = url.hash.slice(1).split("?", 1)[0];
      url.hash = (
        (fragment.startsWith("/") || fragment.startsWith("!/")) &&
        fragment.length <= 1024 && !/[\s#]/.test(fragment)
      ) ? fragment : "";
      return url.toString();
    } catch (_error) {
      return null;
    }
  }

  async function submitDomObservation(message) {
    const requestId = typeof message.requestId === "string" ? message.requestId : "";
    if (message.userGesture !== true) {
      return observationError(requestId, "USER_GESTURE_REQUIRED");
    }

    const snapshot = message.snapshot;
    if (!snapshot || typeof snapshot !== "object") {
      return observationError(requestId, "SNAPSHOT_REQUIRED");
    }

    const [tab] = await chrome.tabs.query({active: true, lastFocusedWindow: true});
    if (!tab || typeof tab.id !== "number" || typeof tab.url !== "string") {
      return observationError(requestId, "CURRENT_TAB_UNAVAILABLE");
    }

    const settings = await getSettings();
    if (!await hasPageAccess(tab.url)) {
      return observationError(requestId, "SOURCE_NOT_ALLOWED");
    }

    const observationUrl = sanitizedTabUrl(tab.url);
    if (!observationUrl) {
      return observationError(requestId, "SOURCE_NOT_ALLOWED");
    }

    const apiBaseUrl = config.normalizeApiBaseUrl(settings.apiBaseUrl);
    if (!apiBaseUrl) {
      return observationError(requestId, "LOCAL_API_ONLY");
    }

    const headers = {"Content-Type": "application/json"};
    if (settings.apiToken) {
      headers.Authorization = `Bearer ${settings.apiToken}`;
    }

    const response = await fetch(`${apiBaseUrl}/api/browser/observations`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        url: observationUrl,
        allowed_origins: [allowlist.getHttpOrigin(tab.url)].filter(Boolean),
        title: typeof snapshot.title === "string" ? snapshot.title : "",
        page_text: typeof snapshot.text === "string" ? snapshot.text : "",
        links: Array.isArray(snapshot.links) ? snapshot.links : [],
        network_requests: Array.isArray(snapshot.networkRequests)
          ? snapshot.networkRequests
          : []
      })
    });
    let body = null;
    try {
      body = await response.json();
    } catch (_error) {
      body = null;
    }
    if (!response.ok) {
      return observationError(requestId, body?.detail || `API_HTTP_${response.status}`);
    }
    return {
      protocolVersion: protocol.version,
      type: protocol.messageTypes.OBSERVATION_SUBMITTED,
      requestId,
      ok: true,
      data: body
    };
  }

  function boundedText(value, maxLength) {
    return typeof value === "string" ? value.trim().slice(0, maxLength) : "";
  }

  async function submitApplicationCapture(message) {
    const requestId = boundedText(message.requestId, 128);
    const allowedKeys = [
      "protocolVersion",
      "type",
      "requestId",
      "userGesture",
      "snapshot",
      "jobId",
      "note"
    ];
    if (message.protocolVersion !== protocol.version) {
      return applicationCaptureResponse(requestId, false, undefined, "PROTOCOL_VERSION_UNSUPPORTED");
    }
    if (message.userGesture !== true) {
      return applicationCaptureResponse(requestId, false, undefined, "USER_GESTURE_REQUIRED");
    }
    if (Object.keys(message).some((key) => !allowedKeys.includes(key))) {
      return applicationCaptureResponse(requestId, false, undefined, "CAPTURE_MESSAGE_INVALID");
    }
    const snapshot = message.snapshot;
    if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) {
      return applicationCaptureResponse(requestId, false, undefined, "SNAPSHOT_REQUIRED");
    }

    const [tab] = await chrome.tabs.query({active: true, lastFocusedWindow: true});
    if (!tab || typeof tab.id !== "number" || typeof tab.url !== "string") {
      return applicationCaptureResponse(requestId, false, undefined, "CURRENT_TAB_UNAVAILABLE");
    }
    const settings = await getSettings();
    if (!await hasPageAccess(tab.url)) {
      return applicationCaptureResponse(requestId, false, undefined, "SOURCE_NOT_ALLOWED");
    }
    const pageUrl = sanitizedTabUrl(tab.url);
    const apiBaseUrl = config.normalizeApiBaseUrl(settings.apiBaseUrl);
    if (!pageUrl || !apiBaseUrl) {
      return applicationCaptureResponse(requestId, false, undefined, "LOCAL_API_ONLY");
    }

    const headers = {"Content-Type": "application/json"};
    if (settings.apiToken) {
      headers.Authorization = `Bearer ${settings.apiToken}`;
    }
    const response = await fetch(`${apiBaseUrl}/api/browser/application-captures`, {
      method: "POST",
      headers,
      credentials: "omit",
      body: JSON.stringify({
        request_id: requestId,
        url: pageUrl,
        title: boundedText(snapshot.title, 500),
        page_text: boundedText(snapshot.text, 20000),
        job_id: boundedText(message.jobId, 200) || null,
        note: boundedText(message.note, 1000) || null
      })
    });
    let body = null;
    try {
      body = await response.json();
    } catch (_error) {
      body = null;
    }
    if (!response.ok) {
      return applicationCaptureResponse(
        requestId,
        false,
        undefined,
        localApiErrorCode(body, response.status)
      );
    }
    const status = typeof body?.status === "string" ? body.status : "";
    if (!["approval_required", "ambiguous", "not_found", "already_recorded"].includes(status)) {
      return applicationCaptureResponse(requestId, false, undefined, "CAPTURE_RESPONSE_INVALID");
    }
    return applicationCaptureResponse(requestId, true, {
      status,
      message: boundedText(body.message, 1000),
      approval: body.approval || null,
      result: body.result || null
    });
  }

  async function authorizeCurrentTab(message) {
    const requestId = typeof message.requestId === "string" ? message.requestId : "";
    if (message.userGesture !== true) {
      return errorResponse(requestId, "USER_GESTURE_REQUIRED");
    }

    const [tab] = await chrome.tabs.query({active: true, lastFocusedWindow: true});
    if (!tab || typeof tab.id !== "number" || typeof tab.url !== "string") {
      return errorResponse(requestId, "CURRENT_TAB_UNAVAILABLE");
    }

    if (!await hasPageAccess(tab.url)) {
      return errorResponse(requestId, "SOURCE_NOT_ALLOWED");
    }

    const authorizedOrigin = allowlist.getHttpOrigin(tab.url);
    await chrome.scripting.executeScript({
      target: {tabId: tab.id},
      files: ["src/protocol.js", "src/actions.js", "src/content-script.js"]
    });

    const response = await chrome.tabs.sendMessage(tab.id, {
      protocolVersion: protocol.version,
      type: protocol.messageTypes.READ_SANITIZED_DOM,
      requestId,
      authorizedOrigin,
      authorization: "user_click_current_tab"
    });

    return response || errorResponse(requestId, "CONTENT_SCRIPT_NO_RESPONSE");
  }

  async function getConfig(message) {
    const requestId = typeof message.requestId === "string" ? message.requestId : "";
    return configResponse(protocol.messageTypes.CONFIG_GET, requestId, await getSettings());
  }

  async function setConfig(message) {
    const requestId = typeof message.requestId === "string" ? message.requestId : "";
    const candidate = message.settings;
    if (!candidate || typeof candidate !== "object") {
      return configError(protocol.messageTypes.CONFIG_SET, requestId, "CONFIG_REQUIRED");
    }

    if (!config.normalizeApiBaseUrl(candidate.apiBaseUrl)) {
      return configError(protocol.messageTypes.CONFIG_SET, requestId, "LOCAL_API_ONLY");
    }

    const settings = config.normalizeSettings(candidate);
    await chrome.storage.local.set(settings);
    return {
      ...configResponse(protocol.messageTypes.CONFIG_SET, requestId, settings),
      bridge: await restartBrowserBridge()
    };
  }

  async function testConfig(message) {
    const requestId = typeof message.requestId === "string" ? message.requestId : "";
    return {
      protocolVersion: protocol.version,
      type: protocol.messageTypes.CONFIG_TEST,
      requestId,
      ok: true,
      bridge: await connectBrowserBridge()
    };
  }

  chrome.runtime.onInstalled.addListener(() => {
    void connectBrowserBridge();
  });
  chrome.runtime.onStartup.addListener(() => {
    void connectBrowserBridge();
  });

  void connectBrowserBridge();

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (sender?.id !== chrome.runtime.id) {
      return undefined;
    }

    if (message?.type === protocol.messageTypes.AUTHORIZE_CURRENT_TAB) {
      if (sender.tab) {
        sendResponse(errorResponse(message.requestId, "EXTENSION_PAGE_REQUIRED"));
        return false;
      }

      authorizeCurrentTab(message)
        .then(sendResponse)
        .catch(() => sendResponse(errorResponse(message.requestId, "AUTHORIZATION_FAILED")));
      return true;
    }

    if (message?.type === protocol.messageTypes.CONFIG_GET) {
      if (!isTrustedExtensionPage(sender)) {
        sendResponse(configError(protocol.messageTypes.CONFIG_GET, message.requestId, "EXTENSION_PAGE_REQUIRED"));
        return false;
      }

      getConfig(message)
        .then(sendResponse)
        .catch(() => sendResponse(configError(protocol.messageTypes.CONFIG_GET, message.requestId, "CONFIG_READ_FAILED")));
      return true;
    }

    if (message?.type === protocol.messageTypes.CONFIG_SET) {
      if (!isTrustedExtensionPage(sender)) {
        sendResponse(configError(protocol.messageTypes.CONFIG_SET, message.requestId, "EXTENSION_PAGE_REQUIRED"));
        return false;
      }

      setConfig(message)
        .then(sendResponse)
        .catch(() => sendResponse(configError(protocol.messageTypes.CONFIG_SET, message.requestId, "CONFIG_WRITE_FAILED")));
      return true;
    }

    if (message?.type === protocol.messageTypes.CONFIG_TEST) {
      if (!isTrustedExtensionPage(sender)) {
        sendResponse(configError(protocol.messageTypes.CONFIG_TEST, message.requestId, "EXTENSION_PAGE_REQUIRED"));
        return false;
      }

      testConfig(message)
        .then(sendResponse)
        .catch(() => sendResponse(configError(protocol.messageTypes.CONFIG_TEST, message.requestId, "CONFIG_TEST_FAILED")));
      return true;
    }

    if (message?.type === protocol.messageTypes.SUBMIT_DOM_OBSERVATION) {
      if (sender.tab) {
        sendResponse(observationError(message.requestId, "EXTENSION_PAGE_REQUIRED"));
        return false;
      }

      submitDomObservation(message)
        .then(sendResponse)
        .catch(() => sendResponse(observationError(message.requestId, "API_REQUEST_FAILED")));
      return true;
    }

    if (message?.type === protocol.messageTypes.SUBMIT_APPLICATION_CAPTURE) {
      if (sender.tab) {
        sendResponse(applicationCaptureResponse(
          message.requestId,
          false,
          undefined,
          "EXTENSION_PAGE_REQUIRED"
        ));
        return false;
      }
      submitApplicationCapture(message)
        .then(sendResponse)
        .catch(() => sendResponse(applicationCaptureResponse(
          message.requestId,
          false,
          undefined,
          "CAPTURE_REQUEST_FAILED"
        )));
      return true;
    }

    return undefined;
  });
})();
