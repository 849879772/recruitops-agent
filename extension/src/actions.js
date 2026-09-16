(() => {
  "use strict";

  const protocol = globalThis.RecruitOpsProtocol;

  // These selectors are extension-owned protocol values. Page content never supplies CSS.
  const selectorWhitelist = Object.freeze({
    application_page: Object.freeze([
      "main",
      "[role='main']",
      "body"
    ])
  });

  const actionDefinitions = Object.freeze({
    observe_application_page: Object.freeze({
      selectorKey: "application_page",
      parameterKeys: Object.freeze([
        "include_vision",
        "vision_fallback_reason",
        "retain_on_pause"
      ])
    })
  });

  const ACTION_REQUEST_KEYS = Object.freeze([
    "action",
    "selectorKey",
    "params"
  ]);

  function isRecord(value) {
    return Boolean(value) && typeof value === "object" && !Array.isArray(value);
  }

  function hasOnlyKeys(value, allowedKeys) {
    return Object.keys(value).every((key) => allowedKeys.includes(key));
  }

  function validateActionRequest(action, selectorKey, params) {
    const definition = actionDefinitions[action];
    if (!definition) {
      return {ok: false, code: "ACTION_NOT_ALLOWED"};
    }
    if (selectorKey !== definition.selectorKey || !selectorWhitelist[selectorKey]) {
      return {ok: false, code: "SELECTOR_NOT_ALLOWED"};
    }

    const candidate = params === undefined ? {} : params;
    if (!isRecord(candidate) || !hasOnlyKeys(candidate, definition.parameterKeys)) {
      return {ok: false, code: "PARAMETERS_NOT_ALLOWED"};
    }

    if (action === "observe_application_page") {
      if (
        Object.prototype.hasOwnProperty.call(candidate, "include_vision") &&
        typeof candidate.include_vision !== "boolean"
      ) {
        return {ok: false, code: "PARAMETERS_INVALID"};
      }
      if (
        Object.prototype.hasOwnProperty.call(candidate, "vision_fallback_reason") &&
        candidate.vision_fallback_reason !== null &&
        candidate.vision_fallback_reason !== "no_structured_evidence_visible_status_likely"
      ) {
        return {ok: false, code: "PARAMETERS_INVALID"};
      }
      if (
        Object.prototype.hasOwnProperty.call(candidate, "retain_on_pause") &&
        typeof candidate.retain_on_pause !== "boolean"
      ) {
        return {ok: false, code: "PARAMETERS_INVALID"};
      }
      const includeVision = candidate.include_vision === true;
      const visionFallbackReason = candidate.vision_fallback_reason ?? null;
      if (
        (includeVision && visionFallbackReason !== "no_structured_evidence_visible_status_likely") ||
        (!includeVision && visionFallbackReason !== null)
      ) {
        return {ok: false, code: "PARAMETERS_INVALID"};
      }
      return {
        ok: true,
        action,
        selectorKey,
        params: {
          include_vision: includeVision,
          vision_fallback_reason: visionFallbackReason,
          retain_on_pause: candidate.retain_on_pause !== false
        }
      };
    }
    if (
      !Number.isInteger(candidate.page) || candidate.page < 1 || candidate.page > 100 ||
      typeof candidate.apply_filters !== "boolean"
    ) return {ok: false, code: "PARAMETERS_INVALID"};
    return {
      ok: true,
      action,
      selectorKey,
      params: {page: candidate.page, apply_filters: candidate.apply_filters}
    };
  }

  function validateActionMessage(message) {
    if (!isRecord(message) || !hasOnlyKeys(message, ACTION_REQUEST_KEYS)) {
      return {ok: false, code: "ACTION_MESSAGE_INVALID"};
    }
    return validateActionRequest(message.action, message.selectorKey, message.params);
  }

  function selectorCandidates(selectorKey) {
    const selectors = selectorWhitelist[selectorKey];
    if (!selectors) {
      return [];
    }
    return [...selectors];
  }

  globalThis.RecruitOpsActions = Object.freeze({
    actionDefinitions,
    selectorWhitelist,
    validateActionRequest,
    validateActionMessage,
    selectorCandidates
  });
})();
