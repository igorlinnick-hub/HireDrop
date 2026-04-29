// JobFlow service worker
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
// API helpers
// ---------------------------------------------------------------------------

async function apiGet(path) {
  const token = await getAuthToken();
  const headers = token ? { Authorization: `Bearer ${token}` } : {};
  const res = await fetch(`${CONFIG.API_BASE}${CONFIG.API_V1}${path}`, { headers });
  if (res.status === 401) {
    await chrome.storage.local.remove("supabase_token");
    chrome.runtime.sendMessage({ type: "AUTH_EXPIRED" }).catch(() => {});
    throw new Error("Authentication required — please reconnect your account");
  }
  if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
  return res.json();
}

async function apiPost(path, body) {
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
    await chrome.storage.local.remove("supabase_token");
    chrome.runtime.sendMessage({ type: "AUTH_EXPIRED" }).catch(() => {});
    throw new Error("Authentication required — please reconnect your account");
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
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "badge-refresh") updateBadge();
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
      await chrome.storage.local.set({ supabase_token: msg.token });
      // Refresh profile cache with new token
      await fetchAndCacheProfile().catch(() => {});
      return { stored: true };
    }
    case "GET_AUTH_STATUS": {
      const data = await chrome.storage.local.get("supabase_token");
      return { authenticated: !!data.supabase_token };
    }
    case "LOGOUT": {
      await chrome.storage.local.remove(["supabase_token", "profile", "profileCachedAt"]);
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

      const url = buildIndeedUrl(filters.keywords, filters.location, filters.job_type);
      const tab = await chrome.tabs.create({ url, active: true });

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

      try {
        const data = await chrome.storage.local.get("campaignTabId");
        if (data.campaignTabId) {
          chrome.tabs.sendMessage(data.campaignTabId, { type: "CAMPAIGN_STOPPED" }).catch(() => {});
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
