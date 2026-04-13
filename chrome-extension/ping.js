// JobFlow extension detection — responds to dashboard ping
window.addEventListener("message", function (e) {
  if (e.source === window && e.data === "JOBFLOW_PING") {
    window.postMessage("JOBFLOW_PONG", "*");
  }
});
