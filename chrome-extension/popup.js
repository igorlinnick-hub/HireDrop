// CONFIG is loaded from config.js via popup.html

const $ = (id) => document.getElementById(id);

function send(msg) {
  return new Promise((resolve) => chrome.runtime.sendMessage(msg, resolve));
}

function escapeHtml(text) {
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// ---------------------------------------------------------------------------
// Auth banner helpers
// ---------------------------------------------------------------------------

function showAuthBanner(title, sub) {
  $("auth-banner-title").textContent = title || "Connect Your Account";
  $("auth-banner-sub").textContent =
    sub ||
    "Sign in to HireDrop to start automating your job search. Your session is synced from the dashboard.";
  $("auth-banner").classList.add("visible");
  $("body-main").style.display = "none";
}

function hideAuthBanner() {
  $("auth-banner").classList.remove("visible");
  $("body-main").style.display = "";
}

$("btn-connect").addEventListener("click", () => {
  chrome.tabs.create({ url: CONFIG.DASHBOARD_URL + CONFIG.CONNECT_PATH });
});

// ---------------------------------------------------------------------------
// CAPTCHA alert helpers
// ---------------------------------------------------------------------------

let _captchaTabId = null;

function showCaptchaAlert(signal) {
  $("captcha-alert").classList.add("visible");
}

function hideCaptchaAlert() {
  $("captcha-alert").classList.remove("visible");
  _captchaTabId = null;
}

$("btn-go-indeed").addEventListener("click", () => {
  if (_captchaTabId) {
    chrome.tabs.update(_captchaTabId, { active: true });
  } else {
    chrome.tabs.query({ url: "*://*.indeed.com/*" }, (tabs) => {
      if (tabs.length) chrome.tabs.update(tabs[0].id, { active: true });
    });
  }
});

// ---------------------------------------------------------------------------
// Inline warning helper
// ---------------------------------------------------------------------------

function showWarn(text) {
  const el = $("start-warn");
  el.textContent = text;
  el.classList.add("visible");
}

function hideWarn() {
  $("start-warn").classList.remove("visible");
}

// ---------------------------------------------------------------------------
// Connection check
// ---------------------------------------------------------------------------

let isConnected = false;

async function checkConnection() {
  try {
    const res = await send({ type: "CHECK_CONNECTION" });
    if (res && res.connected) {
      $("conn-dot").className = "conn-dot ok";
      $("conn-text").textContent = "Connected";
      isConnected = true;
      return true;
    }
  } catch {}
  $("conn-dot").className = "conn-dot err";
  $("conn-text").textContent = "Offline";
  isConnected = false;
  $("btn-start").disabled = true;
  return false;
}

// ---------------------------------------------------------------------------
// Profile
// ---------------------------------------------------------------------------

async function loadProfile() {
  const profile = await send({ type: "GET_PROFILE" });
  if (profile && profile.name) {
    const fullName = [profile.name, profile.last_name].filter(Boolean).join(" ");
    $("profile-name").textContent = fullName;
  }
  return profile;
}

// ---------------------------------------------------------------------------
// Activity log
// ---------------------------------------------------------------------------

async function renderActivityLog() {
  const { activity_log } = await chrome.storage.local.get("activity_log");
  const logs = (activity_log || []).slice(0, 10);
  const el = $("log-list");

  if (!logs.length) {
    el.innerHTML = '<div class="log-empty">No activity yet — start a campaign to begin</div>';
    return;
  }

  el.innerHTML = logs
    .map(
      (l) =>
        '<div class="log-item"><span class="log-time">' +
        escapeHtml(l.time) +
        '</span><span class="log-msg ' +
        (l.cls || "") +
        '">' +
        escapeHtml(l.text) +
        "</span></div>"
    )
    .join("");
}

// ---------------------------------------------------------------------------
// Elapsed time formatter
// ---------------------------------------------------------------------------

function formatElapsed(ms) {
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

let campaignStartedAt = null;
let elapsedTimer = null;

function startElapsedTimer() {
  stopElapsedTimer();
  elapsedTimer = setInterval(() => {
    if (campaignStartedAt) {
      $("run-time").textContent = formatElapsed(Date.now() - campaignStartedAt);
    }
  }, 1000);
}

function stopElapsedTimer() {
  if (elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

// ---------------------------------------------------------------------------
// Status — update entire UI from GET_STATUS
// ---------------------------------------------------------------------------

async function loadStatus() {
  const status = await send({ type: "GET_STATUS" });
  if (!status || status.error) return;

  const running = status.campaignRunning;

  // Stats
  $("stat-today").textContent = status.todayCount || 0;
  $("stat-week").textContent = status.totalApplications || 0;
  $("stat-total").textContent = status.totalJobs || 0;

  if (running) {
    $("campaign-stopped").style.display = "none";
    $("campaign-running").style.display = "";

    if (status.startedAt) {
      campaignStartedAt = new Date(status.startedAt).getTime();
      $("run-time").textContent = formatElapsed(Date.now() - campaignStartedAt);
      startElapsedTimer();
    }

    if (status.currentJob) {
      $("current-job").style.display = "";
      $("cj-title").textContent = status.currentJob.title || "";
      $("cj-company").textContent = status.currentJob.company || "";
    } else {
      $("current-job").style.display = "none";
    }

    // Hide CAPTCHA alert once campaign is running normally
    if (!status.captchaDetected) hideCaptchaAlert();
  } else {
    $("campaign-stopped").style.display = "";
    $("campaign-running").style.display = "none";
    campaignStartedAt = null;
    stopElapsedTimer();

    const limit = status.limitPerPlatform || 50;
    const today = status.todayCount || 0;
    $("limit-text").textContent = `${today} / ${limit} today`;
    $("btn-start").disabled = !isConnected;
  }

  renderActivityLog();
}

// ---------------------------------------------------------------------------
// Start campaign — with profile completeness check
// ---------------------------------------------------------------------------

$("btn-start").addEventListener("click", async () => {
  hideWarn();
  $("btn-start").disabled = true;
  $("btn-start").textContent = "Checking...";

  const profile = await send({ type: "GET_PROFILE" });

  // Profile completeness gate
  if (!profile || !profile.name) {
    showWarn("Go to the Dashboard → complete your profile first.");
    $("btn-start").textContent = "Start Campaign";
    $("btn-start").disabled = false;
    return;
  }
  if (!profile.keywords || profile.keywords.length === 0) {
    showWarn("No job keywords set. Open Dashboard → Profile and add keywords like \"marketing manager\".");
    $("btn-start").textContent = "Start Campaign";
    $("btn-start").disabled = false;
    return;
  }
  if (!profile.resume_url) {
    showWarn("No resume uploaded. Open Dashboard → Profile → upload your PDF resume.");
    $("btn-start").textContent = "Start Campaign";
    $("btn-start").disabled = false;
    return;
  }

  $("btn-start").textContent = "Starting...";

  const filters = {
    keywords: profile.keywords,
    platforms: profile.platforms || ["indeed"],
    location: profile.location || "",
    job_type: profile.job_type || "",
  };

  const res = await send({ type: "START_CAMPAIGN", filters });

  if (res && res.started) {
    addLog("Campaign started — Indeed tab opened", "ok");
  } else {
    addLog("Failed to start: " + (res?.error || "unknown"), "err");
  }

  $("btn-start").textContent = "Start Campaign";
  await loadStatus();
});

// ---------------------------------------------------------------------------
// Stop campaign
// ---------------------------------------------------------------------------

$("btn-stop").addEventListener("click", async () => {
  $("btn-stop").disabled = true;
  hideCaptchaAlert();
  const res = await send({ type: "STOP_CAMPAIGN" });
  if (res && res.stopped) addLog("Campaign stopped", "");
  $("btn-stop").disabled = false;
  await loadStatus();
});

// ---------------------------------------------------------------------------
// Add log entry helper
// ---------------------------------------------------------------------------

async function addLog(text, cls) {
  const { activity_log } = await chrome.storage.local.get("activity_log");
  const logs = activity_log || [];
  logs.unshift({
    time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    text,
    cls: cls || "",
  });
  if (logs.length > 50) logs.length = 50;
  await chrome.storage.local.set({ activity_log: logs });
  renderActivityLog();
}

// ---------------------------------------------------------------------------
// Dashboard button
// ---------------------------------------------------------------------------

$("btn-dash").addEventListener("click", () => {
  chrome.tabs.create({ url: CONFIG.DASHBOARD_URL });
});

// ---------------------------------------------------------------------------
// Message listener — LOG, AUTH_EXPIRED, DETECTION_TRIPPED
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "LOG") {
    addLog(msg.text, msg.cls || "");
    // If CAPTCHA resolved, hide alert
    if (msg.text && msg.text.includes("CAPTCHA resolved")) hideCaptchaAlert();
    if (msg.cls === "ok") loadStatus();
  }
  if (msg.type === "AUTH_EXPIRED") {
    showAuthBanner(
      "Session Expired",
      "Your HireDrop session has expired. Open the dashboard and log in again — the extension will reconnect automatically."
    );
  }
  if (msg.type === "DETECTION_TRIPPED") {
    _captchaTabId = msg.data?.tabId || null;
    showCaptchaAlert(msg.data?.signal);
    addLog(`⚠️ CAPTCHA / security check — solve it in the Indeed tab`, "err");
  }
});

// ---------------------------------------------------------------------------
// Polling — refresh status every 2 seconds
// ---------------------------------------------------------------------------

let _isAuthenticated = false;

function startPolling() {
  setInterval(async () => {
    if (!_isAuthenticated) {
      const authStatus = await send({ type: "GET_AUTH_STATUS" });
      if (authStatus && authStatus.authenticated) {
        _isAuthenticated = true;
        hideAuthBanner();
        renderActivityLog();
        const connected = await checkConnection();
        if (connected) await Promise.all([loadProfile(), loadStatus()]);
      }
    } else {
      loadStatus();
    }
  }, 2000);
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

(async () => {
  const authStatus = await send({ type: "GET_AUTH_STATUS" });
  if (!authStatus || !authStatus.authenticated) {
    showAuthBanner();
    startPolling();
    return;
  }

  _isAuthenticated = true;
  hideAuthBanner();
  renderActivityLog();
  const connected = await checkConnection();
  if (connected) await Promise.all([loadProfile(), loadStatus()]);
  startPolling();
})();
