// "Let Drop finish it": one handed-back ATS form, refilled in a visible window. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/finish-run.test.js
//
// What must hold, against the REAL background/content code:
//   1. only a Greenhouse/Lever/Ashby form on their own hosts starts; a live run, a finish
//      already going, a spent daily budget, a spent per-platform cap or a spent free limit
//      refuses it, and a slow server never holds the answer past a beat;
//   2. a start writes the run state BEFORE the window navigates to the form (the filler
//      asks for its run at init), and releases the posting's "applied" marks;
//   3. it is not a campaign: the heartbeat reports not-running and ignores the server's
//      should_run; the ATS watchdog never reloads the person's page; no debugger;
//   4. its outcome ends it, never walks on: a send closes the window, anything else
//      leaves the window for the person; a marker whose run is over never blocks;
//   5. at a wall, handBackJob does NOT hand back (no ATS_JOB_FAILED): it leaves the
//      submit belt open for the person's own Submit and tells background FINISH_WALL;
//   6. Start during the fill: the finish page neither submits nor hands back, and
//      background drops its late messages instead of moving the new run's queue;
//   7. Stop + "Let Drop finish it": the stopped campaign's page does not submit, and its
//      messages never close the finish window or end the finish run;
//   8. an unconfirmed send on a finish run is a wall, not a send (window stays);
//   9. the per-platform cap on a finish run is a wall, never a Stop of a campaign.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

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

const caseOf = (type) => sliceFrom(BG, `    case "${type}": {`, "\n    }\n");

const BG_SRC = [
  "const DEFAULT_DAILY_TOTAL = 30;",
  "const DEFAULT_PER_PLATFORM = 20;",
  sliceFrom(BG, "const FINISH_ATS = ", "\n"),
  sliceFrom(BG, "const FINISH_ATS_HOST = ", "\n"),
  sliceFrom(BG, "const FINISH_MAX_MS = ", "\n"),
  sliceFrom(BG, "const FINISH_STATUS_WAIT_MS = ", "\n"),
  sliceFrom(BG, "async function finishRunActive() {", "\n}\n"),
  sliceFrom(BG, "async function healFinishRun() {", "\n}\n"),
  sliceFrom(BG, "async function fromStaleRun(msg, sender) {", "\n}\n"),
  sliceFrom(BG, "async function startFinishRun(h) {", "\n}\n"),
  sliceFrom(BG, "async function endFinishRun({ note = \"\", closeWindow = false } = {}) {", "\n}\n"),
  sliceFrom(BG, "async function releaseAppliedMarks(rows) {", "\n}\n"),
  sliceFrom(BG, "async function advanceAtsQueue({ sent = false } = {}) {", "\n}\n"),
  sliceFrom(BG, "const ATS_WATCHDOG_SILENT_MS", "\n"),
  sliceFrom(BG, "async function atsWalkWatchdog() {", "\n}\n"),
  sliceFrom(BG, "async function sendExtensionPing() {", "\n}\n"),
  sliceFrom(BG, "async function captureActiveAutomationTab() {", "\n}\n"),
  // The message handlers a finish run talks through, wired as handleMessage wires them.
  "globalThis.__msg = async function (msg, sender) {\n  switch (msg.type) {\n" +
    ["FINISH_WALL", "APPLICATION_SAVED", "ATS_JOB_DONE", "ATS_JOB_FAILED"].map(caseOf).join("\n") +
    "\n  }\n  return null;\n};",
].join("\n");

