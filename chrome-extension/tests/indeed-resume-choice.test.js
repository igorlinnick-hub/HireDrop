// Indeed: after a parsed upload is refused at "Review your resume details", pick the Indeed
// Resume instead of uploading. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/indeed-resume-choice.test.js
//
// What went wrong (live run 10-02, 21:21–21:46Z): every application where we uploaded the
// PDF (filled=[resume]) went resume-selection → structured-data-intro → structured-data-review
// → "Continue" refused ×3 → hand-back. 3/3, and 14 of 17 blind hand-backs before it. The one
// application that went through never uploaded. The review step shows no alert, aria-invalid
// or empty required input, so the run also logs the page structure (structuredReviewSnapshot).
//
// The fixture is Indeed's resume-selection form CAPTURED from the live page (10-02, Igor's
// Chrome; svg/img/pdf preview stripped), not written by hand.

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
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

const prefer = extract("  async function preferIndeedResume(filled) {");
const snap = extract("  function structuredReviewSnapshot() {");
check("preferIndeedResume + structuredReviewSnapshot found", !!prefer && !!snap);

function world(storage, html = FORM, pathname = "/beta/indeedapply/form/resume-selection-module/resume-selection") {
  const dom = new JSDOM(`<!doctype html><body><main>${html}</main></body>`,
    { url: `https://smartapply.indeed.com${pathname}`, runScripts: "outside-only" });
  const w = dom.window;
  w.HTMLElement.prototype.getClientRects = function () { return [1]; };
  w.__store = { ...storage };
  w.detectPlatform = () => "indeed";
  w.storageGet = async (k) => ({ [k]: w.__store[k] });
  w.storageSet = async (o) => { Object.assign(w.__store, o); return true; };
  w.humanClick = async (el) => { el.click(); };
  w.sleep = async () => {};
  w.humanDelay = () => 0;
  w.eval(`${prefer}\n${snap}\nwindow.__prefer = preferIndeedResume; window.__snap = structuredReviewSnapshot;`);
  return w;
}
const q = (w, id) => w.document.querySelector(`[data-testid="${id}"]`);

(async () => {
  {
    const w = world({ indeedSdrRefusedAt: Date.now() });
    check("captured page starts on the uploaded file", q(w, "resume-selection-file-resume-radio-card-input").checked);
    const filled = [];
    const chosen = await w.__prefer(filled);
    check("after a refusal: the Indeed Resume card is chosen on the real markup",
      chosen && q(w, "resume-selection-structured-resume-radio-card-input").checked &&
      !q(w, "resume-selection-file-resume-radio-card-input").checked && filled.includes("indeed-resume"));
    check("…and remembered for the next snapshot", w.__store.indeedLastResumeKind === "indeed");
  }
  {
    const w = world({});
    const chosen = await w.__prefer([]);
    check("no refusal yet → the upload path stays (tailored PDF)", !chosen &&
      q(w, "resume-selection-file-resume-radio-card-input").checked);
  }
  {
    const noCard = FORM.replace(/<div data-testid="resume-selection-structured-resume-radio-card"[\s\S]*?(?=<div data-testid="resume-selection-file-resume-radio-card")/, "");
    const w = world({ indeedSdrRefusedAt: 1 }, noCard);
    check("fixture without the Indeed Resume card really lacks it", !q(w, "resume-selection-structured-resume-radio-card-input"));
    check("no Indeed Resume on the account → upload as before", (await w.__prefer([])) === false);
  }
  {
    const w = world({ indeedSdrRefusedAt: 1 }, FORM, "/beta/indeedapply/form/questions-module/questions/1");
    check("only on the resume-selection step", (await w.__prefer([])) === false);
  }
  {
    // Structure only: no card bodies. The text "Jane Doe, jane@x.com" must not leave the page.
    const w = world({}, `<section data-testid="sdr-work-card"><p>Jane Doe, jane@x.com</p>
      <span data-testid="missing-dates-badge">Missing info</span><button>Edit</button></section>
      <button data-testid="continue-button">Continue</button>`, "/beta/indeedapply/form/resume-module/structured-data-review");
    const line = w.__snap();
    check("snapshot keeps testids, actions and badges", /sdr-work-card/.test(line) && /Edit/.test(line) && /Missing info/.test(line), line);
    check("…never the card body", !/Jane|jane@/.test(line), line);
  }
  check("the step loop asks preferIndeedResume before uploading",
    /const indeedResumeChosen = await preferIndeedResume\(filled\);[\s\S]{0,120}const resumeInput = indeedResumeChosen \? null : findResumeInput\(\);/.test(SRC));
  check("a refusal on structured-data-review logs the snapshot and sets the flag",
    /structured-data-review\/\.test\(location\.pathname\)\) \{[\s\S]{0,300}structuredReviewSnapshot\(\)[\s\S]{0,200}indeedSdrRefusedAt: Date\.now\(\)/.test(SRC));

  console.log(failures ? `\n${failures} failure(s)` : "\nall good");
  process.exit(failures ? 1 : 0);
})();
