// Fixture test for the DAILY AUTO-START (auto-daily.js + its wiring in background.js).
//
//   node <repo>/jobflow/chrome-extension/tests/auto-daily.test.js
//
// Why it exists (measured 10-06): over 14 days, 6 had zero applications and 4 had no run at
// all — a run only happened when the user opened the dashboard and pressed Start. The worker
// already wakes every minute, so it presses Start itself, once a day, opt-in.
//
// What must hold (the product rules, executed against the real code):
//   1. Fires once per local day, not before hour + that day's jitter (0-40 min, picked once
//      and stored); a new day gets a new jitter and fires again.
//   2. Never fires: while a campaign runs here; when today's cap is reached (server count or
//      the local counter); with no previous launch to repeat (and says why); at/after 21:00.
//      A "running" answer from the server is retried (a zombie flag clears in ≤10 min).
//   3. Catch-up: asleep / Chrome closed at 9 → fires at the first wake or browser start the
//      same day.
//   4. A server refusal of /campaign/start STOPS the start (it used to be swallowed by a
//      catch {} and the run went ahead) and is surfaced: a feed line + a notification —
//      for manual and auto starts alike, announced once.
//   5. The wiring: one start path for both, lastLaunch written at every manual start, the
//      minute alarm + onStartup drive the tick, a human Stop is today's answer, ping.js
//      bridges GET/SET, and nothing in the manifest changed for it.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const AD = require("../auto-daily.js");

const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
const PING = fs.readFileSync(path.join(__dirname, "..", "ping.js"), "utf8");
const POPUP = fs.readFileSync(path.join(__dirname, "..", "popup.js"), "utf8");
const MANIFEST = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8"));

let failures = 0;
function check(name, ok, detail) {
  if (!ok) {
    failures += 1;
    console.error(`FAIL ${name}${detail !== undefined ? `\n  ${typeof detail === "string" ? detail : JSON.stringify(detail)}` : ""}`);
  } else {
    console.log(`ok   ${name}`);
  }
}

// A local wall-clock moment on a given day (the SW's zone is the user's zone).
const at = (day, h, m) => new Date(2026, 9, day, h, m || 0, 0, 0);
const dayOf = (d) => d.toLocaleDateString("en-CA"); // background.js localDay()

const STATUS_OK = { running: false, today_applications: 3, daily_limit: 30 };
const LAST = { filters: { keywords: ["product manager"], platforms: ["indeed", "greenhouse"], platform_mode: "all" }, at: "2026-10-05T16:00:00Z" };

// A harness around hdAutoDailyTick: an in-memory storage and recorded side effects.
function harness(opts = {}) {
  const store = {
    autoDaily: { enabled: true, hour: 9, enabledAt: at(1, 8).getTime() },
    lastLaunch: LAST,
    ...(opts.store || {}),
  };
  const h = {
    store, starts: [], logs: [], notes: [], clock: opts.now || at(6, 9),
    running: !!opts.running,
    status: opts.status === undefined ? STATUS_OK : opts.status,
    startResult: opts.startResult || { started: true },
  };
  h.deps = {
    get: async (keys) => Object.fromEntries((Array.isArray(keys) ? keys : [keys]).map((k) => [k, store[k]])),
    set: async (obj) => { Object.assign(store, JSON.parse(JSON.stringify(obj))); },
    now: () => new Date(h.clock.getTime()),
    localDay: () => dayOf(h.clock),
    rand: () => (opts.jitterFrac === undefined ? 0.5 : opts.jitterFrac), // 0.5 → 20 min
    isRunningLocally: async () => h.running,
    fetchStatus: async () => (typeof h.status === "function" ? h.status() : h.status),
    start: async (filters) => {
      h.starts.push(filters);
      return typeof h.startResult === "function" ? h.startResult() : h.startResult;
    },
    log: async (t) => h.logs.push(t),
    notify: async (title, message, p) => h.notes.push({ title, message, path: p }),
  };
  h.tick = (trigger = "alarm") => AD.hdAutoDailyTick(trigger, h.deps);
  return h;
}

