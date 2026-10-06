// Daily auto-start — "Apply every day automatically" (opt-in, default OFF).
//
// Why (measured 2026-10-06): over 14 days 6 had zero applications and 4 had no run at all;
// no run ever reached the daily cap. A run only happened when the user opened the dashboard
// and pressed Start — and a Safari user forgets. The service worker already wakes every
// minute (the ext-ping alarm), so it can press Start itself.
//
// The setting lives HERE, in chrome.storage.local, not on the server: the schedule belongs
// to the machine where Chrome runs. The dashboard only reads and writes it through ping.js.
//
// Rules (product decision, final):
//   - fires once per user-LOCAL day, at hour + a jitter of 0-40 min picked once per day;
//   - asleep / Chrome closed at that time → fires on the first wake or browser start later
//     the same local day, but never at or after 21:00;
//   - never while a campaign is running, never once today's cap is reached;
//   - starts the SAME way the dashboard does (background.js startCampaign) with the filters
//     of the user's last manual launch; no last launch → it doesn't fire and records why.
//
// Loaded into the service worker with importScripts(). Everything below takes its clock,
// storage and side effects as arguments, so tests/auto-daily.test.js runs the real code.

const HD_AUTO_DAILY_KEY = "autoDaily"; // { enabled, hour, enabledAt }
const HD_AUTO_DAILY_STATE_KEY = "autoDailyState"; // today's record, see hdAutoDailyPlan
const HD_LAST_LAUNCH_KEY = "lastLaunch"; // { filters, at } — written at every manual start
// The last refusal we NOTIFIED about: { reason, at }. Spans days on purpose — the same
// refusal every morning (quota used up, employer answers missing…) is a feed line, not a
// daily notification. A different reason or a successful start resets it.
const HD_AUTO_DAILY_NOTICE_KEY = "autoDailyLastNotice";
// A claimed start that never recorded its outcome (the worker died mid-start) is called
// interrupted once it's this old — a real start answers well within it.
const HD_AUTO_DAILY_STALE_START_MS = 5 * 60 * 1000;
const HD_AUTO_DAILY_DEFAULT_HOUR = 9;
// Latest selectable hour: 20:00 + 40 min jitter = 20:40, still inside the 21:00 cut-off.
const HD_AUTO_DAILY_MAX_HOUR = 20;
const HD_AUTO_DAILY_MAX_JITTER_MIN = 40;
const HD_AUTO_DAILY_CUTOFF_MIN = 21 * 60; // no auto-start at or after 21:00 local
// A start that failed for a reason that heals by itself (server unreachable right after
// wake, a zombie "running" flag the heartbeat TTL clears within 10 min) is retried, a few
// times, spaced out. Anything else is final for the day.
const HD_AUTO_DAILY_RETRY_MS = 5 * 60 * 1000;
const HD_AUTO_DAILY_MAX_TRIES = 4;
const HD_AUTO_DAILY_TRANSIENT = ["mode_unknown", "status_unreachable", "server_running", "start_threw"];

// Server refusals of POST /campaign/start (app/routers/campaign.py) in words, with where to
// fix each. Shared by manual and auto starts — the refusal notification uses it.
const HD_START_REFUSALS = {
  onboarding_incomplete: { text: "Your profile setup isn't finished", path: "/onboarding" },
  us_only: { text: "HireDrop applies to US jobs only and your profile location is outside the US", path: "/dashboard/settings" },
  employer_answers_missing: { text: "Some questions employers ask aren't answered yet", path: "/dashboard/settings?tab=forms" },
  disposable_email: { text: "This account uses a disposable email address", path: "/dashboard/settings" },
  lever_needs_tap: { text: "Lever is the only board selected and it needs Tap mode", path: "/dashboard" },
};

function hdStartRefusal(reason) {
  return HD_START_REFUSALS[reason] || { text: `HireDrop refused to start (${reason || "unknown reason"})`, path: "/dashboard" };
}

