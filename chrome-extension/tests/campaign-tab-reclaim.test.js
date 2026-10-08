// Campaign-tab ownership across an Apply click that opens a new tab. Run:
//
//   node chrome-extension/tests/run-all.js     (or this file directly with jsdom on NODE_PATH)
//
// The bug (live 10-06, ext 1.8.43, activity_log):
//   22:51:12 Clicking Apply: Social Media Coordinator @ daniéle laser + aesthetics
//   22:51:14 🛈 Staying idle on www.indeed.com/job/social-media-coordinator- — not the campaign tab
//   22:51:31 Skip (no form after Apply): Social Media Coordinator @ daniéle laser + aesthetics
//   22:51:39 🛈 Staying idle on www.indeed.com/viewjob — not the campaign tab
//   (silence → the e2e driver called the run stalled)
// The click opened a tab on www.indeed.com/job/… (not an application). Its content script
// asked AM_I_CAMPAIGN_TAB first and went idle; then the capture tick moved campaignTabId onto
// it because it was the window's active "capturable" page; then the walking tab skipped onto
// the next /viewjob and woke as "not the campaign tab". Zero automating tabs.
//
// Driven here with the REAL code: background.js's capture/adoption + AM_I_CAMPAIGN_TAB (+ the
// reclaim it now has), content.js's runPhase gate, DOM observer, init, and the Apply tails of
// the Indeed and ZipRecruiter detail phases — over one fake chrome.storage / chrome.tabs that
// every tab shares. Each tab is its own jsdom + vm context, like a real content script.
//   1. the 10-06 sequence: the walk keeps exactly one automating tab, the stray tab is closed;
//   2. the legit follow: Indeed's wizard in its own tab is adopted, WAKES (it had cached
//      "not me" — live 09-28 it sat on its form for 13 min) and the walking tab steps aside
//      instead of skipping under it; in tap mode too;
//   3. 08-15 stays closed: a tab the human opened never starts walking, is never adopted, and
//      cannot reclaim the campaign;
//   4. ZipRecruiter's "no form after 40s" path cleans up the same way.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

function sliceOf(src, startMarker, endMarker) {
  const s = src.indexOf(startMarker);
  const e = src.indexOf(endMarker, s);
  if (s < 0 || e < 0) {
    console.error(`Could not locate [${startMarker}] .. [${endMarker}] — markers moved.`);
    process.exit(2);
  }
  return src.slice(s, e);
}

// ---- content.js pieces ------------------------------------------------------------------
const RUNPHASE = sliceOf(SRC, "  const LINKEDIN_APPLY_ENABLED = false;", "  async function _runPhaseInner() {");
const OBSERVER = sliceOf(SRC, "  let phaseDebounce = null;", "  // ---- CAPTURE KIT");
const INIT = sliceOf(SRC, "  async function init() {", "  // Wait for page to be ready");
// The Indeed detail phase from the Apply click to its end (+ whatever helpers sit between it
// and waitForFormVisible); wrapped as a function of the three locals it reads.
const INDEED_TAIL =
  "async function indeedApplyTail(applyBtn, jobTitle, jobCompany) {\n" +
  sliceOf(SRC, '    log("Clicking Apply button...", "");', "  async function waitForFormVisible(");
const ZR_TAIL =
  "async function zrApplyTail(applyBtn2, jobTitle, jobCompany) {\n" +
  sliceOf(SRC, '    log("Clicking Quick Apply...", "");', "  // A job whose application was already STARTED");

// ---- background.js pieces ---------------------------------------------------------------
// Capture + adoption (+ reclaim) live between CAPTURE_HOSTS and the activity-log section.
// sendScreenshot is swapped for a recorder: CDP is not what is under test.
const BG_CAPTURE = sliceOf(BG, "const CAPTURE_HOSTS", "// Activity log").replace(
  "async function sendScreenshot(tabId) {",
  "async function __realSendScreenshot(tabId) {",
);
// The real message cases, from AM_I_CAMPAIGN_TAB up to the next handler.
const BG_CASES = sliceOf(BG, '    case "AM_I_CAMPAIGN_TAB": {', '    case "PLATFORM_EXHAUSTED": {');

// ---- one browser: shared storage + tabs ------------------------------------------------
function makeBrowser({ tabs, store }) {
  const data = { ...store };
  const listeners = [];
  const tabMap = new Map(tabs.map((t) => [t.id, { ...t }]));
  const removed = [];
  const shots = [];
  const local = {
    async get(keys) {
      const out = {};
      for (const k of [].concat(keys)) if (data[k] !== undefined) out[k] = data[k];
      return out;
    },
    async set(obj) {
      const changes = {};
      for (const [k, v] of Object.entries(obj)) {
        if (data[k] !== v) changes[k] = { oldValue: data[k], newValue: v };
        data[k] = v;
      }
      if (Object.keys(changes).length) for (const fn of listeners) fn(changes, "local");
    },
  };
  const chromeTabs = {
    async query(q) {
      return [...tabMap.values()].filter((t) =>
        (q.windowId === undefined || t.windowId === q.windowId) && (q.active === undefined || !!t.active === q.active));
    },
    async get(id) {
      const t = tabMap.get(id);
      if (!t) throw new Error(`No tab with id: ${id}`);
      return { ...t };
    },
    async update(id, props) {
      const t = tabMap.get(id);
      if (!t) throw new Error(`No tab with id: ${id}`);
      if (props.active) for (const o of tabMap.values()) if (o.windowId === t.windowId) o.active = false;
      Object.assign(t, props);
      return { ...t };
    },
    async remove(id) {
      if (!tabMap.has(id)) throw new Error(`No tab with id: ${id}`);
      tabMap.delete(id);
      removed.push(id);
    },
  };
  const bgSandbox = {
    chrome: { storage: { local }, tabs: chromeTabs },
    URL, console,
    __shots: shots,
    done: null,
  };
  vm.createContext(bgSandbox);
  vm.runInContext(
    BG_CAPTURE +
    "\nasync function sendScreenshot(tabId) { __shots.push(tabId); }\n" +
    "async function __handle(msg, sender) { switch (msg.type) {\n" + BG_CASES +
    "\n    default: return { ok: true };\n  } }\n",
    bgSandbox,
  );
  const browser = {
    data, tabMap, removed, shots, listeners,
    // A click that opens a tab: Chrome makes it the window's active tab, opener = clicker.
    openTab(t) {
      for (const o of tabMap.values()) if (o.windowId === t.windowId) o.active = false;
      tabMap.set(t.id, { active: true, ...t });
    },
    async captureTick() {
      bgSandbox.done = null;
      vm.runInContext("done = captureActiveAutomationTab();", bgSandbox);
      return await bgSandbox.done;
    },
    async handle(msg, tabId) {
      const tab = tabMap.get(tabId);
      bgSandbox.__msg = msg;
      bgSandbox.__sender = { tab: tab ? { ...tab } : undefined };
      vm.runInContext("done = __handle(__msg, __sender);", bgSandbox);
      return await bgSandbox.done;
    },
  };
  return browser;
}

