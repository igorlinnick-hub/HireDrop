// After Stop, the walk does nothing more: no judge, no verdict line, no click, no navigation.
//
//   node <repo>/jobflow/chrome-extension/tests/stop-halts-walk.test.js
//
// Why it exists (live 10-08): "⏹ Campaign stopped (requested by you)" at 05:43:15, then
// "✓ Good fit (42): … @ Hello! Destination Management" at :23 and "Skip (no Apply button)"
// at :25; and stopped 02:53:54 → "Warmup complete — navigating to job search" at :58, then
// the navigation. Nothing was submitted, but each was the walk resuming after an await — the
// fit judge, the warmup's scroll passes, a pre-navigation pause — without asking whether the
// campaign was still on. waitForApplyButton even answers null on Stop, and the caller read
// that as "this posting has no Apply button".
//
// The REAL functions run here (sliced out of content.js) over the captured Indeed job page
// (fixtures/indeed-viewjob-no-cmp.html) and the captured ZipRecruiter pane
// (fixtures/ziprecruiter-serp-right-pane.html). Stop is the same storage flag content.js
// already reads everywhere (isCampaignRunning → campaignRunning), flipped by a stub at the
// exact await where the live Stop landed. Each Stop case has a control without Stop, so a
// harness that never reaches the action can't pass for the wrong reason.
// The Indeed homepage DOM for the warmup is minimal, not captured (only the search input and
// its submit button matter there).

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM, VirtualConsole } = require("jsdom");

const CS = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const FIX = (f) => fs.readFileSync(path.join(__dirname, "fixtures", f), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${typeof detail === "string" ? detail : JSON.stringify(detail)})`}`);
}

// A top-level function inside content.js's IIFE closes at two-space indent.
function fn(signature) {
  const a = CS.indexOf(signature);
  const b = a < 0 ? -1 : CS.indexOf("\n  }\n", a);
  if (a < 0 || b < 0) {
    console.error(`markers moved: ${signature}`);
    process.exit(2);
  }
  return CS.slice(a, b + 5);
}

const WARMUP = fn("  function urlsSameJob(a, b) {") + fn("  function findIndeedWhereInput(whatInput) {") +
  fn("  async function sessionWarmup() {");
const PHASE2_INDEED = fn("  async function phase2_indeed() {");
const WAIT_APPLY = fn("  async function waitForApplyButton(timeoutMs = 8000) {");
const PHASE2_ZR = fn("  async function phase2_ziprecruiter() {");
const SKIP_NEXT = fn("  async function skipToNextJob() {");

function storage(store) {
  return {
    storageGet: async (keys) => {
      const out = {};
      for (const k of [].concat(keys)) if (k in store) out[k] = store[k];
      return out;
    },
    storageSet: async (patch) => { Object.assign(store, patch); },
    storageRemove: async (keys) => { for (const k of [].concat(keys)) delete store[k]; return true; },
    isCampaignRunning: async () => store.campaignRunning === true,
  };
}

const stop = (store) => { store.campaignRunning = false; };

