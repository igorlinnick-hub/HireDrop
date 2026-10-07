// The Indeed results page is judged as a whole BEFORE any posting on it is opened. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/indeed-prejudge.test.js
//
// Why: the walk opened every card and judged it on its page, one at a time — ~18 s per
// rejected posting. 10-07: 18 rejections in a row on one phrase, 0 applied, then Indeed ran
// dry; a third of all judge calls since 09-22 re-judged a posting already rejected, because
// no live verdict was ever stored. A person reads the results the same way: click a card, the
// posting shows in the right-hand pane (live 10-07: ~0.45 s per posting, Indeed's own
// request). tests/fixtures/indeed-serp-pane.html is that pane, captured live.
//
// The real phase1_indeed runs here against the captured SERP (indeed-serp-decoy.html) and
// the captured pane; a card click is simulated the way the page answers it (vjk in the URL,
// the pane redrawn). Storage, navigation and messaging are stubbed. What must hold:
//   1. Cards the server skips are never opened, logged one line each in the job page's own
//      wording (run_report counts them as fit gate / company cap), and remembered locally.
//   2. Cards that fit go to pendingJobs carrying their job_id; unjudged ones go too.
//   3. A page where nothing fits moves on like an empty page; Broad cap hands the walk on.
//   4. Any failure (pane unreadable, judge down) = the old path: every card opened, judged live.
//   5. Tap mode and pool runs never pre-judge (the person is the filter there).
//   6. The job page sends the stored job_id back to ASSESS_FIT.
//   7. The pane's text is the posting, not its CSS; /rpc/jobdescs is never called.

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
const PANE = FIX("indeed-serp-pane.html");

const JK_FIT = "9a46b4b76cdc3d37"; // "Marketing Coordinator"
const JK_OFF = "689d5e3af5fd2ec8"; // "Events Marketing Specialist"
const LINK = (jk) => `https://www.indeed.com/viewjob?jk=${jk}`;

