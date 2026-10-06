// Submit belt + campaign-tab gate (../content.js). Run:
//
//   node chrome-extension/tests/run-all.js     (or this file directly with jsdom on NODE_PATH)
//
// The bug (Snorkel, 09-28, ext 1.8.15): a Greenhouse Submit is a FULL page load to
// /jobs/<id>/confirmation, which kills phase_ats before APPLICATION_SAVED. The record then
// hung on the NEW page's script reaching recordWokeOnPostApply — only the pool/auto walk
// branches call it, and only in the campaign tab with the run still on. The log after the
// landing said "Staying idle … not the campaign tab": the form had been filled by the DOM
// observer, which called runPhase with no campaign-tab check at all, and the confirmation
// page woke into init's idle gate. No applications row → backend dedup blind → re-applies.
//
// Pinned here by running the REAL functions (sliced out of content.js) in jsdom/vm:
//   1. posting identity: a prod-shaped confirmation URL maps to the form URL phase_ats saved;
//   2. init records a matching pending submit exactly once, before any gate — in a
//      non-campaign tab and with the campaign stopped — and never advances or automates;
//   3. stale (>10 min) or another posting's pending → nothing recorded;
//   4. the walk branch (pool #217 / auto #277) after the belt → still ONE record in total;
//   5. the DOM observer in a non-campaign tab never reaches the automation (runPhase inner).
// URL shapes only (job-boards.greenhouse.io / job-boards.eu.greenhouse.io): no page DOM is
// invented — the belt reads nothing but the URL and storage.

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

function slice(startMarker, endMarker) {
  const s = SRC.indexOf(startMarker);
  const e = SRC.indexOf(endMarker, s);
  if (s < 0 || e < 0) {
    console.error(`Could not locate [${startMarker}] .. [${endMarker}] — markers moved.`);
    process.exit(2);
  }
  return SRC.slice(s, e);
}

const POSTAPPLY = slice("  const POSTAPPLY_URL_HINTS = [", "  const SUCCESS_TEXTS = [");
const RUNPHASE = slice("  const LINKEDIN_APPLY_ENABLED = false;", "  async function _runPhaseInner() {");
const OBSERVER = slice("  let phaseDebounce = null;", "  // ---- CAPTURE KIT");
const INIT = slice("  async function init() {", "  // Wait for page to be ready");

const SNORKEL_FORM = "https://job-boards.greenhouse.io/snorkelai/jobs/5550001001";
const SNORKEL_CONF = `${SNORKEL_FORM}/confirmation`;
const CISION_FORM = "https://job-boards.eu.greenhouse.io/cision/jobs/7770002002";
const CISION_CONF = `${CISION_FORM}/confirmation`;
const MIN = 60 * 1000;

// One shared chrome.storage.local across contexts (a page reload = a new context, same storage).
function makeStore(init) {
  const data = { ...init };
  const pick = (keys) => {
    const out = {};
    for (const k of [].concat(keys)) if (data[k] !== undefined) out[k] = data[k];
    return out;
  };
  return { data, pick };
}

// A content-script context on `url`. `who` is the background's AM_I_CAMPAIGN_TAB answer.
function makeContext(url, store, opts = {}) {
  const { window } = new JSDOM("<body><main id=m></main></body>", { url });
  const sent = [];
  const inner = [];
  const sandbox = {
    window, document: window.document, location: window.location, navigator: { onLine: true },
    URL, MutationObserver: window.MutationObserver,
    setTimeout, clearTimeout, console, Date,
    storageGet: async (k) => store.pick(k),
    storageSet: async (o) => { Object.assign(store.data, o); return true; },
    storageRemove: async (k) => { for (const x of [].concat(k)) delete store.data[x]; return true; },
    sendMsg: async (m) => {
      sent.push(m);
      if (m.type === "AM_I_CAMPAIGN_TAB") return typeof opts.who === "function" ? opts.who() : (opts.who === undefined ? { known: true, isCampaignTab: true } : opts.who);
      return { ok: true };
    },
    isCampaignRunning: async () => !!store.data.campaignRunning,
    log: () => {}, logBackend: () => {}, safeSend: () => {},
    detectPlatform: () => "greenhouse",
    detectPhase: () => opts.phase || "unknown",
    waitForOnline: async () => {},
    sleep: async () => {}, humanDelay: () => 0,
    reportPlatformAuth: () => {}, watchPlatformAuth: () => {},
    loadSelectors: async () => {}, settleIndeedAuthTransit: async () => {}, sessionWarmup: async () => {},
    chrome: { runtime: { getManifest: () => ({ version: "test" }) } },
    __inner: inner, __sent: sent, done: null,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    'let _runPhaseActive = false;\n' +
    POSTAPPLY + RUNPHASE + "\nasync function _runPhaseInner() { __inner.push(location.href); }\n" +
    OBSERVER + INIT,
    sandbox,
  );
  return sandbox;
}

