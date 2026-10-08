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
const pending = [];
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
  // Skeptic 10-08: ZR's empty Close-only shell between steps can stay up ~30 s. A pane read
  // then would record the job and the dismiss would click the shell's Close (form dropped).
  const { ctx, doc } = load(fixture("ziprecruiter-serp-applied-pane.html"), POST_CODE, POST_EXPORTS);
  doc.body.insertAdjacentHTML("beforeend", postApplyDialog('<button aria-label="Close">Close</button>'));
  check("Applied pane + an open Close-only shell, pane:true → null", ctx.zrPostApplySignal({ pane: true }) === null,
    ctx.zrPostApplySignal({ pane: true }));
}
{
  // Only what a person can see is ZR's word.
  const hiddenCases = {
    "bsf testID under display:none": '<div style="display:none"><div data-testid="bsf-not-eligible">Your application has been submitted!</div></div><button>Continue</button>',
    "submitted text under [hidden]": "<p hidden>Your application has been submitted!</p><button>Continue</button>",
    "submitted text under visibility:hidden": '<p style="visibility:hidden">Your application has been submitted!</p><button>Continue</button>',
    "Send a Message | Skip for Now under display:none": '<div style="display:none"><button>Send a Message</button><button>Skip for Now</button></div><input name="q">',
  };
  for (const [name, inner] of Object.entries(hiddenCases)) {
    const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE, POST_EXPORTS);
    doc.body.insertAdjacentHTML("beforeend", postApplyDialog(inner));
    check(`hidden markup in a shown dialog → null: ${name}`, ctx.zrPostApplySignal() === null, ctx.zrPostApplySignal());
  }
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE, POST_EXPORTS);
  doc.body.insertAdjacentHTML("beforeend",
    '<div id="react-apply-flow-root" style="visibility:hidden"><p>Your application has been submitted!</p></div>');
  check("hidden apply root → null", ctx.zrPostApplySignal() === null);
  doc.getElementById("react-apply-flow-root").style.visibility = "visible";
  check("…the same root shown → zr-post-apply", ctx.zrPostApplySignal() === "zr-post-apply");
}
{
  // The dismiss clicks only on the post-apply screen, never a form shell's Close.
  const DISMISS = slice("  async function dismissZipRecruiterPostApply", "  // Record a ZR application ZR itself confirmed");
  const run = async (inner) => {
    const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE + DISMISS,
      [...POST_EXPORTS, "dismissZipRecruiterPostApply"]);
    const clicked = [];
    ctx.humanClick = async (b) => clicked.push(b.textContent.trim() || b.getAttribute("aria-label"));
    ctx.sleep = async () => {};
    ctx.humanDelay = () => 0;
    doc.body.insertAdjacentHTML("beforeend", postApplyDialog(inner));
    await ctx.dismissZipRecruiterPostApply();
    return clicked;
  };
  pending.push((async () => {
    const shell = await run('<button aria-label="Close">Close</button>');
    check("dismiss leaves a Close-only form shell alone", shell.length === 0, JSON.stringify(shell));
    const post = await run("<p>Send a message to this employer</p><button>Send a Message</button><button>Skip for Now</button><button aria-label=\"Close\">Close</button>");
    check("dismiss on the post-apply screen → Skip for Now only", JSON.stringify(post) === '["Skip for Now"]', JSON.stringify(post));
  })());
}
{
  // A Stop that lands after the 1-click click must not lose the application ZR filed.
  const FORMS = slice("  // A ZipRecruiter dialog is a REAL Quick Apply form only if", "  // =========================================================================\n  // PHASE 3");
  const run = async (inner, running) => {
    const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE + FORMS,
      [...POST_EXPORTS, "waitForZipRecruiterForm"]);
    ctx.isCampaignRunning = async () => running;
    ctx.sleep = async () => {};
    if (inner) doc.body.insertAdjacentHTML("beforeend", postApplyDialog(inner));
    return ctx.waitForZipRecruiterForm(50);
  };
  pending.push((async () => {
    const stopped = await run("<button>Send a Message</button><button>Skip for Now</button>", false);
    check("Stop + post-apply screen → still { applied }", stopped && stopped.applied === "zr-post-apply", JSON.stringify(stopped));
    check("Stop + nothing → false", (await run("", false)) === false);
  })());
  const p2 = slice("  async function phase2_ziprecruiter() {", "  // A job whose application was already STARTED");
  const after = p2.slice(p2.indexOf("await waitForZipRecruiterForm(40000)"));
  check("phase2 records a filed 1-click before its Stop check",
    after.indexOf("formReady.applied") >= 0 && after.indexOf("formReady.applied") < after.indexOf("isCampaignRunning()"));
}
{
  // phase3 asks ZR again at the top of every step and before every give-up exit.
  const p3 = slice("  async function _phase3_fillForm() {", "  // Hand the backend the posting text we just read");
  const at = (needle) => p3.indexOf(needle);
  const loopTop = p3.slice(at("while (formStepCount < maxSteps) {"), at('log("Campaign stopped — aborting form fill"'));
  check("top of each step: ZR filed? before the Stop check", loopTop.includes("zrFiledDuringForm(jobInfo, coverLetter)"));
  const stall = p3.slice(at("if (stallRounds >= 2) {"), at("wouldn't accept our answers"));
  check("before the stall hand-back", stall.includes("zrFiledDuringForm(jobInfo, coverLetter)"));
  const tail = p3.slice(at("if (formStepCount >= maxSteps && !stoppedEarly)") - 200, at("if (formStepCount >= maxSteps && !stoppedEarly)"));
  check("before the step-budget / no-button / abandoned exits", tail.includes("zrFiledDuringForm(jobInfo, coverLetter)"));
  const helper = slice("  async function zrFiledDuringForm", "  async function phase3_fillForm() {");
  check("…on ZipRecruiter only", helper.includes('detectPlatform() !== "ziprecruiter"'));
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
  const sendMsg = doc.createElement("button");
  sendMsg.type = "submit";
  sendMsg.textContent = "Send a Message";
  doc.getElementById("apply").appendChild(sendMsg);
  check("\"Send a Message\" is never a form button (it writes to the employer)", ctx.isDeniedFormButton(sendMsg) === true);
}
{
  const p3 = slice("  async function phase3_fillForm() {", "  async function _phase3_fillForm() {");
  check("phase3 pins ZR's buttons to its modal", p3.includes('formLivesInDialog = detectPlatform() === "ziprecruiter"'));
  check("…and lets go when the form is done", /finally \{\s*formLivesInDialog = false;/.test(p3));
}

Promise.all(pending).then(() => {
  console.log(failures ? `\n${failures} FAILED` : "\nall ok");
  process.exit(failures ? 1 : 0);
});
