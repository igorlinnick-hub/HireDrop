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

// Self-heal a STALE durable key. getAuthToken PREFERS extension_api_key, so once that
// key is revoked/stale every backend call 401s forever while a perfectly fresh
// dashboard-pushed supabase_token sits unused — the extension then self-stops and looks
// completely broken (the "тапалка ни хера не работает" bug, 2026-07-25). On a 401 whose
// request actually used the key, drop it so the next call falls back to the token (and
// ensureExtensionKey re-mints a valid key). Returns true when it dropped the key.
async function dropStaleKeyIfUsed(usedToken) {
  if (!usedToken) return false;
  const { extension_api_key } = await chrome.storage.local.get("extension_api_key");
  if (extension_api_key && usedToken === extension_api_key) {
    await chrome.storage.local.remove("extension_api_key");
    return true;
  }
  return false;
}

// Decode a Supabase JWT's payload (sub, email, …). base64url-safe — plain
// atob() chokes on the '-'/'_' characters base64url allows.
function jwtClaims(token) {
  try {
    const b64 = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(b64));
  } catch {
    return null;
  }
}

// Everything in chrome.storage.local that belongs to ONE HireDrop user.
// chrome.storage is BROWSER-scoped, so on a dashboard user switch (or logout)
// all of this must go — the durable key especially: getAuthToken() prefers it,
// so a stale key silently keeps the extension acting as the PREVIOUS user
// (their profile in forms, their account collecting the applications).
const USER_SCOPED_KEYS = [
  "extension_api_key", "supabase_refresh_token", "profile", "profileCachedAt",
  "appliedUrls", "appliedJobKeys", "todayCount", "platformCounts",
  "campaignFilters", "campaignStartedAt", "currentJob",
  "platformConnections", "captchaWaiting", "reviewMode",
];

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
// Consecutive UNRECOVERED 401s (refresh failed too). One or two are transient (dashboard
// re-pushes a token every minute); a sustained run means the session is genuinely dead
// (logged out / password reset) — a campaign then applies as NOBODY, so self-stop it.
// Module-level is fine: an SW restart resets the count, but the 60s ping re-accumulates
// it quickly if the auth is truly gone.
let _auth401Streak = 0;
const AUTH_401_SELF_STOP = 5;

async function noteAuth401() {
  _auth401Streak++;
  if (_auth401Streak < AUTH_401_SELF_STOP) return;
  const { campaignRunning, campaignWindowId } = await chrome.storage.local.get(["campaignRunning", "campaignWindowId"]);
  if (!campaignRunning) return;
  _auth401Streak = 0;
  await chrome.storage.local.set({
    campaignRunning: false, campaignTabId: null, campaignWindowId: null,
    currentJob: null, captchaWaiting: null, reviewPending: null, reviewDecision: null,
  });
  await chrome.storage.local.remove(["atsQueue", "atsPlatform", "atsNavAt", "atsNavTries"]);
  if (campaignWindowId) chrome.windows.remove(campaignWindowId).catch(() => {});
  updateBadge();
  // No backend call here — auth is dead, /campaign/stop would 401 too. The server-side
  // heartbeat TTL flips the backend flag within ~150s of the pings stopping.
}

async function apiGet(path, { retry = true } = {}) {
  const token = await getAuthToken();
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  const res = await fetch(`${CONFIG.API_BASE}${CONFIG.API_V1}${path}`, { headers });
  if (res.status === 401) {
    if (retry) {
      // Stale durable key → drop it and retry with the dashboard token before anything else.
      if (await dropStaleKeyIfUsed(token)) return apiGet(path, { retry: false });
      const newToken = await refreshAccessToken();
      if (newToken) return apiGet(path, { retry: false });
    }
    noteAuth401().catch(() => {});
    throw new Error("API 401 (token stale — dashboard will refresh it)");
  }
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  _auth401Streak = 0;
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
      if (await dropStaleKeyIfUsed(token)) return apiPost(path, body, { retry: false });
      const newToken = await refreshAccessToken();
      if (newToken) return apiPost(path, body, { retry: false });
    }
    noteAuth401().catch(() => {});
    throw new Error("API 401 (token stale — dashboard will refresh it)");
  }
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  _auth401Streak = 0;
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
        const payload = supabase_token ? jwtClaims(supabase_token) : null;
        if (payload && payload.email) profile.email = payload.email;
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

// Fallback caps used only until the backend's authoritative numbers are fetched at
// campaign start (single source: app/db/subscriptions.py, mirrored into campaignCaps).
// SAFE defaults — the old hardcoded 50 let real applications run past the ban-safety
// rail because the extension counted locally and ignored the backend's 429.
const DEFAULT_PER_PLATFORM = 20;
const DEFAULT_DAILY_TOTAL = 30; // matches the paid (pro) auto cap; real value fetched at campaign start

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
    captchaWaiting: null,
  });
  // 2026-07-12: login detection went fail-closed (positive evidence only).
  // "connected" statuses written by the old fail-open rules ("no login button
  // ⇒ connected") may be false positives — drop them so the new detectors
  // rebuild them honestly on the next site visit. Indeed always required
  // positive evidence, so its status is kept. logged_out entries were positive
  // evidence too — kept.
  {
    const s = await chrome.storage.local.get("platformConnections");
    const conns = s.platformConnections || {};
    let changed = false;
    for (const p of ["ziprecruiter", "glassdoor", "wellfound", "monster", "careerbuilder", "dice"]) {
      if (conns[p] && conns[p].status === "connected") { delete conns[p]; changed = true; }
    }
    if (changed) await chrome.storage.local.set({ platformConnections: conns });
  }
  await fetchAndCacheProfile().catch(() => {});
  ensureExtensionKey().catch(() => {}); // mint durable key ASAP so cold-start never 401s
  updateBadge();
});

