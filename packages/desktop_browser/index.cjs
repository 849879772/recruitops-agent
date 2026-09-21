"use strict";

// Main-process module. Only buildObservationScript's return value enters a page.
const fs = require("node:fs");
const path = require("node:path");
const {createHash, randomUUID} = require("node:crypto");

const RESOURCE_HASHES = Object.freeze({
  "protocol.js": "31d659d16741a19250ca5464ef9d9e6076546647d0859573516444cb4605b33e",
  "actions.js": "2fe91321c38236850c747fbfa812a8e3251d4c992beebf6be5bf40243d239386",
  "application-records.js": "b919e120359b87a99f04353141168e3b88c830fad352e63562e61893b3eb7c67",
  "content-script.js": "d4e75c50293fcbb7d9a4d4e1c103b11ec6f3a4915e310b94fce476966d22a558"
});

function loadObservationResources(directory) {
  const root = directory || (fs.existsSync(path.join(__dirname, "resources"))
    ? path.join(__dirname, "resources") : path.resolve(__dirname, "../../extension/src"));
  return Object.fromEntries(Object.entries(RESOURCE_HASHES).map(([name, hash]) => {
    const source = fs.readFileSync(path.join(root, name), "utf8").replace(/\r\n/g, "\n");
    if (createHash("sha256").update(source).digest("hex") !== hash) {
      throw new Error(`Observation resource integrity mismatch: ${name}`);
    }
    return [name, source];
  }));
}

function packageObservationResources(destination) {
  // Packaging is explicit, never triggered by observation or by webpage input.
  const resources = loadObservationResources();
  fs.mkdirSync(destination, {recursive: true});
  for (const [name, source] of Object.entries(resources)) {
    fs.writeFileSync(path.join(destination, name), source, {encoding: "utf8", flag: "wx"});
  }
}

function object(value, allowed, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)
      || Object.keys(value).some(key => !allowed.includes(key))) {
    throw new TypeError(`Invalid ${label}`);
  }
  return value;
}

function string(value, limit, label) {
  if (typeof value !== "string" || value.length > limit) throw new TypeError(`Invalid ${label}`);
  return value;
}

function identifier(value) {
  if (typeof value !== "string" || !/^[a-zA-Z0-9_.:-]{1,128}$/.test(value)) {
    throw new TypeError("Invalid operation/application identifier");
  }
  return value;
}

function httpUrl(value) {
  string(value, 2048, "page URL");
  const url = new URL(value);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) {
    throw new TypeError("Expected credential-free HTTP(S) URL");
  }
  url.search = "";
  const fragment = url.hash.slice(1).split("?", 1)[0];
  url.hash = /^(?:\/|!\/)[^\s#]{0,1022}$/.test(fragment) ? fragment : "";
  return url;
}

function validateParams(params) {
  object(params, ["operation_id", "page_url"], "observation parameters");
  return {operation_id: identifier(params.operation_id), page_url: httpUrl(params.page_url)};
}

