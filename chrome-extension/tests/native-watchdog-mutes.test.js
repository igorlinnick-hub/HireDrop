// nativeWalkWatchdog's mutes, driven for real. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/native-watchdog-mutes.test.js
//
// Why it exists. The watchdog is the ONLY backstop a native board walk has: content.js
// names a dead link and advances, and everything else — an injection miss, a wall, a page
// that never fires an event — is caught here or not at all. It must also never reload the
// tab under a human who is solving a captcha right this second. Those two duties fight,
// and the fight is settled by arithmetic on two clocks, so it has to be MEASURED.
//
// It wasn't. The mute windows shipped on 09-26 (05baf86) with one source-text assertion
// over the sliced function — /^\s*if \(.*captchaWaiting.*\) return;$/m — which matches the
// PRE-fix code just as well, so the suite was green either way and nothing exercised
// WALL_MUTE_MS, REVIEW_MUTE_MS, handoffIsLive(), nativeWalkPlatform(), the board-scoped
// logged_out mute or the stale-hand-off clear. What the arithmetic actually said: the mute
// is only ever consulted after NATIVE_WATCHDOG_SILENT_MS = 10 min of silence, and it muted
// only hand-offs younger than 8 min — windows that never overlap, so the live-captcha
// protection had been REMOVED, not bounded (run the pre-fix function with a 10-min-old
// captchaWaiting and it clears the CTA and reloads the tab under the human).
//
// So this file drives the shipped functions through a fake chrome.storage / chrome.tabs and
// asserts the BEHAVIOUR on both sides of every window:
//   · the mute must be REACHABLE — strictly longer than the silence it guards (invariant 5);
//   · and it must EXPIRE — a leftover flag never silences the watchdog for the rest of the
//     run, and the dashboard's 2h stale-guard stays the last resort, not the mechanism
//     (invariants 1+2, audit 09-25 docs/reviews/2026-09-25-captcha-resume-audit.md).

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

// Slice by signature + closing brace at a known indent, like the other files here: run the
// REAL code. A test that re-describes the fix in a regex is the thing this file replaces.
function slice(src, signature, indent = "") {
  const start = src.indexOf(signature);
  if (start < 0) return null;
  const end = src.indexOf(`\n${indent}}\n`, start);
  if (end < 0) return null;
  return src.slice(start, end + indent.length + 3);
}

// The constants, handoffIsLive() and nativeWalkPlatform() sit in one contiguous block right
// above the watchdog, so take the whole span rather than naming each constant — whatever
// numbers ship are the numbers under test, including a renamed one.
const constStart = BG.indexOf("const NATIVE_WATCHDOG_SILENT_MS");
const wdStart = BG.indexOf("async function nativeWalkWatchdog() {");
const wdEnd = BG.indexOf("\n}\n", wdStart);
const region = constStart >= 0 && wdStart > constStart && wdEnd > wdStart
  ? BG.slice(constStart, wdEnd + 3)
  : null;
const clearFn = slice(BG, "async function clearHumanHandoff() {");
const logoutFn = slice(BG, "function logoutIsTrustworthy(platform, rec) {");
const hostsConst = (BG.match(/^const INDEED_APPLY_HOSTS = [^;]+;/m) || [])[0];

check("the watchdog + its mute windows slice out of background.js", !!region);
check("clearHumanHandoff() slices out (the stale-clear calls it)", !!clearFn);
check("logoutIsTrustworthy() + INDEED_APPLY_HOSTS slice out", !!logoutFn && !!hostsConst);
if (!region || !clearFn || !logoutFn || !hostsConst) {
  console.log(`\n${failures} FAILED`);
  process.exit(1);
}

const NOW = Date.now();
const minsAgo = (n) => NOW - n * 60 * 1000;

/**
 * Drive nativeWalkWatchdog() once over a fake storage, and report what it DID: the reloads
 * it asked for, the backend calls, the log lines, and the store it left behind.
 */
async function tick(store, { liveTabs = [7] } = {}) {
  const logs = [];
  const reloaded = [];
  const posted = [];
  const sandbox = {
    chrome: {
      storage: {
        local: {
          async get(keys) {
            const out = {};
            for (const k of [].concat(keys)) if (k in store) out[k] = store[k];
            return out;
          },
          async set(obj) { Object.assign(store, obj); },
        },
      },
      tabs: {
        async get(id) {
          if (!liveTabs.includes(id)) throw new Error("No tab with id " + id);
          return { id };
        },
        reload(id) { reloaded.push(id); return Promise.resolve(); },
      },
    },
    addToActivityLog: async (text, cls) => { logs.push({ text, cls }); },
    apiPost: async (url, body) => { posted.push({ url, body }); return {}; },
    updateBadge: () => {},
    // Without URL in the sandbox, nativeWalkPlatform() throws inside its try and returns
    // null for EVERY board — every board-scoped assertion below would then pass for the
    // wrong reason.
    URL,
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    `${hostsConst}\n${logoutFn}\n${clearFn}\n${region}\n` +
    "globalThis.__tick = () => nativeWalkWatchdog();\n" +
    // typeof, not a bare read: a shape that has no mute window at all (490a02e muted on the
    // mere PRESENCE of the flag) must FAIL this file with a named reason, not die of a
    // ReferenceError before a single case runs.
    "globalThis.__win = {\n" +
    "  silent: typeof NATIVE_WATCHDOG_SILENT_MS === 'number' ? NATIVE_WATCHDOG_SILENT_MS : null,\n" +
    "  wall: typeof WALL_MUTE_MS === 'number' ? WALL_MUTE_MS : null,\n" +
    "  review: typeof REVIEW_MUTE_MS === 'number' ? REVIEW_MUTE_MS : null,\n" +
    "};",
    sandbox
  );
  await sandbox.__tick();
  return { store, logs, reloaded, posted, win: sandbox.__win };
}

