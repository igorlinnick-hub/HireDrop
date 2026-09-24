// Fixture test for the pre-click BASELINE in submission verification (../content.js).
// Same manual run as detection-visibility.test.js:
//
//   node chrome-extension/tests/run-all.js       (or this file directly with jsdom on NODE_PATH)
//
// The bug (2 confirmed, one disease): "verified applied" was recorded from text that
// was on the page BEFORE the submit click.
//   1. waitForSubmissionConfirmation scanned document.body for SUCCESS_TEXTS with no
//      baseline — Greenhouse/Lever/Ashby render the job description (with "thank you
//      for your interest" / "we'll be in touch" boilerplate) on the apply page itself,
//      so a validation-blocked submit "verified" on the first beat and the honesty
//      hand-back (gated on !result.verified) never ran. Dedup recorded pre-click made
//      the loss permanent.
//   2. detectSilentSubmission's mid-flow guard only recognised dialogs WITH fields —
//      ZR's bare [Close]-only shell between steps and Indeed's dialog-less SmartApply
//      pages slipped past it, and jobLooksApplied's `|| document` fallback confirmed
//      the current job off a NEIGHBOUR card's "Applied" badge.
// Direction of safety: missing a real confirmation (applied_unconfirmed) is
// acceptable; a false verified is not.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

// ---- Extract the real functions from content.js --------------------------------------
function slice(startMarker, endMarker) {
  const s = SRC.indexOf(startMarker);
  const e = SRC.indexOf(endMarker, s);
  if (s < 0 || e < 0) {
    console.error(`Could not locate [${startMarker}] .. [${endMarker}] — markers moved.`);
    process.exit(2);
  }
  return SRC.slice(s, e);
}

const CODE =
  slice("  const POSTAPPLY_URL_HINTS", "  // React-compatible field filling") +
  slice("  const FIELDISH_SELECTOR", "  // Compact \"what modals are on screen\"") +
  slice("  function isFormVisible()", "  function findResumeInput()") +
  slice("  function jobLooksApplied()", "  // Record a submission that the platform accepted");

function makeSandbox(html, url) {
  const { window } = new JSDOM(`<body>${html}</body>`, {
    url: url || "https://www.ziprecruiter.com/jobs-search?search=welder",
  });
  // jsdom has no layout engine, so offsetParent is null for everything. The functions
  // under test use it as a visibility gate; emulate "visible unless data-hidden".
  Object.defineProperty(window.HTMLElement.prototype, "offsetParent", {
    get() { return this.closest("[data-hidden]") ? null : this.ownerDocument.body; },
  });
  const sandbox = {
    window,
    document: window.document,
    sleep: async () => {}, // instant — loops are bounded by their real-time timeout
    result: null,
    done: null,
  };
  vm.createContext(sandbox);
  return sandbox;
}

async function run(sandbox, expr) {
  vm.runInContext(CODE + `\ndone = (async () => (${expr}))();`, sandbox);
  return await sandbox.done;
}

// The exact wording seen on real ATS apply pages — present BEFORE any click.
const GH_APPLY_PAGE = `
  <h1>Senior Welder</h1>
  <div class="job-desc">
    Thank you for your interest in Acme! We'll be in touch after reviewing
    your application to Acme. Apply below.
  </div>
  <form action="/apply"><input name="first_name"><button type="submit">Submit application</button></form>`;

