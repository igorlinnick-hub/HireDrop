// ZipRecruiter: an application ZR filed must be recorded as one, with the job's place, and
// the filler must never mistake the results page for the form. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/zr-submit-truth.test.js
//
// Ground truth 10-06…07: ZR's 1-click apply files the application on the click itself, but
// we logged "Skip (no ZR form after 40s)" and never called /applications/save — 4 of 10 such
// skips ZR later showed as "Already applied" (one employer had already invited Igor to an
// interview). The same ZR path sent no place, so ZR rows in History read "No location".
//
// Fixtures are live captures from 10-06 (header comment in each file). The post-apply
// screen itself was never captured — it is built here from the labels and testIDs the
// live log and ZR's apply-flow bundle show; the live ZR run is its proof.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM, VirtualConsole } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const fixture = (f) => fs.readFileSync(path.join(__dirname, "fixtures", f), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}
function slice(from, to) {
  const a = SRC.indexOf(from);
  const b = SRC.indexOf(to, a);
  if (a < 0 || b < 0) {
    console.error(`markers moved: ${from} … ${to}`);
    process.exit(2);
  }
  return SRC.slice(a, b);
}

const PAGE = slice("  // ── company on the job page", "  async function phase2_jobDetail() {");
const POST_APPLY = slice("  // Same rule as visibleApplyDialogs(): laid out", "  // Leave ZR's post-apply screen");
const APPLIED = slice("  function jobLooksApplied() {", "  async function detectSilentSubmission");
const DIALOGS = slice("  const FIELDISH_SELECTOR =", "  // The element that owns the current application step.");
const LABEL = slice("  function btnLabel(b) {", "  // Buttons that look actionable but must NEVER be clicked");
const DENY = slice("  const DENY_BTN_RE =", "  // Advance/submit labels across platforms.");

const SERP = "https://www.ziprecruiter.com/jobs-search?search=event+manager&location=Houston%2C+TX&lk=7t6MBMxmFyu0vQEw6DqP7A";
const UUID = "7t6MBMxmFyu0vQEw6DqP7A";

function load(html, code, exports, { laidOut = true } = {}) {
  // Silent console: jsdom can't parse the captured pages' modern CSS.
  const { window } = new JSDOM(html, { url: SERP, virtualConsole: new VirtualConsole() });
  // jsdom has no layout: offsetParent is always null. Everything is "laid out" here, so
  // only `visibility` (which jsdom does compute, inherited) can hide a dialog.
  if (laidOut) {
    Object.defineProperty(window.HTMLElement.prototype, "offsetParent", {
      get() { return this.ownerDocument.body; },
    });
  }
  const ctx = vm.createContext({ document: window.document, window, URL });
  vm.runInContext(code + "\n" + exports.map((f) => `this.${f} = ${f};`).join(" "), ctx);
  return { ctx, doc: window.document, window };
}

// ── the place line ─────────────────────────────────────────────────────────
const PLACE_EXPORTS = ["readZipRecruiterLocation", "readZipRecruiterCardLocation", "cardLocationFor", "jobIdFromUrl"];
{
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), PAGE, PLACE_EXPORTS);
  check("uuid from the ZR job URL", ctx.jobIdFromUrl(SERP) === UUID, ctx.jobIdFromUrl(SERP));
  const got = ctx.readZipRecruiterLocation(doc, UUID, []);
  check("open pane → its place line", got === "Houston, TX • On-site", JSON.stringify(got));
  const card = doc.querySelector('article[id^="job-card-"]');
  const onCard = ctx.readZipRecruiterCardLocation(card);
  check("card → place + arrangement", onCard === "Houston, TX · On-site", JSON.stringify(onCard));
  check("card without a place line → \"\"",
    ctx.readZipRecruiterCardLocation(doc.createElement("article")) === "");
  check("no card → \"\"", ctx.readZipRecruiterCardLocation(null) === "");
}
{
  // ZR redesigns the header: the stored card for THIS uuid answers, not index 0.
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), PAGE, PLACE_EXPORTS);
  doc.querySelector('[data-testid="serp-job-details-title"]').nextElementSibling.remove();
  const pending = [
    { jk: "someOtherUuid000000000", location: "Dallas, TX · Hybrid" },
    { jk: UUID, location: "Katy, TX · Remote" },
  ];
  check("no pane line → this job's stored card", ctx.readZipRecruiterLocation(doc, UUID, pending) === "Katy, TX · Remote",
    JSON.stringify(ctx.readZipRecruiterLocation(doc, UUID, pending)));
}
{
  const { ctx, doc } = load("<body></body>", PAGE, PLACE_EXPORTS);
  check("empty page, no card → \"\"", ctx.readZipRecruiterLocation(doc, UUID, []) === "");
  check("no uuid → \"\", never a neighbour's", ctx.readZipRecruiterLocation(doc, "", [{ jk: "x", location: "Austin, TX" }]) === "");
}
{
  // A header line that is really a paragraph is not a place.
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), PAGE, PLACE_EXPORTS);
  doc.querySelector('[data-testid="serp-job-details-title"]').nextElementSibling.textContent = "x".repeat(200);
  check("over-long header line is skipped", ctx.readZipRecruiterLocation(doc, UUID, [{ jk: UUID, location: "Houston, TX" }]) === "Houston, TX");
}

