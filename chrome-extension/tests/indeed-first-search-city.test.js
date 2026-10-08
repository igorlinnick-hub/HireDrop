// The FIRST Indeed search of a run must use the campaign's city/radius/job type. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/indeed-first-search-city.test.js
//
// The bug (10-08 05:16Z live run; same on 10-06 19:58Z and 22:42Z): profile city
// "San Diego, California, US", radius 25, part-time. sessionWarmup reaches the first SERP
// by TYPING into Indeed's homepage form (a cold URL jump trips Cloudflare Turnstile), but
// it typed only `q`. 'where' kept Indeed's remembered location (Hawaii), radius/jt were
// never applied, the SERP was Waikiki/O'ahu/Pearl Harbor, and the first application went
// to a Hawaii employer. Later pages are URLs built from the filters, so they were right.
//
// The fix has two halves, both driven here with the real functions cut from content.js:
//   1. the warmup types the campaign's `l` into 'where', replacing the pre-filled city;
//   2. the first SERP is compared with campaignTargetUrl once (q/l/radius/jt) and, if it
//      dropped any of them, reopened by URL exactly one time — never a loop.
// A campaign with NO location keeps the old behaviour (nothing typed, nothing checked).
//
// Fixture note: no captured Indeed HOMEPAGE markup exists in tests/fixtures, so the form
// below is minimal hand-built DOM using Indeed's known ids (#text-input-what/-where).
// Live verification of the 'where' selector is pending.

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const CS = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

// Cut a 2-space-indented function (content.js lives inside one IIFE) out of the source.
function cut(header) {
  const i = CS.indexOf(header);
  if (i < 0) return null;
  const k = CS.indexOf("\n  }\n", i);
  return CS.slice(i, k + 4);
}
function constLine(name) {
  const m = CS.match(new RegExp(`\\n  (const ${name} = [^\\n]*;)`));
  return m ? m[1] : null;
}

const TARGET = "https://www.indeed.com/jobs?q=marketing&l=San+Diego%2C+California%2C+US&radius=25&jt=parttime&iafilter=1";
const TARGET_NO_LOC = "https://www.indeed.com/jobs?q=marketing&jt=parttime&iafilter=1";

const SRC = {
  urlsSameJob: cut("  function urlsSameJob(a, b) {"),
  setNativeValue: cut("  function setNativeValue(el, value) {"),
  typeValue: cut("  async function typeValue(el, value) {"),
  findWhere: cut("  function findIndeedWhereInput(whatInput) {"),
  drift: cut("  function indeedSearchDrift(serpHref, targetHref) {"),
  correct: cut("  async function correctFirstIndeedSearch() {"),
  warmup: cut("  async function sessionWarmup() {"),
  params: constLine("INDEED_SEARCH_PARAMS"),
  ttl: constLine("INDEED_SEARCH_CHECK_TTL_MS"),
};
for (const [k, v] of Object.entries(SRC)) check(`content.js has ${k}`, !!v, "not found — renamed or removed?");

// One sandbox = one page: a jsdom document, a fake chrome.storage, recorded navigations.
function makeEnv({ href, html, storage, removeFails = false }) {
  const dom = new JSDOM(`<!doctype html><body>${html || ""}</body>`, { url: "https://www.indeed.com/" });
  const store = { ...storage };
  const navs = [];
  const clicks = [];
  const logs = [];
  const loc = { _href: href };
  Object.defineProperty(loc, "href", {
    get() { return this._href; },
    set(v) { navs.push(v); this._href = v; },
  });
  const win = { location: loc, innerWidth: 1280, innerHeight: 900, scrollBy() {} };
  const deps = {
    window: win,
    document: dom.window.document,
    HTMLInputElement: dom.window.HTMLInputElement,
    HTMLTextAreaElement: dom.window.HTMLTextAreaElement,
    Event: dom.window.Event,
    KeyboardEvent: dom.window.KeyboardEvent,
    sleep: async () => {},
    humanDelay: () => 0,
    moveCursorTo: async () => {},
    humanClick: async (el) => { clicks.push(el); }, // no real click: jsdom can't submit forms
    log: (m) => logs.push(m),
    logBackend: (m) => logs.push(m),
    detectPlatform: () => "indeed",
    isCampaignRunning: async () => !!store.campaignRunning,
    storageGet: async (keys) => {
      const out = {};
      for (const k of [].concat(keys)) if (k in store) out[k] = store[k];
      return out;
    },
    storageSet: async (obj) => { Object.assign(store, obj); return true; },
    storageRemove: async (keys) => {
      if (removeFails) return false;
      for (const k of [].concat(keys)) delete store[k];
      return true;
    },
  };
  const names = Object.keys(deps);
  // Missing pieces are left out (and already reported above) so the warmup half still
  // runs against an older content.js — that is how this file proves it fails there.
  const body = [SRC.params, SRC.ttl, SRC.urlsSameJob, SRC.setNativeValue, SRC.typeValue,
    SRC.findWhere, SRC.drift, SRC.correct, SRC.warmup].filter(Boolean).join("\n");
  const pick = ["sessionWarmup", "correctFirstIndeedSearch", "indeedSearchDrift"]
    .map((n) => `${n}: typeof ${n} === "function" ? ${n} : () => { throw new Error("${n} missing"); }`)
    .join(", ");
  const api = new Function(...names, `${body}\nreturn { ${pick} };`)(...names.map((n) => deps[n]));
  return { api, store, navs, clicks, logs, doc: dom.window.document };
}

