// A refused form step must leave behind what the PAGE said was wrong. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/form-blockers.test.js
//
// What went wrong (prod activity_log, 09-06…10-01): 17 of 29 Indeed hand-backs read
// "Continue refused 3× with nothing left to fill" and carried no clue at all.
// collectUnfilledRequired() returns [] on a page-level form (no dialog → no fallback, ARIA
// widgets dropped), and the pre-hand-back line was the literal "🖐 dialogs=0". 14 of the 17
// died on smartapply …/resume-module/structured-data-review, which renders the user's parsed
// resume, so the capture must name fields without ever reading their values.
//
// The markup below is generic ARIA (alert, aria-invalid + aria-describedby, aria-required
// widgets), not a copy of Indeed's DOM: this pins what formBlockers() extracts from those
// standard signals, not a claim about which signal Indeed's step uses.

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

function extract(signature) {
  const at = SRC.indexOf(signature);
  if (at < 0) return null;
  const open = SRC.indexOf("{", at);
  let depth = 0;
  for (let i = open; i < SRC.length; i++) {
    if (SRC[i] === "{") depth++;
    else if (SRC[i] === "}") {
      depth--;
      if (depth === 0) return SRC.slice(at, i + 1);
    }
  }
  return null;
}

const parts = {
  formBlockers: extract("  function formBlockers() {"),
  formBlockersLine: extract("  function formBlockersLine(fb) {"),
  getFieldLabel: extract("  function getFieldLabel(el) {"),
  visibleApplyDialogs: extract("  function visibleApplyDialogs() {"),
  formScope: extract("  function formScope() {"),
};
for (const [k, v] of Object.entries(parts)) check(`${k} exists`, !!v);
const fieldish = SRC.match(/const FIELDISH_SELECTOR =\s*\n?\s*'[^']*';/);
check("FIELDISH_SELECTOR exists", !!fieldish);
if (failures) process.exit(1);

