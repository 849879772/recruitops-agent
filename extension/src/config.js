(() => {
  "use strict";

  const LOCAL_API_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);
  const DEFAULT_SETTINGS = Object.freeze({
    apiBaseUrl: "http://127.0.0.1:8010",
    apiToken: ""
  });
  const BROWSER_BRIDGE_PATH = "/browser-bridge";
  const BRIDGE_DEVICE_ID_KEY = "recruitopsBrowserBridgeDeviceId";
  const BRIDGE_STATUS_KEY = "recruitopsBrowserBridgeStatus";

  function normalizeApiBaseUrl(value) {
    if (typeof value !== "string") {
      return null;
    }

    const raw = value.trim();
    if (!raw || raw.includes("@") || raw.includes("*")) {
      return null;
    }

    try {
      const url = new URL(raw);
      if (!["http:", "https:"].includes(url.protocol)) {
        return null;
      }
      if (!LOCAL_API_HOSTS.has(url.hostname)) {
        return null;
      }
      if (url.pathname !== "/" || url.search || url.hash) {
        return null;
      }
      return url.origin;
    } catch (_error) {
      return null;
    }
  }

  function normalizeSettings(value) {
    const candidate = value && typeof value === "object" ? value : {};
    const apiBaseUrl = normalizeApiBaseUrl(candidate.apiBaseUrl) || DEFAULT_SETTINGS.apiBaseUrl;
    const apiToken = typeof candidate.apiToken === "string"
      ? candidate.apiToken.trim().slice(0, 2048)
      : "";
    return {apiBaseUrl, apiToken};
  }

  function browserBridgeUrl(value) {
    const apiBaseUrl = normalizeApiBaseUrl(value);
    if (!apiBaseUrl) {
      return null;
    }
    const url = new URL(apiBaseUrl);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.pathname = BROWSER_BRIDGE_PATH;
    url.search = "";
    url.hash = "";
    return url.toString();
  }

  globalThis.RecruitOpsConfig = Object.freeze({
    BRIDGE_DEVICE_ID_KEY,
    BRIDGE_STATUS_KEY,
    BROWSER_BRIDGE_PATH,
    DEFAULT_SETTINGS,
    browserBridgeUrl,
    normalizeApiBaseUrl,
    normalizeSettings
  });
})();