function world({ store, verdicts, paneFor }) {
  const html = `<body>${FIX("indeed-serp-decoy.html")}<div id="vjs-pane"></div></body>`;
  let url = "https://www.indeed.com/jobs?q=marketing&l=remote";
  const { window } = new JSDOM(html, { url, virtualConsole: new VirtualConsole() });
  const doc = window.document;
  const rec = { backend: [], judged: [], clicks: [], fetched: [], nextPage: 0, navTo: null, sent: [] };
  // The results page's answer to a card click: vjk follows the card, the pane redraws.
  for (const a of doc.querySelectorAll("a")) {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      const jk = a.getAttribute("data-jk") || (a.getAttribute("href") || "").match(/jk=([0-9a-z]+)/)?.[1];
      rec.clicks.push(jk);
      const pane = (paneFor || (() => PANE))(jk);
      url = `https://www.indeed.com/jobs?q=marketing&l=remote&vjk=${jk}`;
      doc.getElementById("vjs-pane").innerHTML = pane ? pane.replace("</div>", ` <p>posting ${jk}</p></div>`) : "";
    });
  }
  const box = {
    document: doc,
    window: { location: { get href() { return url; }, set href(v) { rec.navTo = v; } } },
    URL, URLSearchParams, Promise, Set, Map, Date, String,
    fetch: async (u) => { rec.fetched.push(u); throw new Error("no network in tests"); },
    MAX_APPLICATIONS_PER_PLATFORM: 15,
    isCampaignRunning: async () => true,
    getPlatformCount: async () => 0,
    detectPlatform: () => "indeed",
    isEasilyApplyCard: () => true,
    findJobCards: () => Array.from(doc.querySelectorAll(".job_seen_beacon")),
    getAppliedUrls: async () => new Set(),
    log: () => {},
    logBackend: (t) => rec.backend.push(t),
    sleep: () => new Promise((r) => setImmediate(r)),
    humanDelay: () => 0,
    sendMsg: async (m) => {
      rec.sent.push(m.type);
      if (m && m.type === "PREJUDGE_CARDS") {
        rec.judged.push(...m.data.jobs);
        return typeof verdicts === "function" ? verdicts(m.data.jobs) : verdicts;
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
    "this.phase1_indeed = phase1_indeed; this.paneText = paneText;", box);
  return { box, rec, store, doc };
}

// The judge answers per card from a {jk: verdict} table.
const answer = (table, extra = {}) => (jobs) => ({
  results: jobs.map((j) => ({ link: j.link, ...(table[j.link.slice(-16)] || { decision: "unjudged" }) })),
  ...extra,
});
const KW = { campaignFilters: { keywords: ["marketing"] } };
const pending = (store) => (store.pendingJobs || []).map((j) => j.jk);

(async () => {
  // --- 7. the pane's text ------------------------------------------------------------------
  {
    const { box, doc } = world({ store: {}, verdicts: null });
    doc.getElementById("vjs-pane").innerHTML = PANE;
    const t = box.paneText();
    check("pane: the captured posting reads as long text (>= 300)", t.length >= 300, `${t.length}`);
    check("pane: no CSS from the pane's <style> in the text", !/[{}]|css-[a-z0-9]+/.test(t), t.slice(0, 120));
  }

  // --- 1 + 2. one skipped, one fits ---------------------------------------------------------
  {
    const { rec, store, box } = world({
      store: { ...KW },
      verdicts: answer(
        {
          [JK_FIT]: { job_id: "row-fit", decision: "apply", fit_score: 61, source: "judged" },
          [JK_OFF]: { job_id: "row-off", decision: "skip", fit_score: 12, source: "stored",
            reason: "Event logistics role, resume is brand strategy." },
        },
        { reused: 1 }
      ),
    });
    await box.phase1_indeed();
    check("each real card clicked once, decoy never", rec.clicks.length === 2 && !rec.clicks.includes("a1b2c3d4e5f67890"),
      JSON.stringify(rec.clicks));
    check("/rpc/jobdescs (a request Indeed's page never makes) is not called", rec.fetched.length === 0, JSON.stringify(rec.fetched));
    check("both cards judged with their own pane text",
      rec.judged.length === 2 && rec.judged.every((j) => j.description.length >= 300 && j.description.includes(`posting ${j.link.slice(-16)}`)),
      JSON.stringify(rec.judged.map((j) => j.description.length)));
    check("judge gets the canonical /viewjob?jk= link the pool keys on",
      rec.judged.every((j) => /^https:\/\/www\.indeed\.com\/viewjob\?jk=[0-9a-f]{16}$/.test(j.link) && j.platform === "indeed"));
    check("only the fitting card is pending", JSON.stringify(pending(store)) === JSON.stringify([JK_FIT]),
      JSON.stringify(pending(store)));
    check("the fitting card carries its job_id", store.pendingJobs[0].job_id === "row-fit");
    check("the skipped card is never opened", rec.navTo === LINK(JK_FIT), rec.navTo);
    check("skip logged in the job page's own wording (run_report: fit gate)",
      rec.backend.some((t) => t.startsWith("⏭️ Skipped (fit 12): Events Marketing Specialist")), JSON.stringify(rec.backend));
    check("skipped jk remembered locally", (store.processedJobKeys || []).includes(JK_OFF));
    check("one summary line with the counts",
      rec.backend.some((t) => /^⚡ Judged this page ahead in [\d.]+ s: 1 fit you, 1 don't \(1 remembered/.test(t)),
      JSON.stringify(rec.backend));
  }

  // --- chunks: judged while the rest is read -------------------------------------------------
  {
    const chunkSrc = slice("  async function prejudgeIndeedCards(cards) {", "    const answers = await Promise.all(inFlight);");
    check("a chunk is sent without waiting for its answer (judging overlaps reading)",
      /inFlight\.push\(sendCardsToJudge\(chunk, texts\)\)/.test(chunkSrc) && !/await sendCardsToJudge/.test(chunkSrc));
  }

  // --- 2. unjudged keeps its job_id; company cap wording ------------------------------------
  {
    const { rec, store, box } = world({
      store: { ...KW },
      verdicts: answer({
        [JK_FIT]: { job_id: "row-fit", decision: "unjudged", source: "judge_unavailable" },
        [JK_OFF]: { job_id: "row-off", decision: "skip", fit_score: null, source: "company_cap",
          reason: "Company cap — already tried Acme in the last 60 days, one application per company." },
      }),
    });
    await box.phase1_indeed();
    check("unjudged card pending, its job_id kept for a verdict that lands late",
      JSON.stringify(pending(store)) === JSON.stringify([JK_FIT]) && store.pendingJobs[0].job_id === "row-fit",
      JSON.stringify(store.pendingJobs));
    check("summary does not count the unjudged card as a fit",
      rec.backend.some((t) => /: 0 fit you, 1 don't, 1 checked on their page/.test(t)), JSON.stringify(rec.backend));
    check("company cap line keeps the '— Company cap —' marker run_report counts",
      rec.backend.some((t) => /^⏭️ Skipped \(fit \?\): .* — Company cap — /.test(t)), JSON.stringify(rec.backend));
  }

  // --- 3. nothing fits = next page; Broad cap = hand on --------------------------------------
  {
    const { rec, store, box } = world({
      store: { ...KW },
      verdicts: answer({
        [JK_FIT]: { job_id: "a", decision: "skip", fit_score: 20, reason: "no" },
        [JK_OFF]: { job_id: "b", decision: "skip", fit_score: 8, reason: "no" },
      }),
    });
    await box.phase1_indeed();
    check("all skipped: next page once", rec.nextPage === 1, `nextPage=${rec.nextPage}`);
    check("all skipped: nothing opened, pendingJobs untouched", rec.navTo === null && store.pendingJobs === undefined);
  }
  {
    const cap = { decision: "skip", source: "broad_cap", reason: "Broad mode daily limit reached (40 applications). Resumes tomorrow." };
    const { rec, store, box } = world({ store: { ...KW }, verdicts: answer({ [JK_FIT]: cap, [JK_OFF]: cap }) });
    await box.phase1_indeed();
    check("broad cap: platform handed on once", rec.sent.filter((t) => t === "PLATFORM_EXHAUSTED").length === 1, JSON.stringify(rec.sent));
    check("broad cap: no next page, nothing opened", rec.nextPage === 0 && rec.navTo === null && store.pendingJobs === undefined);
  }

  // --- passed-on / applied rows are not fit losses -------------------------------------------
  {
    const { rec, box } = world({
      store: { ...KW },
      verdicts: answer({
        [JK_FIT]: { job_id: "a", decision: "skip", source: "dismissed", reason: "Passed on earlier" },
        [JK_OFF]: { job_id: "b", decision: "skip", source: "applied", reason: "Already applied" },
      }),
    });
    await box.phase1_indeed();
    check("dismissed/applied: no '⏭️ Skipped (fit' line", !rec.backend.some((t) => /Skipped \(fit/.test(t)), JSON.stringify(rec.backend));
    check("dismissed/applied: says why", rec.backend.some((t) => /^Skipping \(passed on earlier\): /.test(t)) &&
      rec.backend.some((t) => /^Skipping duplicate: /.test(t)));
  }

  // --- 4. failures fall back to the old path -------------------------------------------------
  {
    const { rec, store, box } = world({ store: { ...KW }, verdicts: null });
    await box.phase1_indeed();
    check("judge unavailable: every card pending, opened and judged live", pending(store).length === 2, JSON.stringify(pending(store)));
    check("judge unavailable: says so in the log", rec.backend.some((t) => /Search-page judge unavailable/.test(t)), JSON.stringify(rec.backend));
  }
  {
    // A pane that never shows the posting (layout change): no judge call, old path. The
    // reader waits its 5 s per card here, so this case takes ~10 s.
    const { rec, store, box } = world({ store: { ...KW }, verdicts: answer({}), paneFor: () => "" });
    await box.phase1_indeed();
    check("pane unreadable: no judge call", !rec.sent.includes("PREJUDGE_CARDS"), JSON.stringify(rec.sent));
    check("pane unreadable: every card pending", pending(store).length === 2, JSON.stringify(pending(store)));
    check("pane unreadable: says so in the log", rec.backend.some((t) => /no posting text in the results pane/.test(t)));
  }

  // --- 5. tap mode and pool runs never pre-judge -------------------------------------------
  for (const extra of [{ reviewMode: true }, { atsPlatform: "pool" }]) {
    const { rec, store, box } = world({ store: { ...KW, ...extra }, verdicts: answer({}) });
    await box.phase1_indeed();
    check(`${JSON.stringify(extra)}: no card clicked, no judge call`, rec.clicks.length === 0 && !rec.sent.includes("PREJUDGE_CARDS"));
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
    check("background routes PREJUDGE_CARDS to /tools/assess-fit-batch under Chrome's 30 s fetch limit",
      /case "PREJUDGE_CARDS"[\s\S]{0,700}\/tools\/assess-fit-batch[\s\S]{0,400}28000/.test(bg));
    check("no /rpc/jobdescs call anywhere in content.js", !/fetch\([^)]*rpc\/jobdescs/.test(SRC));
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})();