function load(html, { url, classify } = {}) {
  const dom = new JSDOM(`<!doctype html><body>${html}</body>`, {
    url: url || "https://smartapply.indeed.com/beta/indeedapply/form/resume-module/structured-data-review?jk=abc&token=xyz",
    runScripts: "outside-only",
  });
  const w = dom.window;
  // jsdom has no layout: getClientRects() is always empty. Treat everything as visible
  // except what sits under [hidden].
  w.Element.prototype.getClientRects = function () { return this.closest("[hidden]") ? [] : [{}]; };
  Object.defineProperty(w.HTMLElement.prototype, "offsetParent", {
    get() { return this.closest("[hidden]") ? null : this.parentElement; },
  });
  // Chrome has CSS.escape; jsdom does not.
  w.CSS = { escape: (s) => String(s).replace(/["\\\]\[#.:]/g, "\\$&") };
  w.classifyFormButton = classify || (() => {
    const btn = w.document.querySelector("button");
    return btn ? { btn, submit: false, label: btn.textContent.trim() } : { btn: null, submit: false, label: "" };
  });
  w.eval(`${fieldish[0]}\n${parts.getFieldLabel}\n${parts.visibleApplyDialogs}\n${parts.formScope}\n` +
    `${parts.formBlockers}\n${parts.formBlockersLine}\n` +
    "window.__fb = formBlockers; window.__line = formBlockersLine;");
  return w;
}

// ---- 1. A page-level form: every signal is picked up, no value leaks ------------------
{
  const w = load(`
    <header><label for="q">Search</label><input id="q" required></header>
    <main>
      <div role="alert">Please answer the questions below.</div>
      <label for="job">Job title</label>
      <input id="job" value="SECRET-TITLE-VALUE" aria-invalid="true" aria-describedby="job-err">
      <span id="job-err">Enter a valid job title</span>
      <div role="radiogroup" aria-required="true" aria-label="Are you authorized to work in the US?">
        <div role="radio" aria-checked="false">Yes</div><div role="radio" aria-checked="false">No</div>
      </div>
      <div role="combobox" aria-required="true" aria-label="Gender">Select an option</div>
      <div role="combobox" aria-required="true" aria-label="Veteran status"><span aria-selected="true">I am not a veteran</span></div>
      <label for="fn">First name</label><input id="fn" required value="Jane-Private">
      <div hidden><div role="alert">Hidden old error</div></div>
      <button disabled>Continue</button>
    </main>`);
  const fb = w.__fb();
  const json = JSON.stringify(fb);
  check("path is host + pathname, no query string",
    fb.path === "smartapply.indeed.com/beta/indeedapply/form/resume-module/structured-data-review", json);
  check("the page's alert text is captured", fb.alerts.includes("Please answer the questions below."), json);
  check("an invalid field's own error message is captured", fb.alerts.includes("Enter a valid job title"), json);
  check("the invalid field is named by its label", fb.invalid.includes("Job title"), json);
  check("a required ARIA radiogroup with nothing checked is reported",
    fb.reqEmpty.some((s) => s.startsWith("Are you authorized") && s.includes("(radiogroup)")), json);
  check("a required combobox still on its placeholder is reported",
    fb.reqEmpty.some((s) => s.startsWith("Gender") && s.includes("(combobox)")), json);
  check("an answered combobox is NOT reported", !fb.reqEmpty.some((s) => s.startsWith("Veteran")), json);
  check("a filled required input is NOT reported", !fb.reqEmpty.some((s) => /first name/i.test(s)), json);
  check("fields outside the form scope (header search) are ignored", !json.includes("Search"), json);
  check("hidden alerts are ignored", !json.includes("Hidden old error"), json);
  check("the button state is captured", fb.btn && fb.btn.label === "Continue" && fb.btn.disabled === true, json);
  check("no field VALUE ever leaks", !json.includes("SECRET-TITLE-VALUE") && !json.includes("Jane-Private"), json);
  const line = w.__line(fb);
  check("the one-line form carries path, alerts and the disabled button",
    line.includes("path=smartapply.indeed.com/") && line.includes("Please answer") && line.includes('"Continue"(disabled)'), line);
}

// ---- 2. Inside an apply dialog, the page behind it is out of scope -------------------
{
  const w = load(`
    <main><div role="alert">Board-level banner</div></main>
    <div role="dialog"><label for="ph">Phone</label><input id="ph" required><button>Next</button></div>`);
  const fb = w.__fb();
  check("dialog scope: the empty required phone is reported", fb.reqEmpty.some((s) => s.startsWith("Phone")), JSON.stringify(fb));
  check("dialog scope: the board's own alert is not", !fb.alerts.includes("Board-level banner"), JSON.stringify(fb));
}

// ---- 2b. Yes/No questions are named by the QUESTION, and echoed values are masked -----
{
  const w = load(`<main>
    <fieldset><legend>Are you 18 or older?</legend>
      <label><input type="radio" name="q1" required> Yes</label><label><input type="radio" name="q1" required> No</label></fieldset>
    <fieldset><legend>Willing to relocate?</legend>
      <label><input type="radio" name="q2" required aria-invalid="true"> Yes</label><label><input type="radio" name="q2" required> No</label></fieldset>
    <div role="alert">jane.doe@example.com is not a valid address. Phone (808) 555-1234 is invalid.</div>
    <button>Continue</button></main>`);
  const fb = w.__fb();
  const json = JSON.stringify(fb);
  check("an unanswered radio question is reported once, by its question",
    fb.reqEmpty.filter((s) => s.startsWith("Are you 18 or older?")).length === 1 && !fb.reqEmpty.includes("Yes") && !fb.reqEmpty.includes("No"), json);
  check("an invalid radio is named by its question too", fb.invalid.includes("Willing to relocate?"), json);
  check("an email echoed in a page message is masked", !json.includes("jane.doe@example.com") && json.includes("<email>"), json);
  check("a phone number echoed in a page message is masked", !json.includes("555-1234") && json.includes("<num>"), json);
}

// ---- 2c. Size: the logged lines stay under the backend's 2000-char message cap ---------
{
  check("the whole 🖐 line is capped at 1950 code points",
    /logBackend\(Array\.from\(`🖐 \$\{dialogSnapshot\(\)\} \$\{formBlockersLine\(formBlockers\(\)\)\}`\)\.slice\(0, 1950\)/.test(SRC));
  check("the whole STEP line is capped at 1950 code points",
    /logBackend\(Array\.from\(`STEP [\s\S]{0,600}?`\)\.slice\(0, 1950\)\.join\(""\)/.test(SRC));
  const w = load("<main><button>Continue</button></main>",
    { url: "https://smartapply.indeed.com/" + "a/".repeat(400) });
  check("a very long path is clipped so diag stays under background's 3000-char guard",
    Array.from(w.__fb().path).length <= 160 && JSON.stringify(w.__fb()).length < 3000);
}

// ---- 2d. Scope on a page-level form; toasts; polite regions; fallback notes ------------
{
  const w = load(`
    <header><form role="search"><label for="s">What</label><input id="s" required></form></header>
    <main><form id="apply">
      <label for="c">City</label><input id="c" required>
      <div aria-live="polite">519 / 1500</div><div aria-live="polite">page 1 of 2</div>
      <div aria-live="polite">Select an answer to continue</div>
      <button>Continue</button></form></main>
    <div role="alert">Something went wrong, try again</div>`);
  const fb = w.__fb();
  const json = JSON.stringify(fb);
  check("a header search form does not steal the scope", fb.reqEmpty.some((s) => s.startsWith("City")) && !json.includes("What"), json);
  check("a toast portaled to the end of <body> is still caught", fb.alerts.includes("Something went wrong, try again"), json);
  check("a polite-region validation message is caught", fb.alerts.includes("Select an answer to continue"), json);
  check("counters in polite regions are not", !json.includes("519 / 1500") && !json.includes("page 1 of 2"), json);
  check("no fallback notes when real signals exist", fb.notes.length === 0, json);
}
{
  const w = load(`<main><h1>Please review your resume details</h1>
    <div class="card"><div class="card-warning">Missing information</div><button>Edit</button></div>
    <button>Continue</button></main>`);
  const fb = w.__fb();
  check("with no ARIA signal at all, the page's warning text and heading are kept as notes",
    fb.notes.includes("Missing information") && fb.notes.includes("Please review your resume details"), JSON.stringify(fb));
  check("…and they reach the one-line form", w.__line(fb).includes('notes=["Missing information"'), w.__line(fb));
}

// ---- 3. Diagnostics must never break the hand-back -----------------------------------
{
  const w = load("<main><button>Continue</button></main>", { classify: () => { throw new Error("boom"); } });
  const fb = w.__fb();
  check("a throw inside the capture returns {error} instead of propagating", fb && fb.error === "boom", JSON.stringify(fb));
  check("the line degrades to blockers=?", w.__line(fb).startsWith("blockers=? (boom)"), w.__line(fb));
}

// ---- 4. Wiring: hand-back → activity log only, never the user-facing questions --------
{
  // Fixed window, not brace-matching: the default `extra = {}` would end the match early.
  const hbAt = SRC.indexOf("  async function handBackJob(reason, extra = {}) {");
  const hb = hbAt < 0 ? "" : SRC.slice(hbAt, SRC.indexOf("\n  }\n", hbAt));
  check("handBackJob sends diag (guarded for the vm-sandboxed tests)",
    /diag: typeof formBlockers === "function" \? formBlockers\(\) : null/.test(hb), hb.slice(0, 200));
  const at = BG.indexOf('case "ATS_JOB_FAILED": {');
  const caseSrc = BG.slice(at, BG.indexOf("questions: unfilled,", at) + 40);
  check("background puts diag into the activity-log metadata", /\{ diag: f\.diag \}/.test(caseSrc));
  check("the /handbacks questions stay the unfilled labels only", /questions: unfilled,/.test(caseSrc) && !/questions:[^\n]*diag/.test(caseSrc));
}

console.log(failures ? `\n${failures} failure(s)` : "\nall good");
process.exit(failures ? 1 : 0);
