(() => {
  "use strict";

  function parseHttpUrl(value) {
    if (typeof value !== "string") {
      return null;
    }

    const raw = value.trim();
    // Userinfo and wildcards are rejected before deriving an exact task origin.
    if (!raw || raw.includes("@") || raw.includes("*")) {
      return null;
    }

    try {
      const url = new URL(raw);
      if (url.protocol !== "http:" && url.protocol !== "https:") {
        return null;
      }
      return url;
    } catch (_error) {
      return null;
    }
  }

  function getHttpOrigin(value) {
    const url = parseHttpUrl(value);
    return url && url.origin !== "null" ? url.origin : null;
  }

  globalThis.RecruitOpsAllowlist = Object.freeze({
    getHttpOrigin
  });
})();