async function run(ctx, expr) {
  vm.runInContext(`done = (async () => (${expr}))();`, ctx);
  return await ctx.done;
}
const saved = (ctx) => ctx.__sent.filter((m) => m.type === "APPLICATION_SAVED");
const advanced = (ctx) => ctx.__sent.filter((m) => m.type === "ATS_JOB_DONE");
const pending = (url, ageMs, extra = {}) => ({
  url, jobKey: "staff ml engineer|snorkelai", title: "Staff ML Engineer", company: "Snorkelai",
  platform: "greenhouse", letter: "Dear Snorkel team", ts: Date.now() - ageMs, ...extra,
});
const NOT_CAMPAIGN = { known: true, isCampaignTab: false };

(async () => {
  // ---- 1. posting identity on prod URL shapes ---------------------------------------
  {
    const ctx = makeContext(SNORKEL_CONF, makeStore({}));
    const has = await run(ctx, 'typeof postingIdentity === "function"');
    check("postingIdentity() exists", has, "no posting identity → nothing can match a pending submit");
    if (has) {
      const id = (u) => vm.runInContext(`postingIdentity(${JSON.stringify(u)})`, ctx);
      check("GH confirmation ↔ its form URL", id(SNORKEL_CONF) === id(SNORKEL_FORM), `${id(SNORKEL_CONF)} vs ${id(SNORKEL_FORM)}`);
      check("GH EU (Cision) confirmation ↔ its form URL", id(CISION_CONF) === id(CISION_FORM), `${id(CISION_CONF)} vs ${id(CISION_FORM)}`);
      check("another posting id does not match", id(SNORKEL_CONF) !== id("https://job-boards.greenhouse.io/snorkelai/jobs/5550001002"));
      check("another host does not match", id(CISION_CONF) !== id("https://job-boards.greenhouse.io/cision/jobs/7770002002"));
      check("both confirmation shapes are post-apply pages",
        vm.runInContext(`isPostApplyPath(${JSON.stringify(new URL(SNORKEL_CONF).pathname)}) && isPostApplyPath(${JSON.stringify(new URL(CISION_CONF).pathname)})`, ctx));
    }
  }

  // ---- 2. the Snorkel case: non-campaign tab wakes on /confirmation ------------------
  {
    const store = makeStore({ campaignRunning: true, campaignTabId: 7, pendingAtsSubmit: pending(SNORKEL_FORM, 20 * 1000) });
    const ctx = makeContext(SNORKEL_CONF, store, { who: NOT_CAMPAIGN });
    await run(ctx, "init()");
    const s = saved(ctx);
    check("non-campaign tab on confirmation → exactly one APPLICATION_SAVED", s.length === 1, `got ${s.length}`);
    if (s.length) {
      const d = s[0].data;
      check("  …for the pending posting", d.job_title === "Staff ML Engineer" && d.job_url === SNORKEL_FORM, JSON.stringify(d));
      check("  …with the letter that was typed in", d.cover_letter === "Dear Snorkel team", d.cover_letter);
      check("  …as applied_unconfirmed", d.status === "applied_unconfirmed" && d.verified === false, d.status);
    }
    check("  records only: no queue advance", advanced(ctx).length === 0, `${advanced(ctx).length} ATS_JOB_DONE`);
    check("  records only: no automation in that tab", ctx.__inner.length === 0, `inner ran ${ctx.__inner.length}x`);
    check("  pending key is cleared", store.data.pendingAtsSubmit === undefined);
    // Same page woken again (reload): nothing left to record.
    const again = makeContext(SNORKEL_CONF, store, { who: NOT_CAMPAIGN });
    await run(again, "init()");
    check("  a second wake on the same page records nothing", saved(again).length === 0, `got ${saved(again).length}`);
  }

  // ---- 2b. campaign stopped between click and landing --------------------------------
  {
    const store = makeStore({ campaignRunning: false, pendingAtsSubmit: pending(SNORKEL_FORM, 60 * 1000) });
    const ctx = makeContext(SNORKEL_CONF, store);
    await run(ctx, "init()");
    check("campaign stopped → still exactly one APPLICATION_SAVED", saved(ctx).length === 1, `got ${saved(ctx).length}`);
  }

  // ---- 3. stale / non-matching pending → nothing ---------------------------------------
  {
    const store = makeStore({ campaignRunning: true, campaignTabId: 7, pendingAtsSubmit: pending(SNORKEL_FORM, 11 * MIN) });
    const ctx = makeContext(SNORKEL_CONF, store, { who: NOT_CAMPAIGN });
    await run(ctx, "init()");
    check("stale pending (11 min) → no record", saved(ctx).length === 0, `got ${saved(ctx).length}`);
    check("  stale pending is dropped", store.data.pendingAtsSubmit === undefined);
  }
  {
    const other = "https://job-boards.greenhouse.io/snorkelai/jobs/5550001002";
    const store = makeStore({ campaignRunning: true, campaignTabId: 7, pendingAtsSubmit: pending(other, 20 * 1000) });
    const ctx = makeContext(SNORKEL_CONF, store, { who: NOT_CAMPAIGN });
    await run(ctx, "init()");
    check("another posting's pending → no record", saved(ctx).length === 0, `got ${saved(ctx).length}`);
    check("  …and it is left alone", !!store.data.pendingAtsSubmit);
  }
  {
    // Not a confirmation page at all (the form itself reloaded) → nothing.
    const store = makeStore({ campaignRunning: false, pendingAtsSubmit: pending(SNORKEL_FORM, 20 * 1000) });
    const ctx = makeContext(SNORKEL_FORM, store);
    await run(ctx, "init()");
    check("form page (not post-apply) → no record", saved(ctx).length === 0, `got ${saved(ctx).length}`);
  }

  // ---- 4. walk branch after the belt → one record in total ---------------------------
  {
    // Campaign tab, pool run (#217) on the EU shape: init's belt records, then the walk
    // branch's recordWokeOnPostApply must advance without writing a second row.
    const store = makeStore({
      campaignRunning: true, campaignTabId: 7, atsPlatform: "pool",
      atsQueue: [{ title: "Account Executive", company: "Cision", applyUrl: CISION_FORM }],
      pendingAtsSubmit: pending(CISION_FORM, 30 * 1000, { title: "Account Executive", company: "Cision" }),
    });
    const ctx = makeContext(CISION_CONF, store);
    await run(ctx, "init()");
    const handled = await run(ctx, "recordWokeOnPostApply()");
    check("belt + walk branch (same page) → one APPLICATION_SAVED", saved(ctx).length === 1, `got ${saved(ctx).length}`);
    check("  the walk still advances once", handled === true && advanced(ctx).length === 1, `handled=${handled}, advanced=${advanced(ctx).length}`);
    // A later context on the same confirmation (reload in the campaign tab, auto walk #277).
    const later = makeContext(CISION_CONF, store);
    await run(later, "recordWokeOnPostApply()");
    check("  a later wake (auto walk) writes no second row", saved(later).length === 0, `got ${saved(later).length}`);
  }
  {
    // No pending at all (older build's submit / client-side nav): #217 unchanged.
    const store = makeStore({
      campaignRunning: true, atsPlatform: "greenhouse",
      atsQueue: [{ title: "Staff ML Engineer", company: "Snorkelai", applyUrl: SNORKEL_FORM }],
    });
    const ctx = makeContext(SNORKEL_CONF, store);
    await run(ctx, "recordWokeOnPostApply()");
    check("no pending → walk still records from the queue (#217)", saved(ctx).length === 1, `got ${saved(ctx).length}`);
    const again = makeContext(SNORKEL_CONF, store);
    await run(again, "recordWokeOnPostApply()");
    check("  …and a second wake on that page does not record it again", saved(again).length === 0, `got ${saved(again).length}`);
  }
  {
    // phase_ats recorded in its own context (Ashby inline success / client-side nav):
    // the pending is cleared and a later wake on a confirmation writes nothing.
    const store = makeStore({ pendingAtsSubmit: pending(SNORKEL_FORM, 5 * 1000) });
    const form = makeContext(SNORKEL_FORM, store);
    const has = await run(form, 'typeof markSubmitRecorded === "function"');
    check("markSubmitRecorded() exists (phase_ats clears the belt after its own record)", has);
    if (has) {
      await run(form, `markSubmitRecorded(${JSON.stringify(SNORKEL_FORM)})`);
      const conf = makeContext(SNORKEL_CONF, store);
      await run(conf, "init()");
      check("  in-context record → belt records nothing more", saved(conf).length === 0, `got ${saved(conf).length}`);
    }
  }

  // ---- 5. observer in a non-campaign tab never automates ------------------------------
  async function observerRun(who, extra = {}) {
    const store = makeStore({ campaignRunning: true, campaignTabId: 7, ...extra });
    const ctx = makeContext(SNORKEL_FORM, store, { who, phase: "form" });
    vm.runInContext("observer.observe(document.body, { childList: true, subtree: true });", ctx);
    ctx.document.getElementById("m").appendChild(ctx.document.createElement("form"));
    await new Promise((r) => setTimeout(r, 1300));
    vm.runInContext("observer.disconnect();", ctx);
    return { ctx, store };
  }
  {
    const { ctx } = await observerRun(NOT_CAMPAIGN);
    check("observer in a non-campaign tab → no automation", ctx.__inner.length === 0, `inner ran ${ctx.__inner.length}x`);
    const k = await observerRun({ known: false });
    check("observer with no campaign tab known → no automation", k.ctx.__inner.length === 0, `inner ran ${k.ctx.__inner.length}x`);
    const ok = await observerRun({ known: true, isCampaignTab: true });
    check("observer in the campaign tab → automates (control)", ok.ctx.__inner.length >= 1, `inner ran ${ok.ctx.__inner.length}x`);
    const silent = await observerRun(null);
    check("observer, background silent → fails open (as init always did)", silent.ctx.__inner.length >= 1, `inner ran ${silent.ctx.__inner.length}x`);
  }
  {
    // The answer is cached, but not past a campaign-tab change (background moves the
    // campaign to the window's active tab).
    let calls = 0;
    let answer = NOT_CAMPAIGN;
    const store = makeStore({ campaignRunning: true, campaignTabId: 7 });
    const ctx = makeContext(SNORKEL_FORM, store, { who: () => { calls++; return answer; } });
    await run(ctx, "runPhase()");
    await run(ctx, "runPhase()");
    check("campaign-tab answer is cached", calls === 1 && ctx.__inner.length === 0, `asked ${calls}x, inner ${ctx.__inner.length}x`);
    store.data.campaignTabId = 9;
    answer = { known: true, isCampaignTab: true };
    await run(ctx, "runPhase()");
    check("  …and re-asked when campaignTabId changes", calls === 2 && ctx.__inner.length === 1, `asked ${calls}x, inner ${ctx.__inner.length}x`);
  }

  // ---- structural: the key is written before the click, and is user-scoped ------------
  {
    const atsIdx = SRC.indexOf("async function phase_ats(");
    const clickIdx = SRC.indexOf("await humanClick(submitBtn);", atsIdx);
    const pendIdx = SRC.lastIndexOf("pendingAtsSubmit: {", clickIdx);
    check("phase_ats writes pendingAtsSubmit before the Submit click", pendIdx > atsIdx && pendIdx < clickIdx);
    const scoped = /const USER_SCOPED_KEYS = \[([\s\S]*?)\];/.exec(BG);
    check("pendingAtsSubmit is user-scoped (cleared on account switch)",
      !!scoped && scoped[1].includes('"pendingAtsSubmit"') && scoped[1].includes('"lastRecordedSubmit"'));
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });
