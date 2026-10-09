// ZipRecruiter: an application ZR filed is recorded as one, with the job's place, and the
// filler never mistakes the results page for the form. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/zr-submit-truth.test.js
//
// ZR's 1-click apply files the application on the click itself and opens no form, only a
// post-apply screen; a multi-step form can end the same way after any Continue. Every check
// here calls the real functions out of content.js: the place readers, the post-apply signal,
// phase2_ziprecruiter, waitForZipRecruiterForm, detectSilentSubmission and _phase3_fillForm.
//
// Fixtures are live captures (header comment in each file). The post-apply screen itself was
// never captured: it is built here from the labels and testIDs of ZR's apply-flow bundle, and
// one live ZR application is its proof.

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
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${typeof detail === "string" ? detail : JSON.stringify(detail)})`}`);
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
// A top-level function inside content.js's IIFE closes at two-space indent.
function fn(signature) {
  const a = SRC.indexOf(signature);
  const b = a < 0 ? -1 : SRC.indexOf("\n  }\n", a);
  if (a < 0 || b < 0) {
    console.error(`markers moved: ${signature}`);
    process.exit(2);
  }
  return SRC.slice(a, b + 5);
}

const PAGE = slice("  // ── company on the job page", "  async function phase2_jobDetail() {");
const POST_APPLY = slice("  // ZipRecruiter's own word that the application went through.", "  // Leave ZR's post-apply screen");
const DISMISS = fn("  async function dismissZipRecruiterPostApply() {");
const FINISH = fn("  async function finishZipRecruiterApplied(jobInfo, coverLetter, signal) {");
const APPLIED = slice("  function jobLooksApplied() {", "  async function detectSilentSubmission");
const DIALOGS = slice("  const FIELDISH_SELECTOR =", "  // The element that owns the current application step.");
const LABEL = slice("  function btnLabel(b) {", "  // Buttons that look actionable but must NEVER be clicked");
const DENY = slice("  const DENY_BTN_RE =", "  // Advance/submit labels across platforms.");
const FORMS = slice("  // A ZipRecruiter dialog is a REAL Quick Apply form only if", "  // =========================================================================\n  // PHASE 3");
const PHASE2_ZR = fn("  async function phase2_ziprecruiter() {");
const PHASE3 = fn("  async function phase3_fillForm() {");
const PHASE3_BODY = fn("  async function _phase3_fillForm() {");
const ZR_FILED = fn("  async function zrFiledDuringForm(jobInfo, coverLetter) {");
const SILENT = slice("  const POSTAPPLY_URL_HINTS", "  // React-compatible field filling") +
  slice("  function isFormVisible()", "  function findResumeInput()") +
  slice("  async function detectSilentSubmission", "  // Record a submission that the platform accepted");

const SERP = "https://www.ziprecruiter.com/jobs-search?search=event+manager&location=Houston%2C+TX&lk=7t6MBMxmFyu0vQEw6DqP7A";
const UUID = "7t6MBMxmFyu0vQEw6DqP7A";

function load(html, code, exports, { url = SERP } = {}) {
  // Silent console: jsdom can't parse the captured pages' modern CSS.
  const { window } = new JSDOM(html, { url, virtualConsole: new VirtualConsole() });
  // jsdom has no layout: offsetParent is always null. Everything is "laid out" here, so
  // only `visibility` (which jsdom does compute, inherited) can hide a dialog.
  Object.defineProperty(window.HTMLElement.prototype, "offsetParent", {
    get() { return this.ownerDocument.body; },
  });
  const ctx = vm.createContext({ document: window.document, window, URL });
  vm.runInContext(code + "\n" + exports.map((f) => `this.${f} = ${f};`).join(" "), ctx);
  return { ctx, doc: window.document, window };
}
const postApplyDialog = (inner) => `<div role="dialog" aria-modal="true">${inner}</div>`;
const SEND_OR_SKIP = "<p>Send a message to this employer about why you're interested</p><button>Send a Message</button><button>Skip for Now</button>";