// The settings, cleaned. Anything malformed falls back to OFF / 9:00 — the default is OFF,
// so a corrupt record can never switch the feature on.
function hdAutoDailyNormalize(raw) {
  const r = raw && typeof raw === "object" ? raw : {};
  let hour = Number.isInteger(r.hour) ? r.hour : HD_AUTO_DAILY_DEFAULT_HOUR;
  if (hour < 0) hour = 0;
  if (hour > HD_AUTO_DAILY_MAX_HOUR) hour = HD_AUTO_DAILY_MAX_HOUR;
  return { enabled: r.enabled === true, hour, enabledAt: typeof r.enabledAt === "number" ? r.enabledAt : null };
}

// Minutes since local midnight. `now` is a Date in the user's zone (the SW's zone).
function hdMinuteOfDay(now) {
  return now.getHours() * 60 + now.getMinutes();
}

// Today's record. A new local day gets a fresh one with its own jitter — picked ONCE and
// stored, so the run time doesn't wander every time the worker restarts.
//   { day, jitterMin, done, outcome, reason, message, at, tries, retryAt }
function hdAutoDailyPlan(stored, today, rand) {
  if (stored && stored.day === today && Number.isInteger(stored.jitterMin)) return { state: stored, changed: false };
  const jitterMin = Math.floor((rand || Math.random)() * (HD_AUTO_DAILY_MAX_JITTER_MIN + 1));
  return {
    state: { day: today, jitterMin: Math.min(jitterMin, HD_AUTO_DAILY_MAX_JITTER_MIN), done: false, tries: 0 },
    changed: true,
  };
}

function hdAutoDailyDueMin(cfg, state) {
  return cfg.hour * 60 + (state.jitterMin || 0);
}

// Pure decision for one tick: "off" | "done" | "wait" | "too_late" | "fire".
function hdAutoDailyDecide(cfg, state, now) {
  if (!cfg.enabled) return "off";
  if (state.done) return "done";
  const nowMin = hdMinuteOfDay(now);
  if (nowMin < hdAutoDailyDueMin(cfg, state)) return "wait";
  if (nowMin >= HD_AUTO_DAILY_CUTOFF_MIN) return "too_late";
  if (state.retryAt && now.getTime() < state.retryAt) return "wait";
  return "fire";
}

// When the next auto-start will happen, for the dashboard. Today's time is exact (its jitter
// is known); a later day is a window, because its jitter is picked on that day.
function hdAutoDailyNextRun(cfg, state, now) {
  if (!cfg.enabled) return null;
  const at = (dayOffset, min) => {
    const d = new Date(now.getTime());
    d.setDate(d.getDate() + dayOffset);
    d.setHours(0, 0, 0, 0);
    return d.getTime() + min * 60000;
  };
  const decision = hdAutoDailyDecide(cfg, state, now);
  if (decision === "wait") {
    const due = state.retryAt && hdMinuteOfDay(now) >= hdAutoDailyDueMin(cfg, state)
      ? state.retryAt
      : at(0, hdAutoDailyDueMin(cfg, state));
    return { earliest: due, latest: due, exact: true, day: "today" };
  }
  if (decision === "fire") {
    const t = now.getTime();
    return { earliest: t, latest: t, exact: true, day: "today" };
  }
  return {
    earliest: at(1, cfg.hour * 60),
    latest: at(1, cfg.hour * 60 + HD_AUTO_DAILY_MAX_JITTER_MIN),
    exact: false,
    day: "tomorrow",
  };
}

// After the settings change: a time that has ALREADY passed today does not fire a minute
// later — the first run is tomorrow. Otherwise switching the toggle on at 14:00 with 9:00
// selected would pop the automation window up immediately, which nobody asked for.
function hdAutoDailyAfterSet(cfg, state, now) {
  if (!cfg.enabled || state.done) return state;
  if (hdMinuteOfDay(now) < hdAutoDailyDueMin(cfg, state)) return state;
  return {
    ...state,
    done: true,
    outcome: "skipped",
    reason: "set_after_time",
    message: "Today's time had already passed when this was set — the first run is tomorrow.",
    at: now.getTime(),
  };
}

