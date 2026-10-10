// A Start the extension refuses says why in the feed, and a stale "signed out" can't refuse it.
//
//   node <repo>/jobflow/chrome-extension/tests/start-refusal-visible.test.js
//
// Why it exists (live 10-08 03:05Z): "▶ Start received by the extension" at 03:05:09, the
// extension's GET /campaign/status at 03:05:09.9, the dashboard's POST /campaign/stop at
// 03:05:11 — and nothing else. startCampaign() had refused, but only onboarding_incomplete
// ever wrote a feed line; every other pre-flight refusal answered the dashboard, which
// showed it for a moment and rolled back. The cause had to be found by elimination:
// platformConnections.indeed = {logged_out, host: secure.indeed.com}, written by the
// detector #388 fixed. The host rule trusts that host, a reload/update keeps
// chrome.storage.local, and open Indeed tabs keep orphaned old scripts — so the record
// outlived its code, and every Chrome Web Store update would refuse a signed-in user's first
// Start the same way.
//
// What must hold, against the REAL startCampaign / logoutIsTrustworthy /
// getPlatformConnections / writers (sliced out of background.js and content.js):
//   1. Every `started: false` exit writes exactly ONE warn line naming the reason and its
//      code, and returns what it returned before (plus `logged`, which tells the daily
//      auto-start not to write a second line).
//   2. A logged_out stamped by ANOTHER build — or by no build (pre-stamp) — is dropped:
//      Start goes ahead and commits (window opened, campaignRunning raised).
//   3. A logged_out stamped by THIS build still refuses (not_connected, login tab opened).
//   4. Every writer stamps the running build: background's PLATFORM_AUTH and
//      PLATFORM_LOGIN_REQUIRED, and content.js reportPlatformAuth.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const AD = require("../auto-daily.js");

const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
const CS = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${typeof detail === "string" ? detail : JSON.stringify(detail)})`}`);
}

function sliceFrom(src, startMark, endMark) {
  const i = src.indexOf(startMark);
  const j = i < 0 ? -1 : src.indexOf(endMark, i);
  if (i < 0 || j < 0) {
    console.error(`markers moved: ${startMark}`);
    process.exit(2);
  }
  return src.slice(i, j + endMark.length);
}

const BUILD = "2.0.0";
const OTHER_BUILD = "1.9.0";

const START_SRC = [
  (BG.match(/^const INDEED_APPLY_HOSTS = [^;]+;/m) || [""])[0],
  sliceFrom(BG, "function logoutIsTrustworthy(platform, rec) {", "\n}\n"),
  sliceFrom(BG, "async function getPlatformConnections() {", "\n}\n"),
  sliceFrom(BG, "async function refuseStart(reason, source) {", "\n}\n"),
  sliceFrom(BG, "async function refuseStartLocally(res, source) {", "\n}\n"),
  sliceFrom(BG, "const HD_OPEN_NOTIF_PREFIX", "\n}\n"),
  sliceFrom(BG, "async function startCampaign(rawFilters, { source = \"manual\" } = {}) {",
    "  return { started: true, tabId: tab.id, windowId: tabInfo.windowId };\n}\n"),
].join("\n");

// The two writers live inside handleMessage's switch; run their case blocks as-is.
const WRITER_CASES = [
  sliceFrom(BG, "    case \"PLATFORM_AUTH\": {", "      return { ok: true };\n    }\n"),
  sliceFrom(BG, "    case \"PLATFORM_LOGIN_REQUIRED\": {", "      return { ok: true };\n    }\n"),
].join("\n");

const PROFILE = { onboarding_completed: true, resume_url: "r", keywords: ["pm"], platforms: ["indeed"] };
const STATUS = { submit_mode: "auto", submit_mode_known: true, limit_per_platform: 20, daily_limit: 30 };