(async () => {
  // --- 1. Warmup: Stop during the scroll passes ------------------------------------------
  // Opened on the Indeed homepage with no search box in reach → the fallback navigation, the
  // exact line of the 02:53 incident.
  function warm({ stopAtSleep = 0, stopWhileTyping = false, homepage = "" } = {}) {
    const store = { campaignRunning: true, campaignWarmedUp: false,
      campaignTargetUrl: "https://www.indeed.com/jobs?q=marketing+coordinator&l=Houston%2C+TX" };
    const rec = { backend: [], navTo: null, clicks: [] };
    const { window } = new JSDOM(`<body>${homepage}</body>`, { url: "https://www.indeed.com/", virtualConsole: new VirtualConsole() });
    let sleeps = 0;
    const box = {
      ...storage(store),
      document: window.document,
      window: {
        scrollBy() {}, innerWidth: 1280, innerHeight: 900,
        location: { get href() { return "https://www.indeed.com/"; }, set href(v) { rec.navTo = v; } },
      },
      detectPlatform: () => "indeed",
      log: () => {},
      logBackend: (t) => rec.backend.push(t),
      moveCursorTo: async () => {},
      sleep: async () => { sleeps += 1; if (sleeps === stopAtSleep) stop(store); },
      humanDelay: () => 0,
      humanClick: async (el) => rec.clicks.push(el.id || el.tagName),
      typeValue: async () => { if (stopWhileTyping) stop(store); },
      KeyboardEvent: window.KeyboardEvent,
      URL, Math, Date, Promise,
    };
    vm.createContext(box);
    vm.runInContext(`${WARMUP}\nthis.__run = sessionWarmup;`, box);
    return { box, store, rec };
  }
  {
    const w = warm();
    await w.box.__run();
    check("control: warmup without Stop navigates to the search and says so",
      w.rec.navTo === w.store.campaignTargetUrl && w.rec.backend.includes("Warmup complete — navigating to job search"),
      { nav: w.rec.navTo, log: w.rec.backend });
  }
  {
    const w = warm({ stopAtSleep: 1 });
    await w.box.__run();
    check("Stop during the scroll passes: no navigation", w.rec.navTo === null, w.rec.navTo);
    check("…no 'Warmup complete' line", !w.rec.backend.some((t) => /Warmup complete/.test(t)), w.rec.backend);
    check("…and the run is not marked warmed (a stopped warmup is not a done one)", w.store.campaignWarmedUp === false);
  }
  {
    const HOME = '<form><input id="text-input-what" name="q"><button type="submit" id="search-go">Find jobs</button></form>';
    const c = warm({ homepage: HOME });
    await c.box.__run();
    check("control: the typed search is submitted", c.rec.clicks.includes("search-go"), c.rec.clicks);
    const w = warm({ homepage: HOME, stopWhileTyping: true });
    await w.box.__run();
    check("Stop while the search is being typed: the search is never submitted",
      !w.rec.clicks.includes("search-go"), w.rec.clicks);
  }

  // --- 2. Indeed job page: Stop while the fit judge answers -------------------------------
  const INDEED_URL = "https://www.indeed.com/viewjob?jk=d8b40b219fcb72ca";
  const INDEED_PAGE = FIX("indeed-viewjob-no-cmp.html");
  function indeedJob({ stopOnFirstSleep = false, stopInJudge = false, stopInRecord = false } = {}) {
    const store = { campaignRunning: true, pendingJobs: [], reviewMode: false };
    const rec = { backend: [], sent: [], clicks: [], skips: 0, recorded: 0 };
    const { window } = new JSDOM(INDEED_PAGE, { url: INDEED_URL, virtualConsole: new VirtualConsole() });
    const BTN = { id: "indeedApplyButton" };
    let sleeps = 0;
    const box = {
      ...storage(store),
      document: window.document,
      location: { pathname: "/viewjob", href: INDEED_URL },
      window: { location: { href: INDEED_URL } },
      MAX_APPLICATIONS_PER_PLATFORM: 15,
      UNREADABLE_STREAK_LIMIT: 5,
      getPlatformCount: async () => 0,
      keywordCapReached: async () => false,
      goBackToIndeedJobList: async () => {},
      log: () => {},
      logBackend: (t) => rec.backend.push(t),
      sleep: async () => { sleeps += 1; if (stopOnFirstSleep && sleeps === 1) stop(store); },
      humanDelay: () => 0,
      titleFromDocumentTitle: () => "",
      readIndeedJobCompany: () => "Adventure Loom Htx",
      readIndeedJobLocation: () => "Houston, TX",
      readJobDescription: (el) => (el ? el.textContent.trim() : ""),
      cardCompanyFor: () => ({}),
      cardLocationFor: () => "",
      jobIdFromUrl: () => "d8b40b219fcb72ca",
      titleMatchesKeywords: () => true,
      titleGateKeywords: async () => ["events"],
      sendMsg: async (m) => {
        rec.sent.push(m.type);
        if (m.type !== "ASSESS_FIT") return {};
        if (stopInJudge) stop(store); // the person pressed Stop while the judge was thinking
        return { decision: "apply", judged: true, fit_score: 42 };
      },
      recordJobDescription: async () => { rec.recorded += 1; if (stopInRecord) stop(store); },
      findApplyButton: () => BTN,
      shouldMisclick: () => false,
      performMisclick: async () => {},
      humanClick: async (el) => rec.clicks.push(el.id),
      waitForFormVisible: async () => false,
      isFormVisible: () => false,
      phase3_fillForm: async () => {},
      skipToNextJob: async () => { rec.skips += 1; },
      Date, Promise,
    };
    vm.createContext(box);
    vm.runInContext(`var lastPhase = "";\n${WAIT_APPLY}\n${PHASE2_INDEED}\nthis.__run = phase2_indeed;`, box);
    return { box, store, rec };
  }
  {
    const c = indeedJob();
    await c.box.__run();
    check("control: the captured job page is judged, logged as a fit, and its Apply clicked",
      c.rec.backend.includes("✓ Good fit (42): Events Associate @ Adventure Loom Htx") && c.rec.clicks.includes("indeedApplyButton"),
      { log: c.rec.backend, clicks: c.rec.clicks });
  }
  {
    const s = indeedJob({ stopInJudge: true });
    await s.box.__run();
    check("Stop while the judge answers: no 'Good fit' line (the 05:43:23 line)",
      !s.rec.backend.some((t) => /Good fit/.test(t)), s.rec.backend);
    check("…no Apply click, no skip to the next job, nothing recorded",
      s.rec.clicks.length === 0 && s.rec.skips === 0 && s.rec.recorded === 0,
      { clicks: s.rec.clicks, skips: s.rec.skips, recorded: s.rec.recorded });
    check("…and no 'Skip (…)' verdict either", !s.rec.backend.some((t) => /^Skip \(|Skipped/.test(t)), s.rec.backend);
  }
  {
    const s = indeedJob({ stopInRecord: true });
    await s.box.__run();
    check("Stop after the verdict, before the Apply button: no 'Skip (no Apply button)' (the 05:43:25 line)",
      !s.rec.backend.some((t) => /no Apply button/.test(t)), s.rec.backend);
    check("…no skip to the next job, no click", s.rec.skips === 0 && s.rec.clicks.length === 0,
      { skips: s.rec.skips, clicks: s.rec.clicks });
  }
  {
    const s = indeedJob({ stopOnFirstSleep: true });
    await s.box.__run();
    check("Stop while the page is being read: the judge is never called", !s.rec.sent.includes("ASSESS_FIT"), s.rec.sent);
  }
  {
    // The contract the fix leans on: a null from waitForApplyButton can mean "stopped".
    const store = { campaignRunning: false };
    const box = { ...storage(store), findApplyButton: () => ({ id: "x" }), sleep: async () => {}, Date, Promise };
    vm.createContext(box);
    vm.runInContext(`${WAIT_APPLY}\nthis.__wait = waitForApplyButton;`, box);
    check("waitForApplyButton answers null on Stop even with a button on the page", (await box.__wait(8000)) === null);
  }

  // --- 3. ZipRecruiter: Stop while the judge answers must not click Quick Apply ------------
  const ZR_URL = "https://www.ziprecruiter.com/jobs-search?search=social+media+content+creator&location=Houston%2C+TX&lk=SJQj3h4stZuPhfYM0UTJjA";
  const ZR_PAGE = FIX("ziprecruiter-serp-right-pane.html");
  function zrJob({ stopInJudge = false } = {}) {
    const store = { campaignRunning: true, reviewMode: false };
    const rec = { backend: [], clicks: [], skips: 0 };
    const { window } = new JSDOM(ZR_PAGE, { url: ZR_URL, virtualConsole: new VirtualConsole() });
    const BTN = { id: "quick-apply" };
    const box = {
      ...storage(store),
      document: window.document,
      window: { location: { href: ZR_URL } },
      MAX_APPLICATIONS_PER_PLATFORM: 15,
      getPlatformCount: async () => 0,
      keywordCapReached: async () => false,
      goBackToZipRecruiterJobList: async () => {},
      waitForZipRecruiterRightPanel: async () => true,
      log: () => {},
      logBackend: (t) => rec.backend.push(t),
      sleep: async () => {},
      humanDelay: () => 0,
      readZipRecruiterCompany: () => "Acme Dental",
      jobIdFromUrl: () => "SJQj3h4stZuPhfYM0UTJjA",
      zrDedupeKey: (u) => u,
      getAppliedUrls: async () => new Set(),
      getAppliedJobKeys: async () => new Set(),
      getHandedBackKeys: async () => new Set(),
      jobDedupKey: (t, c) => `${t}|${c}`,
      titleMatchesKeywords: () => true,
      titleGateKeywords: async () => ["social media"],
      jobLooksApplied: () => false,
      addAppliedJobKey: async () => {},
      routeExternalToAts: async () => false,
      // The real one polls isCampaignRunning first and answers null on Stop — same contract.
      waitForZipRecruiterApplyButton: async () => ((store.campaignRunning === true) ? BTN : null),
      findZipRecruiterApplyButton: () => BTN,
      sendMsg: async (m) => {
        if (m.type !== "ASSESS_FIT") return {};
        if (stopInJudge) stop(store);
        return { decision: "apply", judged: true, fit_score: 55 };
      },
      recordJobDescription: async () => {},
      humanClick: async (el) => rec.clicks.push(el.id),
      waitForZipRecruiterForm: async () => false,
      dialogSnapshot: () => "",
      phase3_fillForm: async () => {},
      skipToNextJob: async () => { rec.skips += 1; },
      Date, Promise,
    };
    vm.createContext(box);
    vm.runInContext(`var lastPhase = "";\n${PHASE2_ZR}\nthis.__run = phase2_ziprecruiter;`, box);
    return { box, store, rec };
  }
  {
    const c = zrJob();
    await c.box.__run();
    check("control: the captured ZR pane is judged and Quick Apply is clicked", c.rec.clicks.includes("quick-apply"),
      { log: c.rec.backend, clicks: c.rec.clicks });
  }
  {
    const s = zrJob({ stopInJudge: true });
    await s.box.__run();
    check("Stop while the judge answers: Quick Apply is NOT clicked", s.rec.clicks.length === 0, s.rec.clicks);
    check("…no 'Good fit' line, no skip", !s.rec.backend.some((t) => /Good fit/.test(t)) && s.rec.skips === 0,
      { log: s.rec.backend, skips: s.rec.skips });
  }

  // --- 4. The pause before a navigation: Stop inside it ------------------------------------
  function nextJob({ stopInPause = false } = {}) {
    const store = { campaignRunning: true, currentJobIndex: 0,
      pendingJobs: [{ title: "A", jk: "aaaa1111bbbb2222" }, { title: "B", jk: "cccc3333dddd4444" }] };
    const rec = { navTo: null };
    const box = {
      ...storage(store),
      log: () => {},
      sendMsg: async () => ({}),
      goBackToJobList: async () => {},
      detectPlatform: () => "indeed",
      sleep: async () => { if (stopInPause) stop(store); },
      humanDelay: () => 0,
      window: { location: { set href(v) { rec.navTo = v; } } },
      Promise,
    };
    vm.createContext(box);
    vm.runInContext(`${SKIP_NEXT}\nthis.__run = skipToNextJob;`, box);
    return { box, rec };
  }
  {
    const c = nextJob();
    await c.box.__run();
    check("control: skipToNextJob opens the next posting", c.rec.navTo === "https://www.indeed.com/viewjob?jk=cccc3333dddd4444", c.rec.navTo);
    const s = nextJob({ stopInPause: true });
    await s.box.__run();
    check("Stop in its 3-5 s pause: no navigation", s.rec.navTo === null, s.rec.navTo);
  }
  // The same shape everywhere: no navigation may fire straight off a sleep. Each such pause
  // is seconds long (15-30 s before the next results page) and Stop lands in it.
  {
    const bare = [...CS.matchAll(/await sleep\([^;]*\);\s*\n(?:\s*\n)*\s*window\.location\.href = /g)]
      .map((m) => CS.slice(0, m.index).split("\n").length);
    check("no `await sleep(…)` is followed directly by a navigation without a Stop check", bare.length === 0,
      `content.js lines ${bare.join(", ")}`);
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