// ── the place line ─────────────────────────────────────────────────────────
const PLACE_EXPORTS = ["readZipRecruiterLocation", "readZipRecruiterCardLocation", "cardLocationFor", "jobIdFromUrl"];
{
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), PAGE, PLACE_EXPORTS);
  check("uuid from the ZR job URL", ctx.jobIdFromUrl(SERP) === UUID, ctx.jobIdFromUrl(SERP));
  const got = ctx.readZipRecruiterLocation(doc, UUID, []);
  check("open pane → its place line", got === "Houston, TX • On-site", got);
  const onCard = ctx.readZipRecruiterCardLocation(doc.querySelector('article[id^="job-card-"]'));
  check("card → place + arrangement", onCard === "Houston, TX · On-site", onCard);
  check("card without a place line → \"\"", ctx.readZipRecruiterCardLocation(doc.createElement("article")) === "");
  check("no card → \"\"", ctx.readZipRecruiterCardLocation(null) === "");
}
{
  // ZR redesigns the header: the stored card for THIS uuid answers, not index 0.
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), PAGE, PLACE_EXPORTS);
  doc.querySelector('[data-testid="serp-job-details-title"]').nextElementSibling.remove();
  const stored = [{ jk: "someOtherUuid000000000", location: "Dallas, TX · Hybrid" }, { jk: UUID, location: "Katy, TX · Remote" }];
  const got = ctx.readZipRecruiterLocation(doc, UUID, stored);
  check("no pane line → this job's stored card", got === "Katy, TX · Remote", got);
}
{
  const { ctx, doc } = load("<body></body>", PAGE, PLACE_EXPORTS);
  check("empty page, no card → \"\"", ctx.readZipRecruiterLocation(doc, UUID, []) === "");
  check("no uuid → \"\", never a neighbour's", ctx.readZipRecruiterLocation(doc, "", [{ jk: "x", location: "Austin, TX" }]) === "");
}
{
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), PAGE, PLACE_EXPORTS);
  doc.querySelector('[data-testid="serp-job-details-title"]').nextElementSibling.textContent = "x".repeat(200);
  check("an over-long header line is not a place", ctx.readZipRecruiterLocation(doc, UUID, [{ jk: UUID, location: "Houston, TX" }]) === "Houston, TX");
}

// ── ZR's post-apply screen ────────────────────────────────────────────────
const POST_EXPORTS = ["zrPostApplySignal", "isShownDialog"];
const POST_CODE = DIALOGS + POST_APPLY + APPLIED;
for (const f of ["ziprecruiter-serp-1click-pane.html", "ziprecruiter-serp-applied-pane.html", "ziprecruiter-serp-right-pane.html"]) {
  const { ctx } = load(fixture(f), POST_CODE, POST_EXPORTS);
  check(`${f}: no post-apply screen → null`, ctx.zrPostApplySignal() === null, ctx.zrPostApplySignal());
}
{
  // The page's own hidden "Settings" modal (visibility:hidden overlay, buttons Close|Close).
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE, [...POST_EXPORTS, "visibleApplyDialogs"]);
  const settings = doc.querySelector('[aria-label="Settings"] [role="dialog"]');
  check("fixture: the hidden Settings modal is mounted", !!settings);
  check("hidden Settings modal is not a shown dialog", ctx.isShownDialog(settings) === false);
  check("…nor an apply dialog", !ctx.visibleApplyDialogs().includes(settings));
}
{
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE, POST_EXPORTS);
  doc.body.insertAdjacentHTML("beforeend", postApplyDialog(SEND_OR_SKIP));
  check("Send a Message | Skip for Now → zr-post-apply", ctx.zrPostApplySignal() === "zr-post-apply", ctx.zrPostApplySignal());
}
{
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE, POST_EXPORTS);
  doc.body.insertAdjacentHTML("beforeend", postApplyDialog('<div data-testid="bsf-not-eligible">Your application has been submitted!</div><button>Close</button>'));
  check("bsf-not-eligible → zr-post-apply", ctx.zrPostApplySignal() === "zr-post-apply");
}
{
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE, POST_EXPORTS);
  doc.body.insertAdjacentHTML("beforeend", `<div style="visibility: hidden">${postApplyDialog(SEND_OR_SKIP)}</div>`);
  check("post-apply screen in a hidden overlay → null", ctx.zrPostApplySignal() === null);
}
{
  // "Be Seen First" is a card badge before anyone applies — never a signal on its own.
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE, POST_EXPORTS);
  doc.body.insertAdjacentHTML("beforeend", postApplyDialog("<p>Be Seen First</p><button>Continue</button>"));
  check("a dialog saying only \"Be Seen First\" → null", ctx.zrPostApplySignal() === null);
}
{
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
  doc.body.insertAdjacentHTML("beforeend", '<div id="react-apply-flow-root" style="visibility:hidden"><p>Your application has been submitted!</p></div>');
  check("hidden apply root → null", ctx.zrPostApplySignal() === null);
  doc.getElementById("react-apply-flow-root").style.visibility = "visible";
  check("…the same root shown → zr-post-apply", ctx.zrPostApplySignal() === "zr-post-apply");
}
{
  // The pane's "Applied" counts only when asked for (pane:true) and only with no dialog open:
  // ZR's empty Close-only shell between steps means the flow is not over.
  const { ctx, doc } = load(fixture("ziprecruiter-serp-applied-pane.html"), POST_CODE, POST_EXPORTS);
  check("Applied pane, no pane flag → null", ctx.zrPostApplySignal() === null);
  check("Applied pane, pane:true → applied-badge", ctx.zrPostApplySignal({ pane: true }) === "applied-badge");
  doc.body.insertAdjacentHTML("beforeend", postApplyDialog('<button aria-label="Close">Close</button>'));
  check("Applied pane + an open Close-only shell, pane:true → null", ctx.zrPostApplySignal({ pane: true }) === null,
    ctx.zrPostApplySignal({ pane: true }));
  const { ctx: c2 } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE, POST_EXPORTS);
  check("1-Click pane, pane:true → null", c2.zrPostApplySignal({ pane: true }) === null);
}

