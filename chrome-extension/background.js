// HireDrop service worker
// All API communication, campaign state, and tab management

importScripts("config.js");

// ---------------------------------------------------------------------------
// Auth helpers
// ---------------------------------------------------------------------------

async function getAuthToken() {
  const data = await chrome.storage.local.get("supabase_token");
  return data.supabase_token || null;
}

// ---------------------------------------------------------------------------
// Token refresh — calls Supabase directly, no backend needed
// ---------------------------------------------------------------------------

async function refreshAccessToken() {
  const data = await chrome.storage.local.get(["supabase_refresh_token", "supabase_url"]);
  const refreshToken = data.supabase_refresh_token;
  if (!refreshToken) return null;

  // CONFIG.SUPABASE_URL must be set in config.js
  const supabaseUrl = CONFIG.SUPABASE_URL;
  if (!supabaseUrl) return null;

  try {
    const res = await fetch(`${supabaseUrl}/auth/v1/token?grant_type=refresh_token`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "apikey": CONFIG.SUPABASE_ANON_KEY },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!res.ok) return null;
    const json = await res.json();
    if (!json.access_token) return null;
    await chrome.storage.local.set({
      supabase_token: json.access_token,
      supabase_refresh_token: json.refresh_token || refreshToken,
    });
    return json.access_token;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function apiGet(path, { retry = true } = {}) {
  const token = await getAuthToken();
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  const res = await fetch(`${CONFIG.API_BASE}${CONFIG.API_V1}${path}`, { headers });
  if (res.status === 401) {
    if (retry) {
      const newToken = await refreshAccessToken();
      if (newToken) return apiGet(path, { retry: false });
    }
    await chrome.storage.local.remove(["supabase_token", "supabase_refresh_token"]);
    chrome.runtime.sendMessage({ type: "AUTH_EXPIRED" }).catch(() => {});
    throw new Error("Session expired — please reconnect at hiredrop.io/extension/connect");
  }
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  return res.json();
}

async function apiPost(path, body, { retry = true } = {}) {
  const token = await getAuthToken();
  const headers = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
  const res = await fetch(`${CONFIG.API_BASE}${CONFIG.API_V1}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (res.status === 401) {
    if (retry) {
      const newToken = await refreshAccessToken();
      if (newToken) return apiPost(path, body, { retry: false });
    }
    await chrome.storage.local.remove(["supabase_token", "supabase_refresh_token"]);
    chrome.runtime.sendMessage({ type: "AUTH_EXPIRED" }).catch(() => {});
    throw new Error("Session expired — please reconnect at hiredrop.io/extension/connect");
  }
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  return res.json();
}

// ---------------------------------------------------------------------------
// Profile — fetch from API, cache in chrome.storage.local
// ---------------------------------------------------------------------------

async function fetchAndCacheProfile() {
  try {
    const profile = await apiGet("/profile");
    await chrome.storage.local.set({
      profile,
      profileCachedAt: Date.now(),
    });
    return profile;
  } catch (err) {
    // Network down or auth error — return whatever we have cached
    const data = await chrome.storage.local.get("profile");
    if (data.profile) return data.profile;
    throw err;
  }
}

async function getCachedProfile() {
  const data = await chrome.storage.local.get(["profile", "profileCachedAt"]);
  if (
    data.profile &&
    data.profileCachedAt &&
    Date.now() - data.profileCachedAt < CONFIG.CACHE_TTL_MS
  ) {
    return data.profile;
  }
  return fetchAndCacheProfile();
}

// ---------------------------------------------------------------------------
// Install / Startup — seed the cache
// ---------------------------------------------------------------------------

const LIMIT_PER_PLATFORM = 50;

chrome.runtime.onInstalled.addListener(async () => {
  await chrome.storage.local.set({
    campaignRunning: false,
    campaignFilters: {},
    campaignStartedAt: null,
    campaignTabId: null,
    todayCount: 0,
    platformCounts: {},
    todayDate: new Date().toISOString().slice(0, 10),
    currentJob: null,
  });
  await fetchAndCacheProfile().catch(() => {});
  updateBadge();
});

chrome.runtime.onStartup.addListener(async () => {
  const data = await chrome.storage.local.get("todayDate");
  const today = new Date().toISOString().slice(0, 10);
  if (data.todayDate !== today) {
    await chrome.storage.local.set({ todayCount: 0, platformCounts: {}, todayDate: today });
  }
  await fetchAndCacheProfile().catch(() => {});
  updateBadge();
});

// ---------------------------------------------------------------------------
// Badge
// ---------------------------------------------------------------------------

async function updateBadge() {
  const data = await chrome.storage.local.get(["todayCount", "campaignRunning"]);
  const count = data.todayCount || 0;
  const running = data.campaignRunning || false;

  chrome.action.setBadgeText({ text: count > 0 ? String(count) : "" });
  chrome.action.setBadgeBackgroundColor({ color: running ? "#10b981" : "#6c5ce7" });
}

// Refresh badge every minute
chrome.alarms.create("badge-refresh", { periodInMinutes: 1 });

// SW keepalive: MV3 service workers die after ~30 s of inactivity. During page
// navigation the content script is silent for several seconds. This 20-second alarm
// ensures the SW (and any active chrome.debugger session) survives across page loads.
chrome.alarms.create("sw-keepalive", { periodInMinutes: 0.33 }); // ~20 s

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "badge-refresh") updateBadge();
  // sw-keepalive: no-op — waking the SW is enough
});

// ---------------------------------------------------------------------------
// Screenshot streaming — captures the automation tab via Chrome DevTools Protocol.
// Works in background without the window needing to be focused.
// Triggered by CAPTURE_SCREENSHOT messages from content.js every 2.5 s.
// ---------------------------------------------------------------------------

// Tracks the tabId for which THIS extension holds an active debugger session.
// Resets to null when the SW restarts (Chrome auto-detaches on SW death) or on detach.
let ownedDebuggerTabId = null;

async function sendScreenshot(tabId) {
  // Attach if we don't already own a session on this tab.
  if (ownedDebuggerTabId !== tabId) {
    try {
      await chrome.debugger.attach({ tabId }, "1.3");
      ownedDebuggerTabId = tabId;
    } catch (e) {
      const msg = (e?.message || "").toLowerCase();
      // Chrome reports "debugger is already attached" when OUR extension already holds
      // the session (e.g. previous SW cycle kept it alive). Any OTHER wording means
      // another debugger (DevTools / Playwright) owns the tab — bail out silently.
      if (msg.includes("already attached") && !msg.includes("another")) {
        ownedDebuggerTabId = tabId; // our session is still alive
      } else {
        return; // another debugger owns this tab — skip this frame
      }
    }
  }

  try {
    const result = await chrome.debugger.sendCommand(
      { tabId },
      "Page.captureScreenshot",
      { format: "jpeg", quality: 40 }
    );
    if (result?.data) {
      await apiPost("/campaign/screenshot", {
        screenshot: `data:image/jpeg;base64,${result.data}`,
      });
    }
  } catch {
    // Tab navigating or session lost — force re-check on next frame.
    ownedDebuggerTabId = null;
  }
}

// Auto-reattach debugger if it gets detached while campaign is still running.
// Reason "canceled_by_user" means the user opened Chrome DevTools — don't fight it.
chrome.debugger.onDetach.addListener(async (source, reason) => {
  if (source.tabId === ownedDebuggerTabId) {
    ownedDebuggerTabId = null;
  }

  if (reason === "canceled_by_user") return;

  const { campaignRunning, campaignTabId } = await chrome.storage.local.get([
    "campaignRunning",
    "campaignTabId",
  ]);

  if (campaignRunning && source.tabId === campaignTabId) {
    setTimeout(async () => {
      try {
        await chrome.debugger.attach({ tabId: campaignTabId }, "1.3");
        ownedDebuggerTabId = campaignTabId;
      } catch {}
    }, 2000);
  }
});

// ---------------------------------------------------------------------------
// Activity log
// ---------------------------------------------------------------------------

async function addToActivityLog(text, cls) {
  const { activity_log } = await chrome.storage.local.get("activity_log");
  const logs = activity_log || [];
  logs.unshift({
    time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    text,
    cls: cls || "",
  });
  if (logs.length > 50) logs.length = 50;
  await chrome.storage.local.set({ activity_log: logs });

  // Best-effort mirror to backend (Phase 4.2). Never blocks; if the user
  // is logged out or the API is down, the local log still works.
  try {
    const level = cls === "err" ? "error" : (cls === "warn" ? "warn" : "info");
    await apiPost("/activity", { message: text, level, phase: "extension" });
  } catch {}
}

// ---------------------------------------------------------------------------
// Indeed search URL builder
// ---------------------------------------------------------------------------

function buildIndeedUrl(keywords, location, jobType) {
  const params = new URLSearchParams();
  if (keywords && keywords.length) params.set("q", keywords.join(" "));
  const locMap = { usa: "United States", remote: "remote", europe: "" };
  const loc = locMap[location] !== undefined ? locMap[location] : location;
  if (loc) params.set("l", loc);
  const jtMap = { "full-time": "fulltime", "part-time": "parttime", contract: "contract" };
  if (jobType && jtMap[jobType]) params.set("jt", jtMap[jobType]);
  params.set("iafilter", "1");
  return `https://www.indeed.com/jobs?${params.toString()}`;
}

// ---------------------------------------------------------------------------
// Message handler
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handleMessage(msg, sender)
    .then(sendResponse)
    .catch((err) => sendResponse({ error: err.message }));
  return true;
});

