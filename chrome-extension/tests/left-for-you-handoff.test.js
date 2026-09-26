// Fixture test for the "we filled it and moved on" hand-off — ../background.js's
// DETECTION_TRIPPED handler and the Lever branch in ../content.js. No JS test runner in
// this repo (see consent-gate.test.js for the reasoning); run it by hand:
//
//   node <repo>/jobflow/chrome-extension/tests/left-for-you-handoff.test.js
//
// Why it exists (audit 09-25, findings 3+5, docs/reviews/2026-09-25-captcha-resume-audit.md).
// A real hCaptcha on a FILLED jobs.lever.co form used to send a plain DETECTION_TRIPPED
// and then ATS_JOB_DONE on the very next line. Two things followed:
//   - background.js persisted captchaWaiting and told the user "Open the automation window,
//     solve it, and the campaign resumes automatically" — while ATS_JOB_DONE PATCHed the job
//     to "skipped" and navigated the SAME tab to the next card. The fill was gone; nothing
//     could ever resume on that posting. The banner promised a resume for an abandoned job.
//   - DETECTION_CLEARED is never sent on this path (its only senders are the two in-page
//     pause loops), and the flag's own contract at background.js says it is "Cleared by
//     DETECTION_CLEARED, START_CAMPAIGN and STOP_CAMPAIGN". So it stood for the rest of the
//     run, muting nativeWalkWatchdog (`if (d.captchaWaiting || d.reviewPending) return;`)
//     and the dashboard's 12-min idle alarm.
//
// What must hold now:
//   1. content.js marks this hand-off kind:"left_for_you" — the walk is NOT parked.
//   2. The advance survives (#72): DETECTION_TRIPPED is still followed by ATS_JOB_DONE.
//   3. background.js writes NO captchaWaiting for left_for_you, and clears a stale one
//      through the shared clearHumanHandoff() (490a02e) — so no flag can mute a watchdog.
//   4. Nothing on that path claims a pause or an automatic resume, and the user gets the
//      URL to finish the application themselves.
//   5. The two REAL pauses are untouched: a captcha and a terms wall still persist the
//      flag with the resume wording. Over-muting them would throw away a human's work.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
const CONTENT = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function ok(name, cond, detail) {
  if (!cond) failures++;
  console.log(`  ${cond ? "ok " : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

// ---- The real handler, lifted out of the message switch ------------------------------
const START = BG.indexOf('case "DETECTION_TRIPPED": {');
const TAIL = "return { handled: true };";
const END = BG.indexOf(TAIL, START);
if (START < 0 || END < 0) {
  console.error("Could not locate the DETECTION_TRIPPED case in background.js — markers moved.");
  process.exit(2);
}
// The case body becomes a function body: swap the `case` line for a signature and close it
// ourselves, so a new `case` added after this one can't leak into the slice.
const HANDLER = BG.slice(START, END + TAIL.length).replace(
  'case "DETECTION_TRIPPED": {',
  "async function handleDetectionTripped(msg) {"
) + "\n}";
// The display-name helper is real too, so the copy under test is the copy users read.
const NAMER = BG.slice(BG.indexOf("function platformDisplayNameFromUrl(url) {"),
  BG.indexOf("async function sendScreenshot(tabId)"));

async function run(data) {
  const calls = { stored: [], cleared: 0, activity: [], localLog: [], notified: [] };
  const box = {
    apiPost: async (p, body) => { calls.activity.push(body); return {}; },
    addToActivityLog: async (text, level) => { calls.localLog.push({ text, level }); },
    clearHumanHandoff: async () => { calls.cleared += 1; },
    chrome: {
      storage: { local: { set: async (obj) => { calls.stored.push(obj); } } },
      notifications: { create: async (n) => { calls.notified.push(n); } },
    },
    Date,
    String,
  };
  vm.createContext(box);
  vm.runInContext(`${NAMER}\n${HANDLER}`, box);
  await box.handleDetectionTripped({ data });
  return calls;
}

const LEVER_URL = "https://jobs.lever.co/acme/1234/apply";

(async () => {
  // ---- 1..4: the Lever "left for you" hand-off --------------------------------------
  {
    const calls = await run({
      signal: 'dom:iframe[title*="hcaptcha" i]',
      url: LEVER_URL,
      phase: "form",
      job_title: "Event Manager",
      company: "Acme",
      needs_captcha: true,
      kind: "left_for_you",
    });

    const flagWrites = calls.stored.filter((o) => "captchaWaiting" in o);
    ok("no captchaWaiting is written for a walk that moved on",
      flagWrites.length === 0,
      `wrote ${JSON.stringify(flagWrites)} — a flag nothing on this path can clear`);
    ok("a stale hand-off is cleared through the shared clearer",
      calls.cleared === 1, `clearHumanHandoff() called ${calls.cleared}x`);

    const text = [
      ...calls.localLog.map((l) => l.text),
      ...calls.notified.map((n) => `${n.title} ${n.message}`),
      ...calls.activity.map((a) => a.message),
    ].join(" | ");
    ok("nothing claims an automatic resume", !/resumes automatically/i.test(text), text);
    ok("nothing claims the campaign is paused", !/paused/i.test(text), text);
    ok("the user gets the link to finish it themselves", text.includes(LEVER_URL), text);
    ok("it names the platform, not 'the job site'", /Lever/.test(text), text);
    ok("the walk is not filed as a run error",
      calls.activity.every((a) => a.level !== "error") &&
      calls.localLog.every((l) => l.level !== "err"),
      JSON.stringify(calls.activity.map((a) => a.level).concat(calls.localLog.map((l) => l.level))));
    ok("the backend row still carries the kind for later forensics",
      calls.activity.some((a) => a.metadata && a.metadata.kind === "left_for_you"));
  }

  // ---- 5: the REAL pauses must keep their flag and their promise ---------------------
  // Invariant 5 of the audit: do not over-mute. A human actually solving a captcha in the
  // automation window must still be protected from a watchdog reload.
  {
    const calls = await run({ signal: "url:challenge", url: "https://www.indeed.com/viewjob?jk=1", phase: "search" });
    const flag = calls.stored.find((o) => "captchaWaiting" in o);
    ok("a real captcha still persists the hand-off", !!flag && !!flag.captchaWaiting);
    ok("a real captcha is still kind 'captcha'", flag && flag.captchaWaiting.kind === "captcha");
    ok("a real captcha still promises the resume",
      calls.notified.some((n) => /resumes automatically/.test(n.message)));
    ok("a real captcha does not clear the hand-off it just set", calls.cleared === 0);
  }
  {
    const calls = await run({ signal: "dom:consent", url: "https://www.indeed.com/", phase: "search", kind: "terms", action: "Accept the updated terms" });
    const flag = calls.stored.find((o) => "captchaWaiting" in o);
    ok("a terms wall still persists the hand-off", !!flag && flag.captchaWaiting.kind === "terms");
    ok("a terms wall still promises the resume",
      calls.notified.some((n) => /resumes automatically/.test(n.message)));
  }

  // ---- content.js: the sender says which shape this is, and still advances -----------
  {
    // Anchor on the branch's own comment — phase_ats has an earlier `platform === "lever"`
    // block (the resume-field quirk), so a bare indexOf lands in the wrong one.
    const s = CONTENT.indexOf("// LEVER hCaptcha — we do NOT solve captchas");
    const e = CONTENT.indexOf("await sleep(humanDelay(2000, 5000));", s);
    ok("found the Lever hCaptcha branch in content.js", s > -1 && e > s);
    const branch = CONTENT.slice(s, e);
    ok("the Lever hand-off is marked left_for_you",
      /DETECTION_TRIPPED[\s\S]*kind: "left_for_you"/.test(branch),
      "without the kind, background.js gives it the captcha treatment again");
    const t = branch.indexOf("DETECTION_TRIPPED");
    const a = branch.indexOf("ATS_JOB_DONE");
    ok("the advance survives (#72): notify, then move on", t > -1 && a > t,
      "the fix is about honesty, not about freezing the walk");
    ok("it still does not submit into an unsolved captcha", !/clickSubmit|submitBtn\.click/.test(branch));
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
