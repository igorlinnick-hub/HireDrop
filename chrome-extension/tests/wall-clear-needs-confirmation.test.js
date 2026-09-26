// A clean page must be CONFIRMED, and only the tab that raised the wall may confirm it. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/wall-clear-needs-confirmation.test.js
//
// Why it exists. 05baf86 made the clean page the authority that retires a human hand-off
// (audit 09-25, finding 2) and closed a real hole — a full-page challenge resolves by a
// TOP-LEVEL NAVIGATION that kills the context waiting to send DETECTION_CLEARED, so the
// one case where the human actually SOLVED the wall was the one case nothing cleared the
// flag. But the first shape of that mechanism opened two new ones, and both of them cut
// against invariant 5 (a LIVE human pause must stay protected — the watchdog must never
// reload a tab under someone who is solving a captcha right now):
//
//   1. ONE detector read decided it. A managed challenge repaints as it works
//      (#challenge-form → #challenge-running), an hCaptcha iframe needs a beat to lay out,
//      and isDetected() only counts boxes it can measure at ≥24x24. Any of those momentary
//      misses retired the hand-off while the human was still at the wall — which also
//      un-muted nativeWalkWatchdog for the very tab they were working in, so the next tick
//      could reload their half-solved challenge. The page must now stay wall-free ACROSS a
//      settle, and both detectors are asked both times (one record carries both walls).
//
//   2. The tab check was a moving POINTER. It compared the reporting tab against
//      campaignTabId — but background.js rewrites campaignTabId as the walk roams (the
//      PLATFORM_EXHAUSTED board hand-off, a re-opened window), so it only proved "you are
//      the walk's tab at this instant", not "you are the tab that raised this wall".
//      DETECTION_TRIPPED now STAMPS the raising tab into the record, and that id is what
//      has to match. Untimestamped legacy records keep the old comparison so a run that
//      was already parked at a wall during the upgrade is not stuck with an unretirable flag,
//      and a record whose tab no longer EXISTS falls back to it too — a pin that outlives its
//      tab is strictly narrower than the comparison it replaced, which would leave the banner
//      standing until the dashboard's 2h stale-guard: the last resort as the mechanism, which
//      the audit refuses. A destroyed tab holds no human, so invariant 5 has nothing to lose.
//
// Every check below drives the REAL sliced functions (content.js reportCleanPage…, the real
// DETECTION_TRIPPED case inside background.js handleMessage, and background.js
// retireHumanHandoffFromCleanPage) through fakes. Against the pre-repair code the four
// checks marked "PRE-REPAIR" fail.

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

function slice(src, signature, indent) {
  const start = src.indexOf(signature);
  if (start < 0) return null;
  const end = src.indexOf(`\n${indent}}\n`, start);
  if (end < 0) return null;
  return src.slice(start, end + indent.length + 3);
}

const reportFn = slice(CONTENT, "async function reportCleanPageIfHandoffPending(", "  ");
const wallPresentFn = slice(CONTENT, "function anyHumanWallPresent(", "  ");
const settleConst = (CONTENT.match(/const WALL_CLEAR_SETTLE_MS = [^;]+;/) || [])[0];
const clearFn = slice(BG, "async function clearHumanHandoff() {", "");
const retireFn = slice(BG, "async function retireHumanHandoffFromCleanPage(", "");
const handleFn = slice(BG, "async function handleMessage(msg, sender) {", "");
const siteNameFn = slice(BG, "function platformDisplayNameFromUrl(url) {", "");

check("sliced content.js reportCleanPageIfHandoffPending()", !!reportFn);
check("sliced content.js anyHumanWallPresent() + the settle constant", !!wallPresentFn && !!settleConst);
check("sliced background.js retire + clear + handleMessage", !!retireFn && !!clearFn && !!handleFn && !!siteNameFn);