// ── the place reaches the server ───────────────────────────────────────────
{
  const p1 = slice("  async function phase1_ziprecruiter() {", "  // HARVEST-TO-POOL (P0c 2026-07-29)");
  check("ZR cards keep their place (pendingJobs = these candidates)",
    /candidates\.push\(\{ title, company, location,/.test(p1));
  const p2 = slice("  async function phase2_ziprecruiter() {", "  // A job whose application was already STARTED");
  check("ZR detail phase reads the place",
    p2.includes("readZipRecruiterLocation(document, jobIdFromUrl(jobUrl), _zrSt.pendingJobs)"));
  check("…and sends it with the posting text",
    p2.includes("recordJobDescription(jobTitle, jobCompany, jobDesc, jobUrl, jobLocation)"));
}

// ── ZR's post-apply screen = a filed application ──────────────────────────
const POST_EXPORTS = ["zrPostApplySignal", "isShownDialog"];
const POST_CODE = POST_APPLY + APPLIED;
function postApplyDialog(inner) {
  return `<div role="dialog" aria-modal="true">${inner}</div>`;
}
for (const f of ["ziprecruiter-serp-1click-pane.html", "ziprecruiter-serp-applied-pane.html", "ziprecruiter-serp-right-pane.html"]) {
  const { ctx } = load(fixture(f), POST_CODE, POST_EXPORTS);
  check(`${f}: no post-apply screen → null`, ctx.zrPostApplySignal() === null, ctx.zrPostApplySignal());
}
{
  // The page's own hidden "Settings" modal (visibility:hidden overlay, buttons Close|Close).
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE + DIALOGS, [...POST_EXPORTS, "visibleApplyDialogs"]);
  const settings = doc.querySelector('[aria-label="Settings"] [role="dialog"]');
  check("fixture: the hidden Settings modal is mounted", !!settings);
  check("hidden Settings modal is not a shown dialog", ctx.isShownDialog(settings) === false);
  check("…nor an apply dialog", !ctx.visibleApplyDialogs().includes(settings));
}
{
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE, POST_EXPORTS);
  doc.body.insertAdjacentHTML("beforeend", postApplyDialog(
    "<p>Send a message to this employer about why you're interested</p><button>Send a Message</button><button>Skip for Now</button>"));
  check("Send a Message | Skip for Now → zr-post-apply", ctx.zrPostApplySignal() === "zr-post-apply", ctx.zrPostApplySignal());
}
{
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE, POST_EXPORTS);
  doc.body.insertAdjacentHTML("beforeend", postApplyDialog('<div data-testid="bsf-not-eligible">Your application has been submitted!</div><button>Close</button>'));
  check("bsf-not-eligible → zr-post-apply", ctx.zrPostApplySignal() === "zr-post-apply");
}
{
  // The same post-apply markup inside a HIDDEN dialog is not a signal.
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE, POST_EXPORTS);
  doc.body.insertAdjacentHTML("beforeend",
    `<div style="visibility: hidden">${postApplyDialog("<button>Send a Message</button><button>Skip for Now</button>")}</div>`);
  check("post-apply screen in a hidden overlay → null", ctx.zrPostApplySignal() === null);
}
{
  // "Be Seen First" is a card badge before anyone applies — never a signal on its own.
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE, POST_EXPORTS);
  doc.body.insertAdjacentHTML("beforeend", postApplyDialog("<p>Be Seen First</p><button>Continue</button>"));
  check("a dialog saying only \"Be Seen First\" → null", ctx.zrPostApplySignal() === null);
}
{
  // The pane flipping to "Applied" counts only when the caller asks for it (pane:true).
  const { ctx } = load(fixture("ziprecruiter-serp-applied-pane.html"), POST_CODE, POST_EXPORTS);
  check("Applied pane, no pane flag → null", ctx.zrPostApplySignal() === null);
  check("Applied pane, pane:true → applied-badge", ctx.zrPostApplySignal({ pane: true }) === "applied-badge");
  const { ctx: c2 } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE, POST_EXPORTS);
  check("1-Click pane, pane:true → null", c2.zrPostApplySignal({ pane: true }) === null);
}
{
  const ds = slice("  async function detectSilentSubmission", "  // Record a submission that the platform accepted");
  const zrAt = ds.indexOf("zrPostApplySignal()");
  const fieldsAt = ds.indexOf('d.querySelector("input, textarea, select")');
  check("a multi-step form's last Continue reads ZR's post-apply screen",
    zrAt > 0 && zrAt < fieldsAt, `zr@${zrAt} fields@${fieldsAt}`);
  const p2 = slice("  async function phase2_ziprecruiter() {", "  // A job whose application was already STARTED");
  check("1-click: the post-apply screen is recorded, not skipped",
    /if \(formReady && formReady\.applied\) \{[\s\S]*?finishZipRecruiterApplied\(/.test(p2) &&
    p2.indexOf("formReady.applied") < p2.indexOf("Skip (no ZR form after 40s)"));
  const finish = slice("  async function finishZipRecruiterApplied", "  // A ZipRecruiter dialog is a REAL Quick Apply form");
  check("…through the same bookkeeping as every verified submit", finish.includes("recordSubmittedApplication(jobInfo"));
  const dismiss = slice("  async function dismissZipRecruiterPostApply", "  // Record a ZR application ZR itself confirmed");
  check("…and it never clicks \"Send a Message\"", !/send a message/i.test(dismiss.replace(/\/\/.*$/gm, "")));
}

// ── the results page is never the form ────────────────────────────────────
{
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), LABEL + DENY, ["isDeniedFormButton", "isPageNavControl"]);
  const next = doc.querySelector('a[title="Next Page"]');
  check("fixture: ZR's next-page arrow is there", !!next);
  check("next-page arrow is denied as a form button", next && ctx.isDeniedFormButton(next) === true);
  const dlg = doc.createElement("div");
  dlg.setAttribute("role", "dialog");
  dlg.innerHTML = "<button>Continue</button><button>Next</button>";
  doc.body.appendChild(dlg);
  const [cont, nxt] = dlg.querySelectorAll("button");
  check("a dialog's Continue is not page navigation", ctx.isPageNavControl(cont) === false);
  check("a dialog's bare Next is not page navigation", ctx.isPageNavControl(nxt) === false);
  // A page-level application form keeps its buttons even under a "pagination"-named step bar.
  doc.body.insertAdjacentHTML("beforeend",
    '<form id="apply"><input name="email"><div class="form-pagination"><button title="Next page">Next</button>' +
    '<button type="submit">Submit application</button></div></form>');
  const [stepNext, submit] = doc.querySelectorAll("#apply button");
  check("a form's Submit under .form-pagination is allowed", ctx.isDeniedFormButton(submit) === false);
  check("a form's Next titled \"Next page\" is allowed", ctx.isDeniedFormButton(stepNext) === false);
  check("…while the results page's arrow outside any form stays denied", ctx.isDeniedFormButton(next) === true);
}
{
  const p3 = slice("  async function phase3_fillForm() {", "  async function _phase3_fillForm() {");
  check("phase3 pins ZR's buttons to its modal", p3.includes('formLivesInDialog = detectPlatform() === "ziprecruiter"'));
  check("…and lets go when the form is done", /finally \{\s*formLivesInDialog = false;/.test(p3));
}

console.log(failures ? `\n${failures} FAILED` : "\nall ok");
process.exit(failures ? 1 : 0);