// The user pressed Stop on a run today: that is today's answer. Without this, a run the
// user stopped at 8:30 would be restarted by the schedule at 9:15 — fighting the human.
// (A run that DIED — laptop closed, window killed — is not a Stop; the schedule still runs.)
function hdAutoDailyAfterUserStop(cfg, state, now) {
  if (!cfg.enabled || state.done) return state;
  return {
    ...state,
    done: true,
    outcome: "skipped",
    reason: "stopped_by_you",
    message: "You stopped a run today — the next auto-start is tomorrow.",
    at: now.getTime(),
    retryAt: null,
  };
}

// One scheduler tick. `deps`:
//   get(keys) / set(obj)          chrome.storage.local
//   now() → Date, localDay() → "YYYY-MM-DD" (background.js localDay — the one day definition)
//   rand() → [0,1)
//   isRunningLocally() → bool     campaignRunning AND its window still exists
//   fetchStatus() → status|null   GET /campaign/status for this user's local day
//   start(filters) → result       background.js startCampaign(filters, {source:"auto"})
//   log(text, cls)                addToActivityLog (local feed + backend /activity)
//   notify(title, message, path)  chrome.notifications, click opens hiredrop.io<path>
// Returns { action, reason? } — what happened, for tests and the console.
async function hdAutoDailyTick(trigger, deps) {
  const got = await deps.get([HD_AUTO_DAILY_KEY, HD_AUTO_DAILY_STATE_KEY]);
  const cfg = hdAutoDailyNormalize(got[HD_AUTO_DAILY_KEY]);
  if (!cfg.enabled) return { action: "off" };
  const now = deps.now();
  const today = deps.localDay();
  const planned = hdAutoDailyPlan(got[HD_AUTO_DAILY_STATE_KEY], today, deps.rand);
  let state = planned.state;
  if (planned.changed) await deps.set({ [HD_AUTO_DAILY_STATE_KEY]: state });

  const finish = async (outcome, reason, message, extra) => {
    state = { ...state, done: true, outcome, reason, message: message || "", at: now.getTime(), retryAt: null, ...(extra || {}) };
    await deps.set({ [HD_AUTO_DAILY_STATE_KEY]: state });
    return { action: outcome, reason };
  };

  // Tell the user once per reason. Same refusal as the last one we notified about → the
  // feed has it, the notification stays quiet (no daily nag).
  const notifyOnce = async (reason, title, message, path) => {
    const last = (await deps.get([HD_AUTO_DAILY_NOTICE_KEY]))[HD_AUTO_DAILY_NOTICE_KEY];
    if (last && last.reason === reason) return false;
    await deps.notify(title, message, path);
    await deps.set({ [HD_AUTO_DAILY_NOTICE_KEY]: { reason, at: now.getTime() } });
    return true;
  };

  // A start claimed earlier that never wrote its outcome: the worker died mid-start. Find out
  // what actually happened instead of letting "claimed" read as "started".
  if (state.done && state.outcome === "starting") {
    if (now.getTime() - (state.claimedAt || 0) < HD_AUTO_DAILY_STALE_START_MS) return { action: "starting" };
    if (await deps.isRunningLocally()) return finish("started", null, "");
    state = { ...state, done: false, outcome: null };
    if (state.tries >= HD_AUTO_DAILY_MAX_TRIES || hdMinuteOfDay(now) >= HD_AUTO_DAILY_CUTOFF_MIN) {
      await deps.log("⏰ Daily auto-start didn't start today — the start was interrupted.", "warn");
      return finish("failed", "interrupted", "Didn't start — the start was interrupted (Chrome closed or the extension restarted).");
    }
    // Still inside the bounded retry budget: try again on this tick.
    state = { ...state, reason: "interrupted", message: "The start was interrupted", retryAt: null };
    await deps.set({ [HD_AUTO_DAILY_STATE_KEY]: state });
  }

  const decision = hdAutoDailyDecide(cfg, state, now);
  if (decision === "off" || decision === "done" || decision === "wait") return { action: decision };

  if (decision === "too_late") {
    // Retries ran out of day: name the real reason, not "Chrome wasn't running".
    if (state.tries > 0) {
      await deps.log(`⏰ Daily auto-start couldn't run today (${state.message || state.reason || "unknown"}).`, "warn");
      return finish("failed", state.reason || "too_late", state.message);
    }
    // Say it in the feed only if the schedule existed before today's time — a toggle
    // switched on at 22:00 missed nothing.
    const due = new Date(now.getTime());
    due.setHours(0, 0, 0, 0);
    const dueAt = due.getTime() + hdAutoDailyDueMin(cfg, state) * 60000;
    if (cfg.enabledAt && cfg.enabledAt < dueAt) {
      await deps.log(
        `⏰ Daily auto-start missed today — Chrome wasn't running between ${hdHourLabel(cfg.hour)} and 9 PM. It will try again tomorrow.`,
        "warn");
    }
    return finish("missed", "too_late", "Missed today — Chrome wasn't running between the scheduled time and 9 PM.");
  }

  // Claim the day BEFORE any await that could overlap with the next tick (the start itself
  // takes several seconds): a second tick sees done=true and walks away. The claim is an
  // explicit "starting" — only a start that returned started:true is ever reported as one.
  state = { ...state, done: true, outcome: "starting", message: "Starting now…", claimedAt: now.getTime(),
    tries: (state.tries || 0) + 1, retryAt: null };
  await deps.set({ [HD_AUTO_DAILY_STATE_KEY]: state });

  const retryOrFinish = async (reason, message) => {
    if (state.tries < HD_AUTO_DAILY_MAX_TRIES) {
      state = { ...state, done: false, outcome: null, reason, message: message || "", retryAt: now.getTime() + HD_AUTO_DAILY_RETRY_MS };
      await deps.set({ [HD_AUTO_DAILY_STATE_KEY]: state });
      return { action: "retry", reason };
    }
    // Still "running" after every retry: a real run elsewhere (another computer), not an
    // error — record it honestly and stay quiet.
    if (reason === "server_running") {
      return finish("skipped", "already_running_elsewhere",
        "Skipped today — a campaign was already running (maybe on another computer).");
    }
    await deps.log(`⏰ Daily auto-start couldn't run today (${message || reason}).`, "warn");
    await notifyOnce(reason, "HireDrop didn't start today", `${message || reason} — open HireDrop to start it.`, "/dashboard");
    return finish("failed", reason, message);
  };

  if (await deps.isRunningLocally()) {
    return finish("skipped", "already_running", "Skipped today — a campaign was already running.");
  }

  const ll = (await deps.get([HD_LAST_LAUNCH_KEY]))[HD_LAST_LAUNCH_KEY];
  if (!ll || !ll.filters || typeof ll.filters !== "object") {
    await deps.log(
      "⏰ Daily auto-start skipped — there's no previous launch to repeat yet. Start once from the dashboard and it takes over from tomorrow.",
      "warn");
    return finish("skipped", "no_last_launch", "No previous launch to repeat yet — start once from the dashboard.");
  }

  const st = await deps.fetchStatus();
  if (!st) return retryOrFinish("status_unreachable", "Couldn't reach HireDrop");
  // Running somewhere else (another computer) — or a zombie flag the heartbeat TTL clears
  // within 10 minutes. Retrying covers the second without ever racing the first.
  if (st.running) return retryOrFinish("server_running", "A campaign is already running");
  const limit = Number(st.daily_limit) || 0;
  const done = Number(st.today_applications) || 0;
  if (limit > 0 && done >= limit) {
    return finish("skipped", "cap_reached", `Skipped today — the daily limit was already reached (${done}/${limit}).`);
  }
  const local = await deps.get(["todayCount", "todayDate", "campaignCaps"]);
  const localTotal = local.todayDate === today ? (local.todayCount || 0) : 0;
  const localCap = local.campaignCaps && local.campaignCaps.dailyTotal;
  if (localCap > 0 && localTotal >= localCap) {
    return finish("skipped", "cap_reached", `Skipped today — the daily limit was already reached (${localTotal}/${localCap}).`);
  }

  await deps.log(
    `⏰ Daily auto-start — starting your campaign with your last launch settings${trigger === "startup" ? " (Chrome just opened)" : ""}.`,
    "info");
  let res;
  try {
    res = await deps.start(ll.filters);
  } catch (e) {
    return retryOrFinish("start_threw", (e && e.message) || "the start failed");
  }
  if (res && res.started) {
    await deps.set({ [HD_AUTO_DAILY_NOTICE_KEY]: null }); // a success resets the "already told you"
    return finish("started", null, "");
  }
  const reason = (res && res.error) || "unknown";
  const message = (res && res.message) || hdStartRefusal(reason).text;
  if (HD_AUTO_DAILY_TRANSIENT.includes(reason)) return retryOrFinish(reason, message);
  // Every refusal reaches the feed (startCampaign already wrote the line for a server
  // refusal — res.logged); the notification only the first time for this reason.
  if (!(res && res.logged)) await deps.log(`⏰ Daily auto-start didn't start: ${message}`, "warn");
  if (!(res && res.notified)) {
    await notifyOnce(reason, "HireDrop didn't start today", `${message} — open HireDrop to fix.`, hdStartRefusal(reason).path);
  }
  return finish("refused", reason, message);
}

