// HireDrop extension <-> dashboard bridge.
// Runs as a content script on the HireDrop website. Forwards two kinds of
// page-originated messages to the background service worker:
//   HIREDROP_PING          → respond with HIREDROP_PONG (extension detection)
//   HIREDROP_STORE_TOKEN   → STORE_TOKEN to background, then HIREDROP_TOKEN_STORED back
//   HIREDROP_READ_STORAGE  → read chrome.storage.local keys, post HIREDROP_STORAGE_DATA back (debug)
window.addEventListener("message", function (e) {
  if (e.source !== window || !e.data) return;

  if (e.data === "HIREDROP_PING") {
    window.postMessage("HIREDROP_PONG", "*");
    return;
  }

  if (typeof e.data === "object" && e.data.type === "HIREDROP_STORE_TOKEN" && typeof e.data.token === "string") {
    chrome.runtime.sendMessage(
      { type: "STORE_TOKEN", token: e.data.token, refresh_token: e.data.refresh_token || "" },
      function (resp) {
        const ok = !!(resp && resp.stored);
        window.postMessage(
          { type: "HIREDROP_TOKEN_STORED", ok, error: chrome.runtime.lastError ? chrome.runtime.lastError.message : null },
          "*"
        );
      }
    );
  }

  if (typeof e.data === "object" && e.data.type === "HIREDROP_READ_STORAGE") {
    const keys = e.data.keys || ["supabase_token", "supabase_refresh_token", "profile"];
    chrome.storage.local.get(keys, function (data) {
      const redacted = {};
      for (const k of keys) {
        const v = data[k];
        if (typeof v === "string" && v.length > 20) {
          redacted[k] = v.slice(0, 12) + "...(len=" + v.length + ")";
        } else {
          redacted[k] = v;
        }
      }
      window.postMessage({ type: "HIREDROP_STORAGE_DATA", data: redacted }, "*");
    });
  }

  if (typeof e.data === "object" && e.data.type === "HIREDROP_START_CAMPAIGN") {
    chrome.runtime.sendMessage(
      { type: "START_CAMPAIGN", filters: e.data.filters || {} },
      function (resp) {
        window.postMessage(
          { type: "HIREDROP_CAMPAIGN_STARTED", ok: !!(resp && resp.started), error: resp && resp.error },
          "*"
        );
      }
    );
  }

  if (typeof e.data === "object" && e.data.type === "HIREDROP_STOP_CAMPAIGN") {
    chrome.runtime.sendMessage({ type: "STOP_CAMPAIGN" }, function () {});
  }
});