// A content script on tab `tabId` (one page load = one context).
function makeTab(browser, tabId, opts = {}) {
  const url = browser.tabMap.get(tabId).url;
  const { window } = new JSDOM("<body><main id=m></main></body>", { url });
  const sent = [];
  const inner = [];
  const skips = [];
  const sandbox = {
    window, document: window.document, location: window.location, navigator: { onLine: true },
    URL, MutationObserver: window.MutationObserver,
    setTimeout, clearTimeout, console, Date,
    storageGet: async (k) => {
      const out = {};
      for (const key of [].concat(k)) if (browser.data[key] !== undefined) out[key] = browser.data[key];
      return out;
    },
    storageSet: async (o) => {
      for (const [k, v] of Object.entries(o)) browser.data[k] = v;
      return true;
    },
    storageRemove: async (k) => { for (const x of [].concat(k)) delete browser.data[x]; return true; },
    sendMsg: async (m) => {
      sent.push(m);
      // Content scripts of a closed tab are gone — nothing answers them.
      if (!browser.tabMap.has(tabId)) return null;
      return await browser.handle(m, tabId);
    },
    isCampaignRunning: async () => !!browser.data.campaignRunning,
    log: () => {}, logBackend: (t) => sent.push({ type: "__log", text: t }), safeSend: () => {},
    detectPlatform: () => opts.platform || "indeed",
    platformLabel: () => "Indeed",
    detectPhase: () => opts.phase || "detail",
    waitForOnline: async () => {},
    sleep: async () => {}, humanDelay: () => 0,
    reportPlatformAuth: () => {}, watchPlatformAuth: () => {},
    recordPendingSubmitOnConfirmation: async () => false,
    loadSelectors: async () => {}, settleIndeedAuthTransit: async () => {}, sessionWarmup: async () => {},
    // Detail-phase tail collaborators.
    shouldMisclick: () => false, performMisclick: async () => {},
    humanClick: async () => { if (opts.onClick) await opts.onClick(); },
    waitForFormVisible: async () => { if (opts.whileWaiting) await opts.whileWaiting(); return false; },
    waitForZipRecruiterForm: async () => { if (opts.whileWaiting) await opts.whileWaiting(); return false; },
    dialogSnapshot: () => "dialogs=0",
    isFormVisible: () => false,
    phase3_fillForm: async () => {},
    skipToNextJob: async () => { skips.push(window.location.href); },
    chrome: {
      runtime: { getManifest: () => ({ version: "test" }) },
      storage: { onChanged: { addListener: (fn) => browser.listeners.push(fn) } },
    },
    __inner: inner, done: null,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    "let _runPhaseActive = false;\n" +
    RUNPHASE + "\nasync function _runPhaseInner() { __inner.push(location.href); }\n" +
    OBSERVER + INIT + INDEED_TAIL + ZR_TAIL,
    sandbox,
  );
  return {
    id: tabId, sandbox, sent, inner, skips,
    async run(expr) {
      sandbox.done = null;
      vm.runInContext(`done = (async () => (${expr}))();`, sandbox);
      return await sandbox.done;
    },
    logs: () => sent.filter((m) => m.type === "__log").map((m) => m.text),
  };
}

// Let fire-and-forget work (storage listeners → runPhase) finish.
async function settle() {
  for (let i = 0; i < 30; i++) await new Promise((r) => setImmediate(r));
}

const WIN = 10;
const VIEWJOB = "https://www.indeed.com/viewjob?jk=a1b2c3d4e5f60718";
const NEXT_VIEWJOB = "https://www.indeed.com/viewjob?jk=0f1e2d3c4b5a6978";
const STRAY_JOB = "https://www.indeed.com/job/social-media-coordinator-3f9e1c2b7a6d5e40";
const WIZARD = "https://smartapply.indeed.com/beta/indeedapply/applybyapplyablejobid?jobKey=a1b2c3d4e5f60718";
const RUNNING = { campaignRunning: true, campaignTabId: 1, campaignWindowId: WIN };

