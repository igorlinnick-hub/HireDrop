// HireDrop extension <-> dashboard bridge.
// Runs as a content script on the HireDrop website. Forwards two kinds of
// page-originated messages to the background service worker:
//   HIREDROP_PING          → respond with HIREDROP_PONG (extension detection)
//   HIREDROP_STORE_TOKEN   → STORE_TOKEN to background, then HIREDROP_TOKEN_STORED back
window.addEventListener("message", function (e) {
  if (e.source !== window || !e.data) return;

  if (e.data === "HIREDROP_PING") {
    window.postMessage("HIREDROP_PONG", "*");
    return;
  }

  if (typeof e.data === "object" && e.data.type === "HIREDROP_STORE_TOKEN" && typeof e.data.token === "string") {
    chrome.runtime.sendMessage(
      { type: "STORE_TOKEN", token: e.data.token },
      function (resp) {
        // resp may be undefined if the service worker is asleep; reply
        // either way so the page can stop spinning.
        const ok = !!(resp && resp.stored);
        window.postMessage(
          { type: "HIREDROP_TOKEN_STORED", ok, error: chrome.runtime.lastError ? chrome.runtime.lastError.message : null },
          "*"
        );
      }
    );
  }
});