async function handleMessage(msg, sender) {
  switch (msg.type) {

    // ----- Auth -----
    case "STORE_TOKEN": {
      const storePayload = { supabase_token: msg.token };
      if (msg.refresh_token) storePayload.supabase_refresh_token = msg.refresh_token;
      await chrome.storage.local.set(storePayload);
      // Refresh profile cache with new token
      await fetchAndCacheProfile().catch(() => {});
      return { stored: true };
    }
    case "GET_AUTH_STATUS": {
      const data = await chrome.storage.local.get("supabase_token");
      return { authenticated: !!data.supabase_token };
    }
    case "LOGOUT": {
      await chrome.storage.local.remove(["supabase_token", "supabase_refresh_token", "profile", "profileCachedAt"]);
      return { loggedOut: true };
    }

    // ----- Profile -----
    case "GET_PROFILE":
      return await getCachedProfile();

    case "REFRESH_PROFILE":
      return await fetchAndCacheProfile();

    case "CHECK_CONNECTION": {
      try {
        await apiGet("/stats");
        return { connected: true };
      } catch {
        return { connected: false };
      }
    }

    case "GET_RESUME_URL": {
      try {
        const r = await apiGet("/profile/resume/url");
        return { url: r.url, expires_in: r.expires_in };
      } catch (err) {
        return { error: err.message };
      }
    }

    case "GET_SELECTORS": {
      try {
        const platform = msg.platform || "indeed";
        const r = await apiGet(`/extension/selectors/${platform}`);
        return { selectors: r.selectors, version: r.version };
      } catch (err) {
        return { error: err.message };
      }
    }

    // ----- Campaign start -----
    case "START_CAMPAIGN": {
      const profile = await getCachedProfile();
      const filters = msg.filters || {
        keywords: profile.keywords || [],
        platforms: profile.platforms || ["indeed"],
        location: profile.location || "",
        job_type: profile.job_type || "",
      };

      try {
        await apiPost("/campaign/start", filters);
      } catch {
        // Continue even if server is down
      }

      const targetUrl = buildIndeedUrl(filters.keywords, filters.location, filters.job_type);

      // Reuse the existing automation window if it's still open — avoids spawning
      // a new window on every campaign start. Chrome shares session cookies across
      // all windows in the same profile, so the dedicated window is already logged
      // into Indeed. A dedicated window keeps captureVisibleTab reliable (always
      // the active/focused window) for the live dashboard preview.
      let tab;
      const prevData = await chrome.storage.local.get(["campaignWindowId", "campaignTabId"]);
      let reusingWindow = false;
      if (prevData.campaignWindowId) {
        try {
          const win = await chrome.windows.get(prevData.campaignWindowId, { populate: true });
          if (win && win.tabs && win.tabs.length > 0) {
            tab = win.tabs[0];
            await chrome.tabs.update(tab.id, { url: "https://www.indeed.com/", active: true });
            await chrome.windows.update(prevData.campaignWindowId, { focused: true });
            reusingWindow = true;
          }
        } catch {
          // Window was closed — create a new one below
        }
      }

      if (!reusingWindow) {
        const win = await chrome.windows.create({
          url: "https://www.indeed.com/",
          focused: true,
          width: 1280,
          height: 900,
        });
        tab = win.tabs[0];
      }

      const tabInfo = await chrome.tabs.get(tab.id);

      await chrome.storage.local.set({
        campaignRunning: true,
        campaignFilters: filters,
        campaignTargetUrl: targetUrl,
        campaignStartedAt: new Date().toISOString(),
        campaignTabId: tab.id,
        campaignWindowId: tabInfo.windowId,
        currentJob: null,
        campaignWarmedUp: false,
        processedJobKeys: [],
        processedPageStarts: [0],
      });

      updateBadge();
      return { started: true, tabId: tab.id, windowId: tabInfo.windowId };
    }

    // ----- Screenshot capture (triggered by content.js every 2.5 s) -----
    case "CAPTURE_SCREENSHOT": {
      const { campaignRunning, campaignTabId } = await chrome.storage.local.get([
        "campaignRunning",
        "campaignTabId",
      ]);
      if (campaignRunning && campaignTabId) {
        await sendScreenshot(campaignTabId);
      }
      return { ok: true };
    }

    // ----- Campaign stop -----
    case "STOP_CAMPAIGN": {
      const stopData = await chrome.storage.local.get(["campaignTabId", "campaignWindowId"]);

      // Clear running state first so the onDetach listener won't auto-reattach
      await chrome.storage.local.set({
        campaignRunning: false,
        campaignTabId: null,
        campaignWindowId: null,
        currentJob: null,
      });

      // Detach debugger now that we've marked campaign as stopped
      if (stopData.campaignTabId) {
        try { await chrome.debugger.detach({ tabId: stopData.campaignTabId }); } catch {}
        ownedDebuggerTabId = null;
      }

      try {
        if (stopData.campaignTabId) {
          chrome.tabs.sendMessage(stopData.campaignTabId, { type: "CAMPAIGN_STOPPED" }).catch(() => {});
        }
      } catch {}

      try {
        await apiPost("/campaign/stop", {});
      } catch {}

      updateBadge();
      return { stopped: true };
    }

    // ----- Application saved (from content.js) -----
    case "APPLICATION_SAVED": {
      const appData = msg.data;
      if (!appData || !appData.job_title) {
        return { error: "Missing application data" };
      }

      let serverResult = null;
      try {
        serverResult = await apiPost("/applications/save", {
          job_title: appData.job_title,
          company: appData.company || "",
          platform: appData.platform || "indeed",
          job_url: appData.job_url || "",
          cover_letter: appData.cover_letter || "",
          status: appData.status || "applied",
        });
      } catch (err) {
        return { saved: false, error: err.message };
      }

      const storageData = await chrome.storage.local.get(["todayCount", "platformCounts", "todayDate"]);
      const today = new Date().toISOString().slice(0, 10);
      let totalCount = storageData.todayCount || 0;
      let platformCounts = storageData.platformCounts || {};
      if (storageData.todayDate !== today) {
        totalCount = 0;
        platformCounts = {};
      }
      totalCount++;
      const platform = appData.platform || "indeed";
      platformCounts[platform] = (platformCounts[platform] || 0) + 1;
      await chrome.storage.local.set({
        todayCount: totalCount,
        platformCounts,
        todayDate: today,
        currentJob: {
          title: appData.job_title,
          company: appData.company,
          savedAt: new Date().toISOString(),
        },
      });

      updateBadge();
      return { saved: true, todayCount: totalCount, platformCount: platformCounts[platform], job_id: serverResult?.job_id };
    }

    // ----- Cover letter generation -----
    case "GENERATE_COVER_LETTER": {
      const job = msg.data;
      if (!job || !job.job_title) {
        return { error: "Missing job data" };
      }

      const profile = await getCachedProfile();
      let letter = "";
      let source = "";

      try {
        const result = await Promise.race([
          apiPost("/tools/cover-letter-preview", {
            keywords: [job.job_title, job.company].filter(Boolean).join(", "),
            style: profile?.writing_style || "",
            job_description: job.description || "",
          }),
          new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 10000)),
        ]);
        if (result && result.letter) {
          letter = result.letter;
          source = "AI";
        }
      } catch {}

      if (!letter) {
        const name = [profile?.name, profile?.last_name].filter(Boolean).join(" ") || "Applicant";
        const skills = (profile?.keywords || []).join(", ") || "relevant skills";
        letter =
          `Dear ${job.company || "Hiring"} Hiring Team,\n\n` +
          `I am excited to apply for the ${job.job_title} position. ` +
          `With my background in ${skills}, I am confident I can contribute to your team.\n\n` +
          `Best regards,\n${name}`;
        source = "fallback";
      }

      await addToActivityLog(
        source === "AI"
          ? `Cover letter generated for ${job.job_title} @ ${job.company || "company"}`
          : `Cover letter fallback used for ${job.job_title} (API unavailable)`,
        source === "AI" ? "ok" : ""
      );

      return { letter, source, job_title: job.job_title, company: job.company };
    }

    // ----- Step failed -----
    case "STEP_FAILED":
      return { ok: true };

    // ----- Backend log (key events from content.js → Campaign Live feed) -----
    case "LOG_BACKEND": {
      const level = msg.level || "info";
      try {
        await apiPost("/activity", { message: msg.text, level, phase: msg.phase || "extension" });
      } catch {}
      await addToActivityLog(msg.text, level === "error" ? "err" : level === "ok" ? "ok" : "");
      return { ok: true };
    }

    // ----- Detection tripped (Phase 5.5) -----
    case "DETECTION_TRIPPED": {
      const data = msg.data || {};
      // Mirror to backend activity log with explicit error level.
      try {
        await apiPost("/activity", {
          message: `Detection tripped (${data.signal}) on ${data.url}`,
          level: "error",
          phase: "detection",
          metadata: { signal: data.signal, page_phase: data.phase, url: data.url },
        });
      } catch {}
      // Local log so the popup shows it without waiting for a refresh.
      await addToActivityLog(`⚠️ Detection on Indeed (${data.signal}) — campaign paused`, "err");
      // System notification so the user sees this even if the popup is closed.
      try {
        await chrome.notifications.create({
          type: "basic",
          iconUrl: "icons/icon128.png",
          title: "HireDrop paused",
          message: `Indeed flagged the session (${data.signal}). Wait a few hours before resuming.`,
        });
      } catch {}
      return { handled: true };
    }

    // ----- Status (popup polls this) -----
    case "GET_STATUS": {
      const data = await chrome.storage.local.get([
        "campaignRunning",
        "campaignFilters",
        "campaignStartedAt",
        "todayCount",
        "platformCounts",
        "todayDate",
        "currentJob",
      ]);

      const today = new Date().toISOString().slice(0, 10);
      let todayCount = data.todayCount || 0;
      let platformCounts = data.platformCounts || {};
      if (data.todayDate !== today) {
        todayCount = 0;
        platformCounts = {};
        await chrome.storage.local.set({ todayCount: 0, platformCounts: {}, todayDate: today });
      }

      let serverStats = null;
      try {
        serverStats = await apiGet("/stats");
      } catch {}

      return {
        campaignRunning: data.campaignRunning || false,
        filters: data.campaignFilters || {},
        startedAt: data.campaignStartedAt || null,
        todayCount,
        platformCounts,
        limitPerPlatform: LIMIT_PER_PLATFORM,
        currentJob: data.currentJob || null,
        totalJobs: serverStats?.total_jobs || 0,
        totalApplications: serverStats?.total_applications || 0,
      };
    }

    // ----- CAPTCHA auto-solve via Railway backend proxy -----
    // API key (CAPSOLVER_API_KEY) lives only in Railway env — never in the extension.
    case "SOLVE_CAPTCHA": {
      try {
        const result = await Promise.race([
          apiPost("/captcha/solve", {
            type: msg.captchaType || "recaptchav2",
            url: msg.url || "",
            sitekey: msg.sitekey || "",
          }),
          new Promise((_, reject) =>
            setTimeout(() => reject(new Error("CAPTCHA solve timeout")), 130000)
          ),
        ]);
        return { token: result.token };
      } catch (err) {
        return { error: err.message };
      }
    }

    default:
      return { error: `Unknown message type: ${msg.type}` };
  }
}

// ---------------------------------------------------------------------------
// Tab closed — stop campaign if campaign tab is closed
// ---------------------------------------------------------------------------

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const data = await chrome.storage.local.get(["campaignTabId", "campaignRunning"]);
  if (data.campaignRunning && data.campaignTabId === tabId) {
    await chrome.storage.local.set({
      campaignRunning: false,
      campaignTabId: null,
      currentJob: null,
    });
    try { await apiPost("/campaign/stop", {}); } catch {}
    updateBadge();
  }
});
