// "Let Drop finish it": one handed-back ATS form, refilled in a visible window. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/finish-run.test.js
//
// What must hold, against the REAL background/content code:
//   1. only a Greenhouse/Lever/Ashby form on their own hosts starts; a live run, a finish
//      already going, or a spent daily budget refuses it;
//   2. a start writes the run state BEFORE the window navigates to the form (the filler
//      asks for its run at init), and releases the posting's "applied" marks;
//   3. it is not a campaign: the heartbeat reports not-running and ignores the server's
//      should_run; the ATS watchdog never reloads the person's page;
//   4. its outcome ends it, never walks on: a send closes the window, anything else
//      leaves the window for the person;
//   5. at a wall, handBackJob does NOT hand back (no ATS_JOB_FAILED): it leaves the
//      submit belt open for the person's own Submit and tells background FINISH_WALL.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

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

const BG_SRC = [
  "const DEFAULT_DAILY_TOTAL = 30;",
  sliceFrom(BG, "const FINISH_ATS = ", "\n"),
  sliceFrom(BG, "const FINISH_ATS_HOST = ", "\n"),
  sliceFrom(BG, "async function finishRunActive() {", "\n}\n"),
  sliceFrom(BG, "async function startFinishRun(h) {", "\n}\n"),
  sliceFrom(BG, "async function endFinishRun({ note = \"\", closeWindow = false } = {}) {", "\n}\n"),
  sliceFrom(BG, "async function releaseAppliedMarks(rows) {", "\n}\n"),
  sliceFrom(BG, "async function advanceAtsQueue({ sent = false } = {}) {", "\n}\n"),
  sliceFrom(BG, "const ATS_WATCHDOG_SILENT_MS", "\n"),
  sliceFrom(BG, "async function atsWalkWatchdog() {", "\n}\n"),
  sliceFrom(BG, "async function sendExtensionPing() {", "\n}\n"),
].join("\n");

function bgSandbox(store = {}, over = {}) {
  const sb = {
    store, logs: [], calls: [], pings: [],
    windowAlive: over.windowAlive !== false,
    campaignAlive: !!over.campaignAlive,
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
    CONFIG: { API_BASE: "https://api", API_V1: "/api/v1" },
    fetch: async (url, o) => { sb.pings.push(JSON.parse(o.body)); return { json: async () => ({ should_run: false }) }; },
    handleMessage: async (m) => { sb.calls.push(["handleMessage", m.type]); },
    navigatePoolNext: async () => { sb.calls.push(["navigatePoolNext"]); },
    buildApprovedAtsQueue: async () => [],
    Date, URL, String, Promise, Array, Set, JSON, Object,
  };
  vm.createContext(sb);
  vm.runInContext(BG_SRC, sb);
  return sb;
}

