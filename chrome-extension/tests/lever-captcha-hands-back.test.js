// The Lever hCaptcha: a hand-back, not a hand-off. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/lever-captcha-hands-back.test.js
//
// Why it exists (audit 09-25, findings 3+5, docs/reviews/2026-09-25-captcha-resume-audit.md).
// Lever puts an INTERACTIVE hCaptcha on submit. We don't solve captchas, so the form we just
// filled cannot be sent — and this posting is over for us. Two rounds got the channel wrong:
//
//   · the oldest path called it "passive — continuing" and clicked submit into the unsolved
//     challenge → silent fail, Lever = 0 applies ever;
//   · the next one sent DETECTION_TRIPPED — the channel that means "the walk is PARKED and a
//     human is about to clear this wall" — and then ATS_JOB_DONE on the very next line. So
//     background.js persisted captchaWaiting and told the user "we filled the form, open it
//     and press submit yourself", while ATS_JOB_DONE navigated THAT SAME TAB to the next card
//     (advanceAtsQueue → navigatePoolNext → chrome.tabs.update) and destroyed the fill. The
//     banner pointed at a page that no longer existed; the flag had no clearer on this path,
//     so it stood for the rest of the run and muted the watchdogs; and the job itself was
//     written off with NO reason — ATS_JOB_DONE PATCHes "skipped" silently, and only in a
//     pool run, so a first-class Lever run wrote it off nowhere at all.
//
// The channel this path always wanted is handBackJob — the same one the rest of phase_ats
// uses for "we can't finish this one". What must hold:
//   1. An hCaptcha at submit produces a HAND-BACK carrying a reason — not a captchaWaiting
//      promising a resume (invariants 2+4).
//   2. The wording never claims the form is filled and waiting for a submit: the navigation
//      on the next tick destroys it, so that is a claim about state that does not survive.
//   3. The walk ADVANCES (the intentional advance, #72) and the job is written off WITH a
//      reason in a native Lever run too, not just a pool run (invariant 1).
//   4. Nothing on this path writes a hand-off flag — no banner over an abandoned posting,
//      and no mute of the only backstop a native walk has.
//   5. A Lever page with NO hCaptcha still falls through to the real submit path.
//   6. The retired "left_for_you" kind is gone from the extension's CODE — and the real
//      captcha channel, which that kind's removal left holding a dangling reference, still
//      records its hand-off.

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

// Run the REAL code, don't re-describe it: a source-text regex that matches the old shape
// AND the new one proves nothing, and shipping exactly that is why this round exists.
function slice(src, signature, indent) {
  const start = src.indexOf(signature);
  if (start < 0) return null;
  const end = src.indexOf(`\n${indent}}\n`, start);
  if (end < 0) return null;
  return src.slice(start, end + indent.length + 3);
}

// The Lever gate itself, lifted verbatim out of phase_ats. Both the old shape and the new
// one open with these two lines, so this slicer finds either — which is the point: the old
// code loaded into this harness fails the checks below.
function sliceLeverGate(src) {
  const start = src.indexOf('    if (platform === "lever") {\n      const _det = isDetected();');
  if (start < 0) return null;
  const end = src.indexOf("\n    }\n", start);
  if (end < 0) return null;
  return src.slice(start, end + 7);
}

const leverGate = sliceLeverGate(CONTENT);
const handBackFn = slice(CONTENT, "async function handBackJob(reason, extra = {}) {", "  ");
const addKeyFn = slice(CONTENT, "async function addHandedBackKey(title, company) {", "  ");
const dedupFn = slice(CONTENT, "function jobDedupKey(title, company) {", "  ");
const localDayFn = slice(CONTENT, "function localDay() {", "  ");
check("found the Lever hCaptcha gate in phase_ats", !!leverGate);
check("found handBackJob + the keys that stop it re-queueing",
  !!handBackFn && !!addKeyFn && !!dedupFn && !!localDayFn);

