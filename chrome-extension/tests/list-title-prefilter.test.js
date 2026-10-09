// The title gate runs on the CARD in the list phase, before a job page is opened. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/list-title-prefilter.test.js
//
// Why: titleMatchesKeywords used to run only in the detail phases, so every off-title card
// was OPENED first and rejected after the page load. Activity log, 14 days to 10-06: 298
// "Skip (title mismatch)" lines over 10 runs (3 users) — ~30 wasted job-page opens per run;
// Igor's 10-05 22:59 run opened 26 postings only to skip them on title, against 11 applied.
// Each open is walk time plus one more page view for Indeed's bot detection.
//
// The real list phases (phase1_indeed, phase1_ziprecruiter) run here against REAL captured
// SERPs (tests/fixtures/indeed-serp-decoy.html, ziprecruiter-serp-right-pane.html), with
// storage, navigation and messaging stubbed. What must hold:
//   1. Off-title cards never reach pendingJobs and are never navigated to.
//   2. They are still HARVESTED to the pool (shared crawl index — another user's match).
//   3. One summary line per page in the backend log, not one per card.
//   4. A page where EVERY card is off-title moves on like an empty page: next page/phrase,
//      nothing written to pendingJobs, and the phrase is NOT retired.
//   5. Pool swipe run: the user's pick is never vetoed by keywords (same as detail phase).
//   6. Indeed decoy cards stay out of both the walk and the harvest.
//   7. List and detail read the SAME rule and keywords (titleGateKeywords).

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM, VirtualConsole } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const FIX = (f) => fs.readFileSync(path.join(__dirname, "fixtures", f), "utf8");

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

const TITLE_BLOCK = slice("  // ── Title relevance", "  // A react-select keeps");
const INDEED_LIST = slice("  function extractCardInfo(card) {", "  async function getAppliedUrls() {");
const ZR_LIST = slice("  async function phase1_ziprecruiter() {", "  async function phase2_ziprecruiter() {");
// ZR cards carry their place line (tests/zr-submit-truth.test.js covers the reader itself).
const ZR_CARD_PLACE = slice("  function readZipRecruiterCardLocation(cardEl) {", "  function readZipRecruiterLocation(");
const ZR_BADGE = (SRC.match(/^ {2}const ZR_NATIVE_BADGE_RE = .*$/m) || [""])[0];
if (!ZR_BADGE) { console.error("ZR_NATIVE_BADGE_RE moved"); process.exit(2); }

function world({ html, url, platform, store }) {
  const { window } = new JSDOM(html, { url, virtualConsole: new VirtualConsole() });
  const rec = { backend: [], ingested: [], nextPage: 0, retired: 0, navTo: null, sets: [] };
  const box = {
    document: window.document,
    window: { location: { get href() { return url; }, set href(v) { rec.navTo = v; } } },
    URL, URLSearchParams, Promise, Set,
    MAX_APPLICATIONS_PER_PLATFORM: 15,
    isCampaignRunning: async () => true,
    getPlatformCount: async () => 0,
    // First-SERP city check (tests/indeed-first-search-city.test.js): nothing armed here.
    correctFirstIndeedSearch: async () => false,
    detectPlatform: () => platform,
    // The logged-out capture carries no "Easily apply" chip (iafilter=1 shows only those
    // cards live), so the chip check is the one thing stubbed on the Indeed side.
    isEasilyApplyCard: () => true,
    findJobCards: () => Array.from(window.document.querySelectorAll(".job_seen_beacon")),
    getAppliedUrls: async () => new Set(),
    log: () => {},
    logBackend: (t) => rec.backend.push(t),
    sleep: async () => {},
    humanDelay: () => 0,
    sendMsg: async (m) => { if (m && m.type === "INGEST_JOBS") rec.ingested.push(...m.data.jobs); return {}; },
    goToNextPage: async () => { rec.nextPage++; },
    goBackToJobList: async () => { rec.nextPage++; },
    skipToNextJob: async () => {},
    retireKeyword: async () => { rec.retired++; },
    storageGet: async (keys) => {
      const list = typeof keys === "string" ? [keys] : keys;
      const out = {};
      for (const k of list) if (k in store) out[k] = store[k];
      return out;
    },
    storageSet: async (patch) => { rec.sets.push(patch); Object.assign(store, patch); },
  };
  vm.createContext(box);
  vm.runInContext(`${TITLE_BLOCK}\n${ZR_BADGE}\n${INDEED_LIST}\n${ZR_CARD_PLACE}\n${ZR_LIST}\n` +
    "this.phase1_indeed = phase1_indeed; this.phase1_ziprecruiter = phase1_ziprecruiter;", box);
  return { box, rec, store };
}