function createObservationAdapter(resourceDirectory) {
  const sources = loadObservationResources(resourceDirectory);
  const marker = "  function dispatchControlEvents(element) {";
  const content = sources["content-script.js"];
  if (content.split(marker).length !== 2) throw new Error("Observation extraction boundary changed");
  const readOnlyPrefix = content.slice(0, content.indexOf(marker));

  function observationScript(params, child = false) {
    const validated = validateParams(params);
    const input = JSON.stringify({requestId: validated.operation_id, pageUrl: validated.page_url.href});
    return `(() => {
      "use strict";
      const input = ${input};
      const globalThis = {NodeFilter: window.NodeFilter,
        getComputedStyle: window.getComputedStyle, performance: window.performance};
      ${sources["protocol.js"]}
      ${sources["actions.js"]}
      ${sources["application-records.js"]}
      return ${readOnlyPrefix}
        if (${!child} && window !== window.top) return actionError(input.requestId, "FRAME_NOT_ALLOWED");
        if (sanitizedHttpUrl(location.href) !== input.pageUrl) return actionError(input.requestId, "SOURCE_NOT_ALLOWED");
        if (${child}) {
          let current = window;
          while (current !== window.top) {
            const owner = current.frameElement;
            if (!owner || !owner.getClientRects().length) {
              return actionError(input.requestId, "FRAME_NOT_ALLOWED");
            }
            for (let ancestor = owner; ancestor; ancestor = ancestor.parentElement) {
              const style = getComputedStyle(ancestor);
              if (ancestor.hidden || ancestor.inert || ancestor.getAttribute("aria-hidden") === "true" ||
                  style.display === "none" || style.visibility !== "visible" || Number(style.opacity) === 0) {
                return actionError(input.requestId, "FRAME_NOT_ALLOWED");
              }
            }
            current = current.parent;
          }
        }
        const reason = pagePauseReason();
        if (reason || !document.body) return actionPause(input.requestId, reason || "state_unclear");
        const validation = actions.validateActionRequest("observe_application_page", "application_page", {});
        const response = createSemanticObservation(input.requestId, validation);
        response.data.page.page_url = sanitizedHttpUrl(location.href);
        response.data.diagnostics.iframeCount = document.querySelectorAll("iframe, frame").length;
        response.data.diagnostics.frameScope = ${JSON.stringify(child ? "single_frame" : "top_only")};
        const lead = (response.data.page.text || "").slice(0, 2500);
        response.data.diagnostics.loginPromptVisible = /(?:^|\\s)(?:(?:欢迎|请)?登录(?:\\s*[/|]\\s*注册)?|注册\\s*[/|]\\s*登录|sign in|log in)(?:\\s|$)/i.test(lead);
        if (!response.data.applicationRecords.length &&
            /(?:^|\\/)(?:login|signin|sign-in)(?:[./]|$)/i.test(location.pathname) &&
            /登录|注册|sign in|log in/i.test(lead)) return actionPause(input.requestId, protocol.pauseReasons.LOGIN_REQUIRED);
        return response;
      })();
    })()`;
  }
  const buildObservationScript = params => observationScript(params);
  const buildFrameObservationScript = params => observationScript(params, true);
  return Object.freeze({buildObservationScript, buildFrameObservationScript, normalizeFrameObservations, normalizeObservation,
    buildManualCaptureScript: buildObservationScript, normalizeManualCapture,
    getFormFillCapability, buildFormPreviewScript, buildFormFillScript,
    normalizeFormPreview, normalizeFormFill});
}

function buildObservationScript(params) {
  return createObservationAdapter().buildObservationScript(params);
}

function buildFrameObservationScript(params) {
  return createObservationAdapter().buildFrameObservationScript(params);
}

// Frame identities come from the main process, never from DOM attributes or page JS.
function normalizeFrameObservations(samples, context, skippedFrameCount = 0) {
  const topUrl = httpUrl(context.page_url);
  const ids = new Set();
  const frames = array(samples, 32, sample => {
    object(sample, ["frameId", "frameUrl", "raw", "unavailable"], "frame sample");
    if (!Number.isSafeInteger(sample.frameId) || sample.frameId < 0 || ids.has(sample.frameId)) throw new TypeError("Invalid frame identity");
    ids.add(sample.frameId);
    const url = httpUrl(sample.frameUrl);
    // Same-origin is the only review frame scope currently authorized by dispatch.
    if (url.origin !== topUrl.origin) throw new TypeError("Frame outside authorized origin scope");
    if (sample.unavailable !== undefined && (sample.unavailable !== true || sample.frameId === 0 || sample.raw !== undefined)) {
      throw new TypeError("Invalid unavailable frame");
    }
    const raw = sample.unavailable ? {protocolVersion: 3, type: "extension.pause_state", requestId: context.operation_id,
      ok: false, pause: {reason: "state_unclear"}} : sample.raw;
    const observation = normalizeObservation(raw, {...context, page_url: sample.frameUrl});
    if (sample.unavailable) observation.error_code = "FRAME_EVIDENCE_UNAVAILABLE";
    return {frameId: sample.frameId, frameUrl: url.href, observation};
  });
  const top = frames.find(frame => frame.frameId === 0);
  if (!top || top.frameUrl !== topUrl.href) throw new TypeError("Missing top frame binding");
  if (!Number.isSafeInteger(skippedFrameCount) || skippedFrameCount < 0) throw new TypeError("Invalid skipped frame count");
  const diagnostics = {...top.observation.result.diagnostics, frameScope: "authorized_frames",
    frameCount: frames.length + skippedFrameCount, successfulFrameCount: frames.filter(f => f.observation.result.page).length,
    skippedFrameCount, unavailableFrameCount: frames.filter(f => f.observation.error_code === "FRAME_EVIDENCE_UNAVAILABLE").length,
    authorizedOrigin: topUrl.origin,
    frames: frames.map(f => ({frameId: f.frameId, frameUrl: f.frameUrl,
      recordCount: f.observation.result.application_records?.length || 0,
      errorCode: f.observation.error_code || "", visibleTextLength: f.observation.result.diagnostics?.visibleTextLength || 0}))};
  const finish = (observation, result) => {
    const output = {...observation, result: {...result, diagnostics}};
    if (Buffer.byteLength(JSON.stringify(output), "utf8") > 262144) throw new TypeError("Observation exceeds bridge payload limit");
    return output;
  };
  // A blocked top document cannot be bypassed by evidence behind its gate.
  if (!top.observation.result.page) return finish(top.observation, top.observation.result);
  const score = f => (f.observation.result.entries?.length || 0) * 1000000
    + (f.observation.result.application_records?.length || 0) * 100000
    + (f.observation.result.semantic_nodes?.length || 0) * 120;
  const ranked = frames.slice().sort((a, b) => score(b) - score(a) || Number(a.frameId !== 0) - Number(b.frameId !== 0));
  const collect = (key, limit) => ranked.flatMap(f => (f.observation.result[key] || [])
    .map(row => ({...row, frameId: f.frameId, frameUrl: f.frameUrl}))).slice(0, limit);
  const records = collect("application_records", 100);
  // Empty helper frames cannot mask useful records or a real auth challenge.
  const pause = ["CAPTCHA_REQUIRED", "LOGIN_REQUIRED"].map(code => frames.find(f => f.observation.error_code === code)).find(Boolean);
  if (!records.length && pause) return finish({...top.observation, status: "STATE_UNCLEAR", error_code: pause.observation.error_code},
    {...top.observation.result, requires_user_action: true});
  const result = {...top.observation.result, application_records: records,
    semantic_nodes: collect("semantic_nodes", 240), entries: collect("entries", 100)};
  const observation = {...top.observation};
  if (records.length) { observation.status = "SUCCEEDED"; delete observation.error_code; }
  return finish(observation, result);
}

function buildManualCaptureScript(params) {
  return createObservationAdapter().buildManualCaptureScript(params);
}

function getFormFillCapability() {
  return Object.freeze({supported: true, code: "LIMITED_NATIVE_V1", enabled_by_default: false,
    preview: true, fill: true, file_selection: "user_only", final_submission: "user_only",
    reason: "Explicit native text/textarea/single-select mappings only; not former plugin parity."});
}

function formParams(params, fill = false) {
  object(params, ["operation_id", "page_url", "fields",
    ...(fill ? ["preview_id", "writes_enabled", "user_confirmed"] : [])], "form parameters");
  const operation_id = identifier(params.operation_id);
  httpUrl(params.page_url);
  const page_url = new URL(params.page_url).href;
  const mappings = array(params.fields, 25, field => {
    object(field, ["field_id", "target", "value"], "field mapping");
    object(field.target, ["id", "name"], "field target");
    const keys = Object.keys(field.target);
    if (keys.length !== 1) throw new TypeError("Expected exactly one id or name target");
    const targetValue = string(field.target[keys[0]], 128, "field target");
    if (!targetValue.trim() || /[\x00-\x1f\x7f]/.test(targetValue)) throw new TypeError("Invalid field target");
    const value = string(field.value, 2000, "field value");
    if (/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value)) throw new TypeError("Invalid field value");
    return {field_id: identifier(field.field_id), target: {[keys[0]]: targetValue}, value};
  });
  if (!mappings.length || new Set(mappings.map(f => f.field_id)).size !== mappings.length) {
    throw new TypeError("Expected nonempty unique field mappings");
  }
  if (Buffer.byteLength(JSON.stringify(mappings), "utf8") > 64 * 1024) throw new TypeError("Form plan too large");
  if (fill) {
    identifier(params.preview_id);
    if (params.writes_enabled !== true || params.user_confirmed !== true) {
      const error = new Error("Explicit user confirmation and writes opt-in required");
      error.code = "FORM_FILL_DISABLED";
      throw error;
    }
  }
  return {operation_id, page_url, fields: mappings};
}

