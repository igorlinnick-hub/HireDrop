// The Indeed results page is judged as a whole BEFORE any posting on it is opened. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/indeed-prejudge.test.js
//
// Why: the walk opened every card and judged it on its page, one at a time — ~18 s per
// rejected posting. 10-07: 18 rejections in a row on one phrase, 0 applied, then Indeed ran
// dry; a third of all judge calls since 09-22 re-judged a posting already rejected, because
// no live verdict was ever stored. Indeed's own results page loads every card's full posting
// in one call (/rpc/jobdescs — captured live 10-07 in Igor's logged-in Chrome: 30 postings in
// 0.36 s; tests/fixtures/indeed-rpc-jobdescs.json is that response for three cards).
//
// The real phase1_indeed runs here against the captured SERP (indeed-serp-decoy.html) with
// the captured posting texts; fetch, storage, navigation and messaging are stubbed. What
// must hold:
//   1. Cards the server skips are never opened, logged one line each in the job page's own
//      wording (run_report counts them as fit gate / company cap), and remembered locally.
//   2. Cards that fit go to pendingJobs carrying their job_id; unjudged ones go too, without.
//   3. A page where nothing fits moves on like an empty page.
//   4. Any failure (no texts, judge down) = the old path: every card opened, judged live.
//   5. Tap mode and pool runs never pre-judge (the person is the filter there).
//   6. The job page sends the stored job_id back to ASSESS_FIT.
//   7. The posting HTML becomes plain text with its line breaks.

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

// The SERP fixture's two real cards, given the captured postings' text.
const RPC = JSON.parse(FIX("indeed-rpc-jobdescs.json"));
const CAPTURED = Object.values(RPC);
const JK_FIT = "9a46b4b76cdc3d37"; // "Marketing Coordinator"
const JK_OFF = "689d5e3af5fd2ec8"; // "Events Marketing Specialist"
const LINK = (jk) => `https://www.indeed.com/viewjob?jk=${jk}`;

function world({ store, rpc, verdicts }) {
  const html = `<body>${FIX("indeed-serp-decoy.html")}</body>`;
  const url = "https://www.indeed.com/jobs?q=marketing&l=remote";
  const { window } = new JSDOM(html, { url, virtualConsole: new VirtualConsole() });
  const rec = { backend: [], prejudged: null, fetched: null, nextPage: 0, navTo: null };
  const box = {
    document: window.document,
    DOMParser: window.DOMParser,
    window: { location: { get href() { return url; }, set href(v) { rec.navTo = v; } } },
    URL, URLSearchParams, Promise, Set, Map, Date, encodeURIComponent,
    AbortController, setTimeout, clearTimeout,
    fetch: async (u) => {
      rec.fetched = u;
      if (rpc === "down") throw new Error("network");
      return { ok: true, json: async () => rpc };
    },
    MAX_APPLICATIONS_PER_PLATFORM: 15,
    isCampaignRunning: async () => true,
    getPlatformCount: async () => 0,
    detectPlatform: () => "indeed",
    isEasilyApplyCard: () => true,
    findJobCards: () => Array.from(window.document.querySelectorAll(".job_seen_beacon")),
    getAppliedUrls: async () => new Set(),
    log: () => {},
    logBackend: (t) => rec.backend.push(t),
    sleep: async () => {},
    humanDelay: () => 0,
    sendMsg: async (m) => {
      if (m && m.type === "PREJUDGE_CARDS") {
        rec.prejudged = m.data.jobs;
        return verdicts;
      }
      return {};
    },
    goToNextPage: async () => { rec.nextPage++; },
    goBackToJobList: async () => { rec.nextPage++; },
    skipToNextJob: async () => {},
    retireKeyword: async () => {},
    storageGet: async (keys) => {
      const list = typeof keys === "string" ? [keys] : keys;
      const out = {};
      for (const k of list) if (k in store) out[k] = store[k];
      return out;
    },
    storageSet: async (patch) => { Object.assign(store, patch); },
  };
  vm.createContext(box);
  vm.runInContext(`${TITLE_BLOCK}\n${INDEED_LIST}\n` +
    "this.phase1_indeed = phase1_indeed; this.postingTextFromHtml = postingTextFromHtml;", box);
  return { box, rec, store };
}

const BOTH = { [JK_FIT]: RPC["325d759b66d98c70"], [JK_OFF]: RPC["4769d19e71afa25e"] };
const KW = { campaignFilters: { keywords: ["marketing"] } };
const pending = (store) => (store.pendingJobs || []).map((j) => j.jk);