(async () => {
  // ---- Bug 1: waitForSubmissionConfirmation ------------------------------------------
  {
    // Boilerplate that was on the page before the click must NOT verify.
    const sb = makeSandbox(GH_APPLY_PAGE, "https://boards.greenhouse.io/acme/jobs/1");
    const baseline = sb.document.body.textContent;
    const r = await run(sb, `waitForSubmissionConfirmation(300, { baselineText: ${JSON.stringify(baseline)} })`);
    check("pre-existing boilerplate does NOT verify", r.verified === false,
      `got verified=${r.verified} signal=${r.signal}`);
    check("unverified comes back as timeout, not a text signal", r.signal === "timeout", r.signal);
  }
  {
    // Genuinely NEW confirmation text (absent from the baseline) must verify.
    const sb = makeSandbox(`<div>Your application has been submitted to Acme.</div>`,
      "https://boards.greenhouse.io/acme/jobs/1");
    const r = await run(sb,
      `waitForSubmissionConfirmation(300, { baselineText: "Senior Welder job description" })`);
    check("genuinely new confirmation text DOES verify", r.verified === true,
      `got verified=${r.verified} signal=${r.signal}`);
    check("signal names the text channel", /^text:/.test(r.signal || ""), r.signal);
  }
  {
    // Defensive fallback: no baseline passed → snapshot NOW → text already on the
    // page still can't verify (fail toward unverified, never toward false verified).
    const sb = makeSandbox(GH_APPLY_PAGE, "https://boards.greenhouse.io/acme/jobs/1");
    const r = await run(sb, `waitForSubmissionConfirmation(300)`);
    check("no-baseline fallback does not verify pre-existing text", r.verified === false,
      `got verified=${r.verified} signal=${r.signal}`);
  }

  // ---- Bug 2: detectSilentSubmission mid-flow shapes ---------------------------------
  {
    // ZR bare [Close]-only shell between steps, with a neighbour card already
    // "Applied" and boilerplate on the page behind the modal: must NOT silent-confirm.
    const sb = makeSandbox(`
      <ul class="job-list"><li><h3>Other Job</h3><button>Applied 09/20</button></li></ul>
      <div data-testid="right-pane"><h2>Current Job</h2>
        <p>We'll be in touch. Thank you for your interest.</p></div>
      <div role="dialog"><button>Close</button></div>`);
    const baseline = ""; // worst case: nothing baselined — the guard alone must hold
    const r = await run(sb, `detectSilentSubmission(300, ${JSON.stringify(baseline)})`);
    check("mid-flow bare-shell does not silent-confirm", r === null, `got ${r}`);
  }
  {
    // Dialog-less SmartApply step page (Indeed renders steps as full pages): the form
    // is still on screen → mid-flow, no confirmation.
    const sb = makeSandbox(`
      <div class="ia-BasePage"><input name="q1"><button>Continue</button></div>
      <div>We've received your application</div>`,
      "https://smartapply.indeed.com/beta/indeedapply/form/questions");
    const r = await run(sb, `detectSilentSubmission(300, "")`);
    check("dialog-less form page does not silent-confirm", r === null, `got ${r}`);
  }
  {
    // The 08-15 fix must keep working: modal closed, the CURRENT job's pane flipped
    // to "Applied" → still detected.
    const sb = makeSandbox(`
      <div data-testid="right-pane"><h2>Current Job</h2><button>Applied</button></div>`);
    const r = await run(sb, `detectSilentSubmission(300, "")`);
    check("current job's own Applied badge still confirms", r === "applied-badge", `got ${r}`);
  }

  // ---- Bug 2: jobLooksApplied scoping ------------------------------------------------
  {
    // Neighbour card's badge with NO right-pane match for the current job.
    const sb = makeSandbox(`
      <ul class="job-list"><li><h3>Other Job</h3><button>Applied 09/20</button></li></ul>
      <div data-testid="right-pane"><h2>Current Job</h2><button aria-label="Quick Apply">Quick Apply</button></div>`);
    const r = await run(sb, `jobLooksApplied()`);
    check("neighbour card's Applied badge does not confirm", r === false, `got ${r}`);
  }
  {
    // No pane at all (badge only in the list) → answer "not applied", never scan
    // the whole document.
    const sb = makeSandbox(`
      <ul class="job-list"><li><button>Applied 09/20</button></li></ul>`);
    const r = await run(sb, `jobLooksApplied()`);
    check("without a right-pane the document is not scanned", r === false, `got ${r}`);
  }

  // ---- STRUCTURAL: every trigger threads its pre-click baseline ----------------------
  check("phase3 submit passes a baseline",
    /waitForSubmissionConfirmation\(45000, \{ baselineText \}\)/.test(SRC),
    "the explicit-submit call site must thread the pre-click snapshot");
  check("phase_ats submit passes a baseline",
    /waitForSubmissionConfirmation\(45000, \{ submitBtn, baselineText \}\)/.test(SRC),
    "the ATS call site must thread the pre-click snapshot");
  check("the silent-submit check passes a baseline",
    /detectSilentSubmission\(nextStepShowing \? 2500 : 9000, baselineText\)/.test(SRC),
    "the Continue-click site must thread the pre-click snapshot");
  check("each baseline is snapshotted BEFORE its click",
    (SRC.match(/const baselineText = document\.body\.textContent \|\| ""/g) || []).length === 3,
    "expected 3 pre-click snapshots (phase3 submit, phase3 continue, phase_ats)");

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