(async () => {
  // ---- 1. the 10-06 sequence --------------------------------------------------------------
  {
    const b = makeBrowser({ tabs: [{ id: 1, windowId: WIN, url: VIEWJOB, active: true }], store: RUNNING });
    let stray = null;
    const walker = makeTab(b, 1, {
      onClick: async () => {
        // 22:51:12 Clicking Apply → a tab on www.indeed.com/job/… opens; its script asks first.
        b.openTab({ id: 2, windowId: WIN, url: STRAY_JOB, openerTabId: 1 });
        stray = makeTab(b, 2);
        await stray.run("init()");
      },
      // 18 s of waiting = several capture ticks with the stray as the window's active tab.
      whileWaiting: async () => { await b.captureTick(); await settle(); await b.captureTick(); await settle(); },
    });
    await walker.run('indeedApplyTail({}, "Social Media Coordinator", "daniéle laser + aesthetics")');
    await settle();

    check("the stray tab went idle at init (22:51:14) — it is not the campaign tab",
      stray.inner.length === 0 && stray.logs().some((l) => l.includes("Staying idle on www.indeed.com/job/")),
      JSON.stringify(stray.logs()));
    check("the capture tick did NOT move the campaign onto a non-application page",
      b.data.campaignTabId === 1, `campaignTabId=${b.data.campaignTabId}`);
    check("the walking tab still skipped the job (no form after Apply)",
      walker.skips.length === 1, `skips=${walker.skips.length}`);
    check("the stray tab the Apply click left open was closed", b.removed.includes(2) && !b.tabMap.has(2),
      `removed=${JSON.stringify(b.removed)}`);
    check("  …and the walking tab is back in front", b.tabMap.get(1).active === true);
    check("  …and the cleanup is in the durable log", walker.logs().some((l) => l.includes("left open")),
      JSON.stringify(walker.logs()));

    // 22:51:39 the walker lands on the next /viewjob — a fresh content script.
    b.tabMap.get(1).url = NEXT_VIEWJOB;
    const next = makeTab(b, 1);
    await next.run("init()");
    await settle();
    const automating = [next, stray].filter((t) => t.inner.length > 0).length;
    check("the next /viewjob is still the campaign tab and walks (not 'Staying idle')",
      next.inner.length === 1 && !next.logs().some((l) => l.includes("Staying idle")),
      `inner=${next.inner.length} logs=${JSON.stringify(next.logs())}`);
    check("exactly one automating tab after the abandoned apply (10-06 had zero)", automating === 1,
      `automating=${automating}`);
  }

  // ---- 2. the legit follow: Indeed's wizard in a tab of its own --------------------------
  for (const reviewMode of [false, true]) {
    const mode = reviewMode ? "tap" : "auto";
    const b = makeBrowser({
      tabs: [{ id: 1, windowId: WIN, url: VIEWJOB, active: true }],
      store: { ...RUNNING, reviewMode },
    });
    let wiz = null;
    const walker = makeTab(b, 1, {
      onClick: async () => {
        b.openTab({ id: 3, windowId: WIN, url: WIZARD, openerTabId: 1 });
        // The wizard's script asks BEFORE the capture tick adopts it (live 09-25/09-28).
        wiz = makeTab(b, 3, { phase: "form" });
        await wiz.run("init()");
      },
      whileWaiting: async () => { await b.captureTick(); await settle(); },
    });
    await walker.run('indeedApplyTail({}, "Operations Manager", "Fieldhouse")');
    await settle();

    check(`[${mode}] the wizard tab is adopted as the campaign tab`, b.data.campaignTabId === 3,
      `campaignTabId=${b.data.campaignTabId}`);
    check(`[${mode}] the wizard tab that had answered "not me" wakes and drives its form`,
      wiz.inner.length === 1, `inner=${wiz.inner.length}`);
    check(`[${mode}] the walking tab steps aside — no skip under the open form`,
      walker.skips.length === 0, `skips=${walker.skips.length}`);
    check(`[${mode}] the wizard tab is not closed`, b.tabMap.has(3) && !b.removed.includes(3),
      `removed=${JSON.stringify(b.removed)}`);
    check(`[${mode}] …and the hand-over is in the durable log`,
      walker.logs().some((l) => l.includes("went on in its own tab")), JSON.stringify(walker.logs()));
    check(`[${mode}] preview: tap never captures, auto captures the wizard`,
      reviewMode ? b.shots.length === 0 : b.shots.includes(3), `shots=${JSON.stringify(b.shots)}`);
  }

  {
    // The listener and the DOM observer both call runPhase as the wizard is adopted. Both
    // pass the entry check during its awaits — only one may drive the form.
    const b = makeBrowser({ tabs: [{ id: 3, windowId: WIN, url: WIZARD, active: true, openerTabId: 1 }], store: { ...RUNNING, campaignTabId: 3 } });
    const wiz = makeTab(b, 3, { phase: "form" });
    await wiz.run("Promise.all([runPhase(), runPhase()])");
    check("two concurrent runPhase calls drive the form once", wiz.inner.length === 1, `inner=${wiz.inner.length}`);
  }

  // ---- 3. 08-15: a tab the human opened never walks, is never adopted, cannot reclaim ----
  {
    const b = makeBrowser({
      tabs: [
        { id: 1, windowId: WIN, url: VIEWJOB, active: false },
        { id: 3, windowId: WIN, url: WIZARD, active: false, openerTabId: 1 },
        // The human's own Indeed tab in their own window.
        { id: 50, windowId: 99, url: NEXT_VIEWJOB, active: true },
      ],
      store: RUNNING,
    });
    const human = makeTab(b, 50);
    await human.run("init()");
    check("a human's Indeed tab stays idle at init", human.inner.length === 0);
    // The campaign moves (wizard adopted) — every idle tab re-asks; the human's must stay idle.
    b.tabMap.get(3).active = true;
    await b.captureTick();
    await settle();
    check("  …the campaign moved to the wizard", b.data.campaignTabId === 3, `campaignTabId=${b.data.campaignTabId}`);
    check("  …and the human's tab still does not walk", human.inner.length === 0, `inner=${human.inner.length}`);
    check("  …and does not log a second 'Staying idle' line for the same page",
      human.logs().filter((l) => l.includes("Staying idle")).length === 1, JSON.stringify(human.logs()));
    await human.run('indeedApplyTail({}, "X", "Y")');
    check("a tab outside the campaign window cannot reclaim the campaign",
      b.data.campaignTabId === 3 && b.removed.length === 0, `campaignTabId=${b.data.campaignTabId} removed=${b.removed}`);
  }
  {
    // In the campaign window: a tab the walk did NOT open (no opener; or another opener) on a
    // job page or even on the wizard is not adopted.
    const b = makeBrowser({
      tabs: [
        { id: 1, windowId: WIN, url: VIEWJOB, active: false },
        { id: 7, windowId: WIN, url: NEXT_VIEWJOB, active: true },
      ],
      store: RUNNING,
    });
    await b.captureTick();
    check("a hand-opened Indeed page in the campaign window is not adopted", b.data.campaignTabId === 1,
      `campaignTabId=${b.data.campaignTabId}`);
    check("  …and the preview keeps showing the campaign tab", b.shots[b.shots.length - 1] === 1,
      `shots=${JSON.stringify(b.shots)}`);
    b.openTab({ id: 8, windowId: WIN, url: WIZARD, openerTabId: 7 });
    await b.captureTick();
    check("a wizard opened by some OTHER tab is not adopted", b.data.campaignTabId === 1,
      `campaignTabId=${b.data.campaignTabId}`);
    b.openTab({ id: 9, windowId: WIN, url: "https://secure.indeed.com/auth?continue=https%3A%2F%2Fsmartapply.indeed.com", openerTabId: 1 });
    await b.captureTick();
    check("the wizard's sign-in bounce (secure.indeed.com/auth) opened by the walk IS adopted",
      b.data.campaignTabId === 9, `campaignTabId=${b.data.campaignTabId}`);
  }

  // ---- 4. ZipRecruiter: "no ZR form after 40s" cleans up the same way --------------------
  {
    const ZR_JOB = "https://www.ziprecruiter.com/jobs-search?search=marketing&lvk=abc";
    const b = makeBrowser({ tabs: [{ id: 1, windowId: WIN, url: ZR_JOB, active: true }], store: RUNNING });
    const walker = makeTab(b, 1, {
      platform: "ziprecruiter",
      onClick: async () => {
        b.openTab({ id: 4, windowId: WIN, url: "https://www.ziprecruiter.com/job-redirect/k/xyz", openerTabId: 1 });
        await makeTab(b, 4, { platform: "ziprecruiter" }).run("init()");
      },
      whileWaiting: async () => { await b.captureTick(); await settle(); },
    });
    await walker.run('zrApplyTail({}, "Marketing Coordinator", "Acme")');
    await settle();
    check("ZR: the campaign stays on the walking tab", b.data.campaignTabId === 1, `campaignTabId=${b.data.campaignTabId}`);
    check("ZR: the tab Quick Apply left open is closed", b.removed.includes(4), `removed=${JSON.stringify(b.removed)}`);
    check("ZR: the walk skips on from the walking tab", walker.skips.length === 1, `skips=${walker.skips.length}`);
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