(async () => {
  // --- 7. the captured posting HTML as plain text ---------------------------------------
  {
    const { box } = world({ store: {}, rpc: {}, verdicts: null });
    const texts = CAPTURED.map((h) => box.postingTextFromHtml(h));
    check("text: no tags left in any captured posting", texts.every((t) => !/<[a-z/][^>]*>/i.test(t)));
    check("text: every captured posting is long enough to judge (>= 300)",
      texts.every((t) => t.length >= 300), JSON.stringify(texts.map((t) => t.length)));
    check("text: paragraphs keep their line breaks", texts.every((t) => t.split("\n").length > 3));
    check("text: entities decoded", !texts.some((t) => /&amp;|&#\d+;/.test(t)));
  }

  // --- 1 + 2. one skipped, one fits ---------------------------------------------------------
  {
    const { rec, store, box } = world({
      store: { ...KW }, rpc: BOTH,
      verdicts: {
        results: [
          { link: LINK(JK_FIT), job_id: "row-fit", decision: "apply", fit_score: 61, source: "judged" },
          { link: LINK(JK_OFF), job_id: "row-off", decision: "skip", fit_score: 12, source: "stored",
            reason: "Event logistics role, resume is brand strategy." },
        ],
        reused: 1,
      },
    });
    await box.phase1_indeed();
    check("one call for the page: both real cards, decoy excluded",
      rec.fetched === `/rpc/jobdescs?jks=${JK_OFF},${JK_FIT}` || rec.fetched === `/rpc/jobdescs?jks=${JK_FIT},${JK_OFF}`,
      rec.fetched);
    check("both cards sent to the judge with their full posting text",
      (rec.prejudged || []).length === 2 && rec.prejudged.every((j) => j.description.length >= 300 && j.platform === "indeed"),
      JSON.stringify((rec.prejudged || []).map((j) => j.description.length)));
    check("judge gets the canonical /viewjob?jk= link the pool keys on",
      (rec.prejudged || []).every((j) => /^https:\/\/www\.indeed\.com\/viewjob\?jk=[0-9a-f]{16}$/.test(j.link)));
    check("only the fitting card is pending", JSON.stringify(pending(store)) === JSON.stringify([JK_FIT]),
      JSON.stringify(pending(store)));
    check("the fitting card carries its job_id", store.pendingJobs[0].job_id === "row-fit");
    check("the skipped card is never opened", rec.navTo === LINK(JK_FIT), rec.navTo);
    const skipLine = rec.backend.find((t) => t.startsWith("⏭️ Skipped (fit 12): Events Marketing Specialist"));
    check("skip logged in the job page's own wording (run_report: fit gate)", !!skipLine, JSON.stringify(rec.backend));
    check("skipped jk remembered locally", (store.processedJobKeys || []).includes(JK_OFF));
    check("one summary line with the counts",
      rec.backend.some((t) => /^⚡ Judged this page ahead in [\d.]+ s: 1 fit you, 1 don't \(1 remembered/.test(t)),
      JSON.stringify(rec.backend));
  }

  // --- 2. unjudged cards are still opened (judged live), company cap wording -------------
  {
    const { rec, store, box } = world({
      store: { ...KW }, rpc: BOTH,
      verdicts: {
        results: [
          { link: LINK(JK_FIT), job_id: "row-fit", decision: "unjudged", source: "judge_unavailable" },
          { link: LINK(JK_OFF), job_id: "row-off", decision: "skip", fit_score: null, source: "company_cap",
            reason: "Company cap — already tried Acme in the last 60 days, one application per company." },
        ],
      },
    });
    await box.phase1_indeed();
    check("unjudged card pending, its job_id kept for a verdict that lands late",
      JSON.stringify(pending(store)) === JSON.stringify([JK_FIT]) && store.pendingJobs[0].job_id === "row-fit",
      JSON.stringify(store.pendingJobs));
    check("summary does not count the unjudged card as a fit",
      rec.backend.some((t) => /: 0 fit you, 1 don't, 1 checked on their page/.test(t)), JSON.stringify(rec.backend));
    check("company cap line keeps the '— Company cap —' marker run_report counts",
      rec.backend.some((t) => /^⏭️ Skipped \(fit \?\): .* — Company cap — /.test(t)), JSON.stringify(rec.backend));
    check("summary names the card left for its page", rec.backend.some((t) => /1 checked on their page/.test(t)));
  }

  // --- 3. nothing fits = next page --------------------------------------------------------
  {
    const { rec, store, box } = world({
      store: { ...KW }, rpc: BOTH,
      verdicts: {
        results: [
          { link: LINK(JK_FIT), job_id: "a", decision: "skip", fit_score: 20, reason: "no" },
          { link: LINK(JK_OFF), job_id: "b", decision: "skip", fit_score: 8, reason: "no" },
        ],
      },
    });
    await box.phase1_indeed();
    check("all skipped: next page once", rec.nextPage === 1, `nextPage=${rec.nextPage}`);
    check("all skipped: nothing opened, pendingJobs untouched", rec.navTo === null && store.pendingJobs === undefined);
  }

  // --- Broad cap spent: hand the walk on, don't page through the search --------------------
  {
    const { rec, store, box } = world({
      store: { ...KW }, rpc: BOTH,
      verdicts: {
        results: [JK_FIT, JK_OFF].map((jk) => ({
          link: LINK(jk), job_id: null, decision: "skip", source: "broad_cap",
          reason: "Broad mode daily limit reached (40 applications). Resumes tomorrow.",
        })),
      },
    });
    const sent = [];
    const orig = box.sendMsg;
    box.sendMsg = async (m) => { sent.push(m.type); return orig(m); };
    await box.phase1_indeed();
    check("broad cap: platform handed on once", sent.filter((t) => t === "PLATFORM_EXHAUSTED").length === 1, JSON.stringify(sent));
    check("broad cap: no next page, nothing opened", rec.nextPage === 0 && rec.navTo === null && store.pendingJobs === undefined);
  }

  // --- passed-on / applied rows are not fit losses -------------------------------------------
  {
    const { rec, box } = world({
      store: { ...KW }, rpc: BOTH,
      verdicts: {
        results: [
          { link: LINK(JK_FIT), job_id: "a", decision: "skip", source: "dismissed", reason: "Passed on earlier" },
          { link: LINK(JK_OFF), job_id: "b", decision: "skip", source: "applied", reason: "Already applied" },
        ],
      },
    });
    await box.phase1_indeed();
    check("dismissed/applied: no '⏭️ Skipped (fit' line", !rec.backend.some((t) => /Skipped \(fit/.test(t)), JSON.stringify(rec.backend));
    check("dismissed/applied: says why", rec.backend.some((t) => /^Skipping \(passed on earlier\): /.test(t)) &&
      rec.backend.some((t) => /^Skipping duplicate: /.test(t)));
  }

  // --- 4. failures fall back to the old path -------------------------------------------------
  for (const [name, rpc, verdicts] of [
    ["judge unavailable", BOTH, null],
    ["Indeed sent no text", {}, { results: [] }],
    ["rpc fetch failed", "down", { results: [] }],
  ]) {
    const { rec, store, box } = world({ store: { ...KW }, rpc, verdicts });
    await box.phase1_indeed();
    check(`${name}: every card pending, opened and judged live`, pending(store).length === 2,
      JSON.stringify(pending(store)));
    check(`${name}: says so in the log`, rec.backend.some((t) => /checking each posting on its page/.test(t)),
      JSON.stringify(rec.backend));
  }

  // --- 5. tap mode and pool runs never pre-judge -------------------------------------------
  for (const extra of [{ reviewMode: true }, { atsPlatform: "pool" }]) {
    const { rec, store, box } = world({ store: { ...KW, ...extra }, rpc: BOTH, verdicts: { results: [] } });
    await box.phase1_indeed();
    check(`${JSON.stringify(extra)}: no pre-judge call`, rec.prejudged === null && rec.fetched === null);
    check(`${JSON.stringify(extra)}: cards pending as before`, pending(store).length === 2);
  }

  // --- 6. the job page hands the stored verdict's id back -------------------------------
  {
    const phase2 = slice("  async function phase2_indeed() {", "  async function phase2_ziprecruiter() {");
    check("phase2_indeed looks the job_id up by the page's jk",
      /pending\.find\(\(j\) => j\.jk === jobKey\)\?\.job_id/.test(phase2));
    check("phase2_indeed sends it with ASSESS_FIT", /\.\.\.\(prejudgedId \? \{ job_id: prejudgedId \} : \{\}\)/.test(phase2));
    const bg = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
    check("background ASSESS_FIT forwards a given job_id", /let jobId = typeof q\.job_id === "string" && q\.job_id \? q\.job_id : null;/.test(bg));
    check("background routes PREJUDGE_CARDS to /tools/assess-fit-batch", /case "PREJUDGE_CARDS"[\s\S]{0,400}\/tools\/assess-fit-batch/.test(bg));
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