// One hCaptcha at submit, driven through the REAL gate and the REAL handBackJob.
async function runLeverGate({ signal }) {
  const sent = [];
  const store = {};
  const sandbox = {
    isDetected: () => ({ detected: !!signal, signal: signal || "" }),
    detectPlatform: () => "lever",
    // The fill is real and irrelevant to the channel: what the gate must get right is WHICH
    // message it sends. The labels are exercised by the hand-back tests that own them.
    collectUnfilledRequired: () => ["hcaptcha"],
    storageGet: async (keys) => {
      const out = {};
      for (const k of [].concat(keys)) if (k in store) out[k] = store[k];
      return out;
    },
    storageSet: async (obj) => { Object.assign(store, obj); },
    sendMsg: async (msg) => { sent.push(msg); return {}; },
    logBackend: async () => {},
    log: () => {},
    window: { location: { href: "https://jobs.lever.co/acme/1234/apply" } },
    // phase_ats's own local, and deliberately provided even though the current gate reads the
    // URL off window.location through handBackJob: the PRE-repair shape read `jobUrl` directly,
    // and this harness has to run that shape to the end so the checks below are what fails on
    // it — not a missing sandbox global.
    jobUrl: "https://jobs.lever.co/acme/1234/apply",
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    `${dedupFn}\n${localDayFn}\n${addKeyFn}\n${handBackFn}\n` +
    `globalThis.__gate = async function (platform, jobTitle, jobCompany) {\n${leverGate}\n  return "fell-through";\n};`,
    sandbox);
  const verdict = await sandbox.__gate("lever", "Staff Engineer", "Acme");
  return { sent, store, verdict };
}

