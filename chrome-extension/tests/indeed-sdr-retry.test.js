// Indeed: when "Review your resume details" refuses the UPLOADED resume, walk back to
// resume-selection and retry THIS job once with the Indeed Resume. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/indeed-sdr-retry.test.js
//
// The bug (scripts/handback_reasons.py, 14 days to 10-06): 15 Indeed hand-backs at
// resume-module/structured-data-review, 11 of the last 7 days' 32. #320 learned the lesson
// (indeedSdrRefusedAt → preferIndeedResume picks the Indeed Resume), but only for the NEXT
// job: the first refused job on every browser was still handed back.
//
// What is captured and what is synthetic:
//   - resume-selection is Indeed's real form, CAPTURED from the live page (10-02, same
//     fixture as indeed-resume-choice.test.js).
//   - the route order is the live one, from the STEP lines of one content script
//     (Bowtech, 10-05 21:42–21:44Z): resume-selection → questions/1 → intervention →
//     supporting-info → structured-data-intro → structured-data-review.
//   - the back control's label and placement come from the live 🔘 button census
//     (10-05 23:26Z: `button:Go back`, page header, outside <main>).
//   - SYNTHETIC: the pages between resume-selection and the review (one heading + Continue)
//     and the "Go back" element itself (<button aria-label="Go back">) — minimal shapes, no
//     DOM of them was captured. The route change on click is our model of the SPA.

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
const FORM = fs.readFileSync(path.join(__dirname, "fixtures", "indeed-resume-selection.html"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}
function extract(signature) {
  const at = SRC.indexOf(signature);
  if (at < 0) return null;
  const open = SRC.indexOf("{", at + signature.length - 1);
  let depth = 0;
  for (let i = open; i < SRC.length; i++) {
    if (SRC[i] === "{") depth++;
    else if (SRC[i] === "}") { depth--; if (depth === 0) return SRC.slice(at, i + 1); }
  }
  return null;
}
const constOf = (name) => (new RegExp(`const ${name} = ([^;]+);`).exec(SRC) || [])[1];

const fns = {
  prefer: extract("  async function preferIndeedResume(filled) {"),
  claim: extract("  async function claimIndeedSdrRetry(jobInfo, choice) {"),
  jobKey: extract("  function indeedJobKey(jobInfo) {"),
  choiceFor: extract("  async function indeedResumeChoiceFor(jobInfo) {"),
  snap: (extract("  function maskPii(s) {") || "") && extract("  function maskPii(s) {") + "\n" + extract("  function structuredReviewSnapshot() {"),
  findBack: extract("  function findIndeedBackButton() {"),
  walk: extract("  async function walkBackToResumeSelection() {"),
};
check("claimIndeedSdrRetry / indeedResumeChoiceFor / findIndeedBackButton / walkBackToResumeSelection exist in content.js",
  Object.values(fns).every(Boolean) && !!constOf("INDEED_BACK_MAX"),
  Object.entries(fns).filter(([, v]) => !v).map(([k]) => k).join(","));

const BASE = "/beta/indeedapply/form/";
const ROUTES = [
  "resume-selection-module/resume-selection",
  "questions-module/questions/1",
  "questions-module/intervention",
  "questions-module/supporting-info",
  "resume-module/structured-data-intro",
  "resume-module/structured-data-review",
];

// A SmartApply-shaped SPA: header with "Go back" (not on the first step), <main> with the
// step. `back(route)` decides where a Go back click lands (default: the previous route).
function world({ storage = {}, start = ROUTES.length - 1, back, asyncRoute = false, running = true, header } = {}) {
  const dom = new JSDOM("<!doctype html><body><header id=hd></header><main id=mn></main></body>",
    { url: `https://smartapply.indeed.com${BASE}${ROUTES[start]}`, runScripts: "outside-only" });
  const w = dom.window;
  w.HTMLElement.prototype.getClientRects = function () { return [1]; };
  w.__store = { ...storage };
  w.__clicks = 0;
  w.__running = running;
  let idx = start;
  const render = () => {
    const first = idx === 0;
    w.document.getElementById("hd").innerHTML = header != null ? header
      : first ? "" : '<button type="button" aria-label="Go back"><svg></svg></button>';
    w.document.getElementById("mn").innerHTML = first ? FORM
      : `<h1>Step ${idx}</h1><button data-testid="continue-button">Continue</button>`;
  };
  const go = (to) => {
    if (typeof to === "string") { w.history.pushState({}, "", to); w.document.getElementById("mn").innerHTML = "<h1>elsewhere</h1>"; return; }
    idx = to;
    w.history.pushState({}, "", `${BASE}${ROUTES[idx]}`);
    render();
  };
  render();
  w.document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("button");
    if (!b || !/back/i.test(b.getAttribute("aria-label") || b.textContent)) return;
    const to = back ? back(idx) : idx - 1;
    if (to === null) return; // a dead button: the route never changes
    if (asyncRoute) w.setTimeout(() => go(to), 0); else go(to);
  });
  w.detectPlatform = () => "indeed";
  w.storageGet = async (k) => Object.fromEntries((Array.isArray(k) ? k : [k]).map((x) => [x, w.__store[x]]));
  w.storageSet = async (o) => { Object.assign(w.__store, o); return true; };
  w.isCampaignRunning = async () => w.__running;
  w.humanClick = async (el) => { w.__clicks++; el.click(); };
  w.sleep = () => new Promise((r) => w.setTimeout(r, 0)); // yields, so async routes land
  w.humanDelay = () => 0;
  w.eval(`const INDEED_SDR_TTL_MS = ${constOf("INDEED_SDR_TTL_MS")};
const INDEED_BACK_MAX = ${constOf("INDEED_BACK_MAX")};
${fns.prefer}\n${fns.claim}\n${fns.jobKey}\n${fns.choiceFor}\n${fns.findBack}\n${fns.walk}\n${fns.snap}
window.__prefer = preferIndeedResume; window.__snap = structuredReviewSnapshot;
window.__claim = async (job, choice) => claimIndeedSdrRetry(job, choice || await indeedResumeChoiceFor(job));
window.__choice = indeedResumeChoiceFor; window.__key = indeedJobKey;
window.__find = findIndeedBackButton; window.__walk = walkBackToResumeSelection;`);
  return w;
}
const q = (w, id) => w.document.querySelector(`[data-testid="${id}"]`);
const job = { url: "https://www.indeed.com/viewjob?jk=abc123", title: "Event Coordinator", company: "Bowtech Archery" };
const keyOf = (j) => `${j.url || ""}|${j.title || ""}@${j.company || ""}`; // = indeedJobKey (checked below)
// What the upload branch writes for a job: kind, whether the Indeed Resume was offered, and whose.
const uploaded = (j, offered = true) => ({ indeedLastResumeKind: "file", indeedResumeOffered: offered, indeedResumeJob: keyOf(j) });

