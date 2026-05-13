// JobFlow extension <-> dashboard bridge.
// Runs as a content script on the JobFlow website. Forwards two kinds of
// page-originated messages to the background service worker:
//   JOBFLOW_PING          → respond with JOBFLOW_PONG (extension detection)
//   JOBFLOW_STORE_TOKEN   → STORE_TOKEN to background, then JOBFLOW_TOKEN_STORED back
window.addEventListener("message", function (e) {
  if (e.source !== window || !e.data) return;

  if (e.data === "JOBFLOW_PING") {
    window.postMessage("JOBFLOW_PONG", "*");
    return;
  }

  if (typeof e.data === "object" && e.data.type === "JOBFLOW_STORE_TOKEN" && typeof e.data.token === "string") {
    chrome.runtime.sendMessage(
      { type: "STORE_TOKEN", token: e.data.token },
      function (resp) {
        // resp may be undefined if the service worker is asleep; reply
        // either way so the page can stop spinning.
        const ok = !!(resp && resp.stored);
        window.postMessage(
          { type: "JOBFLOW_TOKEN_STORED", ok, error: chrome.runtime.lastError ? chrome.runtime.lastError.message : null },
          "*"
        );
      }
    );
  }
});
