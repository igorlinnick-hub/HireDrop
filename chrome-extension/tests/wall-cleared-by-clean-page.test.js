// The hand-off that outlived the wall. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/wall-cleared-by-clean-page.test.js
//
// Why it exists (audit 09-25, finding 2, docs/reviews/2026-09-25-captcha-resume-audit.md).
// Igor asked: "after 5 minutes does the campaign continue automatically, or does it hang?"
// For a full-page wall the honest answer used to be "it continues, but it lies about it".
// A Cloudflare managed challenge / Indeed "Security Check" / DataDome resolves by a
// TOP-LEVEL NAVIGATION back to the original URL, and that navigation destroys the
// content-script context. Both exits from the captcha pause and both from the terms pause
// live inside that context, so the ONE case where the human actually SOLVED the wall was
// the one case where DETECTION_CLEARED never fired — and nothing else cleared
// captchaWaiting. For the rest of the run the dashboard begged for a wall that was gone,
// and nativeWalkWatchdog stayed muted by the stale flag ("running" while nothing walks).
//
// What must hold:
//   1. A clean page in the CAMPAIGN tab retires a recorded hand-off (captcha AND terms).
//   2. A clean page in ANY OTHER tab retires nothing — a human parked at the wall in the
//      automation window must never be declared done because another tab looks fine
//      (invariant 5: do not over-mute, the LIVE pause stays protected).
//   3. No hand-off recorded → no message and no log line: the walk passes clean pages
//      constantly and none of them is an event.
//   4. A page that is captcha-clean but still consent-GATED is not clean: one record
//      carries both walls.
//   5. nativeWalkWatchdog still parks on a live hand-off — this fix closes the SOURCE of
//      the stale flag, it does not delete the protection.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
const CONTENT = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

// Slice one function out of a source file by its signature and its closing brace at a
// known indent. Same trick detection-visibility.test.js uses: run the REAL code, don't
// re-describe it in the test (a re-described fix is the "заглушка" this suite is for).
function slice(src, signature, indent) {
  const start = src.indexOf(signature);
  if (start < 0) return null;
  const end = src.indexOf(`\n${indent}}\n`, start);
  if (end < 0) return null;
  return src.slice(start, end + indent.length + 3);
}

// --- background: who may retire a hand-off --------------------------------------------
const clearFn = slice(BG, "async function clearHumanHandoff() {", "");
const retireFn = slice(BG, "async function retireHumanHandoffFromCleanPage(", "");
check("background has clearHumanHandoff() (490a02e — reused, not duplicated)", !!clearFn);
check("background has retireHumanHandoffFromCleanPage()", !!retireFn,
  "nothing but a dying content-script context can clear the flag");

function runRetire(store, tabId) {
  const logs = [];
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
    },
    addToActivityLog: async (text, cls) => { logs.push({ text, cls }); },
    apiPost: async (url, body) => { posted.push({ url, body }); return {}; },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(`${clearFn}\n${retireFn}\nglobalThis.__call = (t) => retireHumanHandoffFromCleanPage(t);`, sandbox);
  return sandbox.__call(tabId).then((res) => ({ res, store, logs, posted }));
}

const WALL = { url: "https://www.indeed.com/viewjob?jk=abc", site: "Indeed", kind: "captcha", at: Date.now() };