function bgSandbox(store = {}, over = {}) {
  const sb = {
    store, logs: [], calls: [], pings: [], posted: [], patched: [], shots: [],
    windowAlive: over.windowAlive !== false,
    campaignAlive: !!over.campaignAlive,
    status: over.status === undefined ? {} : over.status, // what /campaign/status answers
    chrome: {
      storage: { local: {
        get: async (keys) => {
          const ks = typeof keys === "string" ? [keys] : keys;
          const o = {};
          for (const k of ks) if (k in store) o[k] = store[k];
          return o;
        },
        set: async (obj) => { sb.calls.push(["set", Object.keys(obj)]); Object.assign(store, obj); },
        remove: async (ks) => { for (const k of [].concat(ks)) delete store[k]; },
      } },
      windows: {
        create: async (o) => { sb.calls.push(["windows.create", o.url, o.focused]); return { id: 7, tabs: [{ id: 70 }] }; },
        remove: async (id) => { sb.calls.push(["windows.remove", id]); },
        get: async (id) => { if (!sb.windowAlive) throw new Error("gone"); return { id, state: "normal" }; },
        update: async () => ({}),
      },
      tabs: {
        update: async (id, o) => { sb.calls.push(["tabs.update", id, o.url || null]); return { id }; },
        reload: async (id) => { sb.calls.push(["tabs.reload", id]); },
        query: async () => [{ id: store.campaignTabId, url: "https://job-boards.greenhouse.io/acme/jobs/123" }],
      },
      runtime: { getManifest: () => ({ version: "9.9.9" }), id: "ext" },
    },
    campaignAliveLocally: async () => sb.campaignAlive,
    localDay: () => "2026-10-09",
    addToActivityLog: async (t) => sb.logs.push(t),
    detachDebugger: async () => {},
    updateBadge: () => {},
    clearHumanHandoff: async () => {},
    getAuthToken: async () => "tok",
    apiGet: async (p) => {
      sb.calls.push(["apiGet", p]);
      if (sb.status === "hang") return new Promise(() => {});
      if (sb.status === "fail") throw new Error("API 500");
      return sb.status;
    },
    apiPost: async (url, body) => { sb.posted.push({ url, body }); return {}; },
    apiPatch: async (url, body) => { sb.patched.push({ url, body }); return {}; },
    queueOutbox: async () => {},
    isNetworkError: () => false,
    sendScreenshot: async (tabId) => { sb.shots.push(tabId); },
    isCapturableAutomationUrl: () => true,
    CONFIG: { API_BASE: "https://api", API_V1: "/api/v1" },
    fetch: async (url, o) => { sb.pings.push(JSON.parse(o.body)); return { json: async () => ({ should_run: false }) }; },
    handleMessage: async (m) => { sb.calls.push(["handleMessage", m.type]); },
    navigatePoolNext: async () => { sb.calls.push(["navigatePoolNext"]); },
    buildApprovedAtsQueue: async () => [],
    setTimeout, Date, URL, String, Promise, Array, Set, JSON, Object,
  };
  vm.createContext(sb);
  vm.runInContext(BG_SRC, sb);
  sb.msg = (m, tabId) => sb.__msg(m, tabId == null ? {} : { tab: { id: tabId } });
  return sb;
}

const GH = {
  id: "hb1", url: "https://job-boards.greenhouse.io/acme/jobs/123", platform: "greenhouse",
  job_title: "Marketing Manager", company: "Acme", job_id: "j1",
};
const start = (sb, h = GH) => vm.runInContext(`startFinishRun(${JSON.stringify(h)})`, sb);

// ---- content.js: the REAL phase_ats on a jsdom form, everything around it stubbed ------
const PHASE_SRC = [
  sliceFrom(CS, "  async function currentFinishRun() {", "\n  }\n"),
  sliceFrom(CS, "  let _phaseFinishId = null;", "\n"),
  sliceFrom(CS, "  const runMsg = ", "\n"),
  sliceFrom(CS, "  async function runToken() {", "\n  }\n"),
  sliceFrom(CS, "  async function handBackJob(reason, extra = {}) {", "\n  }\n"),
  sliceFrom(CS, "  async function phase_ats(platform) {", "    await markSubmitRecorded(jobUrl);\n  }\n"),
].join("\n");

// `onFill` runs while the screener is being filled: the moment a person presses Start or
// Stop in the middle of a fill. `confirm` is what the page says after the click.
// `onSubmitReady` runs after the form is filled and its button found: the last moment
// before the submit guard.
function phaseSandbox(store, { onFill = null, onSubmitReady = null, confirm = { verified: true, signal: "text" }, count = 0 } = {}) {
  const url = "https://job-boards.greenhouse.io/acme/jobs/123";
  const { window } = new JSDOM(`<body><h1>Marketing Manager</h1><main><form>
    <input id="first" name="first_name"><button id="submit" type="submit">Submit application</button>
  </form></main></body>`, { url });
  const sent = [], ev = [];
  const sb = {
    window, document: window.document, location: window.location,
    store, sent, ev,
    MAX_APPLICATIONS_PER_PLATFORM: 20,
    storageGet: async (keys) => { const o = {}; for (const k of [].concat(keys)) if (k in store) o[k] = store[k]; return o; },
    storageSet: async (o) => { Object.assign(store, o); },
    storageRemove: async (ks) => { for (const k of [].concat(ks)) delete store[k]; },
    isCampaignRunning: async () => !!store.campaignRunning,
    getPlatformCount: async () => count,
    sendMsg: async (m) => { sent.push(m); return m.type === "ASSESS_FIT" ? { decision: "apply" } : {}; },
    log: () => {}, logBackend: () => {}, logFormDiagnostic: () => {},
    showFinishBanner: (t) => ev.push(["banner", t]),
    finishWall: async (reason) => { ev.push(["wall", reason]); },
    addHandedBackKey: async () => {},
    collectUnfilledRequired: () => [],
    getAppliedUrls: async () => new Set(), getAppliedJobKeys: async () => new Set(),
    jobDedupKey: (t, c) => `${t}|${c}`.toLowerCase(),
    recordJobDescription: async () => {},
    findFieldBySelectorsOrLabel: () => null, typeValue: async () => {},
    sleep: async () => {}, humanDelay: () => 0,
    resolveEmail: async () => "a@b.c",
    findResumeInput: () => null, uploadResume: async () => {},
    fillCoverLetterIfAsked: async () => "",
    fillRadioQuestions: async () => 0,
    fillTextQuestions: async () => { if (onFill) await onFill(store); return 1; },
    fillSelectQuestions: async () => 0, fillComboboxes: async () => 0,
    coverLetterKeyFor: () => "k",
    classifyFormButton: () => { if (onSubmitReady) onSubmitReady(store); return { label: "Submit application", submit: true }; },
    findFormButton: () => window.document.getElementById("submit"),
    waitForFormReady: async () => {},
    snapshotFormAnswers: async () => {},
    addAppliedUrl: async () => {}, addAppliedJobKey: async () => {},
    markSubmitRecorded: async () => {},
    recordLocalApplication: async () => { ev.push(["count+"]); },
    subtractLocalApplication: async () => { ev.push(["count-"]); },
    shouldMisclick: () => false, performMisclick: async () => {},
    humanClick: async () => { ev.push(["click"]); },
    waitForSubmissionConfirmation: async () => confirm,
    greenhouseAsksEmailCode: () => false,
    isDetected: () => ({ signal: "" }),
    fitSourceNote: () => "", awaitReview: async () => "skip",
    detectPlatform: () => "greenhouse",
    Date, String, Promise, Set, Array, Object, JSON,
  };
  vm.createContext(sb);
  vm.runInContext(`${PHASE_SRC}\nglobalThis.run = () => phase_ats("greenhouse");`, sb);
  return sb;
}
const types = (sent) => sent.map((m) => m.type);
const clicked = (sb) => sb.ev.some((e) => e[0] === "click");
const walls = (sb) => sb.ev.filter((e) => e[0] === "wall").map((e) => e[1]);

