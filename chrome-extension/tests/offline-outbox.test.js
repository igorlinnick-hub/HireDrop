// Fixture test for lane 1+2 — keep-awake + offline resilience. Run like the others:
//
//   node <repo>/jobflow/chrome-extension/tests/offline-outbox.test.js
//
// Why it exists (decision 09-21, STATUS.md "ночная смена"): the screen dimming after
// 30 min used to take the whole run with it, and a network drop LOST the record of a
// submit that had already reached the employer (apiPost threw, nothing retried).
//
// What must hold:
//   1. manifest declares "power"; keep-awake is synced from updateBadge (the single
//      point every campaignRunning flip converges on) at level "system" — screen may
//      dim, machine must not sleep — and the start of a run SAYS it holds the machine
//      awake (silent keep-awake is malware behavior).
//   2. The walk PAUSES offline: runPhase checks navigator.onLine before doing work.
//   3. A failed /applications/save is queued ONLY for network/5xx failures — a 4xx is
//      a real refusal and must not be replayed; 429 keeps its stop path.
//   4. flushOutbox: success drains the item, network failure keeps it (with a retry
//      bound), a 4xx drops it. Wired to the ext-ping minute tick.
//   5. Wake-up after a sleep gap refreshes the watchdog clocks BEFORE nudging the tab —
//      otherwise atsWalkWatchdog reloads a half-filled form the moment the lid opens.
//   6. The automation tab opts out of Memory Saver (autoDiscardable: false).

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
const CT = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const MANIFEST = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8"));

let failures = 0;
function check(name, ok, detail) {
  if (!ok) {
    failures += 1;
    console.error(`FAIL ${name}${detail ? `\n  ${detail}` : ""}`);
  } else {
    console.log(`ok   ${name}`);
  }
}

// ── 1. keep-awake ──────────────────────────────────────────────────────────
check("manifest declares the power permission", MANIFEST.permissions.includes("power"));
check("keep-awake level is \"system\" (screen may dim, machine must not sleep)",
  BG.includes('chrome.power.requestKeepAwake("system")'));
{
  const badge = BG.slice(BG.indexOf("async function updateBadge()"));
  check("updateBadge syncs keep-awake to campaignRunning",
    badge.slice(0, badge.indexOf("\n}")).includes("syncKeepAwake(running)"));
}
check("the run announces it is holding the machine awake",
  BG.includes("Keeping your computer awake while the campaign runs"));
check("the announcement is honest about the lid",
  BG.includes("Closing the laptop lid still puts it to sleep"));

// ── 2. offline pause in the walk ───────────────────────────────────────────
{
  const rp = CT.slice(CT.indexOf("async function runPhase()"), CT.indexOf("async function _runPhaseInner()"));
  check("runPhase parks on navigator.onLine=false before any work",
    rp.includes("!navigator.onLine") && rp.includes("waitForOnline()"));
  check("runPhase re-checks the campaign after the park (Stop may have landed)",
    rp.indexOf("waitForOnline()") < rp.lastIndexOf("isCampaignRunning()"));
}

// ── 3. save failure routing ────────────────────────────────────────────────
{
  const saved = BG.slice(BG.indexOf('case "APPLICATION_SAVED"'));
  const block = saved.slice(0, saved.indexOf("// Free taste"));
  check("network/5xx save failure is queued", block.includes('queueOutbox("/applications/save", savePayload)'));
  check("queue gate is network-or-5xx, never plain 4xx",
    /isNetworkError\(err\) \|\| \/API 5\\d\\d\//.test(block));
  check("429 still stops the campaign", block.includes('stopped: "limit"'));
}

// ── 4. flushOutbox semantics, executed ─────────────────────────────────────
{
  const START = BG.indexOf("function isNetworkError(err)");
  const END = BG.indexOf("// end outbox");
  check("outbox block markers exist", START >= 0 && END > START);
  const store = { outbox: [
    { path: "/applications/save", body: { job_title: "OK Job" }, queuedAt: Date.now(), tries: 0 },
    { path: "/applications/save", body: { job_title: "Offline Job" }, queuedAt: Date.now(), tries: 0 },
    { path: "/applications/save", body: { job_title: "Refused Job" }, queuedAt: Date.now(), tries: 0 },
  ]};
  const logs = [];
  const sandbox = {
    navigator: { onLine: true },
    chrome: { storage: { local: {
      get: async (k) => ({ [typeof k === "string" ? k : k[0]]: store[typeof k === "string" ? k : k[0]] }),
      set: async (obj) => Object.assign(store, obj),
    }}},
    addToActivityLog: async (t) => logs.push(t),
    apiPost: async (p, body) => {
      if (body.job_title === "Offline Job") throw new TypeError("Failed to fetch");
      if (body.job_title === "Refused Job") throw new Error("API 422: Unprocessable");
      return { saved: true };
    },
    Date, Array, Object, JSON, Math, console,
  };
  vm.createContext(sandbox);
  vm.runInContext(BG.slice(START, END), sandbox);
  const run = vm.runInContext("flushOutbox()", sandbox);
  const done = (async () => { await run; })();
  require("node:child_process"); // no-op; keeps require pattern consistent
  const finish = done.then(() => {
    check("flush drains the delivered item and keeps the network-failed one",
      store.outbox.length === 1 && store.outbox[0].body.job_title === "Offline Job",
      `outbox after flush: ${JSON.stringify(store.outbox)}`);
    check("the kept item's retry counter advanced", store.outbox[0].tries === 1);
    check("the 4xx item was dropped with a visible line",
      logs.some((l) => l.includes("Gave up on a queued report") && l.includes("Refused Job")));
    check("delivery is announced", logs.some((l) => l.includes("delivered 1 queued application report")));

    // wired to the tick
    const alarm = BG.slice(BG.indexOf("chrome.alarms.onAlarm.addListener"));
    const tick = alarm.slice(0, alarm.indexOf("\n});")); // column-0 close = the listener's own
    check("flushOutbox runs on the ext-ping tick", tick.includes("flushOutbox().catch"));
    check("sleep-gap detector runs on the ext-ping tick", tick.includes("detectSleepGap().catch"));

    // ── 5. wake-up grace ordering ──────────────────────────────────────────
    const gap = BG.slice(BG.indexOf("async function detectSleepGap()"), BG.indexOf("chrome.alarms.onAlarm.addListener"));
    check("wake-up refreshes watchdog clocks before nudging the tab",
      gap.indexOf("grace.atsNavAt") >= 0 &&
      gap.indexOf("grace.atsNavAt") < gap.indexOf('{ type: "CAMPAIGN_STARTED" }'));

    // ── 6. Memory Saver opt-out ────────────────────────────────────────────
    check("automation tab opts out of Memory Saver",
      BG.includes("autoDiscardable: false"));

    if (failures) {
      console.error(`\n${failures} check(s) failed`);
      process.exit(1);
    }
    console.log("\nall checks passed");
  });
  finish.catch((e) => { console.error("flush run crashed:", e); process.exit(1); });
}