function sandbox(over = {}) {
  const store = { ...(over.store || {}) };
  const sb = {
    logs: [], notes: [], windowsCreated: 0, tabsCreated: 0, started: 0,
    addToActivityLog: async (text, cls) => { sb.logs.push({ text, cls }); },
    endFinishRun: async () => false,
    getCachedProfile: async () => (over.profile !== undefined ? over.profile : PROFILE),
    apiGet: over.apiGet || (async () => STATUS),
    apiPost: async (p) => { if (p === "/campaign/start") sb.started += 1; return { started: true }; },
    pickPrimaryPlatform: (ps) => (ps && ps[0] === "ziprecruiter" ? "ziprecruiter" : "indeed"),
    buildApprovedAtsQueue: over.buildApprovedAtsQueue || (async () => []),
    buildAtsQueue: over.buildAtsQueue || (async () => ({ queue: [], pool: 0, offSearch: 0, error: null })),
    pickAtsOpener: over.pickAtsOpener || (() => null),
    hdLinkedInOnlySelection: (ps) => Array.isArray(ps) && ps.length === 1 && ps[0] === "linkedin",
    CAMPAIGN_START_PLATFORMS: ["indeed", "ziprecruiter"],
    ATS_PLATFORMS: ["greenhouse", "lever", "ashby"],
    POOL_NATIVE_ALL: ["indeed", "ziprecruiter"],
    platformLabel: (p) => (p === "ziprecruiter" ? "ZipRecruiter" : "Indeed"),
    platformLoginUrl: () => "https://secure.indeed.com/auth",
    buildPlatformUrl: () => "https://www.indeed.com/jobs?q=pm",
    platformEntryUrl: () => "https://www.indeed.com/",
    updateBadge: () => {},
    hdStartRefusal: AD.hdStartRefusal,
    chrome: {
      runtime: { getManifest: () => ({ version: BUILD }) },
      storage: { local: {
        get: async (k) => (typeof k === "string" ? { [k]: store[k] } : Object.fromEntries(k.map((x) => [x, store[x]]))),
        set: async (o) => { Object.assign(store, JSON.parse(JSON.stringify(o))); },
        remove: async (ks) => { for (const k of [].concat(ks)) delete store[k]; },
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
    JSON, Date, Error, Array, Object, String, Number, Math, Set, Promise, console,
  };
  sb.store = store;
  vm.createContext(sb);
  vm.runInContext(`${START_SRC}\nasync function __writer(msg) { switch (msg.type) {\n${WRITER_CASES}\n} }`, sb);
  return sb;
}

const start = (sb, filters, source = "manual") =>
  vm.runInContext(`startCampaign(${JSON.stringify(filters)}, { source: ${JSON.stringify(source)} })`, sb);

const loggedOut = (extra) => ({ status: "logged_out", checkedAt: "2026-10-08T02:00:00.000Z", host: "secure.indeed.com", ...extra });

(async () => {
  // --- 1. Every refusal says why, once, with its code ------------------------------------
  const IND = { keywords: ["pm"], platforms: ["indeed"] };
  const CASES = [
    { code: "onboarding_incomplete", over: { profile: { onboarding_completed: false } }, filters: IND },
    { code: "no_keywords", over: { profile: { ...PROFILE, keywords: [] } }, filters: { keywords: [], platforms: ["indeed"] } },
    { code: "mode_unknown", over: { apiGet: async () => { throw new Error("offline"); } }, filters: IND },
    { code: "free_limit_reached", over: { apiGet: async () => ({ ...STATUS, free_limit: 40, free_used: 40 }) }, filters: IND },
    { code: "no_approved_jobs", over: { apiGet: async () => ({ ...STATUS, submit_mode: "tap" }) }, filters: IND },
    { code: "lever_needs_tap", over: {}, filters: { keywords: ["pm"], platforms: ["lever"] } },
    { code: "linkedin_not_ready", over: {}, filters: { keywords: ["pm"], platforms: ["linkedin"] } },
    { code: "not_connected", over: { store: { platformConnections: { indeed: loggedOut({ extVersion: BUILD }) } } }, filters: IND },
    { code: "not_connected", source: "auto", over: { store: { platformConnections: { indeed: loggedOut({ extVersion: BUILD }) } } }, filters: IND },
    { code: "no_ats_jobs", over: { pickAtsOpener: () => "greenhouse" }, filters: { keywords: ["pm"], platforms: ["greenhouse"] } },
  ];
  const seen = new Set();
  for (const c of CASES) {
    const sb = sandbox(c.over);
    const src = c.source || "manual";
    const res = await start(sb, c.filters, src);
    seen.add(c.code);
    const label = `${c.code}${c.source ? ` (${c.source})` : ""}`;
    check(`${label}: refused with that code`, res && res.started === false && res.error === c.code, res);
    // Progress lines that may precede a refusal (no_ats_jobs is decided after the commit
    // line) are not refusals; anything else after "Start received" would be a second one.
    const after = sb.logs.slice(sb.logs.findIndex((l) => l.text.startsWith("▶ Start received")) + 1)
      .filter((l) => !l.text.startsWith("Heads-up: no resume") && !l.text.startsWith("Starting your campaign"));
    const lines = after.filter((l) => l.text.includes(`(${c.code})`));
    check(`${label}: exactly one feed line names it, at warn`,
      lines.length === 1 && lines[0].cls === "warn", sb.logs);
    check(`${label}: …and nothing else after "Start received" (no second line)`, after.length === 1, after);
    const why = res.message || AD.hdStartRefusal(c.code).text;
    check(`${label}: the line carries the user-readable reason`, lines.length === 1 && lines[0].text.includes(why), lines);
    check(`${label}: the daily auto-start's line prefix when the schedule pressed Start`,
      lines.length === 1 && (src === "auto"
        ? lines[0].text.startsWith("⏰ Daily auto-start didn't start: ")
        : lines[0].text.startsWith("Didn't start: ")), lines);
    check(`${label}: tells the caller the line is written (auto-daily must not write another)`, res.logged === true, res);
    check(`${label}: no automation window, campaignRunning never raised`,
      sb.windowsCreated === 0 && sb.store.campaignRunning !== true, { w: sb.windowsCreated, r: sb.store.campaignRunning });
  }
  // Every `started: false` in startCampaign is covered above — a new exit must join this list.
  {
    const body = sliceFrom(BG, "async function startCampaign(rawFilters", "  return { started: true, tabId: tab.id, windowId: tabInfo.windowId };\n}\n");
    const codes = [...body.matchAll(/started: false,?\s*(?:\n\s*)?error: "([a-z_]+)"/g)].map((m) => m[1]);
    const missing = [...new Set(codes)].filter((c) => !seen.has(c));
    check("every literal refusal code in startCampaign is exercised here", codes.length >= 9 && missing.length === 0,
      { codes, missing });
    // Bare `return { started: false` (no helper) would be a silent refusal again.
    check("no refusal in startCampaign bypasses the feed line",
      !/return \{\s*\n?\s*started: false/.test(body), "a `return { started: false … }` without refuseStartLocally/refuseStart");
  }

  // Return values are what the dashboard / popup / auto-start already read.
  {
    const sb = sandbox({ profile: { ...PROFILE, keywords: [] } });
    const res = await start(sb, { keywords: [], platforms: ["indeed"] }, "manual");
    check("no_keywords keeps its message for the dashboard",
      res.error === "no_keywords" && res.message === "Add at least one keyword first — the campaign needs something to search for.", res);
    const nc = sandbox({ store: { platformConnections: { indeed: loggedOut({ extVersion: BUILD }) } } });
    const r2 = await start(nc, IND, "manual");
    check("not_connected keeps platform + message, and the manual start still opens the login page",
      r2.platform === "indeed" && /^Sign into Indeed first/.test(r2.message) && nc.tabsCreated === 1, r2);
    const na = sandbox({ store: { platformConnections: { indeed: loggedOut({ extVersion: BUILD }) } } });
    const r3 = await start(na, IND, "auto");
    check("…and the 9 AM schedule still opens no tab", r3.error === "not_connected" && na.tabsCreated === 0, r3);
  }

  // --- 2. A logged_out that outlived its code is dropped; Start commits -------------------
  for (const [label, rec] of [
    ["another build", loggedOut({ extVersion: OTHER_BUILD })],
    ["no build at all (written before the stamp — the 10-08 record)", loggedOut()],
  ]) {
    const sb = sandbox({ store: { platformConnections: { indeed: rec, ziprecruiter: { status: "connected", checkedAt: "x", host: "www.ziprecruiter.com" } } } });
    const res = await start(sb, IND, "manual");
    check(`stale logged_out from ${label}: Start goes ahead`, res && res.started === true, res);
    check(`…commits: window opened, /campaign/start sent, campaignRunning raised`,
      sb.windowsCreated === 1 && sb.started === 1 && sb.store.campaignRunning === true,
      { w: sb.windowsCreated, s: sb.started, r: sb.store.campaignRunning });
    check(`…no login tab, no refusal line`, sb.tabsCreated === 0 && !sb.logs.some((l) => /\(not_connected\)/.test(l.text)), sb.logs);
    check(`…the record is gone from storage (the dashboard's direct read heals too)`,
      !("indeed" in (sb.store.platformConnections || {})), sb.store.platformConnections);
    check(`…other records untouched`, sb.store.platformConnections.ziprecruiter.status === "connected");
  }
  {
    // A search-host guess is still dropped by the host rule, version or not.
    const sb = sandbox({ store: { platformConnections: { indeed: loggedOut({ host: "www.indeed.com", extVersion: BUILD }) } } });
    const res = await start(sb, IND, "manual");
    check("a this-build logged_out read on the SEARCH host is still not trusted (host rule intact)", res.started === true, res);
  }
  {
    // ZipRecruiter has no host rule — the version rule is what drops its stale record.
    const zr = { keywords: ["pm"], platforms: ["ziprecruiter"] };
    const stale = sandbox({ store: { platformConnections: { ziprecruiter: { status: "logged_out", checkedAt: "x", host: "www.ziprecruiter.com" } } } });
    check("a pre-stamp ZipRecruiter logged_out no longer refuses", (await start(stale, zr)).started === true);
    const live = sandbox({ store: { platformConnections: { ziprecruiter: { status: "logged_out", checkedAt: "x", host: "www.ziprecruiter.com", extVersion: BUILD } } } });
    const r = await start(live, zr);
    check("a this-build ZipRecruiter logged_out still refuses", r.started === false && r.error === "not_connected" && r.platform === "ziprecruiter", r);
  }

  // --- 3/4. Writers stamp the running build; their record gates the next Start ----------
  {
    const sb = sandbox();
    await vm.runInContext(`__writer({ type: "PLATFORM_AUTH", platform: "indeed", status: "logged_out", host: "secure.indeed.com" })`, sb);
    const rec = sb.store.platformConnections.indeed;
    check("PLATFORM_AUTH stamps the running build", rec && rec.extVersion === BUILD && rec.host === "secure.indeed.com", rec);
    const res = await start(sb, IND, "manual");
    check("…and a real sign-out it recorded still refuses Start", res.started === false && res.error === "not_connected", res);
  }
  {
    // The message cannot vouch for a build: background stamps its own.
    const sb = sandbox();
    await vm.runInContext(`__writer({ type: "PLATFORM_AUTH", platform: "indeed", status: "connected", host: "secure.indeed.com", extVersion: "0.0.1" })`, sb);
    check("PLATFORM_AUTH ignores a version carried in the message", sb.store.platformConnections.indeed.extVersion === BUILD,
      sb.store.platformConnections.indeed);
  }
  {
    const sb = sandbox();
    await vm.runInContext(`__writer({ type: "PLATFORM_LOGIN_REQUIRED", platform: "indeed", host: "smartapply.indeed.com", url: "https://smartapply.indeed.com/x" })`, sb);
    const rec = sb.store.platformConnections.indeed;
    check("PLATFORM_LOGIN_REQUIRED (the campaign's own login wall) stamps the running build",
      rec && rec.status === "logged_out" && rec.extVersion === BUILD, rec);
    const res = await start(sb, IND, "manual");
    check("…and gates the next Start", res.started === false && res.error === "not_connected", res);
  }
  {
    // content.js writes the record straight into storage — it must stamp too, or background
    // would drop every logged_out the page itself detected.
    const fn = sliceFrom(CS, "  function extVersion() {", "\n  }\n") +
      sliceFrom(CS, "  async function reportPlatformAuth() {", "\n  }\n");
    const store = {};
    const sent = [];
    const box = {
      chrome: { runtime: { getManifest: () => ({ version: BUILD }) } },
      window: { location: { hostname: "secure.indeed.com" } },
      detectPlatform: () => "indeed",
      settleIndeedAuthTransit: async () => {},
      detectPlatformAuth: () => "logged_out",
      sleep: async () => {},
      storageGet: async (k) => ({ [k]: store[k] }),
      storageSet: async (o) => { Object.assign(store, o); },
      safeSend: (m) => sent.push(m),
      Date,
    };
    vm.createContext(box);
    vm.runInContext(`${fn}\nthis.__report = reportPlatformAuth;`, box);
    await box.__report();
    const rec = (store.platformConnections || {}).indeed;
    check("content.js reportPlatformAuth stamps the running build",
      rec && rec.status === "logged_out" && rec.extVersion === BUILD && rec.host === "secure.indeed.com", rec);
    const sb = sandbox({ store: { platformConnections: store.platformConnections } });
    const res = await start(sb, IND, "manual");
    check("…so a sign-out the page detected still refuses Start", res.started === false && res.error === "not_connected", res);
  }
  {
    // An update runs onInstalled, which sweeps platformConnections with the same rule — that
    // is what heals the dashboard's own gate (ping.js reads storage directly).
    const onInstalled = sliceFrom(BG, "chrome.runtime.onInstalled.addListener(async () => {", "\n});\n");
    check("onInstalled sweeps stored records through logoutIsTrustworthy",
      /for \(const \[p, rec\] of Object\.entries\(conns\)\) \{\s*\n\s*if \(!logoutIsTrustworthy\(p, rec\)\) \{ delete conns\[p\]; changed = true; \}/.test(onInstalled));
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