(async () => {
  // ---- 0. without finishRun an ordinary campaign is exactly as before ---------------
  {
    const store = {
      campaignRunning: true, campaignTabId: 70, campaignWindowId: 7, atsPlatform: "pool",
      atsQueue: [{ id: "a", applyUrl: "https://job-boards.greenhouse.io/a/jobs/1" }, { id: "b", applyUrl: "https://jobs.lever.co/b/2" }],
      atsNavAt: Date.now() - 60 * 60 * 1000, atsNavTries: 0,
    };
    const sb = bgSandbox(store);
    await vm.runInContext("sendExtensionPing()", sb);
    check("a campaign still reports campaign_running:true", sb.pings[0] && sb.pings[0].campaign_running === true, sb.pings);
    check("a campaign still honours the server's should_run:false", store.campaignRunning === false, store);
    store.campaignRunning = true;
    await vm.runInContext("atsWalkWatchdog()", sb);
    check("the ATS watchdog still reloads a silent campaign page", sb.calls.some((c) => c[0] === "tabs.reload"), sb.calls);
    await vm.runInContext("advanceAtsQueue({ sent: true })", sb);
    check("a campaign still walks to the next job", sb.calls.some((c) => c[0] === "navigatePoolNext") && store.atsQueue.length === 1, sb.calls);
    check("a campaign's window is never closed by the walk", !sb.calls.some((c) => c[0] === "windows.remove"), sb.calls);
    await vm.runInContext("captureActiveAutomationTab()", sb);
    check("a campaign still streams its live view", sb.shots.length === 1, sb.shots);
    // Messages from the campaign's own tab (and from tabs without a sender) still move it.
    const before = store.atsQueue.length;
    const r = await sb.msg({ type: "ATS_JOB_DONE" }, 70);
    check("a campaign's ATS_JOB_DONE still advances", r.advanced === true && store.atsQueue.length === before - 1, r);
    store.atsQueue = [{ id: "c", applyUrl: "https://jobs.lever.co/c/3" }, { id: "d", applyUrl: "https://jobs.lever.co/d/4" }];
    await sb.msg({ type: "APPLICATION_SAVED", data: { job_title: "T", company: "C", platform: "greenhouse" } }, 99);
    check("a campaign's save from another tab still advances (no finish run in play)", store.atsQueue.length === 1, store.atsQueue);
  }
  {
    const sent = [];
    const store = { campaignRunning: true, campaignStartedAt: "T1", atsPlatform: "pool" };
    const sb = phaseSandbox(store, { confirm: { verified: false, signal: "timeout" } });
    await sb.run();
    check("a campaign's unconfirmed send is still recorded as applied_unconfirmed (no finishId)",
      sb.sent.some((m) => m.type === "APPLICATION_SAVED" && m.data.status === "applied_unconfirmed" && !m.finishId), types(sb.sent));
    const capped = phaseSandbox({ campaignRunning: true, campaignStartedAt: "T1", atsPlatform: "pool" }, { count: 20 });
    await capped.run();
    check("a campaign at its per-platform cap still stops the campaign", types(capped.sent).includes("STOP_CAMPAIGN"), types(capped.sent));
    void sent;
  }

  // ---- 1. refusals ------------------------------------------------------------------
  {
    const sb = bgSandbox();
    const r1 = await start(sb, { ...GH, platform: "indeed", url: "https://www.indeed.com/viewjob?jk=1" });
    check("an Indeed hand-back is refused", r1.started === false && r1.error === "unsupported", r1);
    const r2 = await start(sb, { ...GH, url: "https://careers.acme.com/jobs?gh_jid=123" });
    check("an employer-hosted Greenhouse page is refused (content.js is not injected there)", r2.error === "unsupported", r2);
    check("a refusal opens no window", !sb.calls.some((c) => c[0] === "windows.create"), sb.calls);
  }
  {
    const sb = bgSandbox({}, { campaignAlive: true });
    const r = await start(sb);
    check("a live campaign refuses it (busy)", r.started === false && r.error === "busy", r);
  }
  {
    const sb = bgSandbox({ todayCount: 30, todayDate: "2026-10-09", campaignCaps: { dailyTotal: 30 } });
    const r = await start(sb);
    check("a spent daily budget refuses it", r.error === "daily_limit", r);
  }
  {
    const sb = bgSandbox({ todayCount: 4, todayDate: "2026-10-09", platformCounts: { greenhouse: 20 }, campaignCaps: { dailyTotal: 30, perPlatform: 20 } });
    const r = await start(sb);
    check("a spent per-platform cap refuses it (daily_limit), before any window", r.error === "daily_limit" &&
      !sb.calls.some((c) => c[0] === "windows.create"), r);
  }
  {
    const sb = bgSandbox({ todayCount: 4, todayDate: "2026-10-09", platformCounts: { greenhouse: 15 }, campaignCaps: { perPlatform: 20 } },
      { status: { limit_per_platform: 15, daily_limit: 30 } });
    const r = await start(sb);
    check("the server's per-platform cap wins over a stale local one", r.error === "daily_limit", r);
  }
  {
    const sb = bgSandbox({ todayCount: 2, todayDate: "2026-10-09", platformCounts: { greenhouse: 2 } },
      { status: { limit_per_platform: 20, daily_limit: 30, today_applications: 9, platform_counts: { greenhouse: 20 } } });
    const r = await start(sb);
    check("the server's count of today's sends wins over a local count that ran low", r.error === "daily_limit", r);
  }
  {
    const sb = bgSandbox({}, { status: { free_limit: 40, free_used: 40 } });
    const r = await start(sb);
    check("a spent free limit refuses it (free_limit), before any window", r.error === "free_limit" &&
      !sb.calls.some((c) => c[0] === "windows.create"), r);
  }
  {
    const sb = bgSandbox({}, { status: "hang" });
    const t0 = Date.now();
    const r = await start(sb);
    const ms = Date.now() - t0;
    check("a server that doesn't answer never holds the reply past ~1.5 s (fail-open on stored caps)", r.started === true && ms < 2500, { r, ms });
  }
  {
    const sb = bgSandbox({}, { status: { limit_per_platform: 12, daily_limit: 25 } });
    await start(sb);
    check("the start hands the filler the caps it was checked against",
      sb.store.campaignCaps && sb.store.campaignCaps.perPlatform === 12 && sb.store.campaignCaps.dailyTotal === 25, sb.store.campaignCaps);
  }

  // ---- 2. start ---------------------------------------------------------------------
  {
    const store = {
      appliedUrls: [GH.url + "?gh_src=x", "https://other.io/a"],
      appliedJobKeys: ["marketing manager|acme", "a|b"],
    };
    const sb = bgSandbox(store);
    const r = await start(sb);
    check("a Greenhouse hand-back starts", r.started === true, r);
    const create = sb.calls.find((c) => c[0] === "windows.create");
    check("the window is focused and opens blank", create && create[1] === "about:blank" && create[2] === true, create);
    const runSet = sb.calls.findIndex((c) => c[0] === "set" && c[1].includes("campaignRunning"));
    const nav = sb.calls.findIndex((c) => c[0] === "tabs.update" && c[2] === GH.url);
    check("run state is written BEFORE the tab navigates to the form", runSet >= 0 && nav > runSet, sb.calls);
    check("finishRun + the one-job pool queue are stored",
      store.finishRun && store.finishRun.handbackId === "hb1" && store.atsPlatform === "pool" &&
      store.atsQueue.length === 1 && store.atsQueue[0].applyUrl === GH.url && store.campaignTabId === 70, store);
    check("its applied marks are released (URL with query, title|company); others kept",
      JSON.stringify(store.appliedUrls) === JSON.stringify(["https://other.io/a"]) &&
      JSON.stringify(store.appliedJobKeys) === JSON.stringify(["a|b"]), store);

    // ---- 3. not a campaign --------------------------------------------------------
    await vm.runInContext("sendExtensionPing()", sb);
    check("the heartbeat reports campaign_running:false", sb.pings[0] && sb.pings[0].campaign_running === false, sb.pings);
    check("should_run:false from the server does not kill it", store.campaignRunning === true && !!store.finishRun, store);
    store.atsNavAt = Date.now() - 60 * 60 * 1000;
    await vm.runInContext("atsWalkWatchdog()", sb);
    check("the ATS watchdog never reloads the person's page", !sb.calls.some((c) => c[0] === "tabs.reload"), sb.calls);
    const live = await vm.runInContext("captureActiveAutomationTab()", sb);
    check("no live-view capture (no debugger on the person's window)", live === false && sb.shots.length === 0, sb.shots);

    const busy = await start(sb);
    check("a second finish while one runs is refused", busy.error === "busy", busy);

    // ---- 4. a send ends it and closes the window ----------------------------------
    await vm.runInContext("advanceAtsQueue({ sent: true })", sb);
    check("a send ends it: flag down, finishRun gone, queue gone",
      store.campaignRunning === false && !store.finishRun && !store.atsQueue && !store.atsPlatform, store);
    check("a send closes its window", sb.calls.some((c) => c[0] === "windows.remove" && c[1] === 7), sb.calls);
    check("it never walks on (no navigate, no board hand-off)",
      !sb.calls.some((c) => c[0] === "navigatePoolNext" || c[0] === "handleMessage"), sb.calls);
  }
  {
    const sb = bgSandbox();
    await start(sb);
    await vm.runInContext("advanceAtsQueue()", sb);
    check("a non-send outcome ends it but leaves the window for the person",
      !sb.store.finishRun && !sb.calls.some((c) => c[0] === "windows.remove"), sb.calls);
  }
  {
    const sb = bgSandbox({}, { windowAlive: true });
    await start(sb);
    sb.windowAlive = false;
    await vm.runInContext("sendExtensionPing()", sb);
    check("a closed window ends it on the next heartbeat", !sb.store.finishRun && sb.store.campaignRunning === false, sb.store);
  }
  {
    // A cap/free-limit refusal on save drops only the flag; the marker must not outlive it.
    const sb = bgSandbox();
    await start(sb);
    sb.store.campaignRunning = false;
    await vm.runInContext("sendExtensionPing()", sb);
    check("a marker whose flag went down is cleared by the heartbeat", !sb.store.finishRun, sb.store);
    const again = await start(sb);
    check("…so the next finish is not refused as busy", again.started === true, again);
  }
  {
    const sb = bgSandbox();
    await start(sb);
    sb.store.campaignRunning = false; // e.g. a 401 streak or an update reset, no heartbeat yet
    const again = await start(sb);
    check("a stale marker never answers busy, even before a heartbeat", again.started === true, again);
  }
  {
    const sb = bgSandbox();
    await start(sb);
    sb.store.finishRun.startedAt = Date.now() - 21 * 60 * 1000;
    await vm.runInContext("sendExtensionPing()", sb);
    check("a fill stuck past FINISH_MAX_MS is ended, its window left", !sb.store.finishRun &&
      !sb.calls.some((c) => c[0] === "windows.remove"), sb.store);
  }
  {
    // A free-limit stop on the finish run's own save drops the flag first; the send must
    // still end the run as a send, never fall into the ordinary walk.
    const sb = bgSandbox();
    await start(sb);
    sb.store.campaignRunning = false;
    await vm.runInContext("advanceAtsQueue({ sent: true })", sb);
    check("a send after the flag went down still ends it as a send (window closed, no walk)",
      !sb.store.finishRun && sb.calls.some((c) => c[0] === "windows.remove") &&
      !sb.calls.some((c) => c[0] === "navigatePoolNext" || c[0] === "handleMessage"), sb.calls);
  }

  // ---- 5. content.js: a wall on a finish run is the person's step -------------------
  {
    const hb = sliceFrom(CS, "  async function handBackJob(reason, extra = {}) {", "\n  }\n");
    const mk = (finishId, current) => {
      const sent = [];
      const ctx = {
        window: { location: { href: GH.url + "?gh_src=x" } },
        document: { body: { textContent: "form" } },
        sent,
        _phaseFinishId: finishId,
        currentFinishRun: async () => current,
        finishWall: async (reason, extra) => { sent.push({ type: "WALL", reason, extra }); },
        addHandedBackKey: async () => { sent.push({ type: "HANDED_BACK_KEY" }); },
        sendMsg: async (m) => { sent.push(m); return {}; },
        log: () => {},
        collectUnfilledRequired: () => [],
        detectPlatform: () => "greenhouse",
      };
      vm.createContext(ctx);
      vm.runInContext(`${hb}\nglobalThis.hb = handBackJob;`, ctx);
      return ctx;
    };
    const a = mk("1", { id: "1" });
    await a.hb("Greenhouse asked for the verification code", { title: "T", company: "C", platform: "greenhouse" });
    check("on a finish run, handBackJob goes to the wall, not ATS_JOB_FAILED", a.sent.length === 1 && a.sent[0].type === "WALL", a.sent);
    const b = mk("1", null);
    await b.hb("submit blocked", { title: "T", company: "C", platform: "greenhouse" });
    check("bug 1: once its finish run is over (Start took over), the page hands nothing back", b.sent.length === 0, b.sent);
    const c = mk(null, { id: "F" });
    await c.hb("resume didn't attach", { title: "Old", company: "Job", platform: "greenhouse" });
    check("bug 2: a campaign page during someone else's finish run never shows the wall there",
      !c.sent.some((m) => m.type === "WALL") && c.sent.some((m) => m.type === "ATS_JOB_FAILED"), c.sent);

    const fw = sliceFrom(CS, "  async function finishWall(reason, extra = {}) {", "\n  }\n");
    const runFw = async (confirm) => {
      const sent2 = [], ev = [];
      const store2 = {};
      const ctx2 = {
        window: { location: { href: GH.url + "?gh_src=x" } },
        document: { body: { textContent: "form" } },
        FINISH_PENDING_TTL_MS: 30 * 60 * 1000,
        _phaseFinishId: "F1", _finishWallShown: false,
        runMsg: (m) => ({ ...m, finishId: "F1" }),
        logBackend: () => {},
        jobDedupKey: (t, c) => `${t}|${c}`.toLowerCase(),
        detectPlatform: () => "greenhouse",
        storageSet: async (o) => Object.assign(store2, o),
        storageRemove: async (k) => { delete store2[k]; },
        showFinishBanner: (t) => ev.push(["banner", t]),
        finishWallLine: () => "",
        markFinishTargets: () => {},
        sendMsg: async (m) => { sent2.push(m); return {}; },
        waitForSubmissionConfirmation: async () => confirm,
        submitAlreadyRecorded: async () => false,
        markSubmitRecorded: async (u, key) => { ev.push(["recorded", key]); delete store2[key || "pendingAtsSubmit"]; },
        recordLocalApplication: async (p) => { ev.push(["count+", p]); },
        Date, String,
      };
      vm.createContext(ctx2);
      vm.runInContext(`${fw}\nglobalThis.fw = finishWall;`, ctx2);
      await ctx2.fw("submit blocked — 2 required fields still empty", { title: "T", company: "C", platform: "greenhouse" });
      return { sent2, ev, store2, ctx2 };
    };
    const w1 = await runFw({ verified: false, signal: "timeout" });
    check("the wall leaves the person's submit belt open under its own key, keyed to the posting, long TTL, counted on send",
      w1.store2.finishPendingSubmit && w1.store2.finishPendingSubmit.url === GH.url &&
      w1.store2.finishPendingSubmit.ttl === 30 * 60 * 1000 && w1.store2.finishPendingSubmit.countLocal === true &&
      !w1.store2.pendingAtsSubmit, w1.store2);
    check("the wall tells background FINISH_WALL (with its run id) and records nothing unconfirmed",
      w1.sent2.length === 1 && w1.sent2[0].type === "FINISH_WALL" && w1.sent2[0].finishId === "F1", w1.sent2);
    check("the wall's banner is marked as the person's step (it outlives the run)", w1.ctx2._finishWallShown === true, w1.ctx2._finishWallShown);
    const w2 = await runFw({ verified: true, signal: "text" });
    check("risk 6: the person's confirmed send is counted locally and recorded once",
      w2.ev.some((e) => e[0] === "count+" && e[1] === "greenhouse") &&
      w2.sent2.filter((m) => m.type === "APPLICATION_SAVED").length === 1 && !w2.store2.finishPendingSubmit &&
      w2.ev.some((e) => e[0] === "recorded" && e[1] === "finishPendingSubmit"), { ev: w2.ev, sent: w2.sent2 });
  }

  // ---- 6. bug 1: Start during the fill ----------------------------------------------
  {
    // content: the finish page loses its run mid-fill; it must not submit or hand back.
    const store = { campaignRunning: true, campaignStartedAt: "T0", finishRun: { id: "F1", title: "Marketing Manager", company: "Acme" }, atsPlatform: "pool" };
    const sb = phaseSandbox(store, {
      onFill: async (s) => { s.finishRun = null; s.campaignStartedAt = "T2"; s.campaignRunning = true; }, // startCampaign
    });
    await sb.run();
    check("bug 1: the finish page never clicks Submit on the new campaign's flag", !clicked(sb), sb.ev);
    check("bug 1: …and sends nothing that moves a queue",
      !sb.sent.some((m) => ["ATS_JOB_FAILED", "ATS_JOB_DONE", "APPLICATION_SAVED"].includes(m.type)), types(sb.sent));
    const late = phaseSandbox({ campaignRunning: true, campaignStartedAt: "T0", finishRun: { id: "F1" }, atsPlatform: "pool" }, {
      onSubmitReady: (s) => { s.finishRun = null; s.campaignStartedAt = "T2"; s.campaignRunning = true; },
    });
    await late.run();
    check("bug 1: Start in the last second before Submit is caught by the submit guard itself", !clicked(late), late.ev);

    // background: whatever the old finish page still sends is dropped.
    const bg = bgSandbox();
    await start(bg);
    await vm.runInContext('endFinishRun({ note: "Start" })', bg); // startCampaign's first line
    Object.assign(bg.store, {
      campaignRunning: true, campaignTabId: 80, campaignWindowId: 8, atsPlatform: "pool",
      atsQueue: [{ id: "n1", applyUrl: "https://jobs.lever.co/n/1" }, { id: "n2", applyUrl: "https://jobs.lever.co/n/2" }],
    });
    const fid = "stale-finish";
    const f1 = await bg.msg({ type: "ATS_JOB_FAILED", finishId: fid, data: { title: "T", reason: "x" } }, 70);
    const f2 = await bg.msg({ type: "ATS_JOB_DONE", finishId: fid }, 70);
    await bg.msg({ type: "APPLICATION_SAVED", finishId: fid, data: { job_title: "Marketing Manager", company: "Acme", platform: "greenhouse" } }, 70);
    check("bug 1: a late hand-back from the replaced finish run is ignored", f1.stale === true && !bg.posted.some((p) => p.url === "/handbacks"), f1);
    check("bug 1: …the new campaign's first job is never PATCHed skipped", bg.patched.length === 0, bg.patched);
    check("bug 1: …and its queue never moves (late DONE, late save)", f2.stale === true && bg.store.atsQueue.length === 2 &&
      !bg.calls.some((c) => c[0] === "navigatePoolNext"), bg.store.atsQueue);
    check("bug 1: the late send is still saved (the employer has it)", bg.posted.some((p) => p.url === "/applications/save"), bg.posted);
  }

  // ---- 7. bug 2: Stop a campaign mid-fill, then "Let Drop finish it" ----------------
  {
    const store = { campaignRunning: true, campaignStartedAt: "T1", atsPlatform: "pool" };
    const sb = phaseSandbox(store, {
      onFill: async (s) => { s.finishRun = { id: "F9" }; s.campaignRunning = true; }, // Stop, then startFinishRun
    });
    await sb.run();
    check("bug 2: the stopped campaign's page never submits the job the person stopped", !clicked(sb), sb.ev);
    const late = phaseSandbox({ campaignRunning: true, campaignStartedAt: "T1", atsPlatform: "pool" }, {
      onSubmitReady: (s) => { s.finishRun = { id: "F9" }; s.campaignRunning = true; },
    });
    await late.run();
    check("bug 2: …even when the finish starts in the last second before Submit", !clicked(late), late.ev);

    const bg = bgSandbox();
    await start(bg); // finish window = tab 70
    const id = bg.store.finishRun.id;
    const fromOld = 50;
    await bg.msg({ type: "APPLICATION_SAVED", data: { job_title: "Old Job", company: "Old", platform: "lever" } }, fromOld);
    check("bug 2: the old tab's save does not close the finish window or end its run",
      bg.store.finishRun && bg.store.finishRun.id === id && !bg.calls.some((c) => c[0] === "windows.remove") &&
      !bg.logs.some((l) => /Drop finished it/.test(l)), { finishRun: bg.store.finishRun, logs: bg.logs });
    const d = await bg.msg({ type: "ATS_JOB_DONE" }, fromOld);
    const f = await bg.msg({ type: "ATS_JOB_FAILED", data: { title: "Old Job", reason: "x" } }, fromOld);
    const w = await bg.msg({ type: "FINISH_WALL" }, fromOld);
    check("bug 2: the old tab's DONE / FAILED / WALL leave the finish run alone",
      d.stale && f.stale && w.stale && bg.store.finishRun && bg.store.finishRun.id === id && bg.patched.length === 0, { d, f, w });
    const own = await bg.msg({ type: "FINISH_WALL", finishId: id }, 70);
    check("…while the finish window's own wall still ends it", own.ok === true && !bg.store.finishRun, own);
  }

  // ---- 8. bug 4: an unconfirmed send on a finish run is a wall ----------------------
  {
    const store = { campaignRunning: true, campaignStartedAt: "T0", finishRun: { id: "F1", title: "Marketing Manager", company: "Acme" }, atsPlatform: "pool" };
    const sb = phaseSandbox(store, { confirm: { verified: false, signal: "timeout" } });
    await sb.run();
    check("bug 4: an unconfirmed send goes to the wall", walls(sb).some((r) => /not confirmed/.test(r)), sb.ev);
    check("bug 4: …nothing is recorded as sent (no save, no receipt) and the count is given back",
      !types(sb.sent).includes("APPLICATION_SAVED") && !types(sb.sent).includes("RECEIPT_CAPTURE") &&
      sb.ev.some((e) => e[0] === "count-") && !store.pendingAtsSubmit, { sent: types(sb.sent), ev: sb.ev });

    const ok = phaseSandbox({ campaignRunning: true, campaignStartedAt: "T0", finishRun: { id: "F1" }, atsPlatform: "pool" });
    await ok.run();
    const save = ok.sent.find((m) => m.type === "APPLICATION_SAVED");
    const rc = ok.sent.find((m) => m.type === "RECEIPT_CAPTURE");
    check("a confirmed send on a finish run is saved and tagged with its run",
      save && save.finishId === "F1" && save.data.status === "applied" && rc && rc.finishId === "F1", ok.sent);
  }

  // ---- 9. bug 3: the per-platform cap on a finish run is a wall, never a Stop ---------
  {
    const store = { campaignRunning: true, campaignStartedAt: "T0", finishRun: { id: "F1", title: "Marketing Manager", company: "Acme" }, atsPlatform: "pool" };
    const sb = phaseSandbox(store, { count: 20 });
    await sb.run();
    check("bug 3: at the cap a finish run sends no STOP_CAMPAIGN", !types(sb.sent).includes("STOP_CAMPAIGN"), types(sb.sent));
    check("bug 3: …it hands the untouched form to the person with the limit named",
      walls(sb).some((r) => /daily limit/.test(r)) && !clicked(sb), sb.ev);
    const line = vm.runInNewContext(`${sliceFrom(CS, "  function finishWallLine(reason) {", "\n  }\n")}\nfinishWallLine("Greenhouse daily limit reached")`, { String });
    check("bug 3: the wall says the limit is reached, in fixed words", /limit for this site is reached/i.test(line), line);
  }

  // ---- 10. the belt records the person's Submit at a wall, even after a campaign started --
  {
    const FORM = "https://job-boards.greenhouse.io/acme/jobs/123";
    const END = "  const SUCCESS_TEXTS = [";
    const BELT = sliceFrom(CS, "  const POSTAPPLY_URL_HINTS = [", END).slice(0, -END.length);
    const beltCtx = (store) => {
      const { window } = new JSDOM("<body></body>", { url: `${FORM}/confirmation` });
      const sent = [], ev = [];
      const ctx = {
        window, document: window.document, location: window.location, URL, Date,
        sent, ev,
        storageGet: async (k) => { const o = {}; for (const x of [].concat(k)) if (x in store) o[x] = store[x]; return o; },
        storageSet: async (o) => { Object.assign(store, o); },
        storageRemove: async (k) => { for (const x of [].concat(k)) delete store[x]; },
        sendMsg: async (m) => { sent.push(m); return {}; },
        logBackend: () => {}, log: () => {},
        detectPlatform: () => "greenhouse",
        recordLocalApplication: async (p) => { ev.push(["count+", p]); },
      };
      vm.createContext(ctx);
      vm.runInContext(`${BELT}\nglobalThis.belt = _recordPendingSubmitOnce;`, ctx);
      return ctx;
    };
    const person = { url: FORM, title: "Marketing Manager", company: "Acme", platform: "greenhouse", ts: Date.now(), ttl: 30 * 60 * 1000, countLocal: true };
    // A campaign started while the person finished: its own pending submit, another posting.
    const store = { finishPendingSubmit: person, pendingAtsSubmit: { url: "https://jobs.lever.co/other/9", title: "X", ts: Date.now() } };
    const ctx = beltCtx(store);
    const ok = await ctx.belt();
    check("risk 13: the person's Submit is recorded although a campaign wrote its own pending submit since",
      ok === true && ctx.sent.length === 1 && ctx.sent[0].type === "APPLICATION_SAVED" &&
      ctx.sent[0].data.job_title === "Marketing Manager" && ctx.sent[0].advance === false, ctx.sent);
    check("risk 6: …counted locally, its key gone, the campaign's pending submit kept",
      ctx.ev.some((e) => e[0] === "count+" && e[1] === "greenhouse") && !store.finishPendingSubmit && !!store.pendingAtsSubmit,
      { ev: ctx.ev, store });
    const again = await beltCtx(store).belt();
    check("…and a second wake on the page records nothing", again === false, again);
    const robot = { pendingAtsSubmit: { url: FORM, title: "Marketing Manager", company: "Acme", platform: "greenhouse", ts: Date.now() } };
    const rctx = beltCtx(robot);
    await rctx.belt();
    check("the robot's own submit is still recorded and NOT counted twice (phase_ats counted it)",
      rctx.sent.length === 1 && !rctx.ev.some((e) => e[0] === "count+"), { sent: rctx.sent, ev: rctx.ev });
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
