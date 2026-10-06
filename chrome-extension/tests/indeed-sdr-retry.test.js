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
  claim: extract("  async function claimIndeedSdrRetry(jobInfo, resumeKind) {"),
  findBack: extract("  function findIndeedBackButton() {"),
  walk: extract("  async function walkBackToResumeSelection() {"),
};
check("claimIndeedSdrRetry / findIndeedBackButton / walkBackToResumeSelection exist in content.js",
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
${fns.prefer}\n${fns.claim}\n${fns.findBack}\n${fns.walk}
window.__prefer = preferIndeedResume; window.__claim = claimIndeedSdrRetry;
window.__find = findIndeedBackButton; window.__walk = walkBackToResumeSelection;`);
  return w;
}
const q = (w, id) => w.document.querySelector(`[data-testid="${id}"]`);
const job = { url: "https://www.indeed.com/viewjob?jk=abc123", title: "Event Coordinator", company: "Bowtech Archery" };

(async () => {
  if (!Object.values(fns).every(Boolean)) {
    console.log(`\n${failures} failure(s)`);
    process.exit(1);
  }

  // 1. The retry itself: refused review → back to resume-selection → the Indeed Resume.
  {
    const w = world({ storage: { indeedLastResumeKind: "file", indeedResumeOffered: true, indeedSdrRefusedAt: Date.now() }, asyncRoute: true });
    check("claims the one retry for this job", await w.__claim(job, "file") === true);
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
    const w = world({ storage: { indeedResumeOffered: true } });
    check("first refusal of a job → retry", await w.__claim(job, "file") === true);
    check("…the job key is in storage (survives a page load)", typeof w.__store.indeedSdrRetryJob === "string" && w.__store.indeedSdrRetryJob.includes("abc123"));
    check("same job refused again → no second retry (hand back)", await w.__claim(job, "file") === false);
    check("a different job → its own retry", await w.__claim({ ...job, url: "https://www.indeed.com/viewjob?jk=zzz", title: "Other" }, "file") === true);
    check("the Indeed Resume itself refused → no retry", await w.__claim({ ...job, url: "u3" }, "indeed") === false);
  }
  {
    const w = world({ storage: { indeedResumeOffered: false } });
    check("resume-selection offered no Indeed Resume → no retry (it would upload the same file)", await w.__claim(job, "file") === false);
    check("…and nothing is recorded", w.__store.indeedSdrRetryJob === undefined);
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
    /indeedSdrRefusedAt: Date\.now\(\)[\s\S]*claimIndeedSdrRetry\(jobInfo, indeedLastResumeKind\)/.test(stall));
  check("…logs 'retrying with your Indeed Resume', walks back, resets the stall guard and continues — before any hand-back",
    /retrying with your Indeed Resume[\s\S]{0,200}await walkBackToResumeSelection\(\)\) \{\s*lastSig = ""; stallRounds = 0;[^}]*continue;/.test(stall));
  check("…a failed walk falls through to the hand-back with a line saying so",
    /Couldn't get back to the resume step/.test(stall)); // `stall` ends at the first handBackJob call
  check("the upload records whether resume-selection offered the Indeed Resume",
    /indeedLastResumeKind: "file",\s*indeedResumeOffered: !!document\.querySelector\('\[data-testid="resume-selection-structured-resume-radio-card-input"\]'\)/.test(SRC));
  check("the new keys are user-scoped (cleared on a dashboard user switch)",
    /USER_SCOPED_KEYS = \[[\s\S]*"indeedResumeOffered", "indeedSdrRetryJob"/.test(BG));
  // No double submit: the retry path clicks only Go back — the walk has no submit/record call.
  check("the walk never records or submits", !/addAppliedUrl|recordLocalApplication|findFormButton|classifyFormButton|APPLICATION_SAVED/.test(fns.walk + fns.claim + fns.findBack));

  console.log(failures ? `\n${failures} failure(s)` : "\nall good");
  process.exit(failures ? 1 : 0);
})();
