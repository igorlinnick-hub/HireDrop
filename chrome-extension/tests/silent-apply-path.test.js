// Silent failures on the submit → record → queue path (docs/reviews/2026-10-09-silent-apply-path.md,
// findings 1, 2, 4). Run:
//
//   node <repo>/jobflow/chrome-extension/tests/silent-apply-path.test.js
//
// What must hold, against the REAL background code:
//   1. the "skipped" status PATCH is never fire-and-forget: a network/5xx failure goes to
//      the outbox (and the outbox replays it as a PATCH), a refusal (404 = no row changed)
//      leaves a durable line with the job id; the walk advances either way;
//   2. a throw inside advanceAtsQueue is logged, and the job now at the head is put back
//      under atsWalkWatchdog instead of freezing the walk with nothing to time;
//   4. the hand-back row survives a network blip through the outbox; a refusal is said
//      out loud.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");

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

const SRC = [
  sliceFrom(BG, "function isNetworkError(err) {", "// end outbox\n"),
  sliceFrom(BG, "async function reportJobStatus(jobId, status) {", "\n}\n"),
  sliceFrom(BG, "async function advanceAtsQueue({ sent = false } = {}) {", "\n}\n"),
  sliceFrom(BG, "const ATS_WATCHDOG_SILENT_MS", "\n"),
  sliceFrom(BG, "async function atsWalkWatchdog() {", "\n}\n"),
  "globalThis.__msg = async function (msg, sender) {\n  switch (msg.type) {\n" +
    ["ATS_JOB_DONE", "ATS_JOB_FAILED"].map(caseOf).join("\n") + "\n  }\n  return null;\n};",
].join("\n");

// net: "ok" | "offline" | "5xx" | "404" | "422", per path prefix
function sandbox(store, net = {}) {
  const sb = {
    store, logs: [], meta: [], patched: [], posted: [], calls: [],
    navigator: { onLine: true },
    chrome: {
      storage: { local: {
        get: async (keys) => { const o = {}; for (const k of [].concat(keys)) if (k in store) o[k] = store[k]; return o; },
        set: async (obj) => {
          if (sb.failSet || (sb.failSetKey && sb.failSetKey in obj)) throw new Error("QUOTA_BYTES quota exceeded");
          Object.assign(store, obj);
        },
        remove: async (ks) => { for (const k of [].concat(ks)) delete store[k]; },
      } },
      tabs: { reload: async (id) => { sb.calls.push(["tabs.reload", id]); }, update: async () => ({}) },
    },
    addToActivityLog: async (t, cls, meta) => { sb.logs.push(t); if (meta) sb.meta.push(meta); },
    answer(p) {
      const k = Object.keys(net).find((x) => p.startsWith(x));
      const mode = k ? net[k] : "ok";
      if (mode === "offline") throw new TypeError("Failed to fetch");
      if (mode === "5xx") throw new Error("API 503: Service Unavailable");
      if (mode === "404") throw new Error("API 404: Not Found");
      if (mode === "422") throw new Error("API 422: Unprocessable Entity");
      return {};
    },
    apiPatch: async (p, body) => { sb.patched.push({ p, body }); return sb.answer(p); },
    apiPost: async (p, body) => { sb.posted.push({ p, body }); return sb.answer(p); },
    navigatePoolNext: async () => { if (sb.failNav) throw new Error("No tab with id: 70"); sb.calls.push(["navigatePoolNext"]); },
    buildApprovedAtsQueue: async () => [],
    handleMessage: async (m) => { sb.calls.push(["handleMessage", m.type]); },
    updateBadge: () => {},
    clearHumanHandoff: async () => {},
    // No finish run here ("Let Drop finish it" has its own test): every sender is the current run.
    finishRunActive: async () => false,
    fromStaleRun: async () => false,
    endFinishRun: async () => false,
    URL, Date, String, Array, Object, JSON, Math, console,
  };
  vm.createContext(sb);
  vm.runInContext(SRC, sb);
  return sb;
}
const run = (sb, expr) => vm.runInContext(expr, sb);
const poolStore = (extra = {}) => ({
  campaignRunning: true, campaignTabId: 70, atsPlatform: "pool",
  atsQueue: [{ id: "j1", applyUrl: "https://job-boards.greenhouse.io/a/jobs/1" }, { id: "j2", applyUrl: "https://jobs.lever.co/b/2" }],
  ...extra,
});