(async () => {
  if (leverGate && handBackFn && addKeyFn && dedupFn && localDayFn) {
    const { sent, store, verdict } = await runLeverGate({ signal: "iframe:hcaptcha.com" });
    check("an hCaptcha at submit stops the submit path", verdict === undefined,
      `gate returned ${JSON.stringify(verdict)} — it must not fall through to humanClick(submitBtn)`);
    const failed = sent.filter((m) => m.type === "ATS_JOB_FAILED");
    check("it hands the job back — exactly one ATS_JOB_FAILED", failed.length === 1,
      `sent: ${JSON.stringify(sent.map((m) => m.type))}`);
    const reason = (failed[0] && failed[0].data && failed[0].data.reason) || "";
    check("the hand-back carries a REASON (the old path wrote the job off with none)",
      /captcha/i.test(reason) && reason.length > 20, JSON.stringify(reason));
    check("…and the reason says nothing was sent", /nothing was sent|not sent|didn't submit/i.test(reason),
      JSON.stringify(reason));
    // Invariant 4. This is the sentence the previous round shipped, and it was false the
    // moment ATS_JOB_DONE navigated the tab.
    check("…and never claims the filled form is waiting for the user's submit",
      !/press submit|hit submit|and submit|awaiting your submit|form is filled|we filled|заполнено/i.test(reason),
      JSON.stringify(reason));
    check("no DETECTION_TRIPPED: nobody is parked, so no 'your turn' hand-off is opened",
      sent.every((m) => m.type !== "DETECTION_TRIPPED"),
      `sent: ${JSON.stringify(sent.map((m) => m.type))}`);
    check("no ATS_JOB_DONE either — the silent, reasonless 'skipped' is gone",
      sent.every((m) => m.type !== "ATS_JOB_DONE"),
      `sent: ${JSON.stringify(sent.map((m) => m.type))}`);
    check("the posting is remembered as handed back, so the next pass won't re-pay for it",
      Array.isArray(store.handedBackKeys) && store.handedBackKeys.includes("staff engineer|acme"),
      JSON.stringify(store.handedBackKeys));

    // 5. The gate must fire ONLY on a real interactive challenge. Greenhouse's invisible
    //    reCAPTCHA auto-solves and Lever pages without a challenge must still be applied to.
    const clean = await runLeverGate({ signal: "" });
    check("a Lever page with no hCaptcha falls through to the real submit path",
      clean.verdict === "fell-through" && clean.sent.length === 0,
      JSON.stringify(clean.sent.map((m) => m.type)));
  }

  // --- background: the hand-back is where the walk advances and the job gets its reason ---
  const tripCase = slice(BG, 'case "DETECTION_TRIPPED": {', "    ");
  const failedCase = slice(BG, 'case "ATS_JOB_FAILED": {', "    ");
  const siteFn = slice(BG, "function platformDisplayNameFromUrl(url) {", "");
  check("found the DETECTION_TRIPPED and ATS_JOB_FAILED handlers", !!tripCase && !!failedCase && !!siteFn);

  function makeBg(store) {
    const calls = { log: [], posted: [], patched: [], advanced: 0, notified: [] };
    const sandbox = {
      chrome: {
        storage: { local: {
          async get(keys) { const o = {}; for (const k of [].concat(keys)) if (k in store) o[k] = store[k]; return o; },
          async set(obj) { Object.assign(store, obj); },
        } },
        notifications: { create: async (o) => { calls.notified.push(o); } },
      },
      addToActivityLog: async (text, cls, meta) => { calls.log.push({ text, cls, meta }); },
      apiPost: async (url, body) => { calls.posted.push({ url, body }); return {}; },
      apiPatch: async (url, body) => { calls.patched.push({ url, body }); return {}; },
      advanceAtsQueue: async () => { calls.advanced++; },
      clearHumanHandoff: async () => { await sandbox.chrome.storage.local.set({ captchaWaiting: null }); },
      URL,
      console,
    };
    vm.createContext(sandbox);
    vm.runInContext(
      `${siteFn}\nglobalThis.__msg = async function (msg, sender) {\n  switch (msg.type) {\n${tripCase}\n${failedCase}\n  }\n  return null;\n};`,
      sandbox);
    return { call: (msg, sender) => sandbox.__msg(msg, sender), calls, store };
  }

  if (tripCase && failedCase && siteFn && leverGate && handBackFn) {
    // The whole path, wired: the real gate's message goes into the real handler. A
    // FIRST-CLASS Lever run — atsPlatform "lever", not "pool" — which is the case the old
    // code wrote off nowhere at all.
    const store = { campaignRunning: true, atsPlatform: "lever", atsQueue: [] };
    const bg = makeBg(store);
    const { sent } = await runLeverGate({ signal: "iframe:hcaptcha.com" });
    for (const m of sent) await bg.call(m, { tab: { id: 42 } });

    const row = bg.calls.posted.find((p) => p.url === "/handbacks");
    check("a native Lever run files the durable to-do row the user can finish from",
      !!row && /captcha/i.test(row.body.reason || "") && !!row.body.url,
      JSON.stringify(bg.calls.posted.map((p) => p.url)));
    check("the walk advances — the intentional advance (#72) survives", bg.calls.advanced === 1,
      `advanced ${bg.calls.advanced} times`);
    check("the feed says whose hands it needs, and why",
      bg.calls.log.some((l) => /Needs your hands/.test(l.text) && /captcha/i.test(l.text)),
      JSON.stringify(bg.calls.log.map((l) => l.text)));
    // Invariant 2+4: no flag, so no banner over a posting the run left behind and no mute of
    // nativeWalkWatchdog — which returns early on a hand-off it can vouch for.
    check("nothing on this path writes a hand-off flag", !("captchaWaiting" in store),
      JSON.stringify(store.captchaWaiting));

    // A POOL run must still be flipped out of `approved` so it never re-queues.
    const poolStore = { campaignRunning: true, atsPlatform: "pool", atsQueue: [{ id: "job-9" }] };
    const poolBg = makeBg(poolStore);
    for (const m of sent) await poolBg.call(m, { tab: { id: 42 } });
    check("a pool run is also flipped out of approved, with its job_id on the row",
      poolBg.calls.patched.some((p) => p.url === "/jobs/job-9/status" && p.body.status === "skipped") &&
      (poolBg.calls.posted.find((p) => p.url === "/handbacks") || {}).body.job_id === "job-9",
      JSON.stringify(poolBg.calls.patched));

    // 6. THE REGRESSION THIS FILE ALSO GUARDS. Removing the "left_for_you" kind left three
    //    reads of a `const` that no longer existed inside DETECTION_TRIPPED. `leftForYou` is
    //    not a global, so a REAL captcha threw a ReferenceError right where the flag is
    //    persisted — and the onMessage listener swallows a rejection into `{ error }`
    //    (background.js:1601), so it threw silently: no hand-off, no local log, no
    //    notification. The human sat at a wall nothing had told them about.
    const wallStore = { campaignRunning: true, campaignTabId: 42 };
    const wallBg = makeBg(wallStore);
    let threw = null;
    try {
      await wallBg.call({ type: "DETECTION_TRIPPED", data: {
        signal: "iframe:hcaptcha.com", url: "https://www.indeed.com/viewjob?jk=abc", phase: "form",
      } }, { tab: { id: 42 } });
    } catch (e) { threw = e; }
    check("a REAL captcha wall does not throw on its way through DETECTION_TRIPPED",
      threw === null, threw && String(threw.message));
    check("…it persists the hand-off, stamped with the tab that holds the human",
      !!wallStore.captchaWaiting && wallStore.captchaWaiting.kind === "captcha" &&
      wallStore.captchaWaiting.tabId === 42 && !!wallStore.captchaWaiting.at,
      JSON.stringify(wallStore.captchaWaiting));
    check("…and tells the user, locally and by notification",
      wallBg.calls.log.some((l) => /human check/i.test(l.text)) && wallBg.calls.notified.length === 1,
      JSON.stringify(wallBg.calls.log.map((l) => l.text)));
    // The consent twin rides the same case and the same flag.
    const termsStore = { campaignRunning: true, campaignTabId: 42 };
    const termsBg = makeBg(termsStore);
    await termsBg.call({ type: "DETECTION_TRIPPED", data: {
      kind: "terms", signal: "dom:accept-terms", url: "https://www.ziprecruiter.com/jobs/x",
      action: "Accept the terms", } }, { tab: { id: 42 } });
    check("the consent wall still records its own kind", termsStore.captchaWaiting &&
      termsStore.captchaWaiting.kind === "terms", JSON.stringify(termsStore.captchaWaiting));
  }

  // The retired kind must be gone from the CONTENT script: nothing may send it any more.
  // background.js still READS it on purpose — an MV3 service-worker update is instant but a
  // content script already injected in an open tab keeps running the old file until that tab
  // navigates, so for one tab-lifetime after this ships a legacy sender still exists.
  const codeLines = (src) => src.split("\n").filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l));
  check("no content script code sends the retired kind \"left_for_you\"",
    !codeLines(CONTENT).some((l) => /left_for_you|leftForYou/.test(l)),
    "the website's branch for it must be reverted too — see notes_for_the_next_reviewer");

  // …and reading it is only worth anything if it is ACTED ON. Mid-repair this handler had the
  // const back and the write unconditional, so a legacy message filed a full captcha hand-off:
  // the banner over an abandoned posting plus a watchdog mute nothing on that path can clear —
  // the exact pair this lane exists to delete.
  if (tripCase && failedCase && siteFn) {
    const legacyStore = { campaignRunning: true, campaignTabId: 42 };
    const legacyBg = makeBg(legacyStore);
    const res = await legacyBg.call({ type: "DETECTION_TRIPPED", data: {
      kind: "left_for_you", signal: "iframe:hcaptcha.com",
      url: "https://jobs.lever.co/acme/1234/apply", phase: "form",
    } }, { tab: { id: 42 } });
    check("a legacy left_for_you message files NO hand-off flag",
      !("captchaWaiting" in legacyStore) && res && res.filed === false,
      JSON.stringify({ store: legacyStore.captchaWaiting, res }));
    check("…and raises no \"campaign paused\" notification for a run that never paused",
      legacyBg.calls.notified.length === 0, JSON.stringify(legacyBg.calls.notified));
    check("…but still names the posting it left behind", legacyBg.calls.log.length === 1 &&
      /not sent/.test(legacyBg.calls.log[0].text), JSON.stringify(legacyBg.calls.log));
    // Invariant 5: this message proves nothing about a wall some OTHER tab holds a human at.
    const liveStore = { campaignRunning: true, campaignTabId: 7,
      captchaWaiting: { url: "https://www.indeed.com/viewjob?jk=abc", site: "Indeed",
                        kind: "captcha", at: Date.now(), tabId: 7 } };
    const liveBg = makeBg(liveStore);
    await liveBg.call({ type: "DETECTION_TRIPPED", data: {
      kind: "left_for_you", signal: "iframe:hcaptcha.com",
      url: "https://jobs.lever.co/acme/1234/apply", phase: "form",
    } }, { tab: { id: 42 } });
    check("…and does not clear a LIVE wall another tab is holding a human at",
      !!liveStore.captchaWaiting && liveStore.captchaWaiting.tabId === 7,
      JSON.stringify(liveStore.captchaWaiting));
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
