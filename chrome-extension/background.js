// HireDrop service worker
// All API communication, campaign state, and tab management

importScripts("config.js");

// ---------------------------------------------------------------------------
// Auth helpers
// ---------------------------------------------------------------------------

async function getAuthToken() {
  // Prefer the durable extension API key (Approach A) — it never expires and doesn't
  // depend on the dashboard tab. Fall back to the dashboard-pushed Supabase token during
  // the transition / if no key has been issued yet.
  const data = await chrome.storage.local.get(["extension_api_key", "supabase_token"]);
  return data.extension_api_key || data.supabase_token || null;
}

// Self-bootstrap the durable key: any connected user has a (dashboard-pushed) Supabase
// token but a legacy install may have no key yet. The first time we see a token and no
// key, mint one with the token so the extension becomes durable — no re-connect needed.
// After this, getAuthToken() uses the key and the token push becomes irrelevant.
async function ensureExtensionKey() {
  const { extension_api_key, supabase_token } = await chrome.storage.local.get([
    "extension_api_key",
    "supabase_token",
  ]);
  if (extension_api_key || !supabase_token) return;
  try {
    const res = await fetch(`${CONFIG.API_BASE}${CONFIG.API_V1}/extension/issue-key`, {
      method: "POST",
      headers: { Authorization: `Bearer ${supabase_token}` },
    });
    if (res.ok) {
      const data = await res.json();
      if (data && data.key) await chrome.storage.local.set({ extension_api_key: data.key });
    }
  } catch { /* try again on the next token push */ }
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

// On 401 we try a one-time refresh, but we NEVER wipe the stored token anymore.
// The dashboard (ExtensionTokenSync) is the source of truth — it re-pushes a fresh
// access token every minute while it's open. A transient 401 in the gap between
// pushes used to call chrome.storage.local.remove() + AUTH_EXPIRED, which flipped the
// popup to "Connect Your Account" and killed the live preview mid-campaign even though
// a fresh token was seconds away. Keeping the token means the next request (or the next
// push) just works. The popup still detects a genuinely-dead session via its live
// CHECK_CONNECTION probe, so we don't lose the "reconnect" signal when it's real.
async function apiGet(path, { retry = true } = {}) {
  const token = await getAuthToken();
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  const res = await fetch(`${CONFIG.API_BASE}${CONFIG.API_V1}${path}`, { headers });
  if (res.status === 401) {
    if (retry) {
      const newToken = await refreshAccessToken();
      if (newToken) return apiGet(path, { retry: false });
    }
    throw new Error("API 401 (token stale — dashboard will refresh it)");
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
    throw new Error("API 401 (token stale — dashboard will refresh it)");
  }
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  return res.json();
}

// ---------------------------------------------------------------------------
// Profile — fetch from API, cache in chrome.storage.local
// ---------------------------------------------------------------------------

// The backend /profile has no email (email lives in Supabase auth, not the
// profile table) — so ATS forms with a blank email field never got filled. The
// user's email IS in the stored JWT's `email` claim; decode it and backfill.
async function fetchAndCacheProfile() {
  try {
    const profile = await apiGet("/profile");
    if (!profile.email) {
      try {
        const { supabase_token } = await chrome.storage.local.get("supabase_token");
        if (supabase_token) {
          const payload = JSON.parse(atob(supabase_token.split(".")[1]));
          if (payload.email) profile.email = payload.email;
        }
      } catch { /* token missing/malformed — leave email empty */ }
    }
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

// Create alarms only if they don't exist — SW restarts must not reset timers.
// Calling chrome.alarms.create with the same name resets the alarm to start NOW,
// so sw-keepalive (every 20s) would perpetually reset ext-ping before it fires.
(async () => {
  const [badge, ping, keepalive] = await Promise.all([
    chrome.alarms.get("badge-refresh"),
    chrome.alarms.get("ext-ping"),
    chrome.alarms.get("sw-keepalive"),
  ]);
  if (!badge)     chrome.alarms.create("badge-refresh", { periodInMinutes: 1 });
  if (!ping)      chrome.alarms.create("ext-ping",      { periodInMinutes: 1 });
  if (!keepalive) chrome.alarms.create("sw-keepalive",  { periodInMinutes: 0.33 });
})();

async function sendExtensionPing() {
  // Use a raw fetch instead of apiPost so a 401 here does NOT clear the stored token.
  // The ping is telemetry-only; auth errors should be silent.
  try {
    const token = await getAuthToken();
    if (!token) return; // nothing to ping with yet
    const data = await chrome.storage.local.get([
      "campaignRunning", "todayCount", "campaignWindowId", "todayDate",
    ]);
    const today = new Date().toISOString().slice(0, 10);
    const todayCount = data.todayDate === today ? (data.todayCount || 0) : 0;

    let windowVisible = false;
    if (data.campaignWindowId) {
      try {
        const win = await chrome.windows.get(data.campaignWindowId);
        windowVisible = win.state === "normal" || win.state === "maximized";
      } catch {}
    }

    const res = await fetch(`${CONFIG.API_BASE}${CONFIG.API_V1}/extension/ping`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        campaign_running: !!data.campaignRunning,
        today_count: todayCount,
        window_visible: windowVisible,
        version: chrome.runtime.getManifest().version,
      }),
    });
    // Backend-authoritative Stop: if the dashboard stopped the campaign but our
    // postMessage stop was dropped (orphaned content script), the flag would stay set
    // and the extension keep applying. Honor the backend's flag as source of truth.
    try {
      const j = await res.json();
      if (j && j.should_run === false && data.campaignRunning) {
        await chrome.storage.local.set({ campaignRunning: false, currentJob: null });
        const { campaignTabId } = await chrome.storage.local.get("campaignTabId");
        if (campaignTabId) detachDebugger(campaignTabId).catch(() => {});
        updateBadge();
      }
    } catch {}
  } catch {}
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "badge-refresh") updateBadge();
  if (alarm.name === "ext-ping") sendExtensionPing();
  // sw-keepalive: no-op — waking the SW is enough
});

