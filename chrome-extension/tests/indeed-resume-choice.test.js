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
  w.eval(`const INDEED_SDR_TTL_MS = ${/const INDEED_SDR_TTL_MS = ([^;]+);/.exec(SRC)[1]};\n${prefer}\n${snap}\nwindow.__prefer = preferIndeedResume; window.__snap = structuredReviewSnapshot;`);
  return w;
}
const q = (w, id) => w.document.querySelector(`[data-testid="${id}"]`);

(async () => {
  {
    const w = world({ indeedSdrRefusedAt: Date.now() });
    check("captured page starts on the uploaded file", q(w, "resume-selection-file-resume-radio-card-input").checked);
    const filled = [];
    const r = await w.__prefer(filled);
    check("after a refusal: the Indeed Resume card is chosen on the real markup",
      r.chosen && r.changed && q(w, "resume-selection-structured-resume-radio-card-input").checked &&
      !q(w, "resume-selection-file-resume-radio-card-input").checked && filled.includes("indeed-resume"));
    check("…and remembered for the next snapshot", w.__store.indeedLastResumeKind === "indeed");
    // Skeptic B1: an already-checked card is NOT progress, or the stall guard never fires
    // and a refused resume step spins 20 rounds instead of handing back.
    const again = [];
    const r2 = await w.__prefer(again);
    check("next round on the same step: still chosen (no upload), but not progress",
      r2.chosen && !r2.changed && again.length === 0, JSON.stringify(r2));
    w.__store.indeedLastResumeKind = "file";
    await w.__prefer([]);
    check("a pre-checked Indeed Resume is still recorded as kind=indeed", w.__store.indeedLastResumeKind === "indeed");
  }
  {
    const w = world({ indeedSdrRefusedAt: Date.now() - 15 * 24 * 3600 * 1000 });
    check("a refusal older than 14 days expires → upload again", !(await w.__prefer([])).chosen);
  }
  {
    const w = world({});
    const r = await w.__prefer([]);
    check("no refusal yet → the upload path stays (tailored PDF)", !r.chosen &&
      q(w, "resume-selection-file-resume-radio-card-input").checked);
  }
  {
    const noCard = FORM.replace(/<div data-testid="resume-selection-structured-resume-radio-card"[\s\S]*?(?=<div data-testid="resume-selection-file-resume-radio-card")/, "");
    const w = world({ indeedSdrRefusedAt: 1 }, noCard);
    check("fixture without the Indeed Resume card really lacks it", !q(w, "resume-selection-structured-resume-radio-card-input"));
    check("no Indeed Resume on the account → upload as before", (await w.__prefer([])).chosen === false);
  }
  {
    const w = world({ indeedSdrRefusedAt: 1 }, FORM, "/beta/indeedapply/form/questions-module/questions/1");
    check("only on the resume-selection step", (await w.__prefer([])).chosen === false);
  }
  {
    // Structure only, never the resume. Card shapes from skeptic B's probe: a header holding
    // title + badge, hashed classes incl. "ErrorBoundary", edit buttons with long aria-labels,
    // body lines that happen to contain "error"/"required"/"add ".
    const w = world({}, `<div class="ErrorBoundary-x"><section data-testid="sdr-work-card" class="css-1a2b">
        <div class="css-hdr"><h3>Senior Software Engineer</h3><span data-testid="missing-dates-badge">Add dates</span></div>
        <p class="css-p">Reduced error rates by 30% across 12 clinics</p>
        <p>Required skills: Epic EHR, HIPAA compliance</p>
        <p>Jane Doe, jane@x.com, (808) 555-0123, 1450 Ala Moana Blvd, Honolulu</p>
        <button aria-label="Edit Senior Software Engineer at Kaiser Permanente">Edit</button></section>
        <span class="css-badge">Missing info</span></div>
      <button data-testid="continue-button">Continue</button>`, "/beta/indeedapply/form/resume-module/structured-data-review");
    const line = w.__snap();
    check("snapshot keeps testids, actions and badges",
      /sdr-work-card/.test(line) && /"Edit Senior"/.test(line) && /Missing info/.test(line) && /missing-dates-badge:Add dates/.test(line), line);
    check("…never the resume: titles, bullets, contacts, address, employer",
      !/Software Engineer"|Reduced|clinics|Epic|Jane|jane@|555|Ala Moana|Kaiser/.test(line), line);
    check("badges come first (the slice may cut ids, never the diagnosis)", line.indexOf("badges=") === 0);
  }
  check("the step loop asks preferIndeedResume before uploading",
    /const indeedResume = await preferIndeedResume\(filled\);\s*\n\s*if \(indeedResume\.changed\) filledAny = true;[\s\S]{0,200}const resumeInput = indeedResumeChosen \? null : findResumeInput\(\);/.test(SRC));
  check("a refusal on structured-data-review logs the snapshot and sets the flag",
    /structured-data-review\/\.test\(location\.pathname\)\) \{[\s\S]{0,300}structuredReviewSnapshot\(\)[\s\S]{0,500}if \(indeedLastResumeKind !== "indeed"\) await storageSet\(\{ indeedSdrRefusedAt: Date\.now\(\) \}\)/.test(SRC));

  console.log(failures ? `\n${failures} failure(s)` : "\nall good");
  process.exit(failures ? 1 : 0);
})();