(async () => {
  // ---- 1. the skipped status -------------------------------------------------------
  {
    const sb = sandbox({});
    await run(sb, 'reportJobStatus("j1", "skipped")');
    check("a landed PATCH queues nothing and logs nothing", sb.patched.length === 1 && !sb.store.outbox && !sb.logs.length, sb);
  }
  for (const mode of ["offline", "5xx"]) {
    const sb = sandbox({}, { "/jobs/": mode });
    await run(sb, 'reportJobStatus("j1", "skipped")');
    const q = (sb.store.outbox || [])[0];
    check(`finding 1: a ${mode} PATCH goes to the outbox as a PATCH`,
      q && q.method === "PATCH" && q.path === "/jobs/j1/status" && q.body.status === "skipped", sb.store.outbox);
  }
  {
    const sb = sandbox({}, { "/jobs/": "404" });
    await run(sb, 'reportJobStatus("j1", "skipped")');
    check("finding 1: a 404 (no row changed) is said, with the job id, and not replayed",
      !sb.store.outbox && sb.logs.some((l) => /approved list/.test(l) && /404/.test(l)) &&
      sb.meta.some((m) => m.type === "job_status_failed" && m.job_id === "j1"), { logs: sb.logs, meta: sb.meta });
  }
  {
    const sb = sandbox(poolStore(), { "/jobs/": "offline" });
    const r = await sb.__msg({ type: "ATS_JOB_DONE" }, { tab: { id: 70 } });
    check("finding 1: ATS_JOB_DONE offline queues the skip and still advances",
      r.advanced && (sb.store.outbox || []).some((i) => i.method === "PATCH" && i.path === "/jobs/j1/status") &&
      sb.store.atsQueue.length === 1, { r, outbox: sb.store.outbox, q: sb.store.atsQueue });
  }
  {
    // The outbox replays a PATCH with apiPatch, a report with apiPost; both announced.
    const store = { outbox: [
      { path: "/applications/save", body: { job_title: "A" }, queuedAt: Date.now(), tries: 0 },
      { path: "/jobs/j1/status", body: { status: "skipped" }, method: "PATCH", queuedAt: Date.now(), tries: 0 },
      { path: "/handbacks", body: { job_title: "H" }, method: "POST", queuedAt: Date.now(), tries: 0 },
    ] };
    const sb = sandbox(store);
    await run(sb, "flushOutbox()");
    check("the outbox replays a queued status as a PATCH and drains everything",
      sb.patched.length === 1 && sb.patched[0].p === "/jobs/j1/status" && sb.posted.length === 2 && store.outbox.length === 0,
      { patched: sb.patched, posted: sb.posted, outbox: store.outbox });
    check("delivery names reports and updates", sb.logs.some((l) => /1 queued application report and 2 queued updates/.test(l)), sb.logs);
  }

  // ---- 4. the hand-back row ---------------------------------------------------------
  {
    const sb = sandbox(poolStore(), { "/handbacks": "offline", "/jobs/": "offline" });
    const r = await sb.__msg({ type: "ATS_JOB_FAILED", data: { title: "Designer", company: "Acme", url: "https://job-boards.greenhouse.io/a/jobs/1", reason: "captcha", platform: "greenhouse" } }, { tab: { id: 70 } });
    const ob = sb.store.outbox || [];
    check("finding 4: offline, the hand-back row goes to the outbox with its job_id",
      ob.some((i) => i.path === "/handbacks" && i.body.job_title === "Designer" && i.body.job_id === "j1"), ob);
    check("finding 1+4: …the skip is queued too, and the walk still advances",
      ob.some((i) => i.method === "PATCH" && i.path === "/jobs/j1/status") && r.advanced && sb.store.atsQueue.length === 1, { ob, r });
    check("finding 4: …and the person is told it will appear later", sb.logs.some((l) => /queued; it will appear/.test(l)), sb.logs);
  }
  {
    const sb = sandbox(poolStore(), { "/handbacks": "422" });
    await sb.__msg({ type: "ATS_JOB_FAILED", data: { title: "Designer", company: "Acme", url: "u", reason: "x" } }, { tab: { id: 70 } });
    check("finding 4: a refused hand-back is said out loud and not replayed",
      !(sb.store.outbox || []).some((i) => i.path === "/handbacks") && sb.logs.some((l) => /didn't reach your hand-back list/.test(l)), sb.logs);
  }
  {
    const sb = sandbox(poolStore());
    await sb.__msg({ type: "ATS_JOB_FAILED", data: { title: "Designer", company: "Acme", url: "u", reason: "x" } }, { tab: { id: 70 } });
    check("online: the row and the skip land, nothing queued",
      sb.posted.some((p) => p.p === "/handbacks") && sb.patched.some((p) => p.p === "/jobs/j1/status") && !sb.store.outbox, sb);
  }

  // ---- 2. a throw inside the advance -------------------------------------------------
  {
    // Fails after the head is dropped and BEFORE the step that stamps atsNavAt: the walk
    // had nothing for the watchdog to time.
    const sb = sandbox(poolStore());
    sb.failSetKey = "poolDoneUrls";
    await run(sb, "advanceAtsQueue()");
    check("finding 2: a failed step is logged durably", sb.logs.some((l) => /Couldn't move on to the next job/.test(l)) &&
      sb.meta.some((m) => m.type === "ats_advance_failed"), sb.logs);
    check("finding 2: …and the head is put back under the watchdog", typeof sb.store.atsNavAt === "number" && sb.store.atsNavTries === 0, sb.store);
    sb.store.atsNavAt = Date.now() - 9 * 60 * 1000;
    await run(sb, "atsWalkWatchdog()");
    check("finding 2: …which then acts on it (reloads the page) instead of a silent freeze",
      sb.calls.some((c) => c[0] === "tabs.reload" && c[1] === 70), sb.calls);
  }
  {
    const sb = sandbox(poolStore());
    sb.failSet = true;
    let threw = false;
    try { await run(sb, "advanceAtsQueue()"); } catch { threw = true; }
    check("finding 2: with storage itself failing it still never throws into the caller", !threw && sb.logs.length === 1, sb.logs);
  }
  {
    const sb = sandbox(poolStore({ campaignRunning: false }));
    sb.failSetKey = "poolDoneUrls";
    await run(sb, "advanceAtsQueue()");
    check("finding 2: a stopped run is not re-armed", sb.store.atsNavAt === undefined, sb.store);
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