(async () => {
  // ── 0. settings hygiene ─────────────────────────────────────────────────────────────
  check("default is OFF", AD.hdAutoDailyNormalize(undefined).enabled === false);
  check("default hour is 9", AD.hdAutoDailyNormalize({}).hour === 9);
  check("a truthy non-boolean never switches it on", AD.hdAutoDailyNormalize({ enabled: "yes" }).enabled === false);
  check("hour is clamped so hour+40 min stays before 21:00", AD.hdAutoDailyNormalize({ enabled: true, hour: 23 }).hour === 20 &&
    20 * 60 + AD.HD_AUTO_DAILY_MAX_JITTER_MIN < AD.HD_AUTO_DAILY_CUTOFF_MIN);
  check("jitter range is 0..40", AD.hdAutoDailyPlan(null, "d", () => 0).state.jitterMin === 0 &&
    AD.hdAutoDailyPlan(null, "d", () => 0.9999).state.jitterMin === 40);
  check("off → nothing happens", (await harness({ store: { autoDaily: { enabled: false, hour: 9 } } }).tick()).action === "off");

  // ── 1. once per local day, after hour + jitter ──────────────────────────────────────
  {
    const h = harness({ now: at(6, 9, 0), jitterFrac: 0.5 }); // jitter 20 → due 9:20
    check("9:00 with a 20-min jitter → waits", (await h.tick()).action === "wait" && h.starts.length === 0);
    check("today's jitter is stored", h.store.autoDailyState.jitterMin === 20 && h.store.autoDailyState.day === dayOf(at(6, 9)));
    h.clock = at(6, 9, 19);
    check("9:19 → still waits", (await h.tick()).action === "wait" && h.starts.length === 0);
    h.clock = at(6, 9, 20);
    const r = await h.tick();
    check("9:20 → fires", r.action === "started" && h.starts.length === 1, r);
    check("fires with the last launch's filters", JSON.stringify(h.starts[0]) === JSON.stringify(LAST.filters));
    check("writes the ⏰ Daily auto-start feed line", h.logs.some((l) => l.startsWith("⏰ Daily auto-start —")));
    h.clock = at(6, 9, 21);
    check("next minute → done, no second start", (await h.tick()).action === "done" && h.starts.length === 1);
    h.clock = at(6, 15, 0);
    await h.tick();
    check("afternoon → still one start today", h.starts.length === 1);
    // Worker restart mid-day: a fresh plan must not re-roll today's jitter.
    const before = h.store.autoDailyState.jitterMin;
    const h2 = harness({ now: at(6, 16), jitterFrac: 0.99, store: { autoDailyState: h.store.autoDailyState } });
    await h2.tick();
    check("a restarted worker keeps today's jitter and record", h2.store.autoDailyState.jitterMin === before && h2.starts.length === 0);
    // Next local day: new jitter, fires again.
    h.clock = at(7, 9, 5);
    check("next day 9:05 (jitter 20) → waits", (await h.tick()).action === "wait");
    check("next day got a fresh record", h.store.autoDailyState.day === dayOf(at(7, 9)) && h.store.autoDailyState.done === false);
    h.clock = at(7, 9, 25);
    check("next day 9:25 → fires again", (await h.tick()).action === "started" && h.starts.length === 2);
  }

  // ── 2. when it must NOT fire ────────────────────────────────────────────────────────
  {
    const h = harness({ now: at(6, 9, 30), running: true });
    const r = await h.tick();
    check("campaign running here → skipped, no start", r.action === "skipped" && r.reason === "already_running" && h.starts.length === 0, r);
    h.running = false;
    h.clock = at(6, 9, 31);
    check("…and that was today's answer", (await h.tick()).action === "done" && h.starts.length === 0);
  }
  {
    const h = harness({ now: at(6, 9, 30), status: { running: false, today_applications: 30, daily_limit: 30 } });
    const r = await h.tick();
    check("server says cap reached → skipped", r.action === "skipped" && r.reason === "cap_reached" && h.starts.length === 0, r);
  }
  {
    const h = harness({
      now: at(6, 9, 30),
      store: { todayCount: 30, todayDate: dayOf(at(6, 9)), campaignCaps: { dailyTotal: 30 } },
      status: { running: false, today_applications: 0, daily_limit: 30 },
    });
    const r = await h.tick();
    check("local counter says cap reached → skipped", r.action === "skipped" && r.reason === "cap_reached" && h.starts.length === 0, r);
  }
  {
    const h = harness({
      now: at(6, 9, 30),
      store: { todayCount: 30, todayDate: dayOf(at(5, 9)), campaignCaps: { dailyTotal: 30 } }, // yesterday's count
    });
    check("yesterday's local count does not block today", (await h.tick()).action === "started");
  }
  {
    const h = harness({ now: at(6, 9, 30), store: { lastLaunch: undefined } });
    const r = await h.tick();
    check("no lastLaunch → does not fire", r.action === "skipped" && r.reason === "no_last_launch" && h.starts.length === 0, r);
    check("…and records why (state + feed line)", h.store.autoDailyState.reason === "no_last_launch" &&
      h.logs.some((l) => l.includes("no previous launch")));
  }
  {
    const h = harness({ now: at(6, 21, 0) });
    const r = await h.tick();
    check("21:00 → too late, no start", r.action === "missed" && h.starts.length === 0, r);
    check("…says so in the feed (schedule existed before today's time)", h.logs.some((l) => l.includes("missed today")));
    const late = harness({ now: at(6, 22, 0), store: { autoDaily: { enabled: true, hour: 9, enabledAt: at(6, 21, 55).getTime() } } });
    await late.tick();
    check("switched on at 21:55 → no 'missed' line (it missed nothing)", late.logs.length === 0 && late.starts.length === 0);
  }
  {
    // Server reports running (another computer, or a zombie the TTL clears) → retry, then go.
    let calls = 0;
    const h = harness({ now: at(6, 9, 30), status: () => (++calls === 1 ? { running: true } : STATUS_OK) });
    const r1 = await h.tick();
    check("server 'running' → retry later, no start", r1.action === "retry" && h.starts.length === 0, r1);
    h.clock = at(6, 9, 32);
    check("…not before the retry spacing", (await h.tick()).action === "wait" && h.starts.length === 0);
    h.clock = at(6, 9, 36);
    check("…then starts once the flag is gone", (await h.tick()).action === "started" && h.starts.length === 1);
  }
  {
    // Status unreachable every time → bounded retries, then one honest failure.
    const h = harness({ now: at(6, 9, 30), status: null });
    let r;
    for (let i = 0; i < AD.HD_AUTO_DAILY_MAX_TRIES + 2; i++) {
      r = await h.tick();
      h.clock = new Date(h.clock.getTime() + AD.HD_AUTO_DAILY_RETRY_MS + 60000);
    }
    check("unreachable server → gives up after MAX_TRIES with one notification",
      h.store.autoDailyState.done && h.store.autoDailyState.outcome === "failed" && h.notes.length === 1 && h.starts.length === 0,
      { state: h.store.autoDailyState, notes: h.notes.length });
  }

  // ── 3. catch-up after sleep / Chrome closed ─────────────────────────────────────────
  {
    // Nothing ran at 9:20 (asleep). First wake / onStartup at 13:07 → fires.
    const h = harness({ now: at(6, 13, 7) });
    const r = await h.tick("startup");
    check("first browser start at 13:07 → catches up", r.action === "started" && h.starts.length === 1, r);
    check("…and the feed line says Chrome just opened", h.logs.some((l) => l.includes("Chrome just opened")));
    const w = harness({ now: at(6, 18, 40) });
    check("first wake at 18:40 → catches up", (await w.tick("alarm")).action === "started");
  }

  // ── settings changes do not pop a window a minute later ─────────────────────────────
  {
    const cfg = AD.hdAutoDailyNormalize({ enabled: true, hour: 9 });
    const plan = AD.hdAutoDailyPlan(null, dayOf(at(6, 14)), () => 0.5).state;
    const after = AD.hdAutoDailyAfterSet(cfg, plan, at(6, 14));
    check("switching on at 14:00 for 9:00 → first run tomorrow", after.done === true && after.reason === "set_after_time");
    const early = AD.hdAutoDailyAfterSet(cfg, plan, at(6, 8));
    check("switching on at 8:00 for 9:00 → runs today", early.done === false);
    const next = AD.hdAutoDailyNextRun(cfg, after, at(6, 14));
    check("next run after that is tomorrow, a 9:00-9:40 window", next.day === "tomorrow" &&
      new Date(next.earliest).getHours() === 9 && new Date(next.latest).getMinutes() === 40 && !next.exact);
    const today = AD.hdAutoDailyNextRun(cfg, plan, at(6, 8));
    check("next run before the time is today's exact time", today.day === "today" && today.exact &&
      new Date(today.earliest).getHours() === 9 && new Date(today.earliest).getMinutes() === 20);
    const stopped = AD.hdAutoDailyAfterUserStop(cfg, plan, at(6, 8, 30));
    check("a human Stop before the time is today's answer", stopped.done && stopped.reason === "stopped_by_you");
  }

  // ── 4. refusal surfaced — the REAL startCampaign, extracted from background.js ───────
  const sliceFn = (startMark, endMark) => {
    const i = BG.indexOf(startMark);
    const j = BG.indexOf(endMark, i);
    if (i < 0 || j < 0) throw new Error(`markers moved: ${startMark}`);
    return BG.slice(i, j + endMark.length);
  };
  const SRC = [
    sliceFn("async function apiError(res) {", "\n}\n"),
    sliceFn("async function apiPost(path, body, { retry = true } = {}) {", "\n}\n"),
    sliceFn("async function refuseStart(reason, source) {", "\n}\n"),
    sliceFn("const HD_OPEN_NOTIF_PREFIX", "\n}\n"),
    sliceFn("async function startCampaign(rawFilters, { source = \"manual\" } = {}) {",
      "  return { started: true, tabId: tab.id, windowId: tabInfo.windowId };\n}\n"),
  ].join("\n");

  function startSandbox(fetchImpl) {
    const store = {};
    const sb = {
      logs: [], notes: [], windowsCreated: 0, tabsCreated: 0,
      CONFIG: { API_BASE: "https://api", API_V1: "/api/v1" },
      fetch: fetchImpl,
      getAuthToken: async () => "tok",
      dropStaleKeyIfUsed: async () => false,
      refreshAccessToken: async () => null,
      noteAuth401: async () => {},
      _auth401Streak: 0,
      addToActivityLog: async (t) => sb.logs.push(t),
      getCachedProfile: async () => ({ onboarding_completed: true, resume_url: "r", keywords: ["pm"], platforms: ["indeed"] }),
      apiGet: async () => ({ submit_mode: "auto", submit_mode_known: true, limit_per_platform: 20, daily_limit: 30 }),
      pickPrimaryPlatform: () => "indeed",
      buildApprovedAtsQueue: async () => [],
      pickAtsOpener: () => null,
      hdLinkedInOnlySelection: () => false,
      CAMPAIGN_START_PLATFORMS: ["indeed", "ziprecruiter"],
      ATS_PLATFORMS: ["greenhouse"],
      POOL_NATIVE_ALL: ["indeed", "ziprecruiter"],
      getPlatformConnections: async () => ({ indeed: { status: "logged_out" } }),
      platformLabel: () => "Indeed",
      platformLoginUrl: () => "https://secure.indeed.com/auth",
      buildPlatformUrl: () => "https://www.indeed.com/jobs?q=pm",
      platformEntryUrl: () => "https://www.indeed.com/",
      updateBadge: () => {},
      hdStartRefusal: AD.hdStartRefusal,
      chrome: {
        storage: { local: {
          get: async (k) => (typeof k === "string" ? { [k]: store[k] } : Object.fromEntries(k.map((x) => [x, store[x]]))),
          set: async (o) => Object.assign(store, o),
          remove: async () => {},
        } },
        notifications: { create: (id, o) => sb.notes.push({ id, ...o }), onClicked: { addListener: () => {} }, clear: () => {} },
        windows: {
          create: async () => { sb.windowsCreated += 1; return { id: 7, tabs: [{ id: 70 }] }; },
          get: async () => { throw new Error("no window"); },
          update: async () => {},
        },
        tabs: {
          create: async () => { sb.tabsCreated += 1; return { id: 1 }; },
          get: async (id) => ({ id, windowId: 7 }),
          update: async () => {},
        },
        power: null,
      },
      JSON, Date, Error, Array, Object, String, Number, Math, Set, Promise, console, encodeURIComponent,
    };
    sb.store = store;
    vm.createContext(sb);
    vm.runInContext(SRC, sb);
    return sb;
  }
  const json = (status, body) => async () => ({ ok: status < 300, status, statusText: status === 403 ? "Forbidden" : "x", json: async () => body });

  {
    // Manual start, server refuses with 403 (the profile is logged-out-free here: connections
    // say indeed is logged_out, so make the board connected for this case).
    const sb = startSandbox(json(403, { detail: "employer_answers_missing" }));
    sb.getPlatformConnections = async () => ({});
    const res = await vm.runInContext(`startCampaign({ keywords: ["pm"], platforms: ["indeed"] }, { source: "manual" })`, sb);
    check("403 refusal stops a MANUAL start", res.started === false && res.error === "employer_answers_missing", res);
    check("…no automation window opened", sb.windowsCreated === 0);
    check("…campaignRunning never raised", sb.store.campaignRunning !== true);
    check("…a feed line names the reason", sb.logs.some((l) => l.includes("refused by HireDrop") && l.includes("questions employers ask")));
    check("…a notification says open HireDrop to fix, and opens the fixing page",
      sb.notes.length === 1 && /open HireDrop to fix/.test(sb.notes[0].message) && sb.notes[0].id.startsWith("hd-open|/dashboard/settings?tab=forms|"),
      sb.notes);
  }
  {
    const sb = startSandbox(json(400, { detail: "lever_needs_tap" }));
    sb.getPlatformConnections = async () => ({});
    const res = await vm.runInContext(`startCampaign({ keywords: ["pm"], platforms: ["indeed"] }, { source: "auto" })`, sb);
    check("400 lever_needs_tap stops an AUTO start too", res.started === false && res.error === "lever_needs_tap" && sb.windowsCreated === 0, res);
  }
  {
    // A server that is DOWN is not a refusal — the old fail-open stays.
    const sb = startSandbox(json(503, {}));
    sb.getPlatformConnections = async () => ({});
    const res = await vm.runInContext(`startCampaign({ keywords: ["pm"], platforms: ["indeed"] }, { source: "manual" })`, sb);
    check("503 → still starts (server down is not a refusal)", res.started === true && sb.windowsCreated === 1, res);
    check("…and raises campaignRunning", sb.store.campaignRunning === true);
  }
  {
    // Auto start takes the server's keyword order; a manual start keeps the dashboard's.
    const ok = json(200, { started: true, filters: { keywords: ["designer", "pm"] } });
    const a = startSandbox(ok); a.getPlatformConnections = async () => ({});
    await vm.runInContext(`startCampaign({ keywords: ["pm", "designer"], platforms: ["indeed"] }, { source: "auto" })`, a);
    check("auto start runs the server's keyword order", JSON.stringify(a.store.campaignFilters.keywords) === '["designer","pm"]', a.store.campaignFilters);
    const m = startSandbox(ok); m.getPlatformConnections = async () => ({});
    await vm.runInContext(`startCampaign({ keywords: ["pm", "designer"], platforms: ["indeed"] }, { source: "manual" })`, m);
    check("manual start keeps the dashboard's order", JSON.stringify(m.store.campaignFilters.keywords) === '["pm","designer"]', m.store.campaignFilters);
  }
  {
    // Signed out of the board: a manual start opens the login page; the 9 AM schedule must not.
    const a = startSandbox(json(200, { started: true }));
    const ra = await vm.runInContext(`startCampaign({ keywords: ["pm"], platforms: ["indeed"] }, { source: "auto" })`, a);
    check("auto + signed out → refuses WITHOUT opening a login tab", ra.error === "not_connected" && a.tabsCreated === 0 && a.windowsCreated === 0, ra);
    const m = startSandbox(json(200, { started: true }));
    await vm.runInContext(`startCampaign({ keywords: ["pm"], platforms: ["indeed"] }, { source: "manual" })`, m);
    check("manual + signed out → opens the login page (unchanged)", m.tabsCreated === 1);
  }
  {
    // End to end: tick → real startCampaign → server 403. One notification, not two.
    const sb = startSandbox(json(403, { detail: "us_only" }));
    sb.getPlatformConnections = async () => ({});
    const h = harness({ now: at(6, 9, 30) });
    h.deps.start = (filters) => vm.runInContext(`startCampaign(${JSON.stringify(filters)}, { source: "auto" })`, sb);
    const r = await h.tick();
    check("auto refusal → today's outcome is 'refused' with the server's reason", r.action === "refused" && r.reason === "us_only" &&
      h.store.autoDailyState.reason === "us_only", r);
    check("…announced once (startCampaign's notification, not a second from the tick)", sb.notes.length === 1 && h.notes.length === 0,
      { sb: sb.notes.length, tick: h.notes.length });
    check("…the feed line says it was the daily auto-start", sb.logs.some((l) => l.startsWith("⏰ Daily auto-start refused")));
  }
  {
    // An extension-side refusal during auto (not from the server) is notified by the tick.
    const h = harness({ now: at(6, 9, 30), startResult: { started: false, error: "not_connected", message: "You're signed out of Indeed" } });
    const r = await h.tick();
    check("auto + extension-side refusal → notified by the tick, final for the day", r.action === "refused" && h.notes.length === 1 &&
      /open HireDrop to fix/.test(h.notes[0].message), h.notes);
  }

  // ── 5. wiring (source shape) ────────────────────────────────────────────────────────
  {
    const handler = BG.slice(BG.indexOf('case "START_CAMPAIGN": {'), BG.indexOf('case "CAPTURE_SCREENSHOT"'));
    check("START_CAMPAIGN delegates to the shared startCampaign", /await startCampaign\(msg\.filters, \{ source: "manual" \}\)/.test(handler));
    check("START_CAMPAIGN has no second copy of the start body", !handler.includes("chrome.windows.create"));
    check("only one function opens the automation window with focused:false",
      (BG.match(/focused: false,\n\s*width: 1280/g) || []).length === 1);
    check("lastLaunch written at a manual start that started", /if \(res && res\.started\) \{[\s\S]{0,200}\[HD_LAST_LAUNCH_KEY\]: \{ filters: msg\.filters/.test(handler));
    check("the old swallow-everything catch is gone",
      !/await apiPost\("\/campaign\/start", filters\);\s*\} catch \{/.test(BG));
    const alarm = BG.slice(BG.indexOf("chrome.alarms.onAlarm.addListener"));
    check("the minute alarm drives the tick", alarm.slice(0, alarm.indexOf("\n});")).includes('autoDailyTick("alarm")'));
    const startup = BG.slice(BG.indexOf("chrome.runtime.onStartup.addListener"));
    const su = startup.slice(0, startup.indexOf("\n});"));
    check("onStartup catches up AFTER the day counters reset",
      su.includes('autoDailyTick("startup")') && su.indexOf("todayCount: 0") < su.indexOf('autoDailyTick("startup")'));
    check("the tick counts today from the user's zone (tz on /campaign/status)", /\/campaign\/status\$\{tz \?/.test(BG));
    check("background loads auto-daily.js", /importScripts\([^)]*"auto-daily\.js"/.test(BG));
    check("autoDaily / autoDailyState / lastLaunch are user-scoped",
      ["autoDaily", "autoDailyState", "lastLaunch"].every((k) => BG.slice(BG.indexOf("const USER_SCOPED_KEYS"), BG.indexOf("];", BG.indexOf("const USER_SCOPED_KEYS"))).includes(`"${k}"`)));
    const stop = BG.slice(BG.indexOf('case "STOP_CAMPAIGN": {'), BG.indexOf('case "APPLICATION_SAVED"'));
    check("a human Stop marks today's schedule", stop.includes("msg.userStop === true") && stop.includes("hdAutoDailyAfterUserStop"));
    check("ping.js marks the dashboard's Stop as a human one", /type: "STOP_CAMPAIGN", userStop: true/.test(PING));
    check("popup marks its Stop as a human one", /type: "STOP_CAMPAIGN", userStop: true/.test(POPUP));
    check("ping.js bridges GET and SET", ["HIREDROP_GET_AUTO_DAILY", "HIREDROP_SET_AUTO_DAILY", "HIREDROP_AUTO_DAILY"].every((t) => PING.includes(t)));
    check("background answers both", ['case "AUTO_DAILY_GET"', 'case "AUTO_DAILY_SET"'].every((t) => BG.includes(t)));
    check("no new permission was needed (alarms + notifications already there)",
      MANIFEST.permissions.includes("alarms") && MANIFEST.permissions.includes("notifications") && MANIFEST.permissions.length === 8);
  }

  if (failures) {
    console.error(`\n${failures} check(s) failed`);
    process.exit(1);
  }
  console.log("\nall auto-daily checks passed");
})().catch((e) => {
  console.error("FAIL (threw)", e);
  process.exit(1);
});
