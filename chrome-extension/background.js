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
    todayDate: localDay(),
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
    // 2026-09-09: same move from the other end. A logged_out that wasn't read on the
    // domain where applying happens is a guess (see logoutIsTrustworthy) — and a sticky
    // one: the dashboard reads this record straight out of storage via ping.js, so its
    // gate would keep refusing to launch and never give the service worker a chance to
    // clean up. Dropping it on update un-sticks browsers already poisoned by the old rule.
    for (const [p, rec] of Object.entries(conns)) {
      if (!logoutIsTrustworthy(p, rec)) { delete conns[p]; changed = true; }
    }
    if (changed) await chrome.storage.local.set({ platformConnections: conns });
  }
  await fetchAndCacheProfile().catch(() => {});
  ensureExtensionKey().catch(() => {}); // mint durable key ASAP so cold-start never 401s
  updateBadge();
});

// The application counters are keyed by DAY, and "day" has to mean the same thing to
// everyone who touches the key. content.js writes `todayDate` in the user's LOCAL day;
// this file used to compare it against the UTC day. In Hawaii (UTC-10) those disagree for
// ten hours out of every twenty-four — and onStartup, seeing a "different day", RESET
// todayCount and platformCounts to zero. The daily and per-platform caps are the
// ban-safety rails, so that quietly handed the engine a fresh budget on every Chrome
// restart during those hours. Live 09-04: two real applications recorded, ping reported
// today_count: 0.
function localDay() {
  return new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD in the user's timezone
}