const GH = {
  id: "hb1", url: "https://job-boards.greenhouse.io/acme/jobs/123", platform: "greenhouse",
  job_title: "Marketing Manager", company: "Acme", job_id: "j1",
};

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
  }

  // ---- 1. refusals ------------------------------------------------------------------
  {
    const sb = bgSandbox();
    const r1 = await vm.runInContext(`startFinishRun(${JSON.stringify({ ...GH, platform: "indeed", url: "https://www.indeed.com/viewjob?jk=1" })})`, sb);
    check("an Indeed hand-back is refused", r1.started === false && r1.error === "unsupported", r1);
    const r2 = await vm.runInContext(`startFinishRun(${JSON.stringify({ ...GH, url: "https://careers.acme.com/jobs?gh_jid=123" })})`, sb);
    check("an employer-hosted Greenhouse page is refused (content.js is not injected there)", r2.error === "unsupported", r2);
    check("a refusal opens no window", !sb.calls.some((c) => c[0] === "windows.create"), sb.calls);
  }
  {
    const sb = bgSandbox({}, { campaignAlive: true });
    const r = await vm.runInContext(`startFinishRun(${JSON.stringify(GH)})`, sb);
    check("a live campaign refuses it (busy)", r.started === false && r.error === "busy", r);
  }
  {
    const sb = bgSandbox({ todayCount: 30, todayDate: "2026-10-09", campaignCaps: { dailyTotal: 30 } });
    const r = await vm.runInContext(`startFinishRun(${JSON.stringify(GH)})`, sb);
    check("a spent daily budget refuses it", r.error === "daily_limit", r);
  }

  // ---- 2. start ---------------------------------------------------------------------
  {
    const store = {
      appliedUrls: [GH.url + "?gh_src=x", "https://other.io/a"],
      appliedJobKeys: ["marketing manager|acme", "a|b"],
    };
    const sb = bgSandbox(store);
    const r = await vm.runInContext(`startFinishRun(${JSON.stringify(GH)})`, sb);
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

    const busy = await vm.runInContext(`startFinishRun(${JSON.stringify(GH)})`, sb);
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
    await vm.runInContext(`startFinishRun(${JSON.stringify(GH)})`, sb);
    await vm.runInContext("advanceAtsQueue()", sb);
    check("a non-send outcome ends it but leaves the window for the person",
      !sb.store.finishRun && !sb.calls.some((c) => c[0] === "windows.remove"), sb.calls);
  }
  {
    const sb = bgSandbox({}, { windowAlive: true });
    await vm.runInContext(`startFinishRun(${JSON.stringify(GH)})`, sb);
    sb.windowAlive = false;
    await vm.runInContext("sendExtensionPing()", sb);
    check("a closed window ends it on the next heartbeat", !sb.store.finishRun && sb.store.campaignRunning === false, sb.store);
  }

  // ---- 5. content.js: a wall on a finish run is the person's step -------------------
  {
    const hb = sliceFrom(CS, "  async function handBackJob(reason, extra = {}) {", "\n  }\n");
    const sent = [];
    const store = {};
    const ctx = {
      window: { location: { href: GH.url + "?gh_src=x" } },
      document: { body: { textContent: "form" } },
      sent, store,
      currentFinishRun: async () => ({ id: "1" }),
      finishWall: async (reason, extra) => { sent.push({ type: "WALL", reason, extra }); },
      addHandedBackKey: async () => { sent.push({ type: "HANDED_BACK_KEY" }); },
      sendMsg: async (m) => { sent.push(m); return {}; },
      collectUnfilledRequired: () => [],
      detectPlatform: () => "greenhouse",
    };
    vm.createContext(ctx);
    vm.runInContext(`${hb}\nglobalThis.hb = handBackJob;`, ctx);
    await ctx.hb("Greenhouse asked for the verification code", { title: "T", company: "C", platform: "greenhouse" });
    check("on a finish run, handBackJob goes to the wall, not ATS_JOB_FAILED",
      sent.length === 1 && sent[0].type === "WALL", sent);

    const fw = sliceFrom(CS, "  async function finishWall(reason, extra = {}) {", "\n  }\n");
    const sent2 = [];
    const store2 = {};
    const ctx2 = {
      window: { location: { href: GH.url + "?gh_src=x" } },
      document: { body: { textContent: "form" } },
      FINISH_PENDING_TTL_MS: 30 * 60 * 1000,
      logBackend: () => {},
      jobDedupKey: (t, c) => `${t}|${c}`.toLowerCase(),
      detectPlatform: () => "greenhouse",
      storageSet: async (o) => Object.assign(store2, o),
      showFinishBanner: () => {},
      finishWallLine: () => "",
      markFinishTargets: () => {},
      sendMsg: async (m) => { sent2.push(m); return {}; },
      waitForSubmissionConfirmation: async () => ({ verified: false, signal: "timeout" }),
      submitAlreadyRecorded: async () => false,
      markSubmitRecorded: async () => {},
      Date, String,
    };
    vm.createContext(ctx2);
    vm.runInContext(`${fw}\nglobalThis.fw = finishWall;`, ctx2);
    await ctx2.fw("submit blocked — 2 required fields still empty", { title: "T", company: "C", platform: "greenhouse" });
    check("the wall leaves the submit belt open for the person, keyed to the posting (no query), with a long TTL",
      store2.pendingAtsSubmit && store2.pendingAtsSubmit.url === GH.url && store2.pendingAtsSubmit.ttl === 30 * 60 * 1000, store2);
    check("the wall tells background FINISH_WALL and records nothing unconfirmed",
      sent2.length === 1 && sent2[0].type === "FINISH_WALL", sent2);
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