function hdHourLabel(hour) {
  const h = hour % 12 === 0 ? 12 : hour % 12;
  return `${h} ${hour < 12 ? "AM" : "PM"}`;
}

// What the dashboard gets back (ping.js → HIREDROP_AUTO_DAILY).
function hdAutoDailyView(cfg, state, lastLaunch, now) {
  const ll = lastLaunch && lastLaunch.filters ? lastLaunch : null;
  return {
    enabled: cfg.enabled,
    hour: cfg.hour,
    maxHour: HD_AUTO_DAILY_MAX_HOUR,
    nextRun: hdAutoDailyNextRun(cfg, state, now),
    today: state && state.day ? {
      day: state.day,
      status: state.done ? (state.outcome || "started") : "pending",
      reason: state.reason || null,
      message: state.message || "",
      at: state.at || null,
    } : null,
    lastLaunch: ll ? {
      at: ll.at || null,
      platforms: Array.isArray(ll.filters.platforms) ? ll.filters.platforms : [],
      keywords: Array.isArray(ll.filters.keywords) ? ll.filters.keywords.length : 0,
    } : null,
  };
}

if (typeof module === "object" && module.exports) {
  module.exports = {
    HD_AUTO_DAILY_KEY, HD_AUTO_DAILY_STATE_KEY, HD_LAST_LAUNCH_KEY,
    HD_AUTO_DAILY_MAX_HOUR, HD_AUTO_DAILY_MAX_JITTER_MIN, HD_AUTO_DAILY_CUTOFF_MIN,
    HD_AUTO_DAILY_MAX_TRIES, HD_AUTO_DAILY_RETRY_MS, HD_AUTO_DAILY_NOTICE_KEY, HD_AUTO_DAILY_STALE_START_MS,
    hdAutoDailyNormalize, hdAutoDailyPlan, hdAutoDailyDecide, hdAutoDailyNextRun,
    hdAutoDailyAfterSet, hdAutoDailyAfterUserStop, hdAutoDailyTick, hdAutoDailyView, hdStartRefusal, hdHourLabel,
  };
}