// ── leaving the post-apply screen ─────────────────────────────────────────
async function dismissOn(inner) {
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE + DISMISS,
    [...POST_EXPORTS, "dismissZipRecruiterPostApply"]);
  const clicked = [];
  ctx.humanClick = async (b) => clicked.push(b.textContent.trim() || b.getAttribute("aria-label"));
  ctx.sleep = async () => {};
  ctx.humanDelay = () => 0;
  doc.body.insertAdjacentHTML("beforeend", postApplyDialog(inner));
  await ctx.dismissZipRecruiterPostApply();
  return clicked;
}
pending.push((async () => {
  const shell = await dismissOn('<button aria-label="Close">Close</button>');
  check("dismiss leaves a Close-only form shell alone", shell.length === 0, shell);
  const post = await dismissOn(`${SEND_OR_SKIP}<button aria-label="Close">Close</button>`);
  check("dismiss on the post-apply screen → Skip for Now, never Send a Message",
    JSON.stringify(post) === '["Skip for Now"]', post);
})());

// ── waiting after the Quick Apply click ───────────────────────────────────
async function waitWith(inner, running) {
  const { ctx, doc } = load(fixture("ziprecruiter-serp-1click-pane.html"), POST_CODE + FORMS,
    [...POST_EXPORTS, "waitForZipRecruiterForm"]);
  ctx.isCampaignRunning = async () => running;
  ctx.sleep = async () => {};
  if (inner) doc.body.insertAdjacentHTML("beforeend", postApplyDialog(inner));
  return ctx.waitForZipRecruiterForm(50);
}
pending.push((async () => {
  const filed = await waitWith(SEND_OR_SKIP, true);
  check("post-apply screen → { applied }", filed && filed.applied === "zr-post-apply", filed);
  const stopped = await waitWith(SEND_OR_SKIP, false);
  check("Stop + post-apply screen → still { applied } (the click was the submit)",
    stopped && stopped.applied === "zr-post-apply", stopped);
  check("Stop + nothing → false", (await waitWith("", false)) === false);
})());