chrome.runtime.onStartup.addListener(async () => {
  const data = await chrome.storage.local.get("todayDate");
  const today = new Date().toISOString().slice(0, 10);
  if (data.todayDate !== today) {
    await chrome.storage.local.set({ todayCount: 0, platformCounts: {}, todayDate: today });
  }
  await fetchAndCacheProfile().catch(() => {});
  ensureExtensionKey().catch(() => {}); // retry durable-key mint on SW wake
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
  if (alarm.name === "ext-ping") {
    sendExtensionPing();
    atsWalkWatchdog().catch(() => {});
    tapPoolIdleRefill().catch(() => {});
  }
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

// Live-preview capture allowlist — ONLY the job sites the automation itself drives, so
// the user's other tabs are never captured (preserves the privacy intent of the original
// Indeed-only guard) while the live window works ACROSS every platform (a CWS requirement:
// the user must see what the extension is doing). Keep in sync with the platforms that
// phase2 / phase3 / phase_ats operate on.
const CAPTURE_HOSTS = ["indeed.com", "ziprecruiter.com", "greenhouse.io", "lever.co", "ashbyhq.com"];
function isCapturableAutomationUrl(url) {
  try {
    const h = new URL(url).hostname;
    return CAPTURE_HOSTS.some((d) => h === d || h.endsWith("." + d));
  } catch {
    return false;
  }
}

// Display name for a job site from its URL — used in user-facing captcha/pause messages
// so they name the actual platform instead of always saying "Indeed".
function platformDisplayNameFromUrl(url) {
  const u = String(url || "");
  if (u.includes("ziprecruiter.com")) return "ZipRecruiter";
  if (u.includes("greenhouse.io")) return "Greenhouse";
  if (u.includes("lever.co")) return "Lever";
  if (u.includes("ashbyhq.com")) return "Ashby";
  if (u.includes("indeed.com")) return "Indeed";
  return "The job site";
}

async function sendScreenshot(tabId) {
  // Only capture the automation's own job-application tabs (never arbitrary user tabs).
  try {
    const tab = await chrome.tabs.get(tabId);
    if (!isCapturableAutomationUrl(tab.url)) return;
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

// Capture the ACTIVE automation tab of the campaign window (follows the apply flow as it
// navigates: Indeed→smartapply, ZR quick-apply, or a board→Greenhouse/Lever hop). Shared
// by the message handler and the service-worker capture loop.
async function captureActiveAutomationTab() {
  const { campaignRunning, campaignTabId, campaignWindowId, reviewMode } = await chrome.storage.local.get([
    "campaignRunning",
    "campaignTabId",
    "campaignWindowId",
    "reviewMode",
  ]);
  if (!campaignRunning) return false;
  // TAP mode shows swipe CARDS, not a live browser preview — so never attach the CDP
  // debugger here. Attaching triggers Chrome's intrusive "HireDrop started debugging
  // this browser" banner for zero benefit in tap. Keeps tap clean and unintrusive.
  if (reviewMode) return false;

  let tabId = campaignTabId;
  if (campaignWindowId != null) {
    try {
      const [active] = await chrome.tabs.query({ windowId: campaignWindowId, active: true });
      if (active && active.id != null && isCapturableAutomationUrl(active.url)) {
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

// LinkedIn search-walk — Easy Apply ONLY (f_AL=true). We only drive the in-modal 1-click
// flow; external-apply jobs are skipped by the content script (PLATFORMS_MASTER_PLAN Phase 2).
function buildLinkedInUrl(keywords, location, jobType) {
  const params = new URLSearchParams();
  if (keywords && keywords.length) params.set("keywords", keywords.join(" "));
  const locMap = { usa: "United States", remote: "Remote", europe: "" };
  const loc = locMap[location] !== undefined ? locMap[location] : (location || "");
  if (loc) params.set("location", loc);
  params.set("f_AL", "true"); // Easy Apply filter — the only jobs our handler can drive
  return `https://www.linkedin.com/jobs/search/?${params.toString()}`;
}

function buildPlatformUrl(platform, keywords, location, jobType) {
  if (platform === "ziprecruiter") return buildZipRecruiterUrl(keywords, location, jobType);
  if (platform === "linkedin") return buildLinkedInUrl(keywords, location, jobType);
  return buildIndeedUrl(keywords, location, jobType);
}

function platformHomeUrl(platform) {
  if (platform === "ziprecruiter") return "https://www.ziprecruiter.com/";
  if (platform === "linkedin") return "https://www.linkedin.com/";
  return "https://www.indeed.com/";
}

// Platforms the extension actually auto-applies on. Everything else in
// filters.platforms is a discovery-only source (scraped, applied to externally).
// LinkedIn = native search-walk like Indeed/ZR (v1 semi-auto: fills, human submits).
const AUTO_APPLY_PLATFORMS = ["indeed", "ziprecruiter", "linkedin"];

// The campaign window targets the first auto-apply platform in the filter list.
// Selecting by membership (not platforms[0]) is robust to discovery platforms
// appearing first in the array.
function pickPrimaryPlatform(platforms) {
  const list = platforms || [];
  return list.find((p) => AUTO_APPLY_PLATFORMS.includes(p)) || "indeed";
}

// ATS platforms applied to POOL-DRIVEN: discovery (board API) fills the job pool, then the
// campaign walks the saved apply URLs in the automation tab (no board search). Only
// zero-touch (no interactive captcha) platforms run FULL-auto here — Lever (hCaptcha) needs
// the human tapalka to advance and is excluded from the auto pool for now (GLOBAL_PLAN P2).
const ATS_PLATFORMS = ["greenhouse", "lever", "ashby"];
const ATS_ZERO_TOUCH_PLATFORMS = ["greenhouse"];

// Build the ATS apply queue for a campaign (GLOBAL_PLAN P1a+P1b): populate the pool via
// /jobs/find-ats, then pull the user's ZERO-TOUCH jobs for this platform (GET /jobs already
// tags zero_touch + orders them first — PR #32), capped at the per-platform rail. Each item
// = { applyUrl, title, company }. Returns [] on any error (campaign then reports "no jobs").
async function buildAtsQueue(platform, perPlatformCap) {
  try { await apiPost("/jobs/find-ats", {}); } catch (e) { /* discovery best-effort */ }
  let jobs = [];
  try { jobs = await apiGet("/jobs"); } catch (e) { return []; }
  const cap = perPlatformCap > 0 ? perPlatformCap : 20;
  // Drop jobs we already applied to (URL or title|company key) — an already-applied job
  // at the queue head used to dead-stop the walk: phase_ats skips it silently and only a
  // real submit advances the queue. Mirrors content.js's dedup (jobDedupKey format).
  const dd = await chrome.storage.local.get(["appliedUrls", "appliedJobKeys"]);
  const appliedUrls = new Set(dd.appliedUrls || []);
  const appliedKeys = new Set(dd.appliedJobKeys || []);
  const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ").trim();
  return (jobs || [])
    // greenhouse queue = zero-touch only (full-auto); lever jobs are human-touch by
    // definition (hCaptcha) and only reach here in tap mode — platform match suffices.
    .filter((j) => j.platform === platform && (platform === "lever" || j.zero_touch === true) && (j.link || j.apply_url))
    .filter((j) => {
      const url = (j.link || j.apply_url).split("?")[0];
      return !appliedUrls.has(url) && !appliedKeys.has(`${norm(j.title)}|${norm(j.company)}`);
    })
    .slice(0, cap)
    .map((j) => ({ applyUrl: j.link || j.apply_url, title: j.title || "", company: j.company || "" }));
}

// TAP-POOL queue (Igor 2026-07-16): the user's APPROVED swipe cards, platform-mixed
// (greenhouse + lever together), capped per platform. The swipe UI PATCHes
// jobs.status="approved"; this consumes them. Zero-touch first (GH before Lever) so the
// no-human items clear while the user is still around for the Lever captchas.
// Minimal PATCH twin of apiPost — used to durably flip a dead pool job's status so it
// stops re-entering the approved queue on every run (live-test finding 2026-07-27:
// a closed posting looped skip→rebuild→same-job forever).
async function apiPatch(path, body, { retry = true } = {}) {
  const token = await getAuthToken();
  const headers = {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
  const res = await fetch(`${CONFIG.API_BASE}${CONFIG.API_V1}${path}`, {
    method: "PATCH",
    headers,
    body: JSON.stringify(body),
  });
  if (res.status === 401 && retry) {
    if (await dropStaleKeyIfUsed(token)) return apiPatch(path, body, { retry: false });
    const newToken = await refreshAccessToken();
    if (newToken) return apiPatch(path, body, { retry: false });
  }
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  return res.json();
}

// Native boards the pool can apply BY LINK (Igor 2026-07-27: "тап должен работать по
// всем платформам"). Split by verification status:
//  - VERIFIED: link-normalization path proven live (Indeed by-link, 2 real applies #68).
//    normalizePoolApplyUrl() returns null unless a jk/vjk is present, so a search URL can
//    never slip through as an un-swiped auto-apply — the footgun is closed. Always in pool.
//  - PENDING: by-link path not yet live-verified (ZR ephemeral /co/…?lk= URLs). Kept behind
//    the tapNativePool storage flag (default OFF) until verified.
//    Flip: chrome.storage.local.set({tapNativePool:true}).
const POOL_NATIVE_VERIFIED = ["indeed"];
const POOL_NATIVE_PENDING = ["ziprecruiter"];
// Every native (verified or pending), for the CF-homepage-warm decision: a native pool head
// must never be cold deep-linked regardless of whether it's flag-gated.
const POOL_NATIVE_ALL = POOL_NATIVE_VERIFIED.concat(POOL_NATIVE_PENDING);

// Return a SAFE single-job URL for the pool walk, or null when one can't be derived
// (null = leave the job out of the queue rather than risk the search-walk footgun).
function normalizePoolApplyUrl(platform, url) {
  if (!url) return null;
  if (platform === "indeed") {
    // Card hrefs come as /rc/clk?jk=…, /jobs?...vjk=…, /viewjob?jk=… — anything with a
    // jk/vjk collapses to the canonical single-job page. /jobs?* without it would be
    // detected as phase "list" (the search walk) — never navigate there in pool mode.
    try {
      const u = new URL(url);
      const jk = u.searchParams.get("jk") || u.searchParams.get("vjk");
      if (jk) return `https://www.indeed.com/viewjob?jk=${jk}`;
    } catch {}
    return null;
  }
  if (platform === "ziprecruiter") {
    // ZR's single-job identity IS a search URL + lk=<uuid> (that's what opens the job in
    // the right pane; detectPhase reads it as "detail"). Harvested pool links come in this
    // form (P0c), so ALLOW search URLs when lk= is present — only a bare search URL (no lk)
    // would re-enter the ZR walk and apply un-swiped jobs. Everything else (standalone job
    // pages) passes through as before.
    if (/jobs-search|candidate\/search/.test(url)) {
      try {
        if (new URL(url).searchParams.get("lk")) return url;
      } catch {}
      return null;
    }
    return url;
  }
  return url; // greenhouse/lever apply URLs are already single-job pages
}

async function buildApprovedAtsQueue(perPlatformCap) {
  let jobs = [];
  try { jobs = await apiGet("/jobs"); } catch (e) { return []; }
  const cap = perPlatformCap > 0 ? perPlatformCap : 15;
  const dd = await chrome.storage.local.get(["appliedUrls", "appliedJobKeys", "tapNativePool", "poolDoneUrls"]);
  const appliedUrls = new Set(dd.appliedUrls || []);
  const appliedKeys = new Set(dd.appliedJobKeys || []);
  // URLs already walked THIS run (applied or skipped) — without this, a dead posting
  // that skips stays `approved` in the DB and the rebuild re-queues it forever
  // (live-test finding: oura?error=true looped every 4s).
  const doneUrls = new Set(dd.poolDoneUrls || []);
  // Verified natives (Indeed) are always poolable; pending natives (ZR) only under the flag.
  const poolPlatforms = dd.tapNativePool === true
    ? ATS_PLATFORMS.concat(POOL_NATIVE_VERIFIED, POOL_NATIVE_PENDING)
    : ATS_PLATFORMS.concat(POOL_NATIVE_VERIFIED);
  const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ").trim();
  const perCount = {};
  const out = [];
  for (const j of (jobs || [])) {
    if (!poolPlatforms.includes(j.platform)) continue;
    if ((j.status || "") !== "approved") continue;
    const url = normalizePoolApplyUrl(j.platform, j.link || j.apply_url);
    if (!url) continue;
    // Dedup: query-stripped base for path-unique ATS URLs, full URL for /viewjob?jk=
    // (whose identity IS the query), plus the title|company key.
    if (doneUrls.has(url) || appliedUrls.has(url.split("?")[0]) || appliedUrls.has(url) ||
        appliedKeys.has(`${norm(j.title)}|${norm(j.company)}`)) continue;
    const c = perCount[j.platform] || 0;
    if (c >= cap) continue;
    perCount[j.platform] = c + 1;
    out.push({ id: j.id, applyUrl: url, title: j.title || "", company: j.company || "", platform: j.platform });
  }
  // GET /jobs already orders zero-touch first (PR #32) — preserved by the linear walk.
  return out;
}

// Navigate the automation tab to the next pool job — CF-safely.
// GH/Lever/Ashby apply URLs have no Cloudflare gate → open them directly. A NATIVE job
// (Indeed/ZR) deep-link is a cold "bot jump" CF answers with "Additional Verification
// Required" UNLESS indeed.com/ziprecruiter.com already carries cf_clearance. The head
// open warms the head's domain; a MIXED pool (GH head + Indeed later) would otherwise
// cold-deep-link that Indeed job. So: the FIRST time we reach each native domain this run,
// route via its HOMEPAGE (campaignWarmedUp=false → content.js sessionWarmup warms CF, then
// navigates to campaignTargetUrl); once warmed, the cf_clearance cookie covers the rest.
async function navigatePoolNext(tabId, job) {
  if (!job || !job.applyUrl) return;
  if (POOL_NATIVE_ALL.includes(job.platform)) {
    const st = await chrome.storage.local.get("poolWarmedNatives");
    const warmed = new Set(st.poolWarmedNatives || []);
    if (!warmed.has(job.platform)) {
      warmed.add(job.platform);
      await chrome.storage.local.set({
        campaignTargetUrl: job.applyUrl,
        campaignWarmedUp: false,
        poolWarmedNatives: Array.from(warmed),
      });
      await chrome.tabs.update(tabId, { url: platformHomeUrl(job.platform) }).catch(() => {});
      return;
    }
  }
  await chrome.storage.local.set({ campaignTargetUrl: job.applyUrl });
  await chrome.tabs.update(tabId, { url: job.applyUrl }).catch(() => {});
}

// Walk the ATS queue one step: drop the head, open the next apply URL in the automation
// tab, or finish the campaign when the queue is empty. Called after a submit
// (APPLICATION_SAVED) AND after any non-submit outcome (ATS_JOB_DONE from phase_ats —
// fit-skip / already-applied / missing form). Before that second caller existed, any
// skipped job dead-stopped the walk: nothing advanced the queue except a real submit.
async function advanceAtsQueue() {
  try {
    const { atsQueue, campaignTabId, atsPlatform: plat, poolDoneUrls } =
      await chrome.storage.local.get(["atsQueue", "campaignTabId", "atsPlatform", "poolDoneUrls"]);
    if (Array.isArray(atsQueue) && atsQueue.length && campaignTabId) {
      const rest = atsQueue.slice(1);
      await chrome.storage.local.set({ atsQueue: rest });
      // Pool mode: remember every walked head (applied OR skipped) for THIS run, so
      // the rebuild below can never re-queue it (dead postings stay `approved` in the
      // DB and would otherwise loop forever).
      if (plat === "pool" && atsQueue[0] && atsQueue[0].applyUrl) {
        const done = (poolDoneUrls || []).concat([atsQueue[0].applyUrl]).slice(-300);
        await chrome.storage.local.set({ poolDoneUrls: done });
      }
      const running = (await chrome.storage.local.get("campaignRunning")).campaignRunning === true;
      if (rest.length && running) {
        // Fresh watchdog window for the next job page (see atsWalkWatchdog).
        await chrome.storage.local.set({ atsNavAt: Date.now(), atsNavTries: 0 });
        await navigatePoolNext(campaignTabId, rest[0]);
      } else if (!rest.length) {
        // Tap swipe-pool: don't finish on empty — the user may have approved more while
        // we worked. Rebuild from newly-approved jobs and keep walking; only go idle
        // (STILL running) when nothing approved is left, so later swipes get picked up
        // (the ext-ping alarm re-checks — see tapPoolIdleRefill).
        const { atsPlatform, campaignCaps } = await chrome.storage.local.get(["atsPlatform", "campaignCaps"]);
        if (atsPlatform === "pool" && running) {
          const cap = (campaignCaps && campaignCaps.perPlatform) || 15;
          const more = await buildApprovedAtsQueue(cap);
          if (more.length) {
            await chrome.storage.local.set({ atsQueue: more, atsNavAt: Date.now(), atsNavTries: 0 });
            await addToActivityLog(`Applying ${more.length} more approved job${more.length > 1 ? "s" : ""} you swiped…`, "info");
            await navigatePoolNext(campaignTabId, more[0]);
          } else {
            await chrome.storage.local.set({ atsQueue: [] });
            await chrome.storage.local.remove(["atsNavAt", "atsNavTries"]);
            await addToActivityLog("Caught up on approved jobs — swipe more and we'll apply them.", "info");
          }
          return;
        }
        await addToActivityLog("ATS pool complete — walked every discovered zero-touch job. Campaign finished.", "ok");
        await chrome.storage.local.set({ campaignRunning: false });
        await chrome.storage.local.remove(["atsNavAt", "atsNavTries"]);
        try { await apiPost("/campaign/stop", {}); } catch {}
        updateBadge();
      }
    }
  } catch (e) { /* advance is best-effort; a failure just pauses the walk */ }
}

// ATS walk WATCHDOG (the Oura freeze, GLOBAL_PLAN P1 polish c): after advancing to the
// next queue page, the content script there once never initialized — no log, no fill,
// walk frozen for an hour with "running" on. Whatever the root cause (SW race on
// tabs.update, injection miss), the walk must self-heal: if a queue page stays silent
// past the window (a legit fill takes ~5-6 min), reload it once; still silent → skip the
// job and advance. Runs off the 60s ext-ping alarm. Every action is logged — visible, not
// silent.
const ATS_WATCHDOG_SILENT_MS = 8 * 60 * 1000;

async function atsWalkWatchdog() {
  const d = await chrome.storage.local.get([
    "campaignRunning", "atsQueue", "atsPlatform", "campaignTabId", "atsNavAt", "atsNavTries",
  ]);
  if (!d.campaignRunning || !d.atsPlatform || !Array.isArray(d.atsQueue) || !d.atsQueue.length) return;
  if (!d.campaignTabId || !d.atsNavAt) return;
  const age = Date.now() - d.atsNavAt;
  if (age < ATS_WATCHDOG_SILENT_MS) return;
  const mins = Math.round(age / 60000);
  if ((d.atsNavTries || 0) === 0) {
    await chrome.storage.local.set({ atsNavAt: Date.now(), atsNavTries: 1 });
    await addToActivityLog(`⏱ Watchdog: ${d.atsPlatform} job page silent for ${mins} min — reloading it`, "warn");
    chrome.tabs.reload(d.campaignTabId).catch(() => {});
  } else {
    await addToActivityLog("⏱ Watchdog: still silent after a reload — skipping this job, moving on", "warn");
    await chrome.storage.local.set({ atsNavTries: 0 });
    await advanceAtsQueue();
  }
}

// Tap swipe-pool idle refill (Igor 2026-07-25 instant rebuild): while a tap run is live
// but the approved queue has drained to empty, the user can swipe MORE cards. advanceAtsQueue
// only fires after a job finishes, so an idle queue would never restart on its own. On the
// 60s ext-ping alarm, rebuild from newly-approved jobs and kick the walk in the EXISTING
// automation window. Idle = queue empty AND no nav in flight (atsNavAt cleared) — so this
// never double-navigates a job that's actively being filled.
async function tapPoolIdleRefill() {
  const d = await chrome.storage.local.get([
    "campaignRunning", "atsPlatform", "atsQueue", "campaignTabId", "atsNavAt", "campaignCaps",
  ]);
  if (!d.campaignRunning || d.atsPlatform !== "pool" || !d.campaignTabId) return;
  if ((Array.isArray(d.atsQueue) && d.atsQueue.length) || d.atsNavAt) return; // busy, not idle
  const cap = (d.campaignCaps && d.campaignCaps.perPlatform) || 15;
  const more = await buildApprovedAtsQueue(cap);
  if (!more.length) return;
  try { await chrome.tabs.get(d.campaignTabId); } catch { return; } // automation tab gone
  await chrome.storage.local.set({ atsQueue: more, atsNavAt: Date.now(), atsNavTries: 0 });
  await addToActivityLog(`Applying ${more.length} more approved job${more.length > 1 ? "s" : ""} you swiped…`, "info");
  await navigatePoolNext(d.campaignTabId, more[0]);
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
      // Identity guard (2026-07-12): detect a dashboard USER SWITCH by JWT sub
      // and reset all user-scoped state, fail-closed. Without this, the old
      // user's durable key stayed put and the new user's campaign ran under
      // the old identity end-to-end. A running campaign is stopped — it was
      // started by (and authenticated as) someone else.
      const claims = jwtClaims(msg.token);
      if (claims && claims.sub) {
        const idData = await chrome.storage.local.get(["hd_user_id", "campaignRunning"]);
        if (idData.hd_user_id !== claims.sub) {
          if (idData.campaignRunning) await handleMessage({ type: "STOP_CAMPAIGN" }, sender);
          await chrome.storage.local.remove(USER_SCOPED_KEYS);
          await chrome.storage.local.set({ hd_user_id: claims.sub });
        }
      }
      const storePayload = { supabase_token: msg.token };
      if (msg.refresh_token) storePayload.supabase_refresh_token = msg.refresh_token;
      await chrome.storage.local.set(storePayload);
      // Auto-upgrade to a durable key the first time we have a token but none yet.
      // After a user switch the key was just cleared, so this mints one for the
      // NEW user with the token that arrived in this very message.
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
      // Full user-scope wipe — not just auth. Leaving dedup history, counts and
      // platform statuses behind bleeds one user's state into the next login.
      const lo = await chrome.storage.local.get("campaignRunning");
      if (lo.campaignRunning) await handleMessage({ type: "STOP_CAMPAIGN" }, sender);
      await chrome.storage.local.remove([...USER_SCOPED_KEYS, "supabase_token", "hd_user_id"]);
      return { loggedOut: true };
    }

    // ----- Profile -----
    case "GET_PROFILE":
      return await getCachedProfile();

    case "REFRESH_PROFILE":
      return await fetchAndCacheProfile();

    case "CHECK_CONNECTION": {
      // Popup open = a good moment to retry minting the durable key (ROADMAP_E2E.md P2):
      // once it exists, getAuthToken() stops depending on the fragile dashboard token push.
      ensureExtensionKey().catch(() => {});
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
      // Self-heal + observability: a fresh Start must not inherit a stale captcha
      // hand-off OR a phantom "running" flag from a prior stalled run (that phantom
      // pinned the tap page on "preparing…" forever). Hard-reset the run state, and
      // log that the SW actually RECEIVED the start — that first log line is how we
      // tell "message never reached the extension" from "campaign ran but stalled".
      await chrome.storage.local.set({
        captchaWaiting: null, campaignRunning: false, currentJob: null,
        reviewPending: null, reviewDecision: null,
        poolDoneUrls: [], // per-run walked-pool memory — a fresh run starts clean
      });
      await chrome.storage.local.remove(["atsQueue", "atsPlatform", "atsNavAt", "atsNavTries"]);
      await addToActivityLog("▶ Start received by the extension — preparing your campaign…", "info");
      const profile = await getCachedProfile();
      // Fail-closed onboarding gate: the popup can start a campaign without the
      // user ever seeing the site (the dashboard's /dashboard/* layout gate
      // can't help here). An un-onboarded profile is empty — the campaign
      // would fill applications with blanks under the user's identity.
      if (!profile || profile.onboarding_completed !== true) {
        await addToActivityLog("Can't start — finish your profile setup first.", "error");
        return { started: false, error: "onboarding_incomplete" };
      }
      // Resume is optional at onboarding — Indeed native applies use the resume
      // stored on Indeed itself, but external-ATS forms (Greenhouse/Lever)
      // hard-require one and the P1 guard will skip them. Say so UPFRONT in
      // the activity feed instead of letting the user wonder why every ATS
      // job silently lands in "skipped".
      if (!profile.resume_url) {
        await addToActivityLog(
          "Heads-up: no resume in your HireDrop profile — company-site (ATS) applications will be skipped until you upload one in Settings. Indeed applies still work (they use the resume on your Indeed account).",
          "warn"
        );
      }
      const raw = msg.filters || {};
      // Always merge with profile so partial/empty filters still work
      const filters = {
        keywords: (raw.keywords && raw.keywords.length) ? raw.keywords : (profile.keywords || []),
        platforms: (raw.platforms && raw.platforms.length) ? raw.platforms : (profile.platforms || ["indeed"]),
        location: raw.location || profile.location || "",
        job_type: raw.job_type || profile.job_type || "",
      };

      // No keywords anywhere (request OR profile) → the campaign has nothing to search
      // for. Refuse with a clear reason instead of "starting" an empty run (the popup
      // Start path has no dashboard-side keyword check). Mirrors /campaign/readiness.
      if (!filters.keywords.length) {
        return {
          started: false,
          error: "no_keywords",
          message: "Add at least one keyword first — the campaign needs something to search for.",
        };
      }

      // Pick the auto-apply platform this campaign targets (first in the filter list)
      const primaryPlatform = pickPrimaryPlatform(filters.platforms);

      // Status BEFORE target selection: Lever/tap-pool eligibility depends on submit_mode.
      let preSt = null;
      try { preSt = await apiGet("/campaign/status"); } catch {}
      const tapMode = !!(preSt && preSt.submit_mode === "tap");

      // reviewMode is now ALWAYS off (Igor 2026-07-25, instant-tap rebuild). The new
      // tap flow pre-approves via the swipe deck — the human decision happens on
      // /dashboard/tap, so approved jobs auto-submit in the background. There is no
      // in-browser fill-and-stop review anymore (auto mode never had one). This also
      // permanently kills the "Auto showed the Tap review panel" mismatch.
      // Caps come from the pre-flight status we already fetched (no redundant second
      // /campaign/status round-trip); ban-safe 20/50 default when status is unreachable.
      await chrome.storage.local.set({
        reviewMode: false,
        campaignCaps: {
          perPlatform: (preSt && preSt.limit_per_platform > 0) ? preSt.limit_per_platform : 20,
          dailyTotal: (preSt && preSt.daily_limit > 0) ? preSt.daily_limit : 50,
        },
      });

      // Free taste (FREE_TASTE_PLAN.md): an exhausted free account must not start at all —
      // the pre-submit caps don't know the lifetime 40-app limit, so a started campaign
      // would submit applications the backend then refuses to save (they reach the
      // employer, invisibly). Server unreachable → fail-open, like the caps.
      if (preSt && preSt.free_limit != null && preSt.free_used >= preSt.free_limit) {
        return {
          started: false,
          error: "free_limit_reached",
          message: `You've used all ${preSt.free_limit} free applications — subscribe to keep applying.`,
        };
      }

      // TAP-POOL (Igor 2026-07-25 instant rebuild): in tap mode the queue is ONLY the
      // user's APPROVED swipe cards (GH + Lever, per-platform cap). Swipe first on the
      // /dashboard/tap deck, machine applies after.
      let tapPoolQueue = [];
      if (tapMode) {
        tapPoolQueue = await buildApprovedAtsQueue((preSt && preSt.limit_per_platform) || 15);
        // Footgun guard: with nothing approved yet, do NOT fall through to an auto walk
        // (native Indeed search / GH auto-sweep) — that would apply jobs the user never
        // swiped. Ask them to swipe first; the deck marks jobs approved instantly and
        // Start applies exactly those.
        if (!tapPoolQueue.length) {
          return {
            started: false,
            error: "no_approved_jobs",
            // Covers both truths honestly: nothing approved yet, OR everything approved
            // was already applied/dead (dedup excluded it) — live-test 2026-07-27 found
            // the old "swipe first" wording gaslighting a user whose swipes WERE consumed.
            message: "Nothing new to apply — jobs you approved before are already applied or closed. Swipe Approve on new cards, then Start.",
          };
        }
      }

      // Pool-driven ATS target (GLOBAL_PLAN P1+P2):
      // - greenhouse: zero-touch → any mode, full-auto;
      // - lever: hCaptcha at submit → tap mode only (human approves + clears it);
      //   in auto mode refuse with a clear message instead of a campaign that can't submit.
      let atsTarget = (filters.platforms || []).find((p) => ATS_ZERO_TOUCH_PLATFORMS.includes(p)) || null;
      if (!tapPoolQueue.length && !atsTarget && (filters.platforms || []).includes("lever")) {
        if (tapMode) {
          atsTarget = "lever";
        } else {
          return {
            started: false,
            error: "lever_needs_tap",
            message: "Lever applications need Tap mode (their captcha requires a human). Switch to Tap and start again.",
          };
        }
      }

      // Pre-flight login check applies only to native board platforms (Indeed/ZR). ATS apply
      // pages are public — no login wall — so skip it in pool-driven mode.
      if (!atsTarget && !tapPoolQueue.length) {
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

      // Immediate feedback: from here we're committed to starting, but opening the
      // window + loading the board + writing the first tailored application takes
      // ~1-2 min. Without a line NOW the Live Activity reads "Waiting for extension"
      // and feels frozen (Igor 2026-07-25). Post progress the moment we commit.
      // (The free-taste gate + preSt fetch already ran earlier — not duplicated here.)
      await addToActivityLog("Starting your campaign — opening the browser and finding jobs now…", "info");

      try {
        // Caps + reviewMode were already stamped from the pre-flight status above,
        // so this is just the start signal — no second /campaign/status round-trip.
        await apiPost("/campaign/start", filters);
      } catch {
        // Continue even if server is down — content.js falls back to safe defaults (20/50).
      }

      // ATS pool-driven mode: build the apply queue and target the FIRST job's apply URL
      // instead of a board search. The automation tab then walks the queue: phase_ats
      // fills (+submits when zero-touch) → APPLICATION_SAVED / ATS_JOB_DONE → advance.
      let atsQueue = [];
      if (tapPoolQueue.length) {
        // Approved-cards queue (platform-mixed). reviewMode is already set from
        // submit_mode above; GH items auto-submit, Lever items stop for the human.
        atsQueue = tapPoolQueue;
        await chrome.storage.local.set({ atsQueue, atsPlatform: "pool", atsNavAt: Date.now(), atsNavTries: 0 });
        await addToActivityLog(`Applying to ${atsQueue.length} approved jobs (your swipes) — working through them now.`, "info");
      } else if (atsTarget) {
        const capState = (await chrome.storage.local.get("campaignCaps")).campaignCaps || {};
        atsQueue = await buildAtsQueue(atsTarget, capState.perPlatform || 20);
        if (!atsQueue.length) {
          return {
            started: false,
            error: "no_ats_jobs",
            message: `No zero-touch ${atsTarget} jobs to apply to yet — try again shortly or broaden your keywords.`,
          };
        }
        await chrome.storage.local.set({ atsQueue, atsPlatform: atsTarget, atsNavAt: Date.now(), atsNavTries: 0 });
        await addToActivityLog(
          atsTarget === "lever"
            ? `Found ${atsQueue.length} Lever jobs — filling each; you approve + clear the captcha.`
            : `Found ${atsQueue.length} zero-touch ${atsTarget} jobs — starting full-auto apply.`,
          "info"
        );
      } else {
        await chrome.storage.local.remove(["atsQueue", "atsPlatform", "atsNavAt", "atsNavTries"]);
      }

      const targetUrl = atsQueue.length ? atsQueue[0].applyUrl
        : buildPlatformUrl(primaryPlatform, filters.keywords, filters.location, filters.job_type);
      // Where the automation window first lands. For a pool run whose FIRST job is an
      // Indeed/ZR native posting we must NOT cold-open its deep /viewjob link — a direct
      // deep-link nav is a bot jump that Cloudflare answers with "Additional Verification
      // Required", and the apply never starts. Open the platform HOMEPAGE instead; content.js
      // sessionWarmup passes CF there (sets cf_clearance), then navigates to targetUrl (the
      // picked job), which now loads clean. GH/Lever pool jobs have no such CF gate, so open
      // their apply URL directly. Non-pool (auto) keeps homepage → typed-search as before.
      // MIXED pool (GH head + Indeed later): the later native deep-link is CF-warmed on the
      // fly by navigatePoolNext (first hit of each native domain routes via its homepage).
      // We seed poolWarmedNatives with the head below so a native head isn't re-warmed.
      const headPlatform = atsQueue.length ? atsQueue[0].platform : null;
      const homeUrl = !atsQueue.length
        // LinkedIn has NO Cloudflare gate, so skip the homepage→search hop (built for Indeed's
        // CF) and open the Easy-Apply search DIRECTLY — the homepage-first warmup was landing
        // on /feed and not reliably navigating on (live 2026-08-01). Direct nav is proven.
        ? (primaryPlatform === "linkedin" ? targetUrl : platformHomeUrl(primaryPlatform))
        : POOL_NATIVE_ALL.includes(headPlatform)
          ? platformHomeUrl(headPlatform)
          : atsQueue[0].applyUrl;
      await addToActivityLog(`Opening the automation window → ${String(homeUrl).slice(0, 70)}`, "info");

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
      await addToActivityLog(`Automation window ${reusingWindow ? "reused" : "opened"} (tab ${tab.id}) — loading the page…`, "info");

      await chrome.storage.local.set({
        campaignRunning: true,
        campaignFilters: filters,
        campaignTargetUrl: targetUrl,
        campaignStartedAt: new Date().toISOString(),
        campaignTabId: tab.id,
        campaignWindowId: tabInfo.windowId,
        currentJob: null,
        campaignWarmedUp: false,
        // A native head is CF-warmed by the homepage open above → seed it so the queue walk
        // doesn't re-warm the same domain. GH/Lever/Ashby heads need no warm, so [] for them.
        poolWarmedNatives: POOL_NATIVE_ALL.includes(headPlatform) ? [headPlatform] : [],
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
    case "DEV_RELOAD": {
      // Reload the unpacked extension from disk (new content.js/background.js) without a
      // manual chrome://extensions click. Triggered only from hiredrop.io via ping.js.
      // Fire-and-forget: the SW restarts, so no response is delivered.
      try { chrome.runtime.reload(); } catch (e) { /* noop */ }
      return { ok: true };
    }

    case "STOP_CAMPAIGN": {
      const stopData = await chrome.storage.local.get(["campaignTabId", "campaignWindowId"]);

      // Clear running state first so the onDetach listener won't auto-reattach.
      // Also drop any pending tap review + ATS queue — a stopped campaign must not
      // leave a dangling review card on the dashboard or resume a half-walked queue.
      await chrome.storage.local.set({
        campaignRunning: false,
        campaignTabId: null,
        campaignWindowId: null,
        currentJob: null,
        captchaWaiting: null,
        reviewPending: null,
        reviewDecision: null,
      });
      await chrome.storage.local.remove(["atsQueue", "atsPlatform"]);

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

      // Free taste: the backend returns the lifetime free counter on every save. Stop AT
      // the limit — the pre-submit caps don't know it, so the next submit would reach the
      // employer and only then be refused (invisible spend). Runs before the ATS advance
      // so a stopped campaign doesn't navigate to the next apply URL.
      if (serverResult && serverResult.free_limit != null && serverResult.free_used >= serverResult.free_limit) {
        await addToActivityLog(
          `All ${serverResult.free_limit} free applications used — subscribe to keep applying. Campaign stopped.`,
          "warn"
        );
        await chrome.storage.local.set({ campaignRunning: false });
        try { await apiPost("/campaign/stop", {}); } catch {}
        updateBadge();
      }

      // ATS pool-driven ADVANCE (GLOBAL_PLAN P1b): after a zero-touch ATS submit, walk the
      // automation tab to the next apply URL in the queue. Native (Indeed/ZR) campaigns have
      // no atsQueue and are driven by in-page navigation, so this is a no-op for them.
      await advanceAtsQueue();

      const cur = await chrome.storage.local.get("platformCounts");
      return { saved: true, platformCount: (cur.platformCounts || {})[platform] || 0, job_id: serverResult?.job_id };
    }

    // ----- ATS queue advance on a NON-submit outcome (fit-skip / already-applied /
    // missing form). phase_ats calls this from every early exit so a skipped job never
    // dead-stops the pool walk; the submit path advances via APPLICATION_SAVED instead.
    // Harvest-to-pool relay: content.js posts the job cards it saw on a board search
    // page; we forward them to the backend pool (INSERT-only server-side). This is the
    // compliant Indeed discovery path — the user's own browser saw these listings.
    // DEV-ONLY self-reload (double-gated): lets the test harness reload the unpacked
    // extension without hands on chrome://extensions. Requires hd_debug===true in
    // storage (set via the whitelisted flag bridge; no prod user has it). Reload wipes
    // nothing — chrome.storage survives.
    case "RELOAD_SELF": {
      const g = await chrome.storage.local.get("hd_debug");
      if (g.hd_debug === true) {
        setTimeout(() => chrome.runtime.reload(), 300);
        return { reloading: true };
      }
      return { reloading: false, error: "hd_debug required" };
    }

    case "INGEST_JOBS": {
      try {
        const jobs = (msg.data && msg.data.jobs) || [];
        if (!jobs.length) return { ok: true, saved: 0 };
        const r = await apiPost("/jobs/ingest", { jobs });
        if (r && r.saved > 0) {
          await addToActivityLog(`Added ${r.saved} new job${r.saved > 1 ? "s" : ""} from this search to your pool`, "info");
        }
        return { ok: true, saved: (r && r.saved) || 0 };
      } catch (e) {
        return { ok: false };
      }
    }

    case "ATS_JOB_DONE": {
      // Pool mode: this head is being skipped (dead posting / fit-skip / no form).
      // Durably flip it out of `approved` in the DB so it never re-enters the queue
      // in FUTURE runs either (the in-run guard is poolDoneUrls). Best-effort.
      try {
        const d = await chrome.storage.local.get(["atsPlatform", "atsQueue"]);
        const head = d.atsPlatform === "pool" && Array.isArray(d.atsQueue) ? d.atsQueue[0] : null;
        if (head && head.id) apiPatch(`/jobs/${head.id}/status`, { status: "skipped" }).catch(() => {});
      } catch {}
      await advanceAtsQueue();
      return { advanced: true };
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
        // FAIL CLOSED (ROADMAP_E2E.md P1): a 401 / timeout / missing verdict must NOT
        // auto-apply — applying to an un-vetted job under the user's identity is the
        // irreversible harm. Skip instead; the content-script gate surfaces it.
        return result && result.decision ? result
          : { decision: "skip", judged: false, failClosed: true, reason: "fit check returned no verdict — skipped for safety" };
      } catch {
        return { decision: "skip", judged: false, failClosed: true, reason: "fit check unavailable (auth/timeout) — skipped for safety" };
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

    // ----- Tap relay: a PHONE approves what this desktop prepared -----
    // Standalone bridge to the backend /review endpoints. awaitReview() (tap
    // review flow) publishes its card and polls the verdict through these, so a
    // dashboard open on a phone — where the chrome.storage bridge can't reach —
    // still gets the card and can decide. Failures are non-fatal: the in-browser
    // bridge remains the primary path.
    case "RELAY_REVIEW_PENDING": {
      try { return await apiPost("/review/pending", msg.data || {}); }
      catch { return { ok: false }; }
    }
    case "FETCH_REVIEW_DECISION": {
      try { return await apiGet("/review/pending"); }
      catch { return { review: null }; }
    }
    case "RELAY_REVIEW_DECIDED": {
      try { return await apiPost("/review/decision", msg.data || {}); }
      catch { return { ok: false }; }
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
      // Name the actual platform (captchas fire on ZR/Greenhouse/Lever too, not just Indeed).
      const site = platformDisplayNameFromUrl(data.url);
      // Persist the hand-off so the popup (GET_STATUS) and the dashboard live
      // view (ping.js HIREDROP_GET_LIVE_STATE) can show a "solve the captcha"
      // CTA that survives popup reopen / page reload. Cleared by
      // DETECTION_CLEARED, START_CAMPAIGN and STOP_CAMPAIGN.
      await chrome.storage.local.set({ captchaWaiting: { url: data.url, site, signal: data.signal, at: Date.now() } });
      // Local log so the popup shows it without waiting for a refresh.
      await addToActivityLog(`⚠️ ${site} asked for a human check — campaign paused`, "err");
      // System notification so the user sees this even if the popup is closed.
      try {
        await chrome.notifications.create({
          type: "basic",
          iconUrl: "icons/icon128.png",
          title: "HireDrop paused — action needed",
          message: `${site} is asking you to verify you're human. Open the automation window, solve it, and the campaign resumes automatically.`,
        });
      } catch {}
      return { handled: true };
    }

    // ----- Detection cleared (human solved the challenge; campaign resumed) -----
    case "DETECTION_CLEARED": {
      await chrome.storage.local.set({ captchaWaiting: null });
      await addToActivityLog("Human check cleared — campaign resumed", "ok");
      try {
        await apiPost("/activity", {
          message: "Human check cleared — campaign resumed",
          level: "info",
          phase: "detection_cleared",
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
        "captchaWaiting",
        "campaignCaps",
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
        limitPerPlatform: (data.campaignCaps && data.campaignCaps.perPlatform > 0) ? data.campaignCaps.perPlatform : DEFAULT_PER_PLATFORM,
        dailyLimit: (data.campaignCaps && data.campaignCaps.dailyTotal > 0) ? data.campaignCaps.dailyTotal : DEFAULT_DAILY_TOTAL,
        currentJob: data.currentJob || null,
        // popup.js hides its captcha alert off this flag; captchaWaiting carries
        // the details (site/url) for richer UIs.
        captchaDetected: !!data.captchaWaiting,
        captchaWaiting: data.captchaWaiting || null,
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

// Closing the WHOLE automation window doesn't reliably fire tabs.onRemoved before the
// process goes away — catch it at the window level too, or the campaign keeps "running"
// with no window (one of the zombie paths in ZOMBIE_FIX_PLAN.md).
chrome.windows.onRemoved.addListener(async (windowId) => {
  const data = await chrome.storage.local.get(["campaignWindowId", "campaignRunning"]);
  if (data.campaignRunning && data.campaignWindowId === windowId) {
    await chrome.storage.local.set({
      campaignRunning: false,
      campaignTabId: null,
      campaignWindowId: null,
      currentJob: null,
    });
    try { await apiPost("/campaign/stop", {}); } catch {}
    updateBadge();
  }
});