// --- the content side: the detectors are read TWICE, across a settle -------------------
//
// `reads` is a script: one {detected, gated} pair per detector read. A wall that renders
// slowly is [clean, detected] — clean on the first look, there on the second. Because
// anyHumanWallPresent() asks BOTH detectors in one call, one script entry feeds one call.
async function runReport(reads, { captchaWaiting = null, clearedWhileWaiting = false } = {}) {
  const sent = [];
  const slept = [];
  let i = 0;
  const store = { captchaWaiting };
  const sandbox = {
    storageGet: async () => (store.captchaWaiting ? { captchaWaiting: store.captchaWaiting } : {}),
    isDetected: () => {
      const r = reads[Math.min(i, reads.length - 1)] || {};
      return { detected: !!r.detected, signal: r.detected ? "dom:#challenge-running" : "" };
    },
    detectConsentGate: () => {
      const r = reads[Math.min(i++, reads.length - 1)] || {};
      return { gated: !!r.gated, label: r.gated ? "Accept" : "" };
    },
    sleep: async (ms) => {
      slept.push(ms);
      // Anything that ends the pause for a different reason while we wait (Stop, the
      // watchdog's honest stop) leaves nothing to report about.
      if (clearedWhileWaiting) store.captchaWaiting = null;
    },
    sendMsg: async (msg) => { sent.push(msg); return { cleared: true }; },
    window: { location: { href: "https://www.indeed.com/viewjob?jk=abc" } },
    console,
  };
  vm.createContext(sandbox);
  // The preamble is whatever the source actually HAS. Pre-repair there is no
  // anyHumanWallPresent() and no settle constant, and reportCleanPageIfHandoffPending()
  // still runs standalone — which is the point: the behavioural checks below must FAIL
  // against that code, not be skipped over it.
  vm.runInContext(
    `${settleConst || ""}\n${wallPresentFn || ""}\n${reportFn}\n` +
    `globalThis.__call = () => reportCleanPageIfHandoffPending();`,
    sandbox);
  const out = await sandbox.__call();
  return { out, sent, slept, store };
}

const WALL = { url: "https://www.indeed.com/viewjob?jk=abc", site: "Indeed",
               signal: "dom:#challenge-form", kind: "captcha", at: Date.now(), tabId: 7 };

