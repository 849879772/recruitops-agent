(() => {
  "use strict";

  const protocol = globalThis.RecruitOpsProtocol;
  const authorizeButton = document.getElementById("authorize");
  const optionsButton = document.getElementById("open-options");
  const status = document.getElementById("status");
  const captureButton = document.getElementById("capture-application");
  const captureJobId = document.getElementById("capture-job-id");
  const captureNote = document.getElementById("capture-note");

  function requestId() {
    return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  authorizeButton.addEventListener("click", async () => {
    authorizeButton.disabled = true;
    status.textContent = "正在等待授权结果...";

    try {
      const response = await chrome.runtime.sendMessage({
        protocolVersion: protocol.version,
        type: protocol.messageTypes.AUTHORIZE_CURRENT_TAB,
        requestId: requestId(),
        userGesture: true
      });

      if (response?.ok) {
        const submitted = await chrome.runtime.sendMessage({
          protocolVersion: protocol.version,
          type: protocol.messageTypes.SUBMIT_DOM_OBSERVATION,
          requestId: requestId(),
          userGesture: true,
          snapshot: response.data
        });
        if (submitted?.ok) {
          status.textContent = "已读取并提交脱敏页面观测。";
        } else if (submitted?.error?.code === "SOURCE_NOT_ALLOWED") {
          status.textContent = "页面已读取，但扩展尚未获得网页访问权限。";
        } else {
          status.textContent = "已读取页面，但提交到本机 Agent 失败。";
        }
      } else if (response?.error?.code === "SOURCE_NOT_ALLOWED") {
        status.textContent = "请先在扩展配置页保存并授权访问招聘网站。";
      } else {
        status.textContent = "页面未读取，请检查配置或页面状态。";
      }
    } catch (_error) {
      status.textContent = "授权失败，请稍后重试。";
    } finally {
      authorizeButton.disabled = false;
    }
  });

  captureButton.addEventListener("click", async () => {
    captureButton.disabled = true;
    status.textContent = "正在读取并匹配当前岗位...";
    try {
      const authorized = await chrome.runtime.sendMessage({
        protocolVersion: protocol.version,
        type: protocol.messageTypes.AUTHORIZE_CURRENT_TAB,
        requestId: requestId(),
        userGesture: true
      });
      if (!authorized?.ok) {
        status.textContent = authorized?.error?.code === "SOURCE_NOT_ALLOWED"
          ? "请先在扩展配置页保存并授权访问招聘网站。"
          : "当前岗位页读取失败。";
        return;
      }
      const submitted = await chrome.runtime.sendMessage({
        protocolVersion: protocol.version,
        type: protocol.messageTypes.SUBMIT_APPLICATION_CAPTURE,
        requestId: requestId(),
        userGesture: true,
        snapshot: authorized.data,
        jobId: captureJobId.value.trim(),
        note: captureNote.value.trim()
      });
      const captureStatus = submitted?.data?.status;
      if (submitted?.ok && captureStatus === "approval_required") {
        status.textContent = "已生成待审批投递记录，请在本机审批中心确认。";
      } else if (submitted?.ok && captureStatus === "already_recorded") {
        status.textContent = "该岗位已经存在于投递记录。";
      } else if (submitted?.ok && captureStatus === "ambiguous") {
        status.textContent = "当前页面对应多个岗位，请填写岗位 ID 后重试。";
      } else if (submitted?.ok && captureStatus === "not_found") {
        status.textContent = "未匹配到当前岗位，请填写岗位 ID 或先同步岗位库。";
      } else {
        status.textContent = `投递记录预览失败：${submitted?.error?.code || "UNKNOWN_ERROR"}`;
      }
    } catch (_error) {
      status.textContent = "投递记录预览请求失败。";
    } finally {
      captureButton.disabled = false;
    }
  });

  optionsButton.addEventListener("click", () => {
    chrome.runtime.openOptionsPage();
  });
})();