function buildFormPreviewScript(params) {
  const plan = formParams(params);
  return `(${runNativeForm.toString()})(${JSON.stringify({mode: "preview", plan, preview_id: randomUUID()})})`;
}

function buildFormFillScript(params) {
  const plan = formParams(params, true);
  return `(${runNativeForm.toString()})(${JSON.stringify({mode: "fill", plan, preview_id: params.preview_id})})`;
}

// Fixed DOM program: no caller code/selectors/callbacks. Run ONLY in the shell's
// persistent dedicated isolated world; its receipt must not live in the page world.
function runNativeForm(input) {
  "use strict";
  const {plan, mode, preview_id} = input;
  const url = new URL(plan.page_url);
  url.search = "";
  const fragment = url.hash.slice(1).split("?", 1)[0];
  url.hash = /^(?:\/|!\/)[^\s#]{0,1022}$/.test(fragment) ? fragment : "";
  const base = {protocol_version: 1, kind: mode === "preview" ? "form_preview" : "form_fill",
    operation_id: plan.operation_id, preview_id, page_url: url.href,
    database_updated: false, submission_attempted: false, frame_scope: "top_only",
    iframe_count: document.querySelectorAll("iframe,frame").length};
  const response = (status, code, fields = []) => ({...base, status, code, fields});
  if (window !== window.top) return response("BLOCKED", "FRAME_NOT_ALLOWED");
  if (location.href !== plan.page_url) return response("BLOCKED", "SOURCE_NOT_ALLOWED");
  const key = "__recruitopsDesktopNativeFormV1";
  if (!Object.prototype.hasOwnProperty.call(globalThis, key)) {
    Object.defineProperty(globalThis, key, {value: {current: null}});
  }
  const state = globalThis[key];
  const previous = state.current;
  // Every fill attempt consumes the receipt, even if its preflight is rejected.
  state.current = null;
  function visible(element) {
    if (!element.isConnected || !element.getClientRects().length) return false;
    for (let parent = element; parent; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (parent.hidden || parent.inert || parent.getAttribute("aria-hidden") === "true"
          || style.display === "none" || style.visibility !== "visible" || Number(style.opacity) === 0) return false;
    }
    return true;
  }
  function gate() {
    if (location.href !== plan.page_url) return "SOURCE_NOT_ALLOWED";
    if (!document.body) return "STATE_UNCLEAR";
    const gates = [
      ["CAPTCHA_REQUIRED", "iframe[src*='captcha'],[data-captcha],[data-sitekey],[data-recruitops-auth='captcha']"],
      ["LOGIN_REQUIRED", "input[type='password'],input[autocomplete='username'],[data-recruitops-auth='login']"],
      ["STATE_UNCLEAR", "[aria-modal='true'],[role='dialog'],[data-recruitops-blocking-overlay]"]
    ];
    for (const [code, selector] of gates) {
      if ([...document.querySelectorAll(selector)].some(visible)) return code;
    }
    return null;
  }
  const paused = gate();
  if (paused) return response("BLOCKED", paused);
  if (mode === "fill" && (!previous || previous.preview_id !== preview_id
      || previous.plan !== JSON.stringify(plan) || Date.now() - previous.created > 120000
      || previous.document !== document || previous.url !== location.href)) {
    return response("BLOCKED", "PREVIEW_STALE");
  }
  const sensitive = /password|passwd|pwd|otp|captcha|one.?time|verification|verify|security.?code|auth.?code|sms.?code|email.?code|pin.?code|pass.?code|2fa|mfa|token|login|sign.?in|username|credit.?card|cc-number|cc-csc|(?:^|[\W_])(?:pin|cvv|cvc|ssn|code)(?:$|[\W_])|\u5bc6\u7801|\u9a8c\u8bc1\u7801|\u6821\u9a8c\u7801|\u52a8\u6001\u7801|\u767b\u5f55/i;
  function descriptor(element) {
    const labelled = (element.getAttribute("aria-labelledby") || "").split(/\s+/)
      .slice(0, 10).map(id => document.getElementById(id)?.textContent || "");
    return [element.id, element.name, element.getAttribute("autocomplete"),
      element.getAttribute("placeholder"), element.getAttribute("aria-label"),
      ...labelled, ...Array.from(element.labels || []).map(label => label.textContent),
      element.form?.getAttribute("action"), element.form?.id, element.form?.name].join(" ");
  }
  function signature(element) {
    return JSON.stringify([element.outerHTML, element.value, descriptor(element),
      element.form?.getAttribute("action"), element.form?.getAttribute("method")]);
  }
  function inspect(field) {
    const [attribute, value] = Object.entries(field.target)[0];
    const controls = [...document.querySelectorAll("input,textarea,select,button,[contenteditable],[role='combobox']")];
    const matches = controls.filter(element => element.getAttribute(attribute) === value);
    const report = {field_id: field.field_id, status: "READY", match_count: matches.length};
    const reject = status => ({report: {...report, status}, element: null});
    if (!matches.length) return reject("NOT_FOUND");
    if (matches.length !== 1) return reject("AMBIGUOUS");
    const element = matches[0];
    if (sensitive.test(descriptor(element))) return reject("SENSITIVE");
    if (!(element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement)
        || element.isContentEditable || element.hasAttribute("role")
        || (element instanceof HTMLInputElement && !["text", "email", "tel", "url"].includes(element.type))
        || (element instanceof HTMLSelectElement && element.multiple)) return reject("UNSUPPORTED");
    if (!visible(element) || element.matches(":disabled") || element.readOnly
        || element.getAttribute("aria-disabled") === "true") return reject("NOT_EDITABLE");
    if (element instanceof HTMLInputElement && /[\r\n]/.test(field.value)) return reject("UNSUPPORTED");
    if (element.maxLength >= 0 && field.value.length > element.maxLength) return reject("VALUE_TOO_LONG");
    if (element instanceof HTMLSelectElement) {
      const options = [...element.options].filter(option => option.value === field.value);
      if (options.length !== 1 || options[0].disabled || options[0].parentElement?.disabled) return reject("OPTION_NOT_FOUND");
    }
    return {report, element, signature: signature(element), description: descriptor(element), form: element.form};
  }
  const inspected = plan.fields.map(inspect);
  const seen = new Set();
  for (const item of inspected) {
    if (item.element && seen.has(item.element)) item.report.status = "DUPLICATE_TARGET";
    seen.add(item.element);
  }
  const reports = inspected.map(item => item.report);
  if (reports.some(report => report.status !== "READY")) return response("UNSUPPORTED", "FIELDS_NOT_READY", reports);
  if (mode === "preview") {
    state.current = {preview_id, plan: JSON.stringify(plan), url: location.href,
      document, created: Date.now(), inspected};
    return response("READY", "PREVIEW_READY", reports);
  }
  if (inspected.some((item, index) => item.element !== previous.inspected[index].element
      || item.signature !== previous.inspected[index].signature)) return response("BLOCKED", "PREVIEW_STALE", reports);
  let touched = 0;
  const results = reports.map(report => ({...report, status: "NOT_ATTEMPTED"}));
  // Native input/change events can run website handlers. Recheck remaining
  // elements after every write; never roll back across unknown site side effects.
  for (let index = 0; index < inspected.length; index++) {
    const field = plan.fields[index];
    const current = inspect(field);
    const item = inspected[index];
    if (gate() || current.report.status !== "READY" || current.element !== item.element
        || current.element.form !== item.form || current.signature !== item.signature) {
      return response(touched ? "PARTIAL" : "BLOCKED", "DOM_CHANGED", results);
    }
    if (item.element.value === field.value) {
      results[index].status = "UNCHANGED";
      continue;
    }
    const prototype = item.element instanceof HTMLSelectElement ? HTMLSelectElement.prototype
      : item.element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    try {
      Object.getOwnPropertyDescriptor(prototype, "value").set.call(item.element, field.value);
      touched++;
      item.element.dispatchEvent(new Event("input", {bubbles: true}));
      const afterInput = inspect(field);
      if (afterInput.report.status !== "READY" || afterInput.element !== item.element
          || afterInput.description !== item.description || afterInput.form !== item.form || gate()) {
        results[index].status = "FAILED";
        return response("PARTIAL", "DOM_CHANGED", results);
      }
      item.element.dispatchEvent(new Event("change", {bubbles: true}));
      const afterChange = inspect(field);
      if (afterChange.report.status !== "READY" || afterChange.element !== item.element
          || afterChange.description !== item.description || afterChange.form !== item.form
          || item.element.value !== field.value || gate()) {
        results[index].status = "FAILED";
        return response("PARTIAL", "VALUE_REJECTED", results);
      }
      results[index].status = "FILLED";
    } catch (_) {
      results[index].status = "FAILED";
      return response("PARTIAL", "VALUE_REJECTED", results);
    }
  }
  for (let index = 0; index < inspected.length; index++) {
    const current = inspect(plan.fields[index]);
    if (current.report.status !== "READY" || current.element !== inspected[index].element
        || current.description !== inspected[index].description || current.form !== inspected[index].form
        || current.element.value !== plan.fields[index].value || gate()) {
      results[index].status = "FAILED";
      return response("PARTIAL", "VALUE_REJECTED", results);
    }
  }
  return response(touched ? "FILLED" : "UNCHANGED", "FILL_COMPLETE", results);
}

const FORM_FIELD_STATUSES = ["READY", "NOT_FOUND", "AMBIGUOUS", "UNSUPPORTED", "SENSITIVE",
  "NOT_EDITABLE", "OPTION_NOT_FOUND", "VALUE_TOO_LONG", "DUPLICATE_TARGET", "NOT_ATTEMPTED", "FILLED", "UNCHANGED", "FAILED"];

function normalizeFormResult(raw, params, fill) {
  const plan = formParams(params, fill);
  object(raw, ["protocol_version", "kind", "operation_id", "preview_id", "page_url", "database_updated",
    "submission_attempted", "frame_scope", "iframe_count", "status", "code", "fields"], "form result");
  if (Buffer.byteLength(JSON.stringify(raw), "utf8") > 64 * 1024) throw new TypeError("Form result too large");
  if (raw.protocol_version !== 1 || raw.kind !== (fill ? "form_fill" : "form_preview")
      || raw.operation_id !== plan.operation_id || raw.page_url !== httpUrl(plan.page_url).href
      || raw.database_updated !== false || raw.submission_attempted !== false || raw.frame_scope !== "top_only"
      || !Number.isInteger(raw.iframe_count) || raw.iframe_count < 0 || raw.iframe_count > 10000) {
    throw new TypeError("Invalid form result identity or boundary");
  }
  identifier(raw.preview_id);
  if (fill && raw.preview_id !== params.preview_id) throw new TypeError("Preview identity mismatch");
  const statuses = fill ? ["FILLED", "UNCHANGED", "PARTIAL", "BLOCKED", "UNSUPPORTED"] : ["READY", "BLOCKED", "UNSUPPORTED"];
  const codes = ["FRAME_NOT_ALLOWED", "SOURCE_NOT_ALLOWED", "STATE_UNCLEAR", "CAPTCHA_REQUIRED", "LOGIN_REQUIRED",
    "PREVIEW_STALE", "FIELDS_NOT_READY", "PREVIEW_READY", "DOM_CHANGED", "VALUE_REJECTED", "FILL_COMPLETE"];
  if (!statuses.includes(raw.status) || !codes.includes(raw.code)) throw new TypeError("Invalid form outcome");
  const reports = array(raw.fields, 25, (report) => {
    object(report, ["field_id", "status", "match_count"], "field result");
    identifier(report.field_id);
    if (!FORM_FIELD_STATUSES.includes(report.status) || !Number.isInteger(report.match_count)
        || report.match_count < 0 || report.match_count > 10000) throw new TypeError("Invalid field result");
    if ((!fill && ["NOT_ATTEMPTED", "FILLED", "UNCHANGED", "FAILED"].includes(report.status))
        || (["READY", "FILLED", "UNCHANGED"].includes(report.status) && report.match_count !== 1)) {
      throw new TypeError("Inconsistent field result");
    }
    return {...report};
  });
  if ((reports.length !== plan.fields.length && !(raw.status === "BLOCKED" && reports.length === 0))
      || reports.some((report, i) => report.field_id !== plan.fields[i].field_id)) throw new TypeError("Form field binding mismatch");
  if ((raw.status === "READY" && (raw.code !== "PREVIEW_READY" || reports.some(r => r.status !== "READY")))
      || (["FILLED", "UNCHANGED"].includes(raw.status) && (raw.code !== "FILL_COMPLETE"
        || reports.some(r => !["FILLED", "UNCHANGED"].includes(r.status))))
      || (raw.status === "FILLED" && !reports.some(r => r.status === "FILLED"))
      || (raw.status === "UNCHANGED" && reports.some(r => r.status !== "UNCHANGED"))) throw new TypeError("Inconsistent form outcome");
  return {...raw, fields: reports};
}

function normalizeFormPreview(raw, params) {
  return normalizeFormResult(raw, params, false);
}

function normalizeFormFill(raw, params) {
  return normalizeFormResult(raw, params, true);
}

function clean(value, limit = 2000) {
  return string(value, limit, "evidence text")
    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, "[redacted-email]")
    .replace(/(?:\+?86[-\s]?)?1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}/g, "[redacted-phone]")
    .replace(/\b\d{17}[\dXx]\b/g, "[redacted-id]");
}

