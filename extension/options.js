(() => {
  "use strict";

  const protocol = globalThis.RecruitOpsProtocol;
  const config = globalThis.RecruitOpsConfig;
  const form = document.getElementById("config-form");
  const apiBaseUrl = document.getElementById("api-base-url");
  const apiToken = document.getElementById("api-token");
  const testConnection = document.getElementById("test-connection");
  const version = document.getElementById("version");
  const status = document.getElementById("status");

  function requestId() {
    return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  function setStatus(message) {
    status.textContent = message;
  }

  function bridgeMessage(result) {
    const messages = {
      connected: "WebSocket 已连接到本机 Agent。",
      connecting: "正在建立本机 WebSocket 长连接。",
      authenticating: "长连接已建立，正在完成本机认证。",
      config_missing: "配置不完整，请填写本机 API 地址和 Token。",
      connection_error: "长连接失败：请确认本机 Agent 正在运行。",
      disconnected: "长连接已断开，后台会自动重连。",
      retry_scheduled: "长连接暂不可用，后台会自动重连。",
      operation_acknowledged: "已接收本机 Agent 的浏览器操作。",
      operation_completed: "浏览器操作已完成并回传结果。",
      operation_cancelled: "浏览器操作已取消。"
    };
    return messages[result?.state] || `连接状态未知：${result?.state || "UNKNOWN"}`;
  }

  async function loadSettings() {
    const stored = await chrome.storage.local.get(config.DEFAULT_SETTINGS);
    const settings = config.normalizeSettings(stored);
    apiBaseUrl.value = settings.apiBaseUrl;
    apiToken.value = settings.apiToken;
  }

  function readSettingsFromForm() {
    const normalizedApiBaseUrl = config.normalizeApiBaseUrl(apiBaseUrl.value);
    if (!normalizedApiBaseUrl) {
      throw new Error("LOCAL_API_ONLY");
    }

    return config.normalizeSettings({
      apiBaseUrl: normalizedApiBaseUrl,
      apiToken: apiToken.value
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const settings = readSettingsFromForm();
      const granted = await chrome.permissions.contains({
        origins: ["<all_urls>"]
      });
      if (!granted) {
        throw new Error("HOST_PERMISSION_REQUIRED");
      }
      await chrome.storage.local.set(settings);
      const response = await chrome.runtime.sendMessage({
        protocolVersion: protocol.version,
        type: protocol.messageTypes.CONFIG_SET,
        requestId: requestId(),
        settings
      });
      setStatus(response?.ok
        ? `已保存本地配置。${bridgeMessage(response.bridge)}`
        : "配置已保存，但后台长连接启动失败。");
    } catch (error) {
      const messages = {
        LOCAL_API_ONLY: "API 地址必须是本机 localhost、127.0.0.1 或 ::1。",
        HOST_PERMISSION_REQUIRED: "扩展尚未获得招聘网站访问权限，请在扩展管理页重新加载当前版本。"
      };
      setStatus(messages[error.message] || "配置保存失败。");
    }
  });

  testConnection.addEventListener("click", async () => {
    testConnection.disabled = true;
    setStatus("正在连接本机 Agent 并检查长连接状态...");
    try {
      const response = await chrome.runtime.sendMessage({
        protocolVersion: protocol.version,
        type: protocol.messageTypes.CONFIG_TEST,
        requestId: requestId()
      });
      setStatus(response?.ok ? bridgeMessage(response.bridge) : "长连接测试失败，请重新加载扩展后再试。");
    } catch (_error) {
      setStatus("扩展后台未响应，请在扩展管理页重新加载当前版本。");
    } finally {
      testConnection.disabled = false;
    }
  });

  version.textContent = `扩展版本：${chrome.runtime.getManifest().version}`;
  loadSettings().catch(() => setStatus("无法读取本地配置。"));
})();