// ── phase2_ziprecruiter, run for real on the captured 1-click pane ────────
async function phase2({ formResult, stopDuringWait = false } = {}) {
  const store = { campaignRunning: true, reviewMode: false, pendingJobs: [] };
  const rec = { described: null, recorded: [], skips: 0, backend: [], dismissed: 0 };
  const { window } = new JSDOM(fixture("ziprecruiter-serp-1click-pane.html"), { url: SERP, virtualConsole: new VirtualConsole() });
  const BTN = { id: "quick-apply" };
  const box = {
    document: window.document, window, URL, Date, Promise,
    storageGet: async (keys) => {
      const out = {};
      for (const k of [].concat(keys)) if (k in store) out[k] = store[k];
      return out;
    },
    storageSet: async (patch) => { Object.assign(store, patch); },
    isCampaignRunning: async () => store.campaignRunning === true,
    MAX_APPLICATIONS_PER_PLATFORM: 15,
    getPlatformCount: async () => 0,
    keywordCapReached: async () => false,
    waitForZipRecruiterRightPanel: async () => true,
    log: () => {},
    logBackend: (t) => rec.backend.push(t),
    sleep: async () => {},
    humanDelay: () => 0,
    cardCompanyFor: () => ({ company: "" }),
    zrDedupeKey: (u) => u,
    getAppliedUrls: async () => new Set(),
    getAppliedJobKeys: async () => new Set(),
    getHandedBackKeys: async () => new Set(),
    jobDedupKey: (t, c) => `${t}|${c}`,
    titleMatchesKeywords: () => true,
    titleGateKeywords: async () => ["event"],
    jobLooksApplied: () => false,
    addAppliedJobKey: async () => {},
    routeExternalToAts: async () => false,
    waitForZipRecruiterApplyButton: async () => BTN,
    findZipRecruiterApplyButton: () => BTN,
    sendMsg: async (m) => (m.type === "ASSESS_FIT" ? { decision: "apply", judged: true, fit_score: 70 } : {}),
    recordJobDescription: async (title, company, desc, url, location) => { rec.described = { title, location }; },
    humanClick: async () => {},
    waitForZipRecruiterForm: async () => {
      if (stopDuringWait) store.campaignRunning = false;
      return formResult;
    },
    recordSubmittedApplication: async (job, cl, signal) => rec.recorded.push(`${job.title}|${signal}`),
    dismissZipRecruiterPostApply: async () => { rec.dismissed++; },
    dialogSnapshot: () => "",
    phase3_fillForm: async () => {},
    skipToNextJob: async () => { rec.skips++; },
  };
  vm.createContext(box);
  vm.runInContext(`var lastPhase = "";\n${PAGE}\n${FINISH}\n${PHASE2_ZR}\nthis.__run = phase2_ziprecruiter;`, box);
  await box.__run();
  return rec;
}
pending.push((async () => {
  const filed = await phase2({ formResult: { applied: "zr-post-apply" } });
  check("the posting text goes out with the open pane's place",
    filed.described && filed.described.location === "Houston, TX • On-site", filed.described);
  check("1-click: the post-apply screen is recorded as an application",
    JSON.stringify(filed.recorded) === '["Events Associate|zr-post-apply"]', filed);
  check("…the screen is closed and the walk moves on, with no \"no ZR form\" skip",
    filed.dismissed === 1 && filed.skips === 1 && !filed.backend.some((t) => /no ZR form/.test(t)), filed);
  const stopped = await phase2({ formResult: { applied: "zr-post-apply" }, stopDuringWait: true });
  check("Stop right after the click: still recorded, the walk does not move on",
    stopped.recorded.length === 1 && stopped.skips === 0, stopped);
  const none = await phase2({ formResult: false });
  check("control: no form and no screen → the old skip, nothing recorded",
    none.recorded.length === 0 && none.backend.some((t) => /no ZR form/.test(t)), none);
})());

// ── a multi-step form's last Continue ─────────────────────────────────────
async function silentOn(url, inner) {
  const { window } = new JSDOM(`<body>${postApplyDialog(inner)}</body>`, { url, virtualConsole: new VirtualConsole() });
  Object.defineProperty(window.HTMLElement.prototype, "offsetParent", { get() { return this.ownerDocument.body; } });
  const box = {
    window, document: window.document, URL, Date, Promise,
    sleep: async () => {},
    detectPlatform: () => (window.location.hostname.includes("ziprecruiter") ? "ziprecruiter" : "indeed"),
  };
  vm.createContext(box);
  vm.runInContext(`${DIALOGS}\n${POST_APPLY}\n${APPLIED}\n${SILENT}\nthis.__d = detectSilentSubmission;`, box);
  return box.__d(300, "");
}
pending.push((async () => {
  check("ZR: the post-apply screen after a Continue is a submit",
    (await silentOn(SERP, SEND_OR_SKIP)) === "zr-post-apply");
  check("…and only on ZipRecruiter",
    (await silentOn("https://smartapply.indeed.com/beta/indeedapply/form/review", SEND_OR_SKIP)) !== "zr-post-apply");
})());