const INDEED_URL = "https://www.indeed.com/jobs?q=project+coordinator&l=remote";
const ZR_URL = "https://www.ziprecruiter.com/jobs-search?search=social+media+content+creator&location=Houston%2C+TX";
const jkOf = (j) => j.jk || ((j.link || "").match(/jk=([0-9a-z]+)/) || [])[1];
const pendingOf = (store) => (store.pendingJobs || []).map((j) => j.title);

(async () => {
  const indeedHtml = `<body>${FIX("indeed-serp-decoy.html")}</body>`;

  // --- Indeed, mixed page ---------------------------------------------------------------
  {
    const { rec, store, box } = world({
      html: indeedHtml, url: INDEED_URL, platform: "indeed",
      store: { campaignFilters: { keywords: ["Project Coordinator"] } },
    });
    await box.phase1_indeed();
    check("indeed: the matching card is the only pending job",
      JSON.stringify(pendingOf(store)) === JSON.stringify(["Marketing Coordinator"]), JSON.stringify(pendingOf(store)));
    check("indeed: the off-title card is never opened",
      rec.navTo === "https://www.indeed.com/viewjob?jk=9a46b4b76cdc3d37", rec.navTo);
    const harvested = rec.ingested.map(jkOf).sort();
    check("indeed: the off-title card is still harvested to the pool",
      JSON.stringify(harvested) === JSON.stringify(["689d5e3af5fd2ec8", "9a46b4b76cdc3d37"]), JSON.stringify(harvested));
    check("indeed: decoy stays out of the harvest", !harvested.includes("a1b2c3d4e5f67890"));
    const lines = rec.backend.filter((t) => /title doesn't match your roles/.test(t));
    check("indeed: ONE summary line for the page",
      lines.length === 1 && /^Skipped 1 of 2 cards: title doesn't match your roles/.test(lines[0]), JSON.stringify(rec.backend));
    check("indeed: summary names the skipped title",
      /Events Marketing Specialist/.test(lines[0] || ""), lines[0]);
  }

  // --- Indeed, every card off-title = an empty page ------------------------------------
  {
    const { rec, store, box } = world({
      html: indeedHtml, url: INDEED_URL, platform: "indeed",
      store: { campaignFilters: { keywords: ["Welder", "Fabrication"] } },
    });
    await box.phase1_indeed();
    check("indeed all-filtered: moves to the next page exactly once", rec.nextPage === 1, `nextPage=${rec.nextPage}`);
    check("indeed all-filtered: no job opened", rec.navTo === null, rec.navTo);
    check("indeed all-filtered: pendingJobs untouched", store.pendingJobs === undefined, JSON.stringify(store.pendingJobs));
    check("indeed all-filtered: phrase not retired", rec.retired === 0);
    check("indeed all-filtered: both real cards still harvested", rec.ingested.length === 2, rec.ingested.length);
    check("indeed all-filtered: the log says why, not 'No Easy Apply jobs'",
      rec.backend.some((t) => /^Skipped 2 of 2 cards/.test(t)) &&
      !rec.backend.some((t) => /No Easy Apply jobs/.test(t)), JSON.stringify(rec.backend));
  }

  // --- Indeed, pool swipe run: keywords never veto the pick ----------------------------
  {
    const { rec, store, box } = world({
      html: indeedHtml, url: INDEED_URL, platform: "indeed",
      store: { atsPlatform: "pool", campaignFilters: { keywords: ["Welder"] } },
    });
    await box.phase1_indeed();
    check("indeed pool run: nothing skipped on title", pendingOf(store).length === 2, JSON.stringify(pendingOf(store)));
    check("indeed pool run: no summary line", !rec.backend.some((t) => /title doesn't match/.test(t)));
  }

  // --- Indeed, no keywords = no filter ------------------------------------------------
  {
    const { store, box } = world({
      html: indeedHtml, url: INDEED_URL, platform: "indeed", store: { campaignFilters: { keywords: [] } },
    });
    await box.phase1_indeed();
    check("indeed no keywords: every real card pending", pendingOf(store).length === 2, JSON.stringify(pendingOf(store)));
  }

  // --- ZipRecruiter, mixed page (20 Quick Apply cards, captured live 10-05) -------------
  const zrHtml = FIX("ziprecruiter-serp-right-pane.html");
  {
    const { rec, store, box } = world({
      html: zrHtml, url: ZR_URL, platform: "ziprecruiter",
      store: { campaignFilters: { keywords: ["social media content creator"] } },
    });
    await box.phase1_ziprecruiter();
    const pending = pendingOf(store);
    const OFF = ["Local Videographer - Houston", "Marketing Coordinator", "Marketing Intern",
      "Marketing & Resident Experience Specialist - Cullen Oaks"];
    check("zr: 16 of 20 cards pending", pending.length === 16, `${pending.length}`);
    check("zr: none of the off-title cards is pending",
      OFF.every((t) => !pending.includes(t)), JSON.stringify(pending));
    check("zr: all 20 cards harvested", rec.ingested.length === 20, `${rec.ingested.length}`);
    check("zr: harvest includes the off-title ones",
      OFF.every((t) => rec.ingested.some((j) => j.title === t)));
    const lines = rec.backend.filter((t) => /title doesn't match your roles/.test(t));
    check("zr: ONE summary line for the page",
      lines.length === 1 && /^Skipped 4 of 20 cards/.test(lines[0]), JSON.stringify(rec.backend));
    check("zr: opens the first matching card", rec.navTo === store.pendingJobs[0].url && /lk=/.test(rec.navTo || ""), rec.navTo);
    check("zr: every pending card keeps its place line (History's place filter)",
      store.pendingJobs.every((j) => /, [A-Z]{2}\b/.test(j.location || "")),
      JSON.stringify(store.pendingJobs.map((j) => j.location).slice(0, 3)));
  }

  // --- ZipRecruiter, all off-title: next page, phrase NOT retired ----------------------
  {
    const { rec, store, box } = world({
      html: zrHtml, url: ZR_URL, platform: "ziprecruiter",
      store: { campaignFilters: { keywords: ["Welder"] } },
    });
    await box.phase1_ziprecruiter();
    check("zr all-filtered: back to the list once (rotates)", rec.nextPage === 1, `nextPage=${rec.nextPage}`);
    check("zr all-filtered: phrase not retired (it HAD results)", rec.retired === 0);
    check("zr all-filtered: nothing opened, pendingJobs untouched",
      rec.navTo === null && store.pendingJobs === undefined);
    check("zr all-filtered: still harvested", rec.ingested.length === 20, `${rec.ingested.length}`);
  }

  // --- one rule: both detail-phase backstops read the same keywords ---------------------
  {
    const uses = SRC.match(/titleMatchesKeywords\(jobTitle, await titleGateKeywords\(\)\)/g) || [];
    check("detail phases (Indeed + ZR) use the same gate keywords as the list phase", uses.length === 2, `${uses.length}`);
    const own = (SRC.match(/titleMatchesKeywords\(/g) || []).length;
    // definition + splitCardsByTitle + the two backstops; a third private copy would show here.
    check("no other caller with its own keyword reading", own === 4, `${own}`);
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