function array(value, max, mapper) {
  if (!Array.isArray(value) || value.length > max) throw new TypeError("Invalid evidence array");
  return value.map(mapper);
}

function number(value, max = 1e7) {
  if (typeof value !== "number" || !Number.isFinite(value) || Math.abs(value) > max) {
    throw new TypeError("Invalid evidence number");
  }
  return value;
}

function fields(value, schema) {
  object(value, Object.keys(schema), "evidence fields");
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, schema[key](item)]));
}

const text = limit => value => clean(value, limit);
const boolean = value => {
  if (typeof value !== "boolean") throw new TypeError("Invalid evidence boolean");
  return value;
};
const confidence = value => {
  if (number(value, 1) < 0) throw new TypeError("Invalid confidence");
  return value;
};
const statusSchema = {status: text(128), label: text(200), context: text(1000),
  evidence: text(2000), confidence};
const recordSchema = {...statusSchema, title: text(200), applied_at: text(80),
  evidence_source: text(128), raw_status_labels: v => array(v, 100, text(200)),
  signals: v => fields(v, Object.fromEntries([
    "unmapped_status", "has_date", "has_operation", "has_volunteer_index",
    "has_explicit_status", "has_active_step", "conflicting_statuses"
  ].map(key => [key, boolean])))};
const nodeSchema = {
  tag: text(24), role: text(64), ariaLabel: text(200), text: text(500),
  classTokens: v => array(v, 12, text(64)),
  attributes: v => fields(v, Object.fromEntries([
    "aria-current", "aria-selected", "aria-checked", "aria-expanded",
    "data-recruitops-application-id", "data-application-id", "data-recruitops-application-status"
  ].map(key => [key, text(128)]))),
  visual: v => fields(v, {color: text(48), backgroundColor: text(48), fontWeight: text(16), opacity: text(16)}),
  rect: v => fields(v, {x: number, y: number, width: number, height: number})
};