(async () => {
  if (!Object.values(fns).every(Boolean)) {
    console.log(`\n${failures} failure(s)`);
    process.exit(1);
  }

  // 1. The retry itself: refused review → back to resume-selection → the Indeed Resume.
  {
    const w = world({ storage: { ...uploaded(job), indeedSdrRefusedAt: Date.now() }, asyncRoute: true });
    check("indeedJobKey = url|title@company", w.__key(job) === keyOf(job), w.__key(job));
    check("claims the one retry for this job", await w.__claim(job) === true);
    const ok = await w.__walk();
    check("walks back from structured-data-review to resume-selection", ok && /resume-selection$/.test(w.location.pathname), w.location.pathname);
    check("…one Go back per step (5), nothing else clicked", w.__clicks === 5, `clicks=${w.__clicks}`);
    const filled = [];
    const r = await w.__prefer(filled);
    check("…and on the CAPTURED resume-selection the Indeed Resume is now chosen instead of the upload",
      r.chosen && r.changed && q(w, "resume-selection-structured-resume-radio-card-input").checked &&
      !q(w, "resume-selection-file-resume-radio-card-input").checked && filled.includes("indeed-resume"));
    check("…recorded as kind=indeed (a second refusal will not re-stamp or retry)", w.__store.indeedLastResumeKind === "indeed");
  }

  // 2. One retry per job, and only when it can help.
  {
    const w = world({ storage: uploaded(job) });
    check("first refusal of a job → retry", await w.__claim(job) === true);
    check("…the job key is in storage (survives a page load)", w.__store.indeedSdrRetryJob === keyOf(job));
    check("same job refused again → no second retry (hand back)", await w.__claim(job) === false);
    const other = { ...job, url: "https://www.indeed.com/viewjob?jk=zzz", title: "Other" };
    Object.assign(w.__store, uploaded(other));
    check("a different job that uploaded → its own retry", await w.__claim(other) === true);
    const third = { ...job, url: "u3" };
    Object.assign(w.__store, { indeedLastResumeKind: "indeed", indeedResumeJob: keyOf(third) });
    check("the Indeed Resume itself refused → no retry", await w.__claim(third) === false);
  }
  {
    const w = world({ storage: uploaded(job, false) });
    check("resume-selection offered no Indeed Resume → no retry (it would upload the same file)", await w.__claim(job) === false);
    check("…and nothing is recorded", w.__store.indeedSdrRetryJob === undefined);
  }
  {
    // Skeptic note 2: kind/offered were browser-global, so a job with no upload of its own
    // (draft resumed mid-form) inherited the previous job's "file" + "offered".
    const prev = { ...job, url: "https://www.indeed.com/viewjob?jk=prev", title: "Previous" };
    const w = world({ storage: uploaded(prev) });
    const c = await w.__choice(job);
    check("another job's upload is not this job's: kind unknown, not offered", c.kind === undefined && c.offered === false, JSON.stringify(c));
    check("…so no retry for a job that never uploaded here", await w.__claim(job) === false && w.__store.indeedSdrRetryJob === undefined);
    const mine = await w.__choice(prev);
    check("…while the job that did upload still reads its own choice", mine.kind === "file" && mine.offered === true);
  }

  // 3. Bounds on the walk: it never spins and never clicks anything but "Go back".
  {
    const w = world({ header: "" });
    check("no Go back on the page → false, no click", (await w.__walk()) === false && w.__clicks === 0);
  }
  {
    const w = world({ header: '<button type="button">Back to search</button><a href="/x" role="link">Go back</a><button aria-label="Go back" hidden>x</button>' });
    check("only an exact, shown 'Go back' button counts (not 'Back to search', a link, a hidden one)", w.__find() === null);
  }
  {
    const w = world({ back: () => null });
    check("Go back that doesn't change the route → false after one click", (await w.__walk()) === false && w.__clicks === 1, `clicks=${w.__clicks}`);
  }
  {
    // A form that never reaches resume-selection: every back lands on another form step.
    const w = world({ back: (i) => (i <= 1 ? 5 : i - 1) });
    const max = Number(constOf("INDEED_BACK_MAX"));
    check(`a loop of steps → stops after INDEED_BACK_MAX (${max}) clicks`, (await w.__walk()) === false && w.__clicks === max, `clicks=${w.__clicks}`);
  }
  {
    const w = world({ start: 1, back: () => "/viewjob" });
    check("a Go back that leaves the apply form → false, no further clicks", (await w.__walk()) === false && w.__clicks === 1, `clicks=${w.__clicks}`);
  }
  {
    const w = world({ running: false });
    check("campaign stopped → no click", (await w.__walk()) === false && w.__clicks === 0);
  }

  // 4. Wiring in the step loop (the loop itself needs a live form; its shape is pinned here).
  const stall = SRC.slice(SRC.indexOf("if (stallRounds >= 2) {"), SRC.indexOf("await handBackJob(", SRC.indexOf("if (stallRounds >= 2) {")));
  check("the refusal stamps indeedSdrRefusedAt BEFORE claiming the retry (the return trip must pick the Indeed Resume)",
    /indeedSdrRefusedAt: Date\.now\(\)[\s\S]*claimIndeedSdrRetry\(jobInfo, choice\)/.test(stall));
  check("…logs 'retrying with your Indeed Resume', walks back, resets the stall guard and continues — before any hand-back",
    /retrying with your Indeed Resume[\s\S]{0,200}await walkBackToResumeSelection\(\)\) \{\s*lastSig = ""; stallRounds = 0;[^}]*continue;/.test(stall));
  check("…a failed walk falls through to the hand-back with a line saying so",
    /Couldn't get back to the resume step/.test(stall)); // `stall` ends at the first handBackJob call
  check("the upload records kind + whether the Indeed Resume was offered, tagged with THIS job",
    /indeedLastResumeKind: "file",\s*indeedResumeOffered: !!document\.querySelector\('\[data-testid="resume-selection-structured-resume-radio-card-input"\]'\),\s*indeedResumeJob: indeedJobKey\(jobInfo\) \}\)/.test(SRC));
  check("choosing the Indeed Resume tags the job too",
    /if \(indeedResumeChosen\) await storageSet\(\{ indeedResumeJob: indeedJobKey\(jobInfo\) \}\);/.test(SRC));
  check("the refusal reads THIS job's choice, not the browser's last one",
    /const choice = await indeedResumeChoiceFor\(jobInfo\);\s*const indeedLastResumeKind = choice\.kind;/.test(stall) && /claimIndeedSdrRetry\(jobInfo, choice\)/.test(stall));

  // Skeptic note 1: the retry pass walks the form again; with the first pass's steps it could
  // run past maxSteps=20, and running out of steps was a silent skip with no hand-back.
  check("the retry pass gets its own step budget",
    /let maxSteps = STEP_BUDGET;/.test(SRC) && /stallRounds = 0;[^}]*maxSteps = formStepCount \+ STEP_BUDGET;\s*continue;/.test(stall));
  const tail = SRC.slice(SRC.indexOf("if (formStepCount >= maxSteps"), SRC.indexOf("Form abandoned without submit", SRC.indexOf("if (formStepCount >= maxSteps")));
  check("running out of steps hands the job back with a reason, then advances — no silent skip",
    /if \(formStepCount >= maxSteps && !stoppedEarly\) \{[\s\S]*await handBackJob\(`the form ran past \$\{maxSteps\} steps[\s\S]*await skipToNextJob\(\);\s*return;/.test(tail), tail.slice(0, 200));
  const loop = SRC.slice(SRC.indexOf("  async function _phase3_fillForm() {"), SRC.indexOf("if (formStepCount >= maxSteps"));
  check("…but a break (no button / Submit vanished) still ends as 'abandoned', not as out-of-steps",
    (loop.match(/stoppedEarly = true;\s*break;/g) || []).length === 2 && (loop.match(/\n\s*break;/g) || []).length === 2);
  check("the new keys are user-scoped (cleared on a dashboard user switch)",
    /USER_SCOPED_KEYS = \[[\s\S]*"indeedResumeOffered", "indeedSdrRetryJob", "indeedResumeJob"/.test(BG));
  // No double submit: the retry path clicks only Go back — the walk has no submit/record call.
  check("the walk never records or submits", !/addAppliedUrl|recordLocalApplication|findFormButton|classifyFormButton|APPLICATION_SAVED/.test(fns.walk + fns.claim + fns.findBack));

  // Skeptic note 3: the snapshot showed `education-card-validation-error` but never its text.
  // SYNTHETIC page: testids are the live ones from the 10-05 21:44Z `🧾 sdr` line; the error
  // wording is invented (the live text was never captured — that is what this logs now).
  {
    const long = "Add the dates you attended this school so employers can see your timeline ".repeat(3);
    const w = world({}); // any page; the snapshot reads <main>
    w.document.getElementById("mn").innerHTML = `<div data-testid="structured-data-review-page">
      <section data-testid="structured-data-review-page-education-section"><ul data-testid="education-list">
        <li data-testid="education-card"><h3 data-testid="education-card-title">B.S. Marketing</h3>
          <div data-testid="education-card-place">University of Hawaii</div>
          <div data-testid="education-card-validation-error">Add dates of attendance</div>
          <button data-testid="education-card-edit-btn">Edit B.S.</button></li>
        <li data-testid="education-card"><div data-testid="education-card-validation-error">${long} jane@x.com (808) 555-0123</div></li>
      </ul></section><button data-testid="continue-button">Continue</button></div>`;
    const line = w.__snap();
    check("snapshot logs Indeed's validation-error text with its testid",
      /errs=\[[^\]]*"education-card-validation-error:Add dates of attendance"/.test(line), line.slice(0, 300));
    const errs = JSON.parse(/errs=(\[[^\]]*\])/.exec(line)[1]);
    check("…each error capped at 120 chars, contacts masked", errs.length === 2 && errs.every((e) => e.split(":").slice(1).join(":").length <= 120) &&
      !/jane@|555/.test(line), JSON.stringify(errs));
    check("…the card itself stays out (title, school)", !/B\.S\. Marketing"|University of Hawaii/.test(line), line);
    check("…and comes before acts/ids (the 1950-char slice cuts ids first)", line.indexOf("errs=") < line.indexOf("acts=") && line.indexOf("acts=") < line.indexOf("ids="));
  }

  console.log(failures ? `\n${failures} failure(s)` : "\nall good");
  process.exit(failures ? 1 : 0);
})();
