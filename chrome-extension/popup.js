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

// A compact stage rail (Scan → Match → Write → Submit) driven by the latest
// activity, instead of a scrolling wall of chatty log lines. The engine still
// writes activity_log; we just read the newest entry and map it to a stage.
const PROC_STAGES = ["Scan", "Match", "Write", "Submit"];

function stageForText(text) {
  const t = (text || "").toLowerCase();
  if (/submit|applied|\bapply|sent|success|complete|done/.test(t)) return 3;
  if (/cover|writ|fill|tailor|\bform|answer|screen|question/.test(t)) return 2;
  if (/match|score|\bfit\b|rank|evaluat|analy/.test(t)) return 1;
  return 0; // scan / search / warmup / found / resuming / opened …
}

// Strip the decorative symbols the engine prepends (⏭ ✓ ⚠ → 🎯 …) so the line
// reads clean; typography carries the meaning.
function cleanMsg(s) {
  try {
    return (s || "").replace(/[\p{Extended_Pictographic}←-⇿✀-➿]/gu, "").replace(/\s{2,}/g, " ").trim();
  } catch {
    return (s || "").trim();
  }
}

function setStage(active) {
  const fill = $("pr-fill");
  if (fill) fill.style.width = active < 0 ? "0%" : `${(active / (PROC_STAGES.length - 1)) * 100}%`;
  // Brand droplet fills as stages advance: empty when idle → full at Submit.
  const drop = $("pd-fill");
  if (drop) {
    const level = active < 0 ? 0 : (active + 1) / PROC_STAGES.length;
    drop.style.transform = `translateY(${Math.round((1 - level) * 100)}%)`;
  }
  for (let i = 0; i < PROC_STAGES.length; i++) {
    const node = $("pn-" + i);
    if (!node) continue;
    node.classList.toggle("done", active >= 0 && i < active);
    node.classList.toggle("active", active === i);
  }
}

async function renderProcess() {
  const section = $("proc-section");
  if (!section) return;
  const { activity_log, campaignRunning } = await chrome.storage.local.get(["activity_log", "campaignRunning"]);
  const latest = (activity_log || [])[0];
  const running = !!campaignRunning;

  if (!latest) {
    section.classList.remove("active", "attn");
    $("proc-state").textContent = running ? "Starting" : "Idle";
    $("proc-action").textContent = running ? "Warming up…" : "Start a campaign to begin";
    setStage(-1);
    return;
  }

  const isErr = latest.cls === "err";
  section.classList.toggle("active", running && !isErr);
  section.classList.toggle("attn", isErr);
  $("proc-state").textContent = isErr ? "Needs you" : running ? "Working" : "Paused";
  $("proc-action").textContent = cleanMsg(latest.text) || "Working…";
  setStage(running && !isErr ? stageForText(latest.text) : -1);
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

    // Show the CAPTCHA alert whenever a hand-off is pending (captchaWaiting in
    // storage) — this survives popup reopen, unlike the DETECTION_TRIPPED
    // runtime message which only reaches an already-open popup. Hide it once
    // the campaign is running normally again.
    if (status.captchaDetected) showCaptchaAlert(status.captchaWaiting?.signal);
    else hideCaptchaAlert();
  } else {
    $("campaign-stopped").style.display = "";
    $("campaign-running").style.display = "none";
    campaignStartedAt = null;
    stopElapsedTimer();

    // Show the daily budget (total across platforms), not the per-platform rail —
    // todayCount is a cross-platform total, so pairing it with the per-platform cap
    // read as "12 / 20" even when the real daily budget was 50. dailyLimit is the
    // honest denominator; both now come from the backend (app/db/subscriptions.py).
    const limit = status.dailyLimit || status.limitPerPlatform || 20;
    const today = status.todayCount || 0;
    $("limit-text").textContent = `${today} / ${limit} today`;
    $("btn-start").disabled = !isConnected;
  }

  renderProcess();
}

// ---------------------------------------------------------------------------
// Start campaign — with profile completeness check
// ---------------------------------------------------------------------------

// Real version from the manifest — the header used to hardcode "v1.3" forever.
try { document.getElementById("hd-version").textContent = "v" + chrome.runtime.getManifest().version; } catch (e) {}

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
    addLog("No resume on server — will use Indeed profile resume if available", "");
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
  } else if (res?.error === "onboarding_incomplete") {
    addLog("Finish your profile setup on hiredrop.io first — the quiz collects the data we fill applications with.", "err");
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
  renderProcess();
}

// ---------------------------------------------------------------------------
// Dashboard button
// ---------------------------------------------------------------------------

$("btn-dash").addEventListener("click", () => {
  chrome.tabs.create({ url: CONFIG.DASHBOARD_URL + "/dashboard" });
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
        renderProcess();
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
  renderProcess();
  const connected = await checkConnection();
  if (connected) await Promise.all([loadProfile(), loadStatus()]);
  startPolling();
})();