// Indeed's homepage search box, with the city Indeed remembered for this browser.
const HOME_FORM = `<form action="/jobs">
  <input id="text-input-what" name="q" aria-label="search: Job title, keywords, or company" value="">
  <input id="text-input-where" name="l" aria-label="Edit location" placeholder="City, state, zip code, or &quot;remote&quot;" value="Hawaii">
  <button type="submit">Find jobs</button>
</form>`;

(async () => {
  if (!SRC.warmup || !SRC.typeValue || !SRC.setNativeValue || !SRC.urlsSameJob) {
    console.log(`\n${failures} FAILED`);
    process.exit(1);
  }
  // A section that needs a function this content.js lacks counts as failed, not crashed.
  const section = async (name, fn) => {
    try { await fn(); } catch (e) { check(name, false, e.message); }
  };

  // ---- 1. Warmup types the campaign city into 'where', replacing Indeed's remembered one --
  {
    const env = makeEnv({
      href: "https://www.indeed.com/",
      html: HOME_FORM,
      storage: { campaignRunning: true, campaignWarmedUp: false, campaignTargetUrl: TARGET },
    });
    await env.api.sessionWarmup();
    const what = env.doc.getElementById("text-input-what");
    const where = env.doc.getElementById("text-input-where");
    check("warmup types the keyword into 'what'", what.value === "marketing", what.value);
    check("warmup replaces the pre-filled city with the campaign's",
      where.value === "San Diego, California, US", JSON.stringify(where.value));
    check("…and submits the form (no URL jump on the cold hop)",
      env.clicks.some((el) => el.tagName === "BUTTON") && env.navs.length === 0,
      `clicks=${env.clicks.map((e) => e.tagName)} navs=${env.navs}`);
    check("the city is typed BEFORE the submit click",
      env.clicks.indexOf(where) > -1 && env.clicks.indexOf(where) < env.clicks.findIndex((e) => e.tagName === "BUTTON"),
      env.clicks.map((e) => e.id || e.tagName).join(","));
    check("the first-SERP check is armed with the campaign URL",
      env.store.indeedSearchCheck && env.store.indeedSearchCheck.url === TARGET, JSON.stringify(env.store.indeedSearchCheck));
  }

  // ---- 2. SERP that lost the city/radius/jt → exactly ONE navigation to the target -------
  const armed = () => ({ url: TARGET, at: Date.now() });
  await section("2. SERP that lost the city/radius/jt → exactly ONE navigation to the target", async () => {
    const env = makeEnv({
      href: "https://www.indeed.com/jobs?q=marketing&l=Hawaii&from=searchOnHP",
      storage: { campaignRunning: true, indeedSearchCheck: armed() },
    });
    const went = await env.api.correctFirstIndeedSearch();
    check("wrong-city SERP → reopened with the campaign URL",
      went === true && env.navs.length === 1 && env.navs[0] === TARGET, JSON.stringify(env.navs));
    check("…and the check is consumed (one shot)", !("indeedSearchCheck" in env.store));

    // ---- 5. Second visit after the correction → no second navigation ---------------------
    const again = await env.api.correctFirstIndeedSearch();
    check("second visit after the correction does not navigate again",
      again === false && env.navs.length === 1, JSON.stringify(env.navs));
  });
  await section("2. SERP that lost the city/radius/jt → exactly ONE navigation to the target", async () => {
    const env = makeEnv({
      href: "https://www.indeed.com/jobs?q=marketing&from=searchOnHP",
      storage: { campaignRunning: true, indeedSearchCheck: armed() },
    });
    await env.api.correctFirstIndeedSearch();
    check("SERP with NO l → one navigation to the target",
      env.navs.length === 1 && env.navs[0] === TARGET, JSON.stringify(env.navs));
  });
  await section("2. SERP that lost the city/radius/jt → exactly ONE navigation to the target", async () => {
    // Right city but the form dropped radius/jt (it cannot carry them) → still corrected.
    const env = makeEnv({
      href: "https://www.indeed.com/jobs?q=marketing&l=San+Diego%2C+California%2C+US&from=searchOnHP",
      storage: { campaignRunning: true, indeedSearchCheck: armed() },
    });
    await env.api.correctFirstIndeedSearch();
    check("right city, missing radius/jt → one navigation",
      env.navs.length === 1 && env.navs[0] === TARGET, JSON.stringify(env.navs));
  });
  await section("2. SERP that lost the city/radius/jt → exactly ONE navigation to the target", async () => {
    // Indeed rewrites the corrected URL again: the check is gone, so no loop.
    const env = makeEnv({
      href: "https://www.indeed.com/jobs?q=marketing&l=Hawaii",
      storage: { campaignRunning: true, indeedSearchCheck: armed() },
    });
    await env.api.correctFirstIndeedSearch();
    await env.api.correctFirstIndeedSearch();
    await env.api.correctFirstIndeedSearch();
    check("a SERP that keeps coming back wrong never loops", env.navs.length === 1, JSON.stringify(env.navs));
  });

  // ---- 3. SERP already matching → no navigation ----------------------------------------
  await section("3. SERP already matching → no navigation", async () => {
    const env = makeEnv({
      href: "https://www.indeed.com/jobs?q=Marketing&l=san+diego,+california,+us&radius=25&jt=parttime&vjk=abc123&from=searchOnHP",
      storage: { campaignRunning: true, indeedSearchCheck: armed() },
    });
    const went = await env.api.correctFirstIndeedSearch();
    check("matching SERP (case/extra params aside) → no navigation",
      went === false && env.navs.length === 0, JSON.stringify(env.navs));
    check("…and the check is still consumed", !("indeedSearchCheck" in env.store));
  });

  // ---- 4. No-location campaign → unchanged behaviour -----------------------------------
  await section("4. No-location campaign → unchanged behaviour", async () => {
    const env = makeEnv({
      href: "https://www.indeed.com/",
      html: HOME_FORM,
      storage: { campaignRunning: true, campaignWarmedUp: false, campaignTargetUrl: TARGET_NO_LOC },
    });
    await env.api.sessionWarmup();
    const where = env.doc.getElementById("text-input-where");
    check("no-location campaign leaves 'where' as Indeed filled it", where.value === "Hawaii", where.value);
    check("…types the keyword and submits as before",
      env.doc.getElementById("text-input-what").value === "marketing" &&
      env.clicks.some((el) => el.tagName === "BUTTON") && env.navs.length === 0);
    check("…and arms no SERP check", !("indeedSearchCheck" in env.store), JSON.stringify(env.store.indeedSearchCheck));
    // Its SERP then comes back in Indeed's remembered city — and is left alone, as today.
    const serp = makeEnv({
      href: "https://www.indeed.com/jobs?q=marketing&l=Hawaii",
      storage: { ...env.store },
    });
    const went = await serp.api.correctFirstIndeedSearch();
    check("…so its SERP is never redirected", went === false && serp.navs.length === 0, JSON.stringify(serp.navs));
  });
  await section("4. No-location campaign → unchanged behaviour", async () => {
    // A stale check from an earlier run must not survive into a fresh warmup.
    const env = makeEnv({
      href: "https://www.indeed.com/",
      html: HOME_FORM,
      storage: { campaignRunning: true, campaignWarmedUp: false, campaignTargetUrl: TARGET_NO_LOC,
        indeedSearchCheck: { url: TARGET, at: Date.now() } },
    });
    await env.api.sessionWarmup();
    check("a fresh warmup drops a leftover check from an earlier run/board",
      !("indeedSearchCheck" in env.store), JSON.stringify(env.store.indeedSearchCheck));
  });

  // ---- Fail-safe edges -------------------------------------------------------------------
  await section("Fail-safe edges", async () => {
    const env = makeEnv({
      href: "https://www.indeed.com/jobs?q=marketing&l=Hawaii",
      storage: { campaignRunning: true, indeedSearchCheck: { url: TARGET, at: Date.now() - 11 * 60 * 1000 } },
    });
    await env.api.correctFirstIndeedSearch();
    check("a stale check (its SERP never loaded) is dropped, not acted on",
      env.navs.length === 0 && !("indeedSearchCheck" in env.store), JSON.stringify(env.navs));
  });
  await section("Fail-safe edges", async () => {
    // A fresh check met on a deeper page (start>0) — e.g. the form never navigated and a
    // recovery took the tab further — is not the first search: leave the walk where it is.
    const env = makeEnv({
      href: "https://www.indeed.com/jobs?q=project+manager&l=Hawaii&start=10",
      storage: { campaignRunning: true, indeedSearchCheck: armed() },
    });
    await env.api.correctFirstIndeedSearch();
    check("a deeper page never gets sent back to the first search",
      env.navs.length === 0 && !("indeedSearchCheck" in env.store), JSON.stringify(env.navs));
  });
  await section("Fail-safe edges", async () => {
    // The walk's own page 1 carries start=0 (goBackToIndeedJobList) — still the walk's URL,
    // e.g. keyword 2 page 1 within the TTL: it must not be pulled back to keyword 1.
    const env = makeEnv({
      href: "https://www.indeed.com/jobs?q=project+manager&l=San+Diego%2C+California%2C+US&radius=25&jt=parttime&sort=date&start=0",
      storage: { campaignRunning: true, indeedSearchCheck: armed() },
    });
    await env.api.correctFirstIndeedSearch();
    check("a walk-built page with start=0 is left alone",
      env.navs.length === 0 && !("indeedSearchCheck" in env.store), JSON.stringify(env.navs));
  });
  await section("Fail-safe edges", async () => {
    const env = makeEnv({
      href: "https://www.indeed.com/jobs?q=marketing&l=Hawaii",
      storage: { campaignRunning: true, indeedSearchCheck: armed() },
      removeFails: true,
    });
    await env.api.correctFirstIndeedSearch();
    check("if the check cannot be consumed, do not navigate (no loop possible)", env.navs.length === 0,
      JSON.stringify(env.navs));
  });
  await section("Fail-safe edges", async () => {
    const env = makeEnv({
      href: "https://www.indeed.com/jobs?q=marketing&l=Hawaii",
      storage: { campaignRunning: false, indeedSearchCheck: armed() },
    });
    const went = await env.api.correctFirstIndeedSearch();
    check("Stop during the pause → no navigation, and the scan still stops", went === true && env.navs.length === 0);
  });
  await section("Fail-safe edges", async () => {
    // Warmup already sitting on a SERP with the right keyword but the wrong city: no form,
    // but the check is armed so phase1 fixes it before scanning.
    const env = makeEnv({
      href: "https://www.indeed.com/jobs?q=marketing&l=Hawaii",
      storage: { campaignRunning: true, campaignWarmedUp: false, campaignTargetUrl: TARGET },
    });
    await env.api.sessionWarmup();
    check("warmup on an already-matching-q SERP arms the check too",
      env.store.indeedSearchCheck && env.store.indeedSearchCheck.url === TARGET && env.navs.length === 0);
  });

  // ---- Pure drift --------------------------------------------------------------------------
  await section("Pure drift", async () => {
    const { api } = makeEnv({ href: "https://www.indeed.com/", storage: {} });
    const d = api.indeedSearchDrift("https://www.indeed.com/jobs?q=marketing&l=San+Diego%2C+California%2C+US", TARGET);
    check("drift names exactly the dropped params", JSON.stringify(d) === '["radius","jt"]', JSON.stringify(d));
    check("iafilter alone is not drift",
      api.indeedSearchDrift(TARGET.replace("&iafilter=1", ""), TARGET).length === 0);
  });

  // ---- STRUCTURAL: phase1_indeed corrects BEFORE anything reads the SERP ------------------
  {
    const p1 = cut("  async function phase1_indeed() {") || "";
    const at = p1.indexOf("correctFirstIndeedSearch()");
    check("phase1_indeed runs the first-search check", at > -1);
    check("…before the empty-q guard", at > -1 && at < p1.indexOf("Redirected to empty search"));
    check("…before the scan", at > -1 && at < p1.indexOf("Scanning job list"));
    check("…and stops when it navigates", /if \(await correctFirstIndeedSearch\(\)\) return;/.test(p1));
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