(async () => {
  if (reportFn) {
    // PRE-REPAIR #1: the captcha widget was mid-render on the first read.
    {
      const { out, sent } = await runReport(
        [{ detected: false }, { detected: true }], { captchaWaiting: { ...WALL } });
      check("PRE-REPAIR a slow-rendering captcha does NOT retire the hand-off",
        out === false && sent.length === 0,
        `one detector read used to be enough — sent ${JSON.stringify(sent)}`);
    }
    // PRE-REPAIR #2: the challenge resolved INTO an Accept-Terms modal a beat later. The
    // pre-repair code checked the consent gate once, before the modal existed.
    {
      const { out, sent } = await runReport(
        [{ gated: false }, { gated: true }], { captchaWaiting: { ...WALL } });
      check("PRE-REPAIR a consent wall that appears after the settle does NOT retire it",
        out === false && sent.length === 0, JSON.stringify(sent));
    }
    // The honest case still works — and it actually waited the real constant.
    {
      const { out, sent, slept } = await runReport(
        [{ detected: false }, { detected: false }], { captchaWaiting: { ...WALL } });
      check("a page that stays wall-free across the settle DOES retire the hand-off",
        out === true && sent.length === 1 && sent[0].type === "WALL_LOOKS_CLEAR", JSON.stringify(sent));
      check("…after a settle long enough for a challenge to repaint (≥4s)",
        slept.length === 1 && slept[0] >= 4000, JSON.stringify(slept));
    }
    // Still silent on the ordinary walk — and, crucially, no 5s stall per clean page.
    {
      const { out, sent, slept } = await runReport([{ detected: false }], { captchaWaiting: null });
      check("no hand-off recorded → no message AND no settle (this runs on every clean page)",
        out === false && sent.length === 0 && slept.length === 0);
    }
    // The pause ended some other way while we waited.
    {
      const { out, sent } = await runReport(
        [{ detected: false }, { detected: false }],
        { captchaWaiting: { ...WALL }, clearedWhileWaiting: true });
      check("a hand-off that disappeared during the settle is not re-reported",
        out === false && sent.length === 0, JSON.stringify(sent));
    }
  }

  // --- the background side: DETECTION_TRIPPED stamps the raising tab --------------------
  // `liveTabs` is which tab ids still EXIST. The pin is only a pin while the tab it names is
  // alive; retireHumanHandoffFromCleanPage() probes chrome.tabs.get for exactly that, the same
  // way the two watchdogs probe for a closed automation tab.
  function bgBox(store, liveTabs = [7, 9, 42]) {
    const logs = [];
    const notes = [];
    const box = {
      chrome: {
        storage: { local: {
          async get(keys) { const o = {}; for (const k of [].concat(keys)) if (k in store) o[k] = store[k]; return o; },
          async set(obj) { Object.assign(store, obj); },
          async remove(keys) { for (const k of [].concat(keys)) delete store[k]; },
        } },
        tabs: { get: async (id) => {
          if (!liveTabs.includes(id)) throw new Error("No tab with id: " + id);
          return { id };
        } },
        notifications: { create: async (n) => { notes.push(n); } },
      },
      addToActivityLog: async (text, cls) => { logs.push({ text, cls }); },
      apiPost: async () => ({}),
      console,
    };
    vm.createContext(box);
    vm.runInContext(
      `${siteNameFn}\n${clearFn}\n${retireFn}\n${handleFn}\n` +
      `globalThis.__msg = (m, s) => handleMessage(m, s);\n` +
      `globalThis.__retire = (t) => retireHumanHandoffFromCleanPage(t);`, box);
    return { box, store, logs, notes };
  }

  const TRIP = {
    type: "DETECTION_TRIPPED",
    data: { signal: "dom:#challenge-form", url: "https://www.indeed.com/viewjob?jk=abc", phase: "detail" },
  };

  if (handleFn && retireFn && clearFn && siteNameFn) {
    // PRE-REPAIR #3: the record now names the tab that is holding the human.
    {
      const { box, store } = bgBox({ campaignRunning: true, campaignTabId: 42 });
      await box.__msg(TRIP, { tab: { id: 42 } });
      check("PRE-REPAIR DETECTION_TRIPPED stamps the raising tab into captchaWaiting",
        !!store.captchaWaiting && store.captchaWaiting.tabId === 42,
        `got ${JSON.stringify(store.captchaWaiting)}`);
    }

    // PRE-REPAIR #4: THE TRAP. The human is at the wall in tab 42. The walk has since
    // roamed — PLATFORM_EXHAUSTED handed the run to another board and campaignTabId is 9
    // now — and tab 9 is on a perfectly clean board home page. Pre-repair, tab 9 ==
    // campaignTabId, so its report retired a wall a human was actively solving and
    // un-muted the watchdog on tab 42.
    {
      const { box, store } = bgBox({ campaignRunning: true, campaignTabId: 42 });
      await box.__msg(TRIP, { tab: { id: 42 } });
      store.campaignTabId = 9; // the walk moved on; the human did not
      const res = await box.__retire(9);
      check("PRE-REPAIR a tab that merely IS campaignTabId now cannot retire another tab's wall",
        res.cleared === false && res.otherTab === true, JSON.stringify(res));
      check("…and the live pause is still standing", !!store.captchaWaiting && store.captchaWaiting.tabId === 42);
      // And the tab that actually raised it still can, even though campaignTabId moved.
      const own = await box.__retire(42);
      check("the tab that RAISED the wall still retires it after campaignTabId moved",
        own.cleared === true && store.captchaWaiting === null, JSON.stringify(own));
    }

    // …and neither must a record whose tab is GONE. The pin is stricter than the
    // campaignTabId comparison it replaced, so without this escape a wall raised in a tab the
    // human then CLOSED (or that a re-opened automation window replaced) could never be
    // retired by anyone: the banner would stand until the dashboard's 2h stale-guard, i.e. the
    // last resort as the mechanism. Invariant 5 protects a LIVE human, and a destroyed tab
    // holds none.
    {
      const { box, store } = bgBox({ campaignRunning: true, campaignTabId: 9 }, [9]);
      await box.__msg(TRIP, { tab: { id: 42 } });   // wall raised in tab 42…
      const res = await box.__retire(9);            // …which no longer exists; the walk is in 9
      check("a wall whose tab is gone can be retired by the walk's current tab",
        res.cleared === true && store.captchaWaiting === null, JSON.stringify(res));
    }
    {
      // Same shape, but tab 42 is ALIVE — a human is at that wall right now. Nobody else
      // speaks for it, not even the walk's tab.
      const { box, store } = bgBox({ campaignRunning: true, campaignTabId: 9 }, [9, 42]);
      await box.__msg(TRIP, { tab: { id: 42 } });
      const res = await box.__retire(9);
      check("…but while that tab is alive the live pause is still nobody else's to retire",
        res.cleared === false && res.otherTab === true && !!store.captchaWaiting, JSON.stringify(res));
    }

    // A record from before the stamp existed must not become unretirable mid-run.
    {
      const legacy = { url: WALL.url, site: "Indeed", kind: "captcha", at: Date.now() }; // no tabId
      const { box, store } = bgBox({ campaignRunning: true, campaignTabId: 7, captchaWaiting: legacy });
      const res = await box.__retire(7);
      check("a legacy record with no tabId falls back to the campaignTabId comparison",
        res.cleared === true && store.captchaWaiting === null, JSON.stringify(res));
    }
    {
      const legacy = { url: WALL.url, site: "Indeed", kind: "captcha", at: Date.now() };
      const { box, store } = bgBox({ campaignRunning: true, campaignTabId: 7, captchaWaiting: legacy });
      const res = await box.__retire(9);
      check("…and that fallback is still a check, not a pass", res.cleared === false && !!store.captchaWaiting);
    }

    // The retired "left_for_you" kind: a pre-repair content script alive in an open tab can
    // still send it (an SW update does not re-inject content scripts). It must file NO record —
    // nothing is parked, so there is nothing to pin and nothing that could ever retire it —
    // and it must not touch a record either, because this message says nothing about the wall
    // some OTHER tab may be holding a human at (invariant 5).
    {
      const { box, store } = bgBox({ campaignRunning: true, campaignTabId: 5 });
      await box.__msg({ type: "DETECTION_TRIPPED",
        data: { ...TRIP.data, kind: "left_for_you" } }, { tab: { id: 5 } });
      check("a legacy left_for_you files no hand-off at all",
        !store.captchaWaiting, JSON.stringify(store.captchaWaiting));
    }
    {
      const { box, store } = bgBox({ campaignRunning: true, campaignTabId: 5,
                                     captchaWaiting: { ...WALL, tabId: 42 } });
      await box.__msg({ type: "DETECTION_TRIPPED",
        data: { ...TRIP.data, kind: "left_for_you" } }, { tab: { id: 5 } });
      check("…and does not retire another tab's live wall on its way past",
        !!store.captchaWaiting && store.captchaWaiting.tabId === 42,
        JSON.stringify(store.captchaWaiting));
    }
  }

  // --- invariant 5, from the other end --------------------------------------------------
  const watchdog = slice(BG, "async function nativeWalkWatchdog() {", "");
  check("nativeWalkWatchdog still parks on a hand-off it can vouch for",
    !!watchdog && /handoffIsLive\(d\.captchaWaiting/.test(watchdog),
    "confirming the clean page closes the SOURCE of a stale flag — it must not delete the " +
    "guard that keeps a tab from being reloaded under a human mid-captcha");

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