// ---------------------------------------------------------------------------
// Screenshot streaming — captures the automation tab via Chrome DevTools Protocol.
// CDP Page.captureScreenshot renders from the tab's compositor, so it works even
// when the automation window is hidden/minimized/behind others — unlike
// captureVisibleTab, which needs the window visible and on top. This lets us keep
// the automation window out of the user's way while still streaming the live view.
// Triggered by CAPTURE_SCREENSHOT messages from content.js.
// ---------------------------------------------------------------------------

// Ensure the debugger is attached to the automation tab. Attaching twice throws
// "Already attached", which we treat as success. Returns false if attach failed
// for any other reason (e.g. the user has DevTools open on that tab).
async function ensureDebuggerAttached(tabId) {
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
    return true;
  } catch (e) {
    const msg = String((e && e.message) || e || "");
    return msg.toLowerCase().includes("already attached");
  }
}

async function detachDebugger(tabId) {
  try {
    await chrome.debugger.detach({ tabId });
  } catch { /* not attached — fine */ }
}

async function sendScreenshot(tabId) {
  // GDPR: only capture Indeed tabs.
  try {
    const tab = await chrome.tabs.get(tabId);
    const allowed = tab.url && (
      tab.url.startsWith("https://www.indeed.com/") ||
      tab.url.startsWith("https://smartapply.indeed.com/")
    );
    if (!allowed) return;
  } catch {
    return;
  }

  if (!(await ensureDebuggerAttached(tabId))) return;

  try {
    const result = await chrome.debugger.sendCommand({ tabId }, "Page.captureScreenshot", {
      format: "jpeg",
      quality: 40,
    });
    if (result && result.data) {
      const dataUrl = "data:image/jpeg;base64," + result.data;
      // Safe upload: raw fetch so a 401 never clears the stored token.
      // A missed frame is fine; losing auth is not.
      const tok = await getAuthToken();
      if (tok) {
        fetch(`${CONFIG.API_BASE}${CONFIG.API_V1}/campaign/screenshot`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${tok}` },
          body: JSON.stringify({ screenshot: dataUrl }),
        }).catch(() => {});
      }
    }
  } catch { /* capture failed — skip frame */ }
}

// Capture the ACTIVE Indeed tab of the automation window (follows the apply flow
// as it navigates to smartapply.indeed.com). Shared by the message handler and the
// service-worker capture loop.
async function captureActiveAutomationTab() {
  const { campaignRunning, campaignTabId, campaignWindowId } = await chrome.storage.local.get([
    "campaignRunning",
    "campaignTabId",
    "campaignWindowId",
  ]);
  if (!campaignRunning) return false;

  let tabId = campaignTabId;
  if (campaignWindowId != null) {
    try {
      const [active] = await chrome.tabs.query({ windowId: campaignWindowId, active: true });
      if (active && active.id != null && active.url &&
          (active.url.startsWith("https://www.indeed.com/") ||
           active.url.startsWith("https://smartapply.indeed.com/"))) {
        tabId = active.id;
        if (tabId !== campaignTabId) {
          chrome.storage.local.set({ campaignTabId: tabId }).catch(() => {});
        }
      }
    } catch { /* window gone — fall through to campaignTabId */ }
  }
  if (tabId != null) await sendScreenshot(tabId);
  return campaignRunning;
}

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

function buildZipRecruiterUrl(keywords, location, jobType) {
  const params = new URLSearchParams();
  if (keywords && keywords.length) params.set("search", keywords.join(" "));
  const locMap = { usa: "United States", remote: "Remote", europe: "" };
  const loc = locMap[location] !== undefined ? locMap[location] : (location || "");
  if (loc) params.set("location", loc);
  const jtMap = { "full-time": "full_time", "part-time": "part_time", contract: "contract" };
  if (jobType && jtMap[jobType]) params.set("employment_type[]", jtMap[jobType]);
  // /candidate/search requires login; /jobs-search works without auth
  return `https://www.ziprecruiter.com/jobs-search?${params.toString()}`;
}

function buildPlatformUrl(platform, keywords, location, jobType) {
  if (platform === "ziprecruiter") return buildZipRecruiterUrl(keywords, location, jobType);
  return buildIndeedUrl(keywords, location, jobType);
}

function platformHomeUrl(platform) {
  if (platform === "ziprecruiter") return "https://www.ziprecruiter.com/";
  return "https://www.indeed.com/";
}

// Platforms the extension actually auto-applies on. Everything else in
// filters.platforms is a discovery-only source (scraped, applied to externally).
const AUTO_APPLY_PLATFORMS = ["indeed", "ziprecruiter"];

// The campaign window targets the first auto-apply platform in the filter list.
// Selecting by membership (not platforms[0]) is robust to discovery platforms
// appearing first in the array.
function pickPrimaryPlatform(platforms) {
  const list = platforms || [];
  return list.find((p) => AUTO_APPLY_PLATFORMS.includes(p)) || "indeed";
}

// Unified auth page per platform (enter email → logs in or creates an account),
// so a brand-new user can register from here. URLs verified live 2026-07-10.
function platformLoginUrl(platform) {
  if (platform === "ziprecruiter") return "https://www.ziprecruiter.com/authn/login?realm=candidates";
  return "https://secure.indeed.com/auth";
}

function platformLabel(platform) {
  if (platform === "ziprecruiter") return "ZipRecruiter";
  return "Indeed";
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
      // Auto-upgrade to a durable key the first time we have a token but none yet.
      ensureExtensionKey().catch(() => {});
      // Use token directly from message (not re-read from storage) to avoid storage-read race
      let pingStatus = "not_attempted";
      try {
        const directToken = msg.token;
        if (!directToken) {
          pingStatus = "no_token_in_msg";
        } else {
          const res = await fetch(`${CONFIG.API_BASE}${CONFIG.API_V1}/extension/ping`, {
            method: "POST",
            headers: { "Content-Type": "application/json", Authorization: `Bearer ${directToken}` },
            body: JSON.stringify({
              campaign_running: false,
              today_count: 0,
              window_visible: false,
              version: chrome.runtime.getManifest().version,
            }),
          });
          pingStatus = String(res.status);
        }
      } catch (e) {
        pingStatus = "error:" + e.message;
      }
      fetchAndCacheProfile().catch(() => {});
      return { stored: true, ping_status: pingStatus };
    }
    // Durable extension API key (Approach A). Stored once at connect; used for all
    // backend calls thereafter — no expiry, no dashboard dependency.
    case "STORE_KEY": {
      if (!msg.key) return { stored: false };
      await chrome.storage.local.set({ extension_api_key: msg.key });
      let pingStatus = "not_attempted";
      try {
        const res = await fetch(`${CONFIG.API_BASE}${CONFIG.API_V1}/extension/ping`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${msg.key}` },
          body: JSON.stringify({
            campaign_running: false,
            today_count: 0,
            window_visible: false,
            version: chrome.runtime.getManifest().version,
          }),
        });
        pingStatus = String(res.status);
      } catch (e) {
        pingStatus = "error:" + e.message;
      }
      fetchAndCacheProfile().catch(() => {});
      return { stored: true, ping_status: pingStatus };
    }
    case "GET_AUTH_STATUS": {
      const data = await chrome.storage.local.get("supabase_token");
      return { authenticated: !!data.supabase_token };
    }
    case "LOGOUT": {
      await chrome.storage.local.remove(["extension_api_key", "supabase_token", "supabase_refresh_token", "profile", "profileCachedAt"]);
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
        const qs = msg.jobUrl ? `?job_url=${encodeURIComponent(msg.jobUrl)}` : "";
        const r = await apiGet(`/profile/resume/url/best${qs}`);
        return { url: r.url, expires_in: r.expires_in, type: r.type };
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
      const raw = msg.filters || {};
      // Always merge with profile so partial/empty filters still work
      const filters = {
        keywords: (raw.keywords && raw.keywords.length) ? raw.keywords : (profile.keywords || []),
        platforms: (raw.platforms && raw.platforms.length) ? raw.platforms : (profile.platforms || ["indeed"]),
        location: raw.location || profile.location || "",
        job_type: raw.job_type || profile.job_type || "",
      };

      // Pick the auto-apply platform this campaign targets (first in the filter list)
      const primaryPlatform = pickPrimaryPlatform(filters.platforms);

      // Pre-flight: don't launch a doomed campaign if we already KNOW the user is
      // logged out of the target platform. Open the login/sign-up page instead and
      // tell the dashboard why. (Unknown/connected → proceed; the in-page login-wall
      // guard still catches a stale "connected" once the window loads.)
      {
        const conns = (await chrome.storage.local.get("platformConnections")).platformConnections || {};
        if (conns[primaryPlatform]?.status === "logged_out") {
          chrome.tabs.create({ url: platformLoginUrl(primaryPlatform) }).catch(() => {});
          return {
            started: false,
            error: "not_connected",
            platform: primaryPlatform,
            message: `Sign into ${platformLabel(primaryPlatform)} first — we opened the login page. Create an account or log in, then start the campaign.`,
          };
        }
      }

      try {
        await apiPost("/campaign/start", filters);
      } catch {
        // Continue even if server is down
      }

      const targetUrl = buildPlatformUrl(primaryPlatform, filters.keywords, filters.location, filters.job_type);
      const homeUrl = platformHomeUrl(primaryPlatform);

      // Automation runs in a dedicated background window — minimized so it doesn't
      // steal focus from the user's browser. Screenshots are captured via CDP
      // Automation runs in a dedicated window that opens behind the current one
      // (focused: false). We keep it visible — captureVisibleTab requires the
      // window to be in normal state and rendering. Minimizing or moving it
      // off-screen breaks screenshot capture.
      let tab;
      const prevData = await chrome.storage.local.get(["campaignWindowId", "campaignTabId"]);
      let reusingWindow = false;
      if (prevData.campaignWindowId) {
        try {
          const win = await chrome.windows.get(prevData.campaignWindowId, { populate: true });
          if (win && win.tabs && win.tabs.length > 0) {
            tab = win.tabs[0];
            await chrome.tabs.update(tab.id, { url: homeUrl, active: true });
            // Restore to normal state in case user minimized it
            chrome.windows.update(prevData.campaignWindowId, { state: "normal" }).catch(() => {});
            reusingWindow = true;
          }
        } catch {
          // Window was closed — create a new one below
        }
      }

      if (!reusingWindow) {
        const win = await chrome.windows.create({
          url: homeUrl,
          focused: false,
          width: 1280,
          height: 900,
        });
        tab = win.tabs[0];
        // Don't minimize — captureVisibleTab only works on visible (normal-state) windows
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

    // ----- Screenshot capture (triggered by content.js) -----
    case "CAPTURE_SCREENSHOT": {
      await captureActiveAutomationTab();
      return { ok: true };
    }

    // ----- Platform account connection -----
    // content.js reports login state whenever the user is on a platform page.
    case "PLATFORM_AUTH": {
      if (msg.platform && msg.status && msg.status !== "unknown") {
        const conns = (await chrome.storage.local.get("platformConnections")).platformConnections || {};
        conns[msg.platform] = { status: msg.status, checkedAt: new Date().toISOString() };
        await chrome.storage.local.set({ platformConnections: conns });
      }
      return { ok: true };
    }

    // Dashboard reads connection status (via ping.js bridge) to render "connect" chips.
    case "GET_PLATFORM_CONNECTIONS": {
      const conns = (await chrome.storage.local.get("platformConnections")).platformConnections || {};
      return { ok: true, connections: conns };
    }

    // Dashboard asks us to open a platform's login/sign-up page in a new tab.
    case "OPEN_PLATFORM_LOGIN": {
      const platform = AUTO_APPLY_PLATFORMS.includes(msg.platform) ? msg.platform : "indeed";
      const tab = await chrome.tabs.create({ url: platformLoginUrl(platform) });
      return { ok: true, tabId: tab.id };
    }

    // The running campaign hit a login wall. Mark the platform logged-out and
    // notify the user; the campaign pauses in-page until they sign in.
    case "PLATFORM_LOGIN_REQUIRED": {
      if (msg.platform) {
        const conns = (await chrome.storage.local.get("platformConnections")).platformConnections || {};
        conns[msg.platform] = { status: "logged_out", checkedAt: new Date().toISOString() };
        await chrome.storage.local.set({ platformConnections: conns });
        try {
          chrome.notifications.create({
            type: "basic",
            iconUrl: "icons/icon128.png",
            title: `Sign into ${platformLabel(msg.platform)}`,
            message: `HireDrop paused — log into ${platformLabel(msg.platform)} in the campaign window and it resumes automatically.`,
            priority: 2,
          });
        } catch {}
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

      try {
        if (stopData.campaignTabId) {
          chrome.tabs.sendMessage(stopData.campaignTabId, { type: "CAMPAIGN_STOPPED" }).catch(() => {});
        }
      } catch {}

      // Release the CDP debugger so the "DevTools" banner clears.
      if (stopData.campaignTabId) await detachDebugger(stopData.campaignTabId);

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
      const platform = appData.platform || "indeed";

      // NOTE: the LOCAL count is now incremented by content.js (recordLocalApplication)
      // BEFORE it sends this message — because an MV3 service worker can run stale code
      // after a reload, which left the count at 0 despite real submissions. Here we
      // only record the "current job" and persist to the backend (best-effort). Do NOT
      // increment the count here or it would double-count.
      await chrome.storage.local.set({
        currentJob: {
          title: appData.job_title,
          company: appData.company,
          savedAt: new Date().toISOString(),
        },
      });
      updateBadge();

      let serverResult = null;
      try {
        serverResult = await apiPost("/applications/save", {
          job_title: appData.job_title,
          company: appData.company || "",
          platform,
          job_url: appData.job_url || "",
          cover_letter: appData.cover_letter || "",
          status: appData.status || "applied",
        });
      } catch (err) {
        addToActivityLog(`⚠️ Applied but backend save failed (${err.message}) — counted locally`, "warn");
      }

      const cur = await chrome.storage.local.get("platformCounts");
      return { saved: true, platformCount: (cur.platformCounts || {})[platform] || 0, job_id: serverResult?.job_id };
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
          new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 25000)),
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

    // ----- Job-fit judge (Fit Engine M1) -----
    case "ASSESS_FIT": {
      const q = msg.data || {};
      try {
        const result = await Promise.race([
          apiPost("/tools/assess-fit", {
            job_title: q.job_title || "",
            company: q.company || "",
            description: String(q.description || "").slice(0, 4000),
            screener_questions: Array.isArray(q.screener_questions) ? q.screener_questions.slice(0, 20) : [],
          }),
          new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 25000)),
        ]);
        // Fail open: never block applying on a judge hiccup.
        return result && result.decision ? result : { decision: "apply", judged: false };
      } catch {
        return { decision: "apply", judged: false };
      }
    }

    // ----- Screener question answering (Loop 4 universal filler) -----
    case "ANSWER_QUESTION": {
      const q = msg.data || {};
      if (!q.question) return { answer: "" };
      try {
        const result = await Promise.race([
          apiPost("/tools/answer-question", {
            question: String(q.question).slice(0, 600),
            options: Array.isArray(q.options) ? q.options.slice(0, 30) : [],
            job_title: q.job_title || "",
            company: q.company || "",
          }),
          new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 25000)),
        ]);
        return { answer: (result && result.answer) || "" };
      } catch {
        return { answer: "" };
      }
    }

    // ----- Step failed -----
    case "STEP_FAILED":
      return { ok: true };

    // ----- Backend log (key events from content.js → Campaign Live feed) -----
    case "LOG_BACKEND": {
      const level = msg.level || "info";
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
      await addToActivityLog(`⚠️ Indeed asked for a human check — campaign paused`, "err");
      // System notification so the user sees this even if the popup is closed.
      try {
        await chrome.notifications.create({
          type: "basic",
          iconUrl: "icons/icon128.png",
          title: "HireDrop paused — action needed",
          message: `Indeed is asking you to verify you're human. Open the automation window, solve it, and the campaign resumes automatically.`,
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

    // CAPTCHA auto-solving (CapSolver) was REMOVED for compliance — captchas are now
    // handed to the user. No SOLVE_CAPTCHA handler.

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
