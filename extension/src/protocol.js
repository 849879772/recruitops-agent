(() => {
  "use strict";

  const messageTypes = Object.freeze({
    AUTHORIZE_CURRENT_TAB: "extension.authorize_current_tab",
    READ_SANITIZED_DOM: "extension.read_sanitized_dom",
    DOM_SNAPSHOT: "extension.dom_snapshot",
    SUBMIT_DOM_OBSERVATION: "extension.submit_dom_observation",
    OBSERVATION_SUBMITTED: "extension.observation_submitted",
    SUBMIT_APPLICATION_CAPTURE: "extension.submit_application_capture",
    APPLICATION_CAPTURE_SUBMITTED: "extension.application_capture_submitted",
    EXECUTE_CONTROLLED_ACTION: "extension.execute_controlled_action",
    CONTROLLED_ACTION_RESULT: "extension.controlled_action_result",
    PAUSE_STATE: "extension.pause_state",
    CONFIG_GET: "extension.config.get",
    CONFIG_SET: "extension.config.set",
    CONFIG_TEST: "extension.config.test"
  });

  const actionTypes = Object.freeze({
    OBSERVE_APPLICATION_PAGE: "observe_application_page"
  });

  const selectorKeys = Object.freeze({
    APPLICATION_PAGE: "application_page"
  });

  const commandTypes = Object.freeze({
    OBSERVE_APPLICATION_STATUS_PAGE: "observe_application_status_page"
  });

  const bridgeProtocol = Object.freeze({
    version: 1,
    dispatchRequiredKeys: Object.freeze([
      "operation_id",
      "operation",
      "command"
    ]),
    commandRequiredKeys: Object.freeze([
      "action",
      "selector_key",
      "params",
      "page_url",
      "origin",
      "application_id",
      "application_ids"
    ])
  });

  const pauseReasons = Object.freeze({
    LOGIN_REQUIRED: "login_required",
    CAPTCHA_REQUIRED: "captcha_required",
    STATE_UNCLEAR: "state_unclear"
  });

  const resumeActions = Object.freeze({
    LOGIN: "resume_after_login",
    CAPTCHA: "resume_after_captcha",
    CONFIRM_STATE: "confirm_state"
  });

  function createPauseState(reason, message) {
    const actionByReason = {
      [pauseReasons.LOGIN_REQUIRED]: resumeActions.LOGIN,
      [pauseReasons.CAPTCHA_REQUIRED]: resumeActions.CAPTCHA,
      [pauseReasons.STATE_UNCLEAR]: resumeActions.CONFIRM_STATE
    };

    if (!actionByReason[reason]) {
      throw new Error(`Unsupported pause reason: ${reason}`);
    }

    return Object.freeze({
      status: "paused",
      reason,
      requiresUserAction: true,
      resumeAction: actionByReason[reason],
      message: typeof message === "string" ? message : "User action required."
    });
  }

  function validateBridgeDispatch(message) {
    const payload = message?.payload;
    const missingFields = [];
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      return Object.freeze({ok: false, code: "COMMAND_INVALID", missingFields: ["payload"]});
    }
    for (const key of bridgeProtocol.dispatchRequiredKeys) {
      if (!Object.prototype.hasOwnProperty.call(payload, key)) missingFields.push(key);
    }
    const command = payload.command;
    if (!command || typeof command !== "object" || Array.isArray(command)) {
      if (!missingFields.includes("command")) missingFields.push("command");
    } else {
      for (const key of bridgeProtocol.commandRequiredKeys) {
        if (!Object.prototype.hasOwnProperty.call(command, key)) {
          missingFields.push(`command.${key}`);
        }
      }
    }
    return Object.freeze({
      ok: missingFields.length === 0,
      code: missingFields.length === 0 ? null : "COMMAND_INVALID",
      missingFields: Object.freeze(missingFields),
      payload,
      command
    });
  }

  globalThis.RecruitOpsProtocol = Object.freeze({
    version: 3,
    messageTypes,
    actionTypes,
    selectorKeys,
    commandTypes,
    bridgeProtocol,
    pauseReasons,
    resumeActions,
    createPauseState,
    validateBridgeDispatch
  });
})();