function normalizeObservation(raw, context) {
  return normalizeEvidence(raw, context, false);
}

function normalizeManualCapture(raw, context) {
  return normalizeEvidence(raw, context, true);
}

function normalizeEvidence(raw, context, capture) {
  object(context, capture ? ["operation_id", "page_url"]
    : ["operation_id", "page_url", "application_ids"], "trusted context");
  const operationId = identifier(context.operation_id);
  const pageUrl = httpUrl(context.page_url);
  const applicationIds = capture ? [] : array(context.application_ids, 100, identifier);
  if (!capture && new Set(applicationIds).size !== applicationIds.length) {
    throw new TypeError("Expected unique application bindings");
  }
  object(raw, ["protocolVersion", "type", "requestId", "ok", "data", "state", "pause", "error"], "observation");
  if (Buffer.byteLength(JSON.stringify(raw), "utf8") > 256 * 1024) {
    throw new TypeError("Observation exceeds bridge payload limit");
  }
  if (raw.protocolVersion !== 3 || raw.requestId !== operationId || typeof raw.ok !== "boolean") {
    throw new TypeError("Observation protocol or operation mismatch");
  }
  const result = {evidence_only: true, database_updated: false,
    ...(capture ? {kind: "manual_capture", requires_user_confirmation: true}
      : {application_ids: applicationIds, ...(applicationIds.length ? {application_id: applicationIds[0]} : {})}),
    page_url: pageUrl.href};
  const envelope = {protocol_version: 1, type: "result", operation_id: operationId,
    event_id: `desktop-${capture ? "capture" : "result"}-${createHash("sha256").update(operationId).digest("hex").slice(0, 40)}`, result};
  if (!raw.ok) {
    if (!["extension.pause_state", "extension.controlled_action_result"].includes(raw.type)) {
      throw new TypeError("Invalid observation type");
    }
    const reasons = {login_required: "LOGIN_REQUIRED", captcha_required: "CAPTCHA_REQUIRED", state_unclear: "STATE_UNCLEAR"};
    const reason = raw.pause?.reason;
    const code = reasons[reason] || (["SOURCE_NOT_ALLOWED", "FRAME_NOT_ALLOWED"].includes(raw.error?.code)
      ? raw.error.code : "STATE_UNCLEAR");
    return {...envelope, status: code.endsWith("NOT_ALLOWED") ? "FAILED" : "STATE_UNCLEAR",
      error_code: code, result: {...result, requires_user_action: true}};
  }
  if (raw.type !== "extension.controlled_action_result") throw new TypeError("Invalid observation type");
  const data = object(raw.data, ["action", "selectorKey", "page", "semanticNodes", "applicationRecords",
    "entries", "diagnostics", "capturedAt"], "observation data");
  if (data.action !== "observe_application_page" || data.selectorKey !== "application_page") {
    throw new TypeError("Unexpected action");
  }
  const page = fields(data.page, {page_url: v => httpUrl(v).href, origin: v => httpUrl(v).origin, path: text(2048), title: text(500),
    text: text(20000), links: v => array(v, 200, x => httpUrl(x).href),
    networkRequests: v => array(v, 500, x => fields(x, {
      method: text(16), url: y => httpUrl(y).href,
      status_code: y => y === null ? null : number(y, 599), resource_type: text(80)
    })), capturedAt: text(80)});
  if (page.page_url !== pageUrl.href || page.origin !== pageUrl.origin || page.path !== clean(pageUrl.pathname, 2048)) {
    throw new TypeError("Observed page does not match trusted current URL");
  }
  const records = array(data.applicationRecords, 100, value => fields(value, recordSchema));
  const nodes = array(data.semanticNodes, 240, value => fields(value, nodeSchema));
  const entries = array(data.entries, 100, value => fields(value, statusSchema));
  const diagnostics = fields(data.diagnostics, {
    readyState: text(32), visibleTextLength: number, visibleMediaCount: number,
    semanticNodeCount: number, recordBlockCount: number, recordCount: number,
    mappedStatusCount: number, iframeCount: number, frameScope: text(32), loginPromptVisible: boolean
  });
  if (diagnostics.frameScope !== "top_only" && (capture || diagnostics.frameScope !== "single_frame")) throw new TypeError("Unsupported frame scope");
  Object.assign(result, {page, captured_at: clean(data.capturedAt, 80), semantic_nodes: nodes,
    application_records: records, entries, diagnostics});
  if (capture) {
    // A page title is not a job identity, and a JD is not proof of submission.
    result.draft = {url: pageUrl.href, title: page.title || "", page_text: page.text || ""};
  }
  // Extraction success is not a status decision or an application identity match.
  const unclear = !page.text?.trim() || (!records.length && diagnostics.iframeCount > 0);
  const normalized = {...envelope, status: unclear ? "STATE_UNCLEAR" : "SUCCEEDED",
    ...(unclear ? {error_code: capture && diagnostics.iframeCount ? "FRAME_EVIDENCE_UNAVAILABLE" : "STATE_UNCLEAR"} : {})};
  if (Buffer.byteLength(JSON.stringify(normalized), "utf8") > 256 * 1024) {
    throw new TypeError("Observation exceeds bridge payload limit");
  }
  return normalized;
}

module.exports = {buildObservationScript, buildFrameObservationScript, normalizeFrameObservations, normalizeObservation, createObservationAdapter,
  buildManualCaptureScript, normalizeManualCapture, getFormFillCapability,
  buildFormPreviewScript, buildFormFillScript, normalizeFormPreview, normalizeFormFill,
  loadObservationResources, packageObservationResources, RESOURCE_HASHES};