(async () => {
  if (clearFn && retireFn) {
    // 1. The campaign tab reports a clean page after solving the interstitial.
    {
      const { res, store, logs, posted } = await runRetire(
        { campaignRunning: true, campaignTabId: 7, captchaWaiting: { ...WALL } }, 7);
      check("campaign tab on a clean page retires the hand-off", res.cleared === true);
      check("the flag is actually null afterwards", store.captchaWaiting === null,
        `got ${JSON.stringify(store.captchaWaiting)}`);
      check("the run is told, locally and in the backend feed",
        logs.length === 1 && /Human check cleared/.test(logs[0].text) && posted.length === 1);
    }

    // 1b. The consent-wall twin: accepting terms navigates too, so it leaked identically.
    {
      const { res, logs } = await runRetire(
        { campaignRunning: true, campaignTabId: 7, captchaWaiting: { ...WALL, kind: "terms" } }, 7);
      check("a terms hand-off is retired the same way", res.cleared === true);
      check("and it is named as terms, not as a captcha",
        logs.length === 1 && /Terms accepted/.test(logs[0].text),
        logs.length ? logs[0].text : "no log");
    }

    // 2. THE TRAP. The human is still at the wall in the automation window; some other
    //    tab of the same profile is on a perfectly clean page.
    {
      const store = { campaignRunning: true, campaignTabId: 7, captchaWaiting: { ...WALL } };
      const { res, logs } = await runRetire(store, 9);
      check("another tab may NOT retire a live hand-off", res.cleared === false && res.otherTab === true);
      check("the live pause survives untouched", store.captchaWaiting && store.captchaWaiting.site === "Indeed");
      check("and it stays silent about it", logs.length === 0);
    }
    {
      const store = { campaignRunning: true, campaignTabId: 7, captchaWaiting: { ...WALL } };
      const { res } = await runRetire(store, undefined); // no sender.tab (popup, ping.js…)
      check("a report with no tab identity is refused", res.cleared === false);
      check("…and leaves the hand-off standing", !!store.captchaWaiting);
    }

    // 3. No noise on the ordinary walk.
    {
      const { res, logs, posted } = await runRetire({ campaignRunning: true, campaignTabId: 7 }, 7);
      check("a clean page with no hand-off recorded is a non-event",
        res.cleared === false && logs.length === 0 && posted.length === 0);
    }

    // A stopped run's flag belongs to the stop paths (490a02e), not to this.
    {
      const store = { campaignRunning: false, campaignTabId: 7, captchaWaiting: { ...WALL } };
      const { res } = await runRetire(store, 7);
      check("a stopped campaign is not this function's business", res.cleared === false);
    }
  }

  // --- content: WHEN the clean page speaks ---------------------------------------------
  const reportFn = slice(CONTENT, "async function reportCleanPageIfHandoffPending(", "  ");
  const wallPresentFn = slice(CONTENT, "function anyHumanWallPresent(", "  ");
  const settleConst = (CONTENT.match(/const WALL_CLEAR_SETTLE_MS = [^;]+;/) || [])[0];
  check("content.js has reportCleanPageIfHandoffPending()", !!reportFn,
    "before this, content.js never read or wrote captchaWaiting at all");
  check("…and asks BOTH detectors through anyHumanWallPresent()", !!wallPresentFn && !!settleConst,
    "one record carries both walls; see wall-clear-needs-confirmation.test.js");

  async function runReport({ captchaWaiting, gated, detected }) {
    const sent = [];
    const sandbox = {
      storageGet: async () => (captchaWaiting ? { captchaWaiting } : {}),
      detectConsentGate: () => ({ gated: !!gated, label: gated ? "Accept" : "" }),
      isDetected: () => ({ detected: !!detected, signal: detected ? "dom:#challenge-form" : "" }),
      sleep: async () => {}, // the settle itself is exercised in wall-clear-needs-confirmation
      sendMsg: async (msg) => { sent.push(msg); return { cleared: true }; },
      window: { location: { href: "https://www.indeed.com/viewjob?jk=abc" } },
      console,
    };
    vm.createContext(sandbox);
    vm.runInContext(`${settleConst}\n${wallPresentFn}\n${reportFn}\nglobalThis.__call = () => reportCleanPageIfHandoffPending();`, sandbox);
    const out = await sandbox.__call();
    return { out, sent };
  }

  if (reportFn && wallPresentFn) {
    {
      const { out, sent } = await runReport({ captchaWaiting: { ...WALL }, gated: false });
      check("a fresh context on a clean page reports the wall gone",
        out === true && sent.length === 1 && sent[0].type === "WALL_LOOKS_CLEAR",
        JSON.stringify(sent));
    }
    {
      const { sent } = await runReport({ captchaWaiting: null, gated: false });
      check("no hand-off → not one message (this runs on EVERY clean page)", sent.length === 0);
    }
    {
      const { out, sent } = await runReport({ captchaWaiting: { ...WALL }, gated: true });
      check("captcha gone but terms modal still up is NOT a clean page",
        out === false && sent.length === 0, JSON.stringify(sent));
    }
  }

  // --- the incident, replayed end to end ------------------------------------------------
  // 09-25 scenario: board walk on Indeed, Cloudflare managed challenge, the human solves
  // it, the challenge NAVIGATES back to /viewjob and the waiting context dies without ever
  // sending DETECTION_CLEARED. What used to happen next was nothing at all. Both halves of
  // the fix are wired together here — the fresh context's report feeds the real background
  // handler — so this passes only if the whole path works, not just each end of it.
  if (reportFn && retireFn && clearFn) {
    const store = {
      campaignRunning: true,
      campaignTabId: 7,
      captchaWaiting: { url: "https://www.indeed.com/viewjob?jk=abc", site: "Indeed",
                        signal: "dom:#challenge-form", kind: "captcha", at: Date.now(),
                        tabId: 7 },
    };
    const bgBox = { chrome: { storage: { local: {
      async get(keys) { const o = {}; for (const k of [].concat(keys)) if (k in store) o[k] = store[k]; return o; },
      async set(obj) { Object.assign(store, obj); },
    } } }, addToActivityLog: async () => {}, apiPost: async () => ({}), console };
    vm.createContext(bgBox);
    vm.runInContext(`${clearFn}\n${retireFn}\nglobalThis.__retire = (t) => retireHumanHandoffFromCleanPage(t);`, bgBox);

    const ctxBox = {
      storageGet: async () => (store.captchaWaiting ? { captchaWaiting: store.captchaWaiting } : {}),
      detectConsentGate: () => ({ gated: false, label: "" }),
      isDetected: () => ({ detected: false, signal: "" }),
      sleep: async () => {},
      // The freshly injected context IS the tab that raised the wall — the challenge
      // navigated the same tab, so the tab id never changed.
      sendMsg: async (msg) => (msg.type === "WALL_LOOKS_CLEAR" ? bgBox.__retire(7) : null),
      window: { location: { href: "https://www.indeed.com/viewjob?jk=abc" } },
      console,
    };
    vm.createContext(ctxBox);
    vm.runInContext(`${settleConst}\n${wallPresentFn}\n${reportFn}\nglobalThis.__report = () => reportCleanPageIfHandoffPending();`, ctxBox);

    check("the hand-off is standing before the walk resumes", !!store.captchaWaiting);
    const cleared = await ctxBox.__report();
    check("the resumed walk's first clean tick retires it", cleared === true);
    check("dashboard no longer begs for a wall that is gone", store.captchaWaiting === null,
      `got ${JSON.stringify(store.captchaWaiting)}`);
  }

  // --- wiring + invariant 5 -------------------------------------------------------------
  check("the clean-page branch is the one that reports it",
    /if \(!det\.detected\) \{[\s\S]{0,600}?await reportCleanPageIfHandoffPending\(\);/.test(CONTENT),
    "a helper nobody calls is not a fix");
  check("background routes WALL_LOOKS_CLEAR through the tab-gated function",
    /case "WALL_LOOKS_CLEAR":\s*\n\s*return await retireHumanHandoffFromCleanPage\(sender && sender\.tab && sender\.tab\.id\)/.test(BG),
    "clearing it without checking the sender tab re-opens the trap in check 2");
  check("the reporting tab is matched against the stamped raiser, not the roaming pointer",
    !!retireFn && /captchaWaiting\.tabId/.test(retireFn),
    "campaignTabId moves with the walk — see wall-clear-needs-confirmation.test.js");
  // Invariant 5 — the watchdog must not reload a tab under a human mid-captcha — used to be
  // asserted here as /^\s*if \(.*captchaWaiting.*\) return;$/m over the sliced watchdog. That
  // line was worthless: a skeptic sliced the PRE-fix function out of 490a02e and the same
  // regex matched it, so this file was green whether the mute was bounded, unbounded, or
  // arithmetically unreachable. It is now MEASURED — both sides of every window, against the
  // shipped function over a fake chrome.storage — in tests/native-watchdog-mutes.test.js.

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