// A native board walk (atsPlatform unset — a pool/ATS queue run is atsWalkWatchdog's job),
// silent for 20 min, tab 7 still open, walking Indeed.
function walk(extra) {
  return {
    campaignRunning: true,
    campaignTabId: 7,
    walkAliveAt: minsAgo(20),
    walkNudges: 0,
    campaignTargetUrl: "https://www.indeed.com/jobs?q=nurse&l=Honolulu",
    ...extra,
  };
}

const WALL = { url: "https://www.indeed.com/viewjob?jk=abc", site: "Indeed", kind: "captcha" };

(async () => {
  // --- 0. The arithmetic itself. A mute the watchdog can never reach is not a mute. -----
  {
    const { win } = await tick(walk());
    const mins = (v) => (v == null ? "absent" : `${Math.round(v / 60000)} min`);
    check("the wall mute outlasts the silence window it guards (so it is REACHABLE)",
      win.wall != null && win.silent != null && win.wall > win.silent,
      `WALL_MUTE_MS ${mins(win.wall)} vs ${mins(win.silent)} of silence — ` +
      "a shorter window is consulted only after it has already expired, i.e. never true");
    check("the review mute outlasts it too", win.review != null && win.review > win.silent,
      `REVIEW_MUTE_MS ${mins(win.review)}`);
  }

  // --- 1. Plain silence: the backstop fires. ---------------------------------------------
  {
    const { store, reloaded, logs } = await tick(walk());
    check("a silent walk with nobody parked gets its reload", reloaded.length === 1 && reloaded[0] === 7);
    check("…counted as a nudge, with the clock restarted", store.walkNudges === 1 && store.walkAliveAt > minsAgo(1));
    check("…and named in the feed", logs.some((l) => /silent for \d+ min/.test(l.text)));
  }
  {
    const store = walk({ walkAliveAt: minsAgo(2), walkNudges: 1 });
    const { reloaded } = await tick(store);
    check("a walk that is still alive is left alone, nudges reset", reloaded.length === 0 && store.walkNudges === 0);
  }

  // --- 2. INVARIANT 5. The human is at the wall RIGHT NOW. -------------------------------
  // 11 min is the whole point: past the 10 min of silence that wakes the watchdog, inside
  // the 5-min pause + 10-min silence the wall record is worth. The pre-fix 8-min window
  // called this "stale" and reloaded the tab under the human.
  {
    const store = walk({ captchaWaiting: { ...WALL, at: minsAgo(11) } });
    const { reloaded, logs } = await tick(store);
    check("a hand-off past the silence window but inside its own pause still mutes the watchdog",
      reloaded.length === 0,
      "reloading here throws away a captcha the human is mid-way through solving");
    check("…and its CTA is left standing", store.captchaWaiting && store.captchaWaiting.site === "Indeed");
    check("…silently: no 'dropping it' line over a live pause", logs.length === 0);
  }
  {
    const store = walk({ captchaWaiting: { ...WALL, at: minsAgo(2) } });
    const { reloaded } = await tick(store);
    check("a fresh hand-off mutes it as well", reloaded.length === 0 && !!store.captchaWaiting);
  }

  // --- 3. INVARIANT 2. The flag EXPIRES — a leftover never silences the run. -------------
  {
    const store = walk({ captchaWaiting: { ...WALL, at: minsAgo(25) } });
    const { reloaded, logs } = await tick(store);
    check("a hand-off older than the pause it describes is dropped", store.captchaWaiting === null);
    check("…and the walk is checked instead of left 'running'", reloaded.length === 1);
    check("…and the drop is explained", logs.some((l) => /older than the pause it describes/.test(l.text)));
  }
  {
    const store = walk({ captchaWaiting: { ...WALL } }); // no .at — an old/foreign writer
    const { reloaded } = await tick(store);
    check("an untimestamped record cannot be vouched for as a live pause",
      store.captchaWaiting === null && reloaded.length === 1);
  }

  // --- 4. The tap review's own, longer window. -------------------------------------------
  {
    const store = walk({ reviewPending: { id: "r1", at: minsAgo(12) } });
    const { reloaded } = await tick(store);
    check("a 12-min-old review card is a live pause (it waits 30 min)", reloaded.length === 0);
    check("…and survives the tick", !!store.reviewPending);
  }
  {
    // 35 min: past the pre-fix 33-min window, inside 30 + 10. Same reachability bug, same fix.
    const store = walk({ reviewPending: { id: "r1", at: minsAgo(35) } });
    const { reloaded } = await tick(store);
    check("a review card still inside 30 min + the silence window keeps its mute",
      reloaded.length === 0 && !!store.reviewPending);
  }
  {
    const store = walk({ reviewPending: { id: "r1", at: minsAgo(55) } });
    const { reloaded } = await tick(store);
    check("a review card older than that is a leftover: the walk gets its reload", reloaded.length === 1);
    check("…and the card goes with it — reviewPending rides the same 'your turn' surface " +
      "as the wall, and ReviewPanel has no stale guard of its own",
      store.reviewPending === null, `got ${JSON.stringify(store.reviewPending)}`);
  }

  // --- 5. The logged_out mute is scoped to the board THIS walk is on. --------------------
  {
    const store = walk({
      platformConnections: { ziprecruiter: { status: "logged_out", checkedAt: minsAgo(2) } },
    });
    const { reloaded } = await tick(store);
    check("a ZR login wall does not mute an INDEED walk", reloaded.length === 1,
      "a user simply not signed into ZipRecruiter used to disarm the only backstop of an Indeed-only run");
  }
  {
    const store = walk({
      campaignTargetUrl: "https://www.ziprecruiter.com/jobs-search?search=nurse",
      platformConnections: { indeed: { status: "logged_out", host: "secure.indeed.com", checkedAt: minsAgo(20) } },
    });
    const { reloaded } = await tick(store);
    check("the board a login wall already failed over from does not mute the new one",
      reloaded.length === 1);
  }
  {
    const store = walk({
      platformConnections: { indeed: { status: "logged_out", host: "secure.indeed.com", checkedAt: minsAgo(2) } },
    });
    const { reloaded } = await tick(store);
    check("a fresh login wall on the walked board DOES mute it", reloaded.length === 0);
  }
  {
    // Same 11-min band as the captcha case: the human is typing their password right now.
    const store = walk({
      platformConnections: { indeed: { status: "logged_out", host: "secure.indeed.com", checkedAt: minsAgo(11) } },
    });
    const { reloaded } = await tick(store);
    check("a login wall past the silence window but inside its pause still mutes it",
      reloaded.length === 0);
  }
  {
    const store = walk({
      platformConnections: { indeed: { status: "logged_out", host: "www.indeed.com", checkedAt: minsAgo(2) } },
    });
    const { reloaded } = await tick(store);
    check("an untrustworthy indeed logged_out (search host, not an apply host) mutes nothing",
      reloaded.length === 1, "logoutIsTrustworthy must stay in the path");
  }
  {
    const store = walk({
      campaignTargetUrl: "https://boards.greenhouse.io/acme/jobs/1",
      platformConnections: { indeed: { status: "logged_out", host: "secure.indeed.com", checkedAt: minsAgo(2) } },
    });
    const { reloaded } = await tick(store);
    check("a target that is not one of the three walkable boards takes no board mute",
      reloaded.length === 1, "nativeWalkPlatform() returns null and no scoped mute may apply");
  }

  // --- 6. INVARIANT 1. Two reloads and nothing: stop honestly, name the reason. ----------
  {
    const store = walk({
      walkNudges: 2,
      captchaWaiting: { ...WALL, at: minsAgo(40) },
      reviewPending: { id: "r1", at: minsAgo(80) },
      reviewDecision: { id: "r1", choice: "skip" },
    });
    const { reloaded, logs, posted } = await tick(store);
    check("after two reloads it stops instead of claiming 'running'",
      store.campaignRunning === false && reloaded.length === 0 && store.walkNudges === 0);
    check("…tells the backend", posted.some((p) => p.url === "/campaign/stop"));
    check("…and says why, in words", logs.some((l) => /stayed silent through two reloads/.test(l.text)));
    check("…and every 'your turn' surface goes down with the run",
      store.captchaWaiting === null && store.reviewPending === null && store.reviewDecision === null,
      `left ${JSON.stringify({ c: store.captchaWaiting, p: store.reviewPending, d: store.reviewDecision })}`);
  }

  // --- 7. The window the user closed by hand is not ours to reload or to stop. -----------
  {
    const store = walk({ captchaWaiting: { ...WALL, at: minsAgo(40) } });
    const { reloaded, logs } = await tick(store, { liveTabs: [] });
    check("a closed automation window ends the tick", reloaded.length === 0 && logs.length === 0);
    check("…and leaves the flag to the ping path that owns a vanished window",
      !!store.captchaWaiting && store.campaignRunning === true);
  }

  // --- 8. Not this watchdog's run. ------------------------------------------------------
  {
    const store = walk({ atsPlatform: "pool" });
    const { reloaded } = await tick(store);
    check("a pool/ATS queue run belongs to atsWalkWatchdog", reloaded.length === 0);
  }
  {
    const store = walk({ campaignRunning: false });
    const { reloaded } = await tick(store);
    check("a stopped campaign is nobody's to reload", reloaded.length === 0);
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
