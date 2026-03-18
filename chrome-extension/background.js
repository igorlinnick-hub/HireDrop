// JobFlow service worker
// All API communication, campaign state, and tab management

const API_BASE = "https://web-production-db45.up.railway.app";

// ---------------------------------------------------------------------------
// API helpers
// ---------------------------------------------------------------------------

async function apiGet(path) {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  return res.json();
}

async function apiPost(path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  return res.json();
}

// ---------------------------------------------------------------------------
// Profile — fetch from API, cache in chrome.storage.local
// ---------------------------------------------------------------------------

async function fetchAndCacheProfile() {
  try {
    const profile = await apiGet("/api/profile");
    await chrome.storage.local.set({
      profile,
      profileCachedAt: Date.now(),
    });
    return profile;
  } catch (err) {
    // Network down — return whatever we have cached
    const data = await chrome.storage.local.get("profile");
    if (data.profile) return data.profile;
    throw err;
  }
}

async function getCachedProfile() {
  const data = await chrome.storage.local.get(["profile", "profileCachedAt"]);
  const fiveMin = 5 * 60 * 1000;
  if (data.profile && data.profileCachedAt && Date.now() - data.profileCachedAt < fiveMin) {
    return data.profile;
  }
  return fetchAndCacheProfile();
}

// ---------------------------------------------------------------------------
// Install / Startup — seed the cache
// ---------------------------------------------------------------------------

const LIMIT_PER_PLATFORM = 50;

chrome.runtime.onInstalled.addListener(async () => {
  // Initialize storage defaults
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
  await fetchAndCacheProfile();
  updateBadge();
});

chrome.runtime.onStartup.addListener(async () => {
  // Reset daily counters if date rolled over
  const data = await chrome.storage.local.get("todayDate");
  const today = new Date().toISOString().slice(0, 10);
  if (data.todayDate !== today) {
    await chrome.storage.local.set({ todayCount: 0, platformCounts: {}, todayDate: today });
  }
  await fetchAndCacheProfile();
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
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "badge-refresh") updateBadge();
});

// ---------------------------------------------------------------------------
// Activity log — same format as popup.js writes
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
}

// ---------------------------------------------------------------------------
// Indeed search URL builder
// ---------------------------------------------------------------------------

function buildIndeedUrl(keywords, location, jobType) {
  const params = new URLSearchParams();

  if (keywords && keywords.length) {
    params.set("q", keywords.join(" "));
  }

  const locMap = { usa: "United States", remote: "remote", europe: "" };
  const loc = locMap[location] !== undefined ? locMap[location] : location;
  if (loc) params.set("l", loc);

  const jtMap = { "full-time": "fulltime", "part-time": "parttime", contract: "contract" };
  if (jobType && jtMap[jobType]) params.set("jt", jtMap[jobType]);

  // Easy Apply filter
  params.set("iafilter", "1");

  return `https://www.indeed.com/jobs?${params.toString()}`;
}

// ---------------------------------------------------------------------------
// Message handler — single listener, all message types
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  handleMessage(msg, sender)
    .then(sendResponse)
    .catch((err) => sendResponse({ error: err.message }));
  return true; // keep channel open for async response
});

async function handleMessage(msg, sender) {
  switch (msg.type) {
    // ----- Profile -----
    case "GET_PROFILE":
      return await getCachedProfile();

    case "REFRESH_PROFILE":
      return await fetchAndCacheProfile();

    case "CHECK_CONNECTION": {
      try {
        await apiGet("/api/stats");
        return { connected: true };
      } catch {
        return { connected: false };
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

      // Persist to server
      try {
        await apiPost("/api/campaign/start", filters);
      } catch {
        // Continue even if server is down — we track locally too
      }

      // Build Indeed search URL and open a tab
      const url = buildIndeedUrl(filters.keywords, filters.location, filters.job_type);
      const tab = await chrome.tabs.create({ url, active: true });

      // Save campaign state locally
      await chrome.storage.local.set({
        campaignRunning: true,
        campaignFilters: filters,
        campaignStartedAt: new Date().toISOString(),
        campaignTabId: tab.id,
        currentJob: null,
      });

      updateBadge();

      return { started: true, tabId: tab.id, url };
    }

    // ----- Campaign stop -----
    case "STOP_CAMPAIGN": {
      await chrome.storage.local.set({
        campaignRunning: false,
        campaignTabId: null,
        currentJob: null,
      });

      // Tell the content script on the campaign tab to stop
      try {
        const data = await chrome.storage.local.get("campaignTabId");
        if (data.campaignTabId) {
          chrome.tabs.sendMessage(data.campaignTabId, { type: "CAMPAIGN_STOPPED" }).catch(() => {});
        }
      } catch {}

      // Persist to server
      try {
        await apiPost("/api/campaign/stop", {});
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

      // Save to server
      let serverResult = null;
      try {
        serverResult = await apiPost("/api/application/save", {
          job_title: appData.job_title,
          company: appData.company || "",
          platform: appData.platform || "indeed",
          job_url: appData.job_url || "",
          cover_letter: appData.cover_letter || "",
          status: appData.status || "applied",
        });
      } catch (err) {
        // Queue for retry? For now just log the error
        return { saved: false, error: err.message };
      }

      // Increment today's total and per-platform counters
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
          apiPost("/api/cover-letter-preview", {
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

      // Fallback if API failed or returned nothing
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

      // Log to activity log
      await addToActivityLog(
        source === "AI"
          ? `Cover letter generated for ${job.job_title} @ ${job.company || "company"}`
          : `Cover letter fallback used for ${job.job_title} (API unavailable)`,
        source === "AI" ? "ok" : ""
      );

      return { letter, source, job_title: job.job_title, company: job.company };
    }

    // ----- Step failed (from content.js) -----
    case "STEP_FAILED": {
      // Just acknowledge — popup sees it via the LOG message the content script also sends
      return { ok: true };
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

      // Reset counters if date changed
      const today = new Date().toISOString().slice(0, 10);
      let todayCount = data.todayCount || 0;
      let platformCounts = data.platformCounts || {};
      if (data.todayDate !== today) {
        todayCount = 0;
        platformCounts = {};
        await chrome.storage.local.set({ todayCount: 0, platformCounts: {}, todayDate: today });
      }

      // Also try to get total stats from server
      let serverStats = null;
      try {
        serverStats = await apiGet("/api/stats");
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

    // ----- Fallback -----
    default:
      return { error: `Unknown message type: ${msg.type}` };
  }
}

// ---------------------------------------------------------------------------
// Tab closed — if the campaign tab is closed, stop the campaign
// ---------------------------------------------------------------------------

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const data = await chrome.storage.local.get(["campaignTabId", "campaignRunning"]);
  if (data.campaignRunning && data.campaignTabId === tabId) {
    await chrome.storage.local.set({
      campaignRunning: false,
      campaignTabId: null,
      currentJob: null,
    });
    try { await apiPost("/api/campaign/stop", {}); } catch {}
    updateBadge();
  }
});