chrome.runtime.onStartup.addListener(async () => {
  const data = await chrome.storage.local.get("todayDate");
  const today = localDay();
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
    const today = localDay();
    const todayCount = data.todayDate === today ? (data.todayCount || 0) : 0;

    // Does the campaign still EXIST? The flag alone cannot answer that: it lives in
    // chrome.storage and survives a closed laptop, so a woken service worker used to
    // report "running" over a run whose window died with the lid — the heartbeat never
    // expired and the campaign was immortal on paper (Igor: "активна, а заявок не
    // прибавляется"). #98 fixed the previous shape of this (ping stamped a pulse
    // unconditionally) and did NOT catch this one, because the ping does say true.
    //
    // The rule both bugs teach: A SIGNAL THAT CAN BE PRODUCED FROM SAVED STATE IS NOT A
    // SIGN OF LIFE. So the pulse is the window's existence.
    //
    // Existence, not visibility: a MINIMIZED window is alive and still applying, so
    // window_visible must stay a separate field and must never gate the heartbeat.
    let windowVisible = false;
    let windowAlive = false;
    if (data.campaignWindowId) {
      try {
        const win = await chrome.windows.get(data.campaignWindowId);
        windowAlive = true;
        windowVisible = win.state === "normal" || win.state === "maximized";
      } catch {}
    }
    const campaignAlive = !!data.campaignRunning && windowAlive;
    // Nobody home: stop claiming a pulse, and put our own flag down so the next Start is
    // a clean start rather than a resume of a corpse. One writer for the backend flag
    // stays the backend's TTL (#98/#107/#111) — this only clears OUR local copy.
    if (data.campaignRunning && !windowAlive) {
      await chrome.storage.local.set({ campaignRunning: false });
      await addToActivityLog(
        "⏹ Campaign window is gone (laptop closed or Chrome quit) — the run ended. Press Start when you're back.",
        "warn"
      );
      updateBadge();
    }

    const res = await fetch(`${CONFIG.API_BASE}${CONFIG.API_V1}/extension/ping`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        campaign_running: campaignAlive,
        today_count: todayCount,
        window_visible: windowVisible,
        version: chrome.runtime.getManifest().version,
        // WHICH install is talking. Chrome happily runs the same unpacked folder twice
        // (and store + unpacked side by side); each copy gets its own id and its own
        // storage, so an idle twin pings "not running" under the same account while the
        // real one is mid-application. Without this field the two are indistinguishable
        // in the logs — it cost most of 08-15 to work out that a twin existed at all.
        instance_id: chrome.runtime.id,
      }),
    });
    // Backend-authoritative Stop: if the dashboard stopped the campaign but our
    // postMessage stop was dropped (orphaned content script), the flag would stay set
    // and the extension keep applying. Honor the backend's flag as source of truth.
    try {
      const j = await res.json();
      if (j && j.should_run === false && data.campaignRunning) {
        // Say so out loud. This path killed a live run mid-form on 08-15 and left NOTHING
        // in the durable log — from the outside the campaign just froze on an open
        // application. Every stop must name itself.
        await addToActivityLog("⏹ Stopped by the server: the backend says this campaign is no longer running.", "warn");
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
    nativeWalkWatchdog().catch(() => {});
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

// `metadata` is OUR telemetry, not the user's: structured detail (unfilled field
// labels, platform, job) that never renders in the UI but lands in
// activity_log.metadata_json server-side, per user. That's what turns a one-off
// hand-back into the cross-user frequency data we fix by. Local storage keeps the
// human-readable line only — the dashboard reads that and nothing else.
async function addToActivityLog(text, cls, metadata) {
  const { activity_log } = await chrome.storage.local.get("activity_log");
  const logs = activity_log || [];
  logs.unshift({
    time: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    text,
    cls: cls || "",
  });
  if (logs.length > 50) logs.length = 50;
  // Every durable line is also a sign of life for the native walk — nativeWalkWatchdog
  // reads this stamp. Piggy-backing on the write we already do keeps it free.
  await chrome.storage.local.set({ activity_log: logs, walkAliveAt: Date.now() });

  // Best-effort mirror to backend (Phase 4.2). Never blocks; if the user
  // is logged out or the API is down, the local log still works.
  try {
    const level = cls === "err" ? "error" : (cls === "warn" ? "warn" : "info");
    const body = { message: text, level, phase: "extension" };
    if (metadata && typeof metadata === "object") body.metadata = metadata;
    await apiPost("/activity", body);
  } catch {}
}

// ---------------------------------------------------------------------------
// Indeed search URL builder
// ---------------------------------------------------------------------------

// A geo radius only makes sense around a real place — never for a remote search
// (Indeed rewrites "l=remote" and a radius there is meaningless). Returns a positive
// integer of miles, or null to omit the param.
function radiusMilesFor(loc, radius) {
  const n = Number(radius);
  if (!loc || String(loc).toLowerCase() === "remote" || !Number.isFinite(n) || n <= 0) return null;
  return Math.round(n);
}

// Work setting (remote / hybrid / on-site) → search refinement.
// Indeed & ZipRecruiter have NO stable work-type URL param (their sc=attr / refine codes
// churn and break silently), so we bias the QUERY with the literal word, which matches how
// postings actually label themselves ("Hybrid", "Remote"). Only 'hybrid' is injected:
// 'remote' is already handled by the location mechanism, and 'onsite' is the default for a
// city search — injecting the word "onsite" would over-narrow (few posts use it).
function workQueryToken(workSetting) {
  return workSetting === "hybrid" ? "hybrid" : "";
}
// LinkedIn HAS a real, stable work-type filter (f_WT): 1=on-site, 2=remote, 3=hybrid.
function workLinkedInWT(workSetting) {
  return { remote: "2", hybrid: "3", onsite: "1" }[workSetting] || "";
}
function joinQuery(keywords, workSetting) {
  const kw = keywords && keywords.length ? keywords.join(" ") : "";
  return [kw, workQueryToken(workSetting)].filter(Boolean).join(" ");
}

function buildIndeedUrl(keywords, location, jobType, radius, workSetting) {
  const params = new URLSearchParams();
  const q = joinQuery(keywords, workSetting);
  if (q) params.set("q", q);
  const locMap = { usa: "United States", remote: "remote", europe: "" };
  const loc = locMap[location] !== undefined ? locMap[location] : location;
  if (loc) params.set("l", loc);
  const rad = radiusMilesFor(loc, radius);
  if (rad) params.set("radius", String(rad)); // Indeed radius= (miles): 0/5/10/15/25/35/50/100
  const jtMap = { "full-time": "fulltime", "part-time": "parttime", contract: "contract" };
  if (jobType && jtMap[jobType]) params.set("jt", jtMap[jobType]);
  params.set("iafilter", "1");
  return `https://www.indeed.com/jobs?${params.toString()}`;
}

function buildZipRecruiterUrl(keywords, location, jobType, radius, workSetting) {
  const params = new URLSearchParams();
  const q = joinQuery(keywords, workSetting);
  if (q) params.set("search", q);
  const locMap = { usa: "United States", remote: "Remote", europe: "" };
  const loc = locMap[location] !== undefined ? locMap[location] : (location || "");
  if (loc) params.set("location", loc);
  const rad = radiusMilesFor(loc, radius);
  if (rad) params.set("radius", String(rad)); // ZipRecruiter radius= (miles)
  const jtMap = { "full-time": "full_time", "part-time": "part_time", contract: "contract" };
  if (jobType && jtMap[jobType]) params.set("employment_type[]", jtMap[jobType]);
  // /candidate/search requires login; /jobs-search works without auth
  return `https://www.ziprecruiter.com/jobs-search?${params.toString()}`;
}

// LinkedIn search-walk — Easy Apply ONLY (f_AL=true). We only drive the in-modal 1-click
// flow; external-apply jobs are skipped by the content script (PLATFORMS_MASTER_PLAN Phase 2).
// ONE keyword phrase per search: joining phrases ("healthcare marketing social media manager")
// over-narrows LinkedIn to ~2 results with 0 Easy Apply (live 2026-08-01: single phrase → 25
// cards, 3-5 Easy Apply). Same root cause as Workday's compound searchText (#80). Phrase
// rotation when one is exhausted = v2.
// location: use a real GEO ("United States") — the literal string "Remote" makes LinkedIn
// rewrite the URL (location→f_WT=2) and DROP f_AL in that rewrite (live 2026-08-01).
function buildLinkedInUrl(keywords, location, jobType, radius, workSetting) {
  const params = new URLSearchParams();
  if (keywords && keywords.length) params.set("keywords", String(keywords[0]));
  const locMap = { usa: "United States", remote: "United States", europe: "" };
  const loc = locMap[location] !== undefined ? locMap[location] : (location || "United States");
  if (loc) params.set("location", loc);
  // Explicit work-setting wins; otherwise fall back to the legacy location==remote branch.
  const wt = workLinkedInWT(workSetting) || (location === "remote" ? "2" : "");
  if (wt) params.set("f_WT", wt); // 1=on-site, 2=remote, 3=hybrid — LinkedIn's own param
  if (wt !== "2") {
    const rad = radiusMilesFor(loc, radius);
    if (rad) params.set("distance", String(rad)); // LinkedIn uses distance= (miles), not radius=
  }
  params.set("f_AL", "true"); // Easy Apply filter — the only jobs our handler can drive
  return `https://www.linkedin.com/jobs/search/?${params.toString()}`;
}

function buildPlatformUrl(platform, keywords, location, jobType, radius, workSetting) {
  if (platform === "ziprecruiter") return buildZipRecruiterUrl(keywords, location, jobType, radius, workSetting);
  if (platform === "linkedin") return buildLinkedInUrl(keywords, location, jobType, radius, workSetting);
  return buildIndeedUrl(keywords, location, jobType, radius, workSetting);
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

// ---- STAGE ORDER (pure; fixtures in tests/platform-order.test.js) ------------------
// Igor's "All connected platforms" verdict (#143/#180): run the boards by yield first —
// Indeed → ZipRecruiter — and only then walk the saved ATS pool. Two separate bugs lived
// in this order before 09-13 and both produced runs that never touched a board:
//   · the opener picked the pool whenever greenhouse was anywhere in the list;
//   · "pool complete" ended the campaign instead of handing off to the untried boards.
// Both now answer to these two functions, so the order exists in one place.

/** The stage a run OPENS on: a selected board always outranks the pool. */
function pickAtsOpener(platforms) {
  const list = platforms || [];
  if (list.some((p) => AUTO_APPLY_PLATFORMS.includes(p))) return null;
  return list.find((p) => ATS_ZERO_TOUCH_PLATFORMS.includes(p)) || null;
}

/** The next stage after `tried` are spent, or null to stop. Pool is always last.
 *  Only SELECTED platforms are eligible — switching a user onto a board they never
 *  picked is the consent breach "All connected" was written to prevent. */
function pickNextStage(tried, selected, conns) {
  const done = tried || [];
  const sel = selected || [];
  const c = conns || {};
  const board = ["indeed", "ziprecruiter"].find(
    (p) => !done.includes(p) && sel.includes(p) && (p === "indeed" || c[p]?.status === "connected"));
  // Indeed needs no stored connection record (the resume lives on the Indeed account);
  // ZR needs a live "connected" one or the walk just hits a login wall.
  return board || ATS_ZERO_TOUCH_PLATFORMS.find((p) => !done.includes(p) && sel.includes(p)) || null;
}
// ------------------------------------------------------------------------------------

// Build the ATS apply queue for a campaign (GLOBAL_PLAN P1a+P1b): populate the pool via
// /jobs/find-ats, then pull this platform's jobs from /jobs/ats-queue, capped at the
// per-platform rail. Each item = { applyUrl, title, company }.
//
// The server endpoint applies the CURRENT search (keywords/type/location) — the same cut
// as the Tap deck. This used to read raw `GET /jobs`, which is the INSERT-only pool, i.e.
// the archive: on 09-13 that walked 15 Oura/Braze engineering postings harvested weeks
// earlier under `ai engineer` for a profile that now reads `event manager`, skipped all 15
// on fit, and ended the run in 2m26s with zero applications. A stale queue is worse than
// an empty one: it looks like work. Returns { queue, pool, offSearch } — offSearch is the
// honest reason an empty queue is empty, so the campaign can say "your pool holds N, none
// match your search" instead of a bare "no jobs".
async function buildAtsQueue(platform, perPlatformCap) {
  try { await apiPost("/jobs/find-ats", {}); } catch (e) { /* discovery best-effort */ }
  const cap = perPlatformCap > 0 ? perPlatformCap : 20;
  let res = null;
  // No fallback to the unfiltered pool on error: falling back to the archive is exactly
  // the failure this replaced. An honest zero beats a plausible wrong queue.
  try { res = await apiGet(`/jobs/ats-queue?platform=${encodeURIComponent(platform)}&limit=${cap}`); }
  catch (e) { return { queue: [], pool: 0, offSearch: 0, error: true }; }
  // Drop jobs we already applied to (URL or title|company key) — an already-applied job
  // at the queue head used to dead-stop the walk: phase_ats skips it silently and only a
  // real submit advances the queue. Mirrors content.js's dedup (jobDedupKey format).
  // Stays client-side: the extension holds the authoritative applied sets.
  const dd = await chrome.storage.local.get(["appliedUrls", "appliedJobKeys"]);
  const appliedUrls = new Set(dd.appliedUrls || []);
  const appliedKeys = new Set(dd.appliedJobKeys || []);
  const norm = (s) => (s || "").toLowerCase().replace(/\s+/g, " ").trim();
  const queue = (res.jobs || [])
    .filter((j) => j.link || j.apply_url)
    .filter((j) => {
      const url = (j.link || j.apply_url).split("?")[0];
      return !appliedUrls.has(url) && !appliedKeys.has(`${norm(j.title)}|${norm(j.company)}`);
    })
    .map((j) => ({ applyUrl: j.link || j.apply_url, title: j.title || "", company: j.company || "" }));
  return { queue, pool: res.pool || 0, offSearch: res.off_search || 0 };
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

async function buildApprovedAtsQueue(perPlatformCap, opts) {
  // The SERVER decides this list now (GET /campaign/queue, jobflow #162). It applies the
  // same four rules this function used to apply locally — approved swipes only, platforms
  // an approve can really be submitted to, nothing already applied, then the per-platform
  // ban cap and today's tier budget — with one difference that matters: "already applied"
  // is matched by POSTING IDENTITY (#161), not by URL text, and it is read from the
  // applications table instead of chrome.storage.
  //
  // That local set was the bug. appliedUrls/appliedJobKeys live in ONE Chrome profile: on
  // a fresh profile they are empty, so a pool row still marked `approved` (because the
  // apply had been written under another spelling of the same URL) would be walked again
  // and the employer would get a SECOND application. It also meant nothing outside that
  // profile could say what was left — no progress on the dashboard, nothing for the phone.
  //
  // poolDoneUrls stays: it is the IN-RUN guard (a posting that skips is not applied, so
  // the server would keep offering it) and it is reset at every campaign start.
  let payload = null;
  try {
    payload = await apiGet("/campaign/queue");
  } catch (e) {
    await addToActivityLog("Couldn't reach the server for your approved list — will retry shortly.", "warn");
    return [];
  }
  const dd = await chrome.storage.local.get(["poolDoneUrls", "tapNativePool"]);
  const doneUrls = new Set(dd.poolDoneUrls || []);
  // Native by-link (Indeed verified, ZR pending) only under the tapNativePool flag:
  // Indeed's SmartApply last step doesn't complete in the throttled background window, so
  // leaving it in the default pool burned every run on non-completing Indeed jobs and
  // never reached the ATS submitters (2026-08-04: todayCount stayed 0 all night).
  const poolPlatforms = dd.tapNativePool === true
    ? ATS_PLATFORMS.concat(POOL_NATIVE_VERIFIED, POOL_NATIVE_PENDING)
    : ATS_PLATFORMS;
  // Caller-supplied exclusions — an auto run drops Lever (its submit needs a human at the
  // captcha), so those approvals wait for a tap run instead of stalling an unattended one.
  const skip = (opts && opts.skipPlatforms) || [];

  const out = [];
  for (const j of (payload && payload.queue) || []) {
    if (!poolPlatforms.includes(j.platform)) continue;
    if (skip.includes(j.platform)) continue;
    const url = normalizePoolApplyUrl(j.platform, j.apply_url);
    if (!url || doneUrls.has(url)) continue;
    out.push({ id: j.id, applyUrl: url, title: j.title || "", company: j.company || "", platform: j.platform });
  }
  // perPlatformCap is the server's now (payload.cap_per_platform) — kept as an argument so
  // callers don't change, and honoured here only as a belt in case an old build calls in.
  const cap = perPlatformCap > 0 ? perPlatformCap : 15;
  const per = {};
  return out.filter((j) => {
    const n = per[j.platform] || 0;
    if (n >= cap) return false;
    per[j.platform] = n + 1;
    return true;
  });
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
  // Fix stale pool rows carrying a DOUBLED Ashby path (/application/application) — that
  // renders a form-less stub -> phase_ats "couldn't open" -> 0 Ashby submits (2026-08-04).
  // Backend now appends /application idempotently; this rescues already-discovered rows.
  if (typeof job.applyUrl === "string") {
    job.applyUrl = job.applyUrl.replace(/\/application\/application(\/?)$/, "/application$1");
  }
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
        const { atsPlatform, campaignCaps, poolLeadMode } =
          await chrome.storage.local.get(["atsPlatform", "campaignCaps", "poolLeadMode"]);
        if (atsPlatform === "pool" && running) {
          const cap = (campaignCaps && campaignCaps.perPlatform) || 15;
          // An auto run must not pick up a Lever approval mid-walk either — same reason as
          // at Start: its captcha needs a human, and nobody is watching an auto run.
          const more = await buildApprovedAtsQueue(
            cap,
            { skipPlatforms: poolLeadMode === "auto" ? ["lever"] : [] },
          );
          if (more.length) {
            await chrome.storage.local.set({ atsQueue: more, atsNavAt: Date.now(), atsNavTries: 0 });
            await addToActivityLog(`Applying ${more.length} more approved job${more.length > 1 ? "s" : ""} you swiped…`, "info");
            await navigatePoolNext(campaignTabId, more[0]);
            return;
          }
          await chrome.storage.local.set({ atsQueue: [] });
          await chrome.storage.local.remove(["atsNavAt", "atsNavTries"]);
          if (poolLeadMode === "auto") {
            // Head start spent — the rest of the run is the ordinary board sweep. Falls
            // through to PLATFORM_EXHAUSTED below, which owns every hand-off and the
            // triedPlatforms ledger (seeded with "pool", so the boards are all still open).
            await addToActivityLog("Sent the jobs you approved — now searching the boards for more.", "info");
          } else {
            await addToActivityLog("Caught up on approved jobs — swipe more and we'll apply them.", "info");
            return;
          }
        }
        // Pool walked — hand off to whatever board is still untried instead of ending the
        // run. This used to stop the campaign outright, which is how a user with all six
        // platforms selected got a 2m26s run that never touched Indeed or ZipRecruiter
        // (09-13). PLATFORM_EXHAUSTED owns every board hand-off and the triedPlatforms
        // ledger; it stops the campaign itself when nothing is left, so the "finished"
        // path lives in exactly one place.
        await chrome.storage.local.remove(["atsNavAt", "atsNavTries"]);
        await handleMessage(
          {
            type: "PLATFORM_EXHAUSTED",
            platform: atsPlatform || "greenhouse",
            reason: poolLeadMode === "auto"
              ? "sent every job you approved"
              : "walked every discovered zero-touch job",
          },
          {},
        );
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

// The NATIVE walk (Indeed / ZipRecruiter search → job → form) had no watchdog at all:
// atsWalkWatchdog only guards a pool/ATS queue, and its guard is the queue head, which a
// native run doesn't have. So any page that gives the walk nothing to act on froze the
// whole campaign with `running` still on — live 09-06, two Indeed "Not Found" pages held
// Igor's automation window for 31 and 15 minutes, and only the SERVER-side stall watch
// noticed. content.js now names a dead link and advances (pageLooksNotFound); this is the
// backstop for every other shape of silence: an injection miss, a wall, a page that never
// fires an event.
//
// It must never fire while the walk is legitimately parked WAITING FOR THE HUMAN — a
// captcha hand-off, a login wall, a tap review. Reloading the tab under a human solving a
// captcha would throw their work away, so each of those states is checked first.
const NATIVE_WATCHDOG_SILENT_MS = 10 * 60 * 1000;

async function nativeWalkWatchdog() {
  const d = await chrome.storage.local.get([
    "campaignRunning", "atsPlatform", "campaignTabId", "walkAliveAt", "walkNudges",
    "captchaWaiting", "reviewPending", "platformConnections",
  ]);
  if (!d.campaignRunning || d.atsPlatform || !d.campaignTabId || !d.walkAliveAt) return;
  const age = Date.now() - d.walkAliveAt;
  if (age < NATIVE_WATCHDOG_SILENT_MS) {
    if (d.walkNudges) await chrome.storage.local.set({ walkNudges: 0 }); // it recovered
    return;
  }
  // Parked on purpose, waiting for Igor's hands — silence here is the FEATURE.
  if (d.captchaWaiting || d.reviewPending) return;
  const conns = d.platformConnections || {};
  const loggedOutRecently = Object.entries(conns).some(
    ([platform, c]) => c && c.status === "logged_out" && c.checkedAt &&
      logoutIsTrustworthy(platform, c) &&
      Date.now() - new Date(c.checkedAt).getTime() < 2 * 60 * 60 * 1000
  );
  if (loggedOutRecently) return;
  try { await chrome.tabs.get(d.campaignTabId); } catch { return; } // window closed by hand

  const mins = Math.round(age / 60000);
  const nudges = d.walkNudges || 0;
  if (nudges < 2) {
    await chrome.storage.local.set({ walkAliveAt: Date.now(), walkNudges: nudges + 1 });
    await addToActivityLog(`⏱ Watchdog: the job page has been silent for ${mins} min — reloading it`, "warn");
    chrome.tabs.reload(d.campaignTabId).catch(() => {});
    return;
  }
  // Two reloads and still nothing. Stop honestly instead of leaving a green "running"
  // badge over a dead walk (the zombie shape of #98, from the other end).
  await chrome.storage.local.set({ campaignRunning: false, walkNudges: 0 });
  await addToActivityLog(
    `⏹ Watchdog: the walk stayed silent through two reloads (${mins} min) — stopping the campaign so it isn't "running" while nothing happens. Start it again anytime.`,
    "warn"
  );
  try { await apiPost("/campaign/stop", {}); } catch {}
  updateBadge();
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
    "poolIdleSince", "poolLeadMode",
  ]);
  if (!d.campaignRunning || d.atsPlatform !== "pool" || !d.campaignTabId) return;
  // Waiting inside the pool for more swipes is the TAPALKA's behaviour. An auto run that
  // led with approved cards has already handed off to the boards by now; this guard makes
  // that explicit so a stray pool state can never park an auto run on "waiting for swipes".
  if (d.poolLeadMode === "auto") return;
  if ((Array.isArray(d.atsQueue) && d.atsQueue.length) || d.atsNavAt) return; // busy, not idle
  const cap = (d.campaignCaps && d.campaignCaps.perPlatform) || 15;
  const more = await buildApprovedAtsQueue(cap);
  if (!more.length) {
    // Idle with nothing approved. Keep listening for phone-remote swipes (that's the feature),
    // but don't leave the green "running" badge on FOREVER — auto-stop after a long idle stretch
    // (2026-08-04, Igor: "кнопка не выключилась / крутилось всю ночь"). 2h is generous: a phone
    // user who's actively swiping tops it up well within that; a walked-away one gets a clean stop.
    const IDLE_STOP_MS = 2 * 60 * 60 * 1000;
    if (!d.poolIdleSince) { await chrome.storage.local.set({ poolIdleSince: Date.now() }); return; }
    if (Date.now() - d.poolIdleSince > IDLE_STOP_MS) {
      await chrome.storage.local.set({ campaignRunning: false });
      await chrome.storage.local.remove(["poolIdleSince", "atsNavAt", "atsNavTries"]);
      await addToActivityLog("Campaign auto-stopped after 2h idle (no new swipes) — start again anytime.", "ok");
      try { await apiPost("/campaign/stop", {}); } catch {}
      updateBadge();
    }
    return;
  }
  try { await chrome.tabs.get(d.campaignTabId); } catch { return; } // automation tab gone
  await chrome.storage.local.set({ atsQueue: more, atsNavAt: Date.now(), atsNavTries: 0, poolIdleSince: null });
  await addToActivityLog(`Applying ${more.length} more approved job${more.length > 1 ? "s" : ""} you swiped…`, "info");
  await navigatePoolNext(d.campaignTabId, more[0]);
}

// ---------------------------------------------------------------------------
// Connection records: provenance decides whether a "logged out" may gate a launch
//
// Indeed keeps its SEARCH host and its APPLY host on separate session surfaces —
// www.indeed.com's gnav renders SignIn for a session that applies perfectly well. That
// guess used to be stored as fact, and both pre-flight gates (this file's START_CAMPAIGN
// and the dashboard's QuickActions, which reads the very same record over the ping.js
// bridge) then refused to launch on Indeed. Live 09-06: gate said logged_out, a campaign
// that hopped to Indeed from ZipRecruiter submitted a real application at 05:11 UTC.
// content.js now stamps each record with the host it was read on; a logged_out for Indeed
// only counts from the domains where applying actually happens. Records written before
// this rule carry no host at all — those are guesses too, and get dropped on first read,
// which is what un-sticks a browser that's already poisoned.
// (INDEED_APPLY_HOSTS is mirrored in content.js — content scripts don't see config.js.)
const INDEED_APPLY_HOSTS = ["smartapply.indeed.com", "secure.indeed.com"];

function logoutIsTrustworthy(platform, rec) {
  if (!rec || rec.status !== "logged_out") return true;
  if (platform !== "indeed") return true; // ZR's login link is server-rendered on every host
  const host = rec.host;
  if (!host) return false;
  return INDEED_APPLY_HOSTS.some((h) => host === h || host.endsWith("." + h));
}

// Every read of platformConnections goes through here, so a poisoned record can't outlive
// the next read — including the dashboard's 10s poll, which is how the website-side gate
// heals without a redeploy.
async function getPlatformConnections() {
  const conns = (await chrome.storage.local.get("platformConnections")).platformConnections || {};
  const kept = {};
  let dropped = false;
  for (const [platform, rec] of Object.entries(conns)) {
    if (!logoutIsTrustworthy(platform, rec)) { dropped = true; continue; }
    kept[platform] = rec;
  }
  if (dropped) await chrome.storage.local.set({ platformConnections: kept });
  return kept;
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

    // Store the posting text the content script just read off the detail page, so the
    // server stops treating an Indeed search snippet as the job description.
    case "SAVE_JOB_DESCRIPTION": {
      const j = msg.data || {};
      if (!j.url || !j.description) return { stored: false };
      try {
        const r = await apiPost("/jobs/describe", {
          link: j.url,
          description: j.description,
          title: j.title || "",
          company: j.company || "",
          platform: j.platform || "indeed",
        });
        return r;
      } catch (err) {
        return { stored: false, error: err.message };
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
        poolIdleSince: null, // reset the 2h idle-auto-stop timer for the fresh run
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
        // Radius was silently dropped here (present in profile + backend, never merged into
        // the extension's campaignFilters) → every city search ran radius-less. Merge it so
        // content.js builders (initial nav + pagination) actually see it. 2026-08-14 fix.
        search_radius_miles: raw.search_radius_miles ?? profile.search_radius_miles ?? null,
        // Work setting (remote/hybrid/onsite) — the "Hybrid" filter. Threaded to URL builders.
        work_setting: raw.work_setting || profile.work_setting || "",
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
      // Auto vs Tap decides whether this run SUBMITS without a human. Reading that off a
      // request that may never have arrived — the `catch {}` above leaves preSt null, and
      // null used to read as "auto" — handed Tap users a full auto walk over cards they
      // never swiped. Third layer of the #98 class: a value that can be produced from
      // nothing is not evidence. Refuse and say why: a campaign that didn't start is
      // recoverable in one click, applications nobody approved are not.
      // (submit_mode_known is the backend's half of the same rule — its profile read has
      // an unreadable case too, and it no longer hides that behind a default "auto".
      // Older backends don't send the field; undefined means "no reason to doubt it".)
      if (!preSt || preSt.submit_mode_known === false) {
        return {
          started: false,
          error: "mode_unknown",
          message: "Couldn't reach HireDrop to check whether you're on Auto or Tap — starting now could apply to jobs you never approved. Check your connection and press Start again.",
        };
      }
      const tapMode = preSt.submit_mode === "tap";

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

      // APPROVED SWIPES LEAD THE RUN — in BOTH modes (Igor 09-19).
      //
      // Tap builds its whole queue from them (that IS tap). Auto used to not look at them
      // at all: `approved` rows are consumed by a tap run and nothing else, so a user who
      // swiped and then ran Auto left a stack nobody would ever pick up — live 09-11, a
      // real account had 4 approved since 09-02, never sent. The dashboard dock (website
      // #169) made that visible and offered a one-click fix, but a surface that reports a
      // dead end is second best to a run that doesn't create one.
      //
      // This does NOT hand Auto more volume: the daily budget is counted from applications
      // actually sent (server-side, one counter for both modes), so approved cards simply
      // take the FIRST slots of the same 30. What changes is the order — the jobs a human
      // picked go before the ones the machine found.
      //
      // Consent is intact in both directions: an approved row is a human decision, so
      // sending it is exactly what was asked; and a run still never touches a card nobody
      // swiped unless the mode says auto.
      const approvedCap = (preSt && preSt.limit_per_platform) || 15;
      let tapPoolQueue = await buildApprovedAtsQueue(
        approvedCap,
        // Lever stops at an hCaptcha a human has to clear. In tap the human is at the
        // wheel, so a Lever card is a legitimate pause; in auto nobody is watching, so it
        // would just hold the window until the watchdog skips it. Leave those approvals
        // for a tap run rather than burning a slot on a submit that can't complete.
        { skipPlatforms: tapMode ? [] : ["lever"] },
      );
      if (tapMode && !tapPoolQueue.length) {
        // Footgun guard (tap only): with nothing approved yet, do NOT fall through to an
        // auto walk (native Indeed search / GH auto-sweep) — that would apply jobs the
        // user never swiped. Auto has no such guard to trip: an empty approved list there
        // just means the run starts the way it always did.
        return {
          started: false,
          error: "no_approved_jobs",
          // Covers both truths honestly: nothing approved yet, OR everything approved
          // was already applied/dead (dedup excluded it) — live-test 2026-07-27 found
          // the old "swipe first" wording gaslighting a user whose swipes WERE consumed.
          message: "Nothing new to apply — jobs you approved before are already applied or closed. Swipe Approve on new cards, then Start.",
        };
      }

      // Pool-driven ATS target (GLOBAL_PLAN P1+P2):
      // - greenhouse: zero-touch → any mode, full-auto;
      // - lever: hCaptcha at submit → tap mode only (human approves + clears it);
      //   in auto mode refuse with a clear message instead of a campaign that can't submit.
      //
      // ORDER: see pickAtsOpener — a selected board outranks the pool, and the pool is
      // reached through PLATFORM_EXHAUSTED once the boards are done.
      const hasBoard = (filters.platforms || []).some((p) => AUTO_APPLY_PLATFORMS.includes(p));
      let atsTarget = pickAtsOpener(filters.platforms);
      // Lever-only runs: no board, no zero-touch pool, just Lever. `hasBoard` guards it
      // because atsTarget is now null whenever a board leads the run — without this, any
      // auto user who merely has Lever ticked alongside Indeed would be refused at Start.
      if (!tapPoolQueue.length && !atsTarget && !hasBoard && (filters.platforms || []).includes("lever")) {
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
        const conns = await getPlatformConnections();
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
        await addToActivityLog(
          tapMode
            ? `Applying to ${atsQueue.length} approved jobs (your swipes) — working through them now.`
            : `Starting with ${atsQueue.length} job${atsQueue.length > 1 ? "s" : ""} you approved, then searching the boards for more.`,
          "info");
      } else if (atsTarget) {
        const capState = (await chrome.storage.local.get("campaignCaps")).campaignCaps || {};
        const built = await buildAtsQueue(atsTarget, capState.perPlatform || 20);
        atsQueue = built.queue;
        if (!atsQueue.length) {
          // Name which zero it is. "No jobs yet" and "your pool is full of jobs that no
          // longer match your search" need different actions from the user, and the old
          // single message sent everyone to "broaden your keywords" — the wrong advice
          // for the case where the keywords are right and the pool is stale.
          return {
            started: false,
            error: "no_ats_jobs",
            message: built.error
              // Never dress a failed read as "no jobs" — a source that silently
              // contributes zero is indistinguishable from a broken one (#113).
              ? `Couldn't load your ${atsTarget} jobs just now (the server didn't answer). Try Start again in a moment.`
              : built.offSearch > 0
              ? `None of the ${built.pool} ${atsTarget} jobs in your pool match your current search — ${built.offSearch} are leftovers from earlier keywords. They'll refresh as new jobs are found.`
              : `No zero-touch ${atsTarget} jobs to apply to yet — try again shortly or broaden your keywords.`,
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
        // ONE keyword per search (index 0 to start); content.js rotates to the next
        // keyword as each is exhausted. Cramming all keywords into one query returned junk.
        : buildPlatformUrl(primaryPlatform, filters.keywords.slice(0, 1), filters.location, filters.job_type, filters.search_radius_miles, filters.work_setting);
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
        // Keyword walk state, all per RUN. content.js goes one page per keyword and
        // rotates through the whole list before deepening (kwLap = which page every
        // phrase is on; kwDone = phrases that returned nothing this run).
        // kwIndex starts at 0 on purpose: WHICH phrase leads a run is the server's
        // decision — /campaign/start round-robins the list (modules/keyword_rotation,
        // cursor in campaign_states.filters) and the dashboard arms us with that order.
        // A second cursor kept here would advance independently of the server's and the
        // two would drift apart.
        kwIndex: 0,
        kwLap: 0,
        kwDone: [],
        // Platform-failover ledger — PLATFORM_EXHAUSTED never revisits these. Seed it with
        // the stage this run actually OPENS on: a pool-led run (no board selected) opens on
        // the ATS target, and seeding "indeed" there would both lie and let the failover
        // walk back into the pool it just finished. An APPROVED-led run opens on the pool,
        // so it seeds "pool" — seeding the board here would burn the board before it ran,
        // which is precisely the 09-13 failure (a run that never touched Indeed) in reverse.
        triedPlatforms: [tapPoolQueue.length ? "pool" : (atsTarget || primaryPlatform)],
        // Who leads the pool walk, and therefore what happens when it drains:
        //   "tap"  → go idle INSIDE the pool and wait for more swipes (that's the tapalka);
        //   "auto" → hand off to the boards, because the approved cards were only the
        //            head start and the rest of the run is the ordinary auto sweep.
        // Without this the auto run would sit idle after the last approved card, looking
        // exactly like a finished campaign while 26 of its 30 slots went unused.
        poolLeadMode: tapPoolQueue.length ? (tapMode ? "tap" : "auto") : null,
        // Consent boundary for that failover (Igor 09-11): the launch modal's default is
        // "All connected platforms" (platform_mode "all") — switching boards is what the
        // user asked for. A single pick ("single") means THIS board only: on exhaustion
        // we stop honestly instead of surprising them on a platform they didn't choose.
        // Absent field (older dashboard) = the old always-failover behavior.
        platformFailover: (filters.platform_mode || "all") !== "single",
        zrNoBtnStreak: 0, // external-apply wall guard counter
        unreadableStreak: 0, // consecutive unreadable job pages — platform-broken detector
        // Stale per-job state from the LAST run must not leak into this one: with these
        // left over, the fresh homepage was treated as an open application form and
        // phase3 ran against it, logging "form abandoned" for a job we never touched
        // (live 08-15, after a Chrome restart).
        currentJobInfo: null,
        generatedCoverLetter: "",
        pendingJobs: [],
        currentJobIndex: 0,
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
        const conns = await getPlatformConnections();
        conns[msg.platform] = { status: msg.status, checkedAt: new Date().toISOString(), host: msg.host || null };
        await chrome.storage.local.set({ platformConnections: conns });
      }
      return { ok: true };
    }

    // Dashboard reads connection status (via ping.js bridge) to render "connect" chips.
    case "GET_PLATFORM_CONNECTIONS": {
      const conns = await getPlatformConnections();
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
        const conns = await getPlatformConnections();
        // The wall was hit on a real page — carry its host so the record survives the
        // provenance check above (a hostless logged_out is treated as a guess).
        conns[msg.platform] = { status: "logged_out", checkedAt: new Date().toISOString(), host: msg.host || null };
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

    // ----- Platform exhausted (from content.js) → autonomous platform failover -----
    // Igor 2026-08-15: a campaign must not grind to a halt because one board turned out
    // to be a wall of external-apply listings / hit its per-platform cap / ran out of
    // keywords. Switch to the next walkable board automatically; stop only when every
    // candidate has been tried. GH/Lever/Ashby run via the ATS queue, not this walk.
    // "Am I the tab this campaign is driving?" Asked by every content script at init so
    // session-restored tabs from an older run stay out of the way (see content.js init).
    case "AM_I_CAMPAIGN_TAB": {
      const tabId = sender && sender.tab && sender.tab.id;
      const { campaignTabId } = await chrome.storage.local.get("campaignTabId");
      if (!tabId || !campaignTabId) return { known: false };
      return { known: true, isCampaignTab: tabId === campaignTabId };
    }

    case "PLATFORM_EXHAUSTED": {
      const ex = await chrome.storage.local.get([
        "campaignRunning", "campaignFilters", "campaignTabId", "triedPlatforms", "platformConnections",
        "platformFailover",
      ]);
      if (!ex.campaignRunning) return { ok: true, stopped: true };
      const NAMES = {
        indeed: "Indeed", ziprecruiter: "ZipRecruiter", linkedin: "LinkedIn",
        greenhouse: "Greenhouse", lever: "Lever", ashby: "Ashby",
      };
      const curPlat = msg.platform || "unknown";
      // Single-platform runs (launch modal "Pick one platform") never switch boards:
      // the user consented to THIS board only. Stop out loud with the road back —
      // "offer the fix, not the exit". Missing key (run started pre-1.8.2) = failover on.
      if (ex.platformFailover === false) {
        await addToActivityLog(
          `${NAMES[curPlat] || curPlat} exhausted (${msg.reason || "no applyable jobs"}) — you picked ` +
          `${NAMES[curPlat] || curPlat} only for this run, so we stopped instead of switching boards. ` +
          `Start again and choose "All connected platforms" to keep going elsewhere.`,
          "warn");
        // Still an orderly ending: the board ran out and the user's own choice said stop
        // here. Not a death — the banner must not blame a browser that never went away.
        return await handleMessage({ type: "STOP_CAMPAIGN", reason: "run complete", outcome: "completed" }, sender);
      }
      const tried = Array.from(new Set([...(ex.triedPlatforms || []), curPlat]));
      const conns = ex.platformConnections || {};
      const next = pickNextStage(tried, (ex.campaignFilters || {}).platforms, conns);
      if (!next) {
        // END OF RUN, and it is a COMPLETION, not a death. The dashboard used to see only
        // `running: false` and print its one story — "the browser that was applying went
        // away: closing your laptop, quitting Chrome" — which on 09-13 told Igor his
        // laptop had closed while he sat in front of it. The outcome rides in metadata,
        // not in the wording, so the banner can't drift from the truth the way a parsed
        // string does (#147's lesson about matching on log text).
        await addToActivityLog(
          `${NAMES[curPlat] || curPlat} exhausted (${msg.reason || "no applyable jobs"}) and no other platform left — run complete.`,
          "ok",
          { outcome: "completed", last_platform: curPlat, tried_platforms: tried });
        return await handleMessage({ type: "STOP_CAMPAIGN", reason: "run complete", outcome: "completed" }, sender);
      }
      const f = ex.campaignFilters || {};
      // Handing off TO the pool is a different move than handing off to a board: there is
      // no search to run, only saved apply URLs to walk. Build the queue first — an empty
      // one means this stage has nothing to offer, so mark it tried and ask again rather
      // than navigating the window to a job that isn't there.
      if (ATS_ZERO_TOUCH_PLATFORMS.includes(next)) {
        const caps = (await chrome.storage.local.get("campaignCaps")).campaignCaps || {};
        const built = await buildAtsQueue(next, caps.perPlatform || 20);
        if (!built.queue.length) {
          await addToActivityLog(
            built.error
              ? `${NAMES[curPlat] || curPlat} exhausted — couldn't load your ${NAMES[next]} jobs (the server didn't answer), so this stage was skipped.`
              : built.offSearch > 0
              ? `${NAMES[curPlat] || curPlat} exhausted — ${NAMES[next]} has ${built.pool} saved jobs but none match your current search (${built.offSearch} are from earlier keywords).`
              : `${NAMES[curPlat] || curPlat} exhausted — no ${NAMES[next]} jobs saved to apply to yet.`,
            built.error ? "warn" : "info");
          await chrome.storage.local.set({ triedPlatforms: tried });
          return await handleMessage({ type: "PLATFORM_EXHAUSTED", platform: next, reason: "nothing applyable in the pool" }, sender);
        }
        await addToActivityLog(
          `${NAMES[curPlat] || curPlat} exhausted (${msg.reason || "no applyable jobs"}) — switching to ${built.queue.length} saved ${NAMES[next]} jobs.`,
          "info");
        await chrome.storage.local.set({
          triedPlatforms: tried,
          atsQueue: built.queue,
          atsPlatform: next,
          atsNavAt: Date.now(),
          atsNavTries: 0,
          campaignWarmedUp: false,
          currentJob: null,
        });
        try {
          await chrome.tabs.update(ex.campaignTabId, { url: built.queue[0].applyUrl });
        } catch (e) {
          await addToActivityLog(`Couldn't open ${NAMES[next]} (${e.message}) — stopping the campaign.`, "error");
          return await handleMessage({ type: "STOP_CAMPAIGN" }, sender);
        }
        return { switched: true, to: next };
      }
      await addToActivityLog(
        `${NAMES[curPlat] || curPlat} exhausted (${msg.reason || "no applyable jobs"}) — switching to ${NAMES[next]} automatically.`,
        "info");
      const targetUrl = buildPlatformUrl(next, (f.keywords || []).slice(0, 1), f.location, f.job_type, f.search_radius_miles, f.work_setting);
      await chrome.storage.local.set({
        triedPlatforms: tried,
        campaignTargetUrl: targetUrl,
        campaignWarmedUp: false, // new board → content.js re-runs its CF warmup hop
        // A fresh board has searched nothing yet: every phrase gets its pages and its
        // slice of the new board's cap back (the cap ledger is per platform already).
        kwIndex: 0,
        kwLap: 0,
        kwDone: [],
        pendingJobs: [],
        currentJobIndex: 0,
        zrRecoveries: 0,
        zrNoBtnStreak: 0,
      });
      // Leaving the pool for a board: drop the half-walked queue, or the ATS advance
      // handler would keep steering the window back into it behind the board search.
      await chrome.storage.local.remove(["atsQueue", "atsPlatform", "atsNavAt", "atsNavTries"]);
      try {
        await chrome.tabs.update(ex.campaignTabId, { url: platformHomeUrl(next) });
      } catch (e) {
        await addToActivityLog(`Couldn't open ${NAMES[next]} (${e.message}) — stopping the campaign.`, "error");
        return await handleMessage({ type: "STOP_CAMPAIGN" }, sender);
      }
      return { switched: true, to: next };
    }

    case "STOP_CAMPAIGN": {
      const stopData = await chrome.storage.local.get(["campaignTabId", "campaignWindowId", "campaignRunning"]);
      if (stopData.campaignRunning) {
        // The terminal line of every run, and the one the dashboard trusts for WHY it
        // ended. "completed" (nothing left to apply to) and a user's Stop are both
        // orderly endings; anything that never reaches this handler — a closed laptop, a
        // killed window — leaves no outcome at all, which is exactly how the dashboard
        // tells a finished run from a vanished one.
        await addToActivityLog(
          `⏹ Campaign stopped (${msg.reason || "requested by you"}).`, "info",
          { outcome: msg.outcome || "stopped_by_user" });
      }

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
      await chrome.storage.local.remove(["atsQueue", "atsPlatform", "poolLeadMode"]);

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
        // A 429 on save = a cap (per-platform / daily / free) was hit server-side. The
        // client-side rail normally stops us BEFORE this, but if the local count drifted
        // (e.g. storage cleared mid-day) the backend is the backstop — STOP now so we
        // don't keep firing real submits past the ban-safety cap (2026-08-09 hardening).
        const is429 = /\b429\b/.test(err.message || "");
        addToActivityLog(
          `⚠️ Applied but backend save failed (${err.message})` +
            (is429 ? " — a daily/platform cap was hit; stopping to stay ban-safe." : " — counted locally"),
          "warn"
        );
        if (is429) {
          await chrome.storage.local.set({ campaignRunning: false });
          try { await apiPost("/campaign/stop", {}); } catch {}
          updateBadge();
          return { saved: false, stopped: "limit" };
        }
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

    // Per-application RECEIPT (council #3, week-1 trust primitive): capture the
    // confirmation-page moment — screenshot (CDP, works on hidden windows) + text
    // snippet + verify signal — so "did it actually land?" is answerable per submit
    // (the Mavenclinic lesson: employer emails are NOT guaranteed; the page is ours).
    // Stored locally (chrome.storage.receipts, capped) — no backend changes/deploys.
    case "RECEIPT_CAPTURE": {
      const r = msg.data || {};
      let shot = null;
      try {
        const tabId = sender && sender.tab && sender.tab.id;
        if (tabId && (await ensureDebuggerAttached(tabId))) {
          const res = await chrome.debugger.sendCommand({ tabId }, "Page.captureScreenshot", {
            format: "jpeg", quality: 55,
          });
          if (res && res.data) shot = "data:image/jpeg;base64," + res.data;
        }
      } catch {}
      try {
        const s = await chrome.storage.local.get("receipts");
        const receipts = s.receipts || [];
        receipts.unshift({
          at: new Date().toISOString(),
          job_title: r.job_title || "", company: r.company || "", platform: r.platform || "",
          job_url: r.job_url || "", page_url: r.page_url || "",
          verified: !!r.verified, signal: r.signal || "",
          snippet: (r.snippet || "").slice(0, 600),
          shot,
        });
        // Screenshots are ~100-200KB each — cap the ledger so storage stays sane.
        await chrome.storage.local.set({ receipts: receipts.slice(0, 10) });
      } catch {}
      return { stored: true, shot: !!shot };
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

    // Terminal hand-back (council 2026-08-04): the filler could NOT complete this job.
    // The invariant: every approved job ends submitted-complete-and-honest OR handed back
    // with a REASON + link — never a silent half-death. This case: (1) tells the user
    // loudly which job needs their hands and why, (2) merges every unfilled field label
    // into the frequency LEDGER (the data that decides which deterministic handlers get
    // built next), (3) durably flips the job out of `approved`, (4) advances the walk.
    case "ATS_JOB_FAILED": {
      const f = msg.data || {};
      const who = [f.title, f.company].filter(Boolean).join(" @ ") || "this job";
      const unfilled = Array.isArray(f.unfilled) ? f.unfilled.slice(0, 25) : [];
      await addToActivityLog(
        `✋ Needs your hands: ${who} — ${f.reason || "couldn't complete the form"}. Finish it yourself: ${f.url || ""}`,
        "warn",
        // Same labels that feed the LOCAL ledger below — mirrored server-side so the
        // ledger survives a reinstall and aggregates across users, not just this browser.
        {
          type: "handback",
          reason: f.reason || "",
          unfilled,
          platform: f.platform || "",
          job_title: f.title || "",
          company: f.company || "",
          job_url: f.url || "",
        }
      );
      // Durable to-do row, read by BOTH the popup block and the dashboard rail badge.
      // The activity line above still carries the story; this carries the STATE — a log
      // line scrolls away and cannot be ticked off (Igor 09-21).
      try {
        await apiPost("/handbacks", {
          job_title: f.title || "",
          company: f.company || "",
          url: f.url || "",
          platform: f.platform || "",
          reason: f.reason || "",
          steps_done: f.steps_done || 0,
        });
      } catch { /* best-effort: the walk must advance even if the row didn't land */ }
      try {
        if (unfilled.length) {
          const s = await chrome.storage.local.get("unfilledLedger");
          const ledger = s.unfilledLedger || {};
          for (const lbl of unfilled) ledger[lbl] = (ledger[lbl] || 0) + 1;
          // Cap ledger size: keep the 200 most-frequent labels.
          const top = Object.entries(ledger).sort((a, b) => b[1] - a[1]).slice(0, 200);
          await chrome.storage.local.set({ unfilledLedger: Object.fromEntries(top) });
        }
      } catch {}
      try {
        const d = await chrome.storage.local.get(["atsPlatform", "atsQueue"]);
        const head = d.atsPlatform === "pool" && Array.isArray(d.atsQueue) ? d.atsQueue[0] : null;
        if (head && head.id) apiPatch(`/jobs/${head.id}/status`, { status: "skipped" }).catch(() => {});
      } catch {}
      await advanceAtsQueue();
      return { advanced: true, handedBack: true };
    }

    // ----- Hand-backs: the jobs waiting on the user's hands (popup block) -----
    // The popup has no token of its own; the SW is the only API gateway.
    case "GET_HANDBACKS": {
      try {
        const r = await apiGet("/handbacks?limit=5");
        return { ok: true, handbacks: r.handbacks || [] };
      } catch (e) {
        // An unreachable list is NOT an empty list — the popup says so instead of
        // rendering "nothing waiting" over jobs that are (#113's rule, again).
        return { ok: false, handbacks: [] };
      }
    }

    case "RESOLVE_HANDBACK": {
      try {
        await apiPost(`/handbacks/${encodeURIComponent(msg.id)}/resolve`, {});
        return { ok: true };
      } catch (e) { return { ok: false }; }
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

    // ----- Engine clock (throttle-immune sleep) -----
    // content.js races every pause against this timer because the campaign window's own
    // clock is throttled when the window is covered (1s alignment; 1/min for chained
    // timers after 5 min hidden). The SW is not. Capped: the longest engine pause is 15s,
    // and an uncapped value would hold the response channel hostage on a bad call.
    case "SLEEP": {
      const ms = Math.max(0, Math.min(30000, Number(msg.ms) || 0));
      await new Promise((r) => setTimeout(r, ms));
      return { ok: true };
    }

    // ----- Step failed -----
    case "STEP_FAILED":
      return { ok: true };

    // ----- Backend log (key events from content.js → Campaign Live feed) -----
    // The walk hit a "Not Found" page. Retire the posting server-side so it stops coming
    // back: content.js keeps the walk moving on its own, but a dead row left at `new` is
    // re-opened and re-skipped on every run, forever. Best-effort — never blocks the walk.
    case "REPORT_DEAD_LINK": {
      try {
        const r = await apiPost("/jobs/dead-link", { url: msg.url || "" });
        return { retired: (r && r.retired) || 0 };
      } catch (e) {
        return { retired: 0, error: String((e && e.message) || e) };
      }
    }

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
      // Two different hand-offs ride this one channel. A captcha asks "are you human";
      // a consent wall asks the account holder to agree to something. Sending someone to
      // "solve the captcha" when the window holds an Accept-Terms modal makes them hunt
      // for a challenge that isn't there — so the copy follows data.kind. Older content
      // scripts don't send the field; undefined keeps the captcha wording.
      const isTerms = data.kind === "terms";
      // Name the actual platform (captchas fire on ZR/Greenhouse/Lever too, not just Indeed).
      const site = platformDisplayNameFromUrl(data.url);
      // Mirror to the backend activity log. A captcha is an error-level event; a terms
      // modal is not — it is the site doing something normal, and filing it as an error
      // inflates the run's error count and hijacks `last_error_msg` on the dashboard.
      try {
        await apiPost("/activity", {
          message: isTerms
            ? `Consent wall (${data.signal}) on ${data.url}`
            : `Detection tripped (${data.signal}) on ${data.url}`,
          level: isTerms ? "warn" : "error",
          phase: "detection",
          metadata: { signal: data.signal, page_phase: data.phase, url: data.url, kind: data.kind || "captcha" },
        });
      } catch {}
      const askLine = isTerms
        ? `${site} is asking you to accept its terms. ${data.action || "Open the automation window and accept them"} — the campaign resumes automatically.`
        : `${site} is asking you to verify you're human. Open the automation window, solve it, and the campaign resumes automatically.`;
      // Persist the hand-off so the popup (GET_STATUS) and the dashboard live
      // view (ping.js HIREDROP_GET_LIVE_STATE) can show a "your turn" CTA that
      // survives popup reopen / page reload. Cleared by DETECTION_CLEARED,
      // START_CAMPAIGN and STOP_CAMPAIGN. `kind`/`action` are the seam the dashboard
      // banner reads to swap its own wording (website lane).
      await chrome.storage.local.set({
        captchaWaiting: {
          url: data.url,
          site,
          signal: data.signal,
          kind: isTerms ? "terms" : "captcha",
          action: data.action || null,
          at: Date.now(),
        },
      });
      // Local log so the popup shows it without waiting for a refresh.
      await addToActivityLog(
        isTerms
          ? `⏸ ${site} wants its terms accepted — campaign paused`
          : `⚠️ ${site} asked for a human check — campaign paused`,
        isTerms ? "warn" : "err"
      );
      // System notification so the user sees this even if the popup is closed.
      try {
        await chrome.notifications.create({
          type: "basic",
          iconUrl: "icons/icon128.png",
          title: "HireDrop paused — action needed",
          message: askLine,
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

      const today = localDay();
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

// Flag forensics: whoever clears campaignRunning, the transition itself is recorded.
// Live 08-15 the extension kept POSTing "campaign_running: false" pings during a running
// campaign — meaning the stored flag went down without any of our stop paths logging it.
// A storage-level watcher is the one instrument that cannot be evaded by a missing log
// line: it fires on the WRITE, whoever made it.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes.campaignRunning) return;
  const { oldValue, newValue } = changes.campaignRunning;
  if (oldValue === true && newValue !== true) {
    addToActivityLog(`🔎 campaignRunning true → ${JSON.stringify(newValue)} (storage write)`, "warn").catch(() => {});
  }
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const data = await chrome.storage.local.get(["campaignTabId", "campaignRunning"]);
  if (data.campaignRunning && data.campaignTabId === tabId) {
    await addToActivityLog("⏹ Campaign stopped — its automation tab was closed.", "warn");
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
    await addToActivityLog("⏹ Campaign stopped — its automation window was closed.", "warn");
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