// ── _phase3_fillForm asks ZR before giving a job up ───────────────────────
// Run for real; every collaborator not named here is an async no-op (the Proxy), so the
// walk goes fill → no button → give up, unless ZR says it filed.
async function phase3({ platform = "ziprecruiter", signalFromCall = 1, stopDuringReady = false } = {}) {
  const { window } = new JSDOM("<body><div role=dialog><input name=a></div></body>", { url: SERP, virtualConsole: new VirtualConsole() });
  const rec = { recorded: [], handed: 0, skips: 0, backend: [] };
  let running = true;
  let zrCalls = 0;
  const named = {
    document: window.document, window, location: window.location,
    Date, Promise, Array, Set, Map, Math, JSON, String, Object, Number, RegExp, Error,
    isCampaignRunning: async () => running,
    waitForFormReady: async () => { if (stopDuringReady) running = false; },
    storageGet: async () => ({ currentJobInfo: { title: "Events Associate", company: "Acme", url: SERP } }),
    coverLetterKeyFor: () => "k",
    formScope: () => window.document,
    platformLabel: () => platform,
    detectPlatform: () => platform,
    zrPostApplySignal: () => (++zrCalls >= signalFromCall ? "zr-post-apply" : null),
    recordSubmittedApplication: async (job, cl, signal) => rec.recorded.push(`${job.title}|${signal}`),
    handBackJob: async () => { rec.handed++; },
    skipToNextJob: async () => { rec.skips++; },
    classifyFormButton: () => ({ btn: null, submit: false, label: "" }),
    findFormButton: () => null,
    waitForFormButton: async () => false,
    visibleApplyDialogs: () => [],
    findResumeInput: () => null,
    formBlockers: () => ({}),
    formBlockersLine: () => "",
    dialogSnapshot: () => "",
    buttonCensus: () => "",
    logBackend: (t) => rec.backend.push(t),
    log: () => {},
    humanDelay: () => 0,
    sleep: async () => {},
  };
  const box = vm.createContext(new Proxy(named, {
    has: () => true,
    get: (t, k) => (k in t || typeof k === "symbol" ? t[k] : async () => 0),
  }));
  vm.runInContext(`${PHASE3_BODY}\n${ZR_FILED}\nthis.__p3 = _phase3_fillForm;`, box);
  await box.__p3();
  return rec;
}
pending.push((async () => {
  const top = await phase3({ signalFromCall: 1 });
  check("ZR filed before the step: recorded, not skipped",
    JSON.stringify(top.recorded) === '["Events Associate|zr-post-apply"]' && top.skips === 0 && top.handed === 0, top);
  const stopped = await phase3({ signalFromCall: 1, stopDuringReady: true });
  check("…also when Stop landed meanwhile", stopped.recorded.length === 1 && stopped.skips === 0, stopped);
  const noButton = await phase3({ signalFromCall: 2 });
  check("ZR filed while the step showed no button: recorded instead of \"abandoned\"",
    noButton.recorded.length === 1 && noButton.skips === 0 && !noButton.backend.some((t) => /abandoned/.test(t)), noButton);
  const indeed = await phase3({ platform: "indeed", signalFromCall: 1 });
  check("control: not ZipRecruiter → no ZR record, the job is given up as before",
    indeed.recorded.length === 0 && indeed.skips === 1, indeed);
})());

// ── phase3 pins a ZR modal form's buttons to the modal ────────────────────
async function pinned({ platform, dialogs, throwInside = false }) {
  const rec = { seen: null, after: null, skips: 0 };
  const box = vm.createContext({
    detectPlatform: () => platform,
    visibleApplyDialogs: () => dialogs,
    logBackend: () => {},
    skipToNextJob: async () => { rec.skips++; },
    rec, throwInside,
  });
  vm.runInContext(`let formLivesInDialog = false;
    async function _phase3_fillForm() { rec.seen = formLivesInDialog; if (throwInside) throw new Error("boom"); }
    ${PHASE3}
    this.__run = async () => { await phase3_fillForm(); rec.after = formLivesInDialog; };`, box);
  await box.__run();
  return rec;
}
pending.push((async () => {
  const zr = await pinned({ platform: "ziprecruiter", dialogs: [{}] });
  check("ZR form in a modal: the lookup is pinned while the form runs, released after", zr.seen === true && zr.after === false, zr);
  const thrown = await pinned({ platform: "ziprecruiter", dialogs: [{}], throwInside: true });
  check("…released after a crash too", thrown.seen === true && thrown.after === false && thrown.skips === 1, thrown);
  const indeed = await pinned({ platform: "indeed", dialogs: [{}] });
  check("not ZipRecruiter → never pinned", indeed.seen === false, indeed);
})());

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

Promise.all(pending).then(() => {
  console.log(failures ? `\n${failures} FAILED` : "\nall ok");
  process.exit(failures ? 1 : 0);
});
