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
//   8. The card Indeed put in the pane itself on load is read, not lost (skeptic 3, 10-07).
//   9. A posting is credited to a card only when the pane's title names it: a late pane
//      (posting A landing while card B is read) never becomes B's verdict.
//  10. No drawn pane (narrow window: a click navigates) or a hidden window = no clicks;
//      three unreadable cards in a row = give up; Stop mid-read stops clicking.
//  11. humanClick and ~4 s between cards, not .click() every 1.4 s.
// Pane timing (live 10-07): vjk follows a click in 5 ms, the old posting stays ~60 ms, the
// new title + text land together at ~1.4 s. The fake clock below replays that.

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

// The pane's title row as captured live (tests/fixtures/indeed-serp-pane-title.html), retitled.
const TITLE_ROW = FIX("indeed-serp-pane-title.html").replace(/^<!--[\s\S]*?-->\s*/, "");
const titleRow = (t) => TITLE_ROW.replace(/(data-testid="vj-job-title"[^>]*>)[^<]*/, (_, open) => open + (t || ""));
// The decoy fixture's three cards again under new jks: a page with four real cards.
const MORE_CARDS = FIX("indeed-serp-decoy.html")
  .replaceAll(JK_FIT, "1111aaaa2222bbbb").replaceAll(JK_OFF, "3333cccc4444dddd").replaceAll("a1b2c3d4e5f67890", "5555eeee6666ffff");
const EVEN_MORE = FIX("indeed-serp-decoy.html")
  .replaceAll(JK_FIT, "7777aaaa8888bbbb").replaceAll(JK_OFF, "9999cccc0000dddd").replaceAll("a1b2c3d4e5f67890", "1212eeee3434ffff")
  .replaceAll("Marketing Coordinator", "Marketing Analyst").replaceAll("Events Marketing Specialist", "Growth Marketing Specialist");

// The results page as a card click meets it, timed the way it behaved live 10-07: vjk
// follows the card at once, the pane empties, and the posting (title + text) lands
// `delayFor(jk)` ms later — or never, when `paneFor(jk)` gives "". Time is a fake clock
// that `sleep` advances, so a 5 s timeout costs nothing here.
function world({ store, verdicts, paneFor, delayFor, titleFor, preselect, more, narrow, hidden, running, sameText, dupe }) {
  const pageHtml = FIX("indeed-serp-decoy.html") + (more === true || more === 2 ? MORE_CARDS : "") +
    (more === 2 || more === "distinct" ? EVEN_MORE : "") +
    (dupe ? FIX("indeed-serp-decoy.html") : "");
  const html = `<body>${pageHtml}<div class="jobsearch-RightPane"><div id="jobsearch-ViewjobPaneWrapper"></div></div></body>`;
  let url = "https://www.indeed.com/jobs?q=marketing&l=remote" + (preselect ? `&vjk=${preselect}` : "");
  const { window } = new JSDOM(html, { url, pretendToBeVisual: true, virtualConsole: new VirtualConsole() });
  const doc = window.document;
  const pane = doc.getElementById("jobsearch-ViewjobPaneWrapper");
  const rec = { backend: [], judged: [], clicks: [], fetched: [], nextPage: 0, navTo: null, sent: [], msgs: [], dry: [] };
  const view = { narrow: !!narrow };
  // jsdom lays nothing out; the pane is drawn unless the window is "narrow" (display:none, 0 px).
  window.HTMLElement.prototype.getBoundingClientRect = function () {
    const w = view.narrow ? 0 : 578;
    return { width: w, height: w ? 743 : 0, left: 0, top: 0, right: w, bottom: w ? 743 : 0 };
  };
  if (hidden) Object.defineProperty(doc, "visibilityState", { get: () => "hidden" });
  let clock = 1e12;
  const due = [];
  const deliver = () => {
    due.sort((a, b) => a.at - b.at);
    while (due.length && due[0].at <= clock) due.shift().draw();
  };
  const titles = {};
  const posting = (jk) => {
    const body = (paneFor || (() => PANE))(jk);
    if (!body) return "";
    const t = titleFor ? titleFor(jk, titles[jk]) : titles[jk];
    return titleRow(t) + (sameText ? body : body.replace("</div>", ` <p>posting ${jk}</p></div>`));
  };
  for (const a of doc.querySelectorAll("a")) {
    const jk = a.getAttribute("data-jk") || (a.getAttribute("href") || "").match(/jk=([0-9a-z]+)/)?.[1];
    if (a.hasAttribute("data-jk")) titles[jk] = a.textContent.trim(); // the card's title link
    a.addEventListener("click", (e) => {
      e.preventDefault();
      rec.clicks.push(jk);
      url = `https://www.indeed.com/jobs?q=marketing&l=remote&vjk=${jk}`;
      pane.innerHTML = "";
      const html = posting(jk);
      if (html) due.push({ at: clock + (delayFor ? delayFor(jk) : 0), draw: () => { pane.innerHTML = html; } });
    });
  }
  if (preselect) pane.innerHTML = posting(preselect);
  let checks = 0;
  const box = {
    document: doc,
    window: { location: { get href() { return url; }, set href(v) { rec.navTo = v; } } },
    URL, URLSearchParams, Promise, Set, Map, String,
    Date: { now: () => clock },
    fetch: async (u) => { rec.fetched.push(u); throw new Error("no network in tests"); },
    MAX_APPLICATIONS_PER_PLATFORM: 15,
    isCampaignRunning: async () => (running ? running(++checks, rec) : true),
    getPlatformCount: async () => 0,
    // First-SERP city check (tests/indeed-first-search-city.test.js): nothing armed here.
    correctFirstIndeedSearch: async () => false,
    detectPlatform: () => "indeed",
    isEasilyApplyCard: () => true,
    findJobCards: () => Array.from(doc.querySelectorAll(".job_seen_beacon")),
    getAppliedUrls: async () => new Set(),
    log: () => {},
    logBackend: (t) => rec.backend.push(t),
    sleep: (ms) => { clock += ms || 0; deliver(); return new Promise((r) => setImmediate(r)); },
    humanDelay: () => 0,
    humanClick: async (el) => el.click(),
    sendMsg: async (m) => {
      rec.sent.push(m.type);
      if (m && m.type === "PREJUDGE_CARDS") {
        rec.msgs.push(m.data);
        rec.judged.push(...m.data.jobs);
        return typeof verdicts === "function" ? verdicts(m.data.jobs) : verdicts;
      }
      return {};
    },
    goToNextPage: async () => { rec.nextPage++; },
    goBackToJobList: async () => { rec.nextPage++; },
    skipToNextJob: async () => {},
    retireKeyword: async () => {},
    judgeKeywordTag: async () => (store.campaignFilters ? { index: 0, keyword: "marketing", page: "run:0:0" } : null),
    retireDryKeyword: async (tag, y) => { rec.dry.push({ tag, y }); return true; },
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
  return { box, rec, store, doc, pane, view };
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
    const { box, pane } = world({ store: {}, verdicts: null });
    pane.innerHTML = PANE;
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
    const chunkSrc = slice("  async function prejudgeIndeedCards(pageCards) {", "    const answers = await Promise.all(inFlight);");
    check("a chunk is sent without waiting for its answer (judging overlaps reading)",
      /inFlight\.push\(sendCardsToJudge\(chunk, texts, tag\)\)/.test(chunkSrc) && !/await sendCardsToJudge/.test(chunkSrc));
  }

  // --- keyword yield: every chunk names the phrase and the page; the skip is decided once ---
  {
    const y = (judged) => ({ keyword: "marketing", pages: 2, judged, fits: 0, dry: true });
    let call = 0;
    const { rec, box } = world({
      store: { ...KW },
      more: 2,
      verdicts: (jobs) => ({
        results: jobs.map((j) => ({ link: j.link, decision: "skip", fit_score: 5, source: "judged", reason: "no" })),
        keyword_yield: y(++call === 1 ? 25 : 22),
      }),
    });
    await box.phase1_indeed();
    check("each chunk of the page carries the phrase and the same page key",
      rec.msgs.length >= 2 && rec.msgs.every((d) => d.keyword === "marketing" && d.page === "run:0:0"),
      JSON.stringify(rec.msgs.map((d) => [d.keyword, d.page, d.jobs.length])));
    check("the dry decision is made once per page, on the fullest tally",
      rec.dry.length === 1 && rec.dry[0].y.judged === 25, JSON.stringify(rec.dry));
  }
  {
    const { rec, box } = world({ store: {}, verdicts: answer({ [JK_FIT]: { job_id: "a", decision: "skip", reason: "no" } }) });
    await box.phase1_indeed();
    check("no keyword list: chunks go without keyword/page and nothing is retired",
      rec.msgs.every((d) => !("keyword" in d) && !("page" in d)) && rec.dry.length === 0, JSON.stringify(rec.msgs));
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
    // A pane that never shows the posting (layout change): no judge call, old path.
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

  // --- 8. the card Indeed put in the pane itself is read, not lost --------------------------
  // Live 10-07: the page opens with its first card already in the pane (vjk = that card).
  // Clicking it redraws nothing, so waiting for a change dropped it on every page.
  for (const [label, withVjk] of [["vjk set", true], ["no vjk yet", false]]) {
    const w = world({ store: { ...KW }, verdicts: answer({}), preselect: JK_FIT });
    // Without vjk: the same pane, before the page has written vjk into the URL.
    if (!withVjk) w.box.window = { location: { href: "https://www.indeed.com/jobs?q=marketing&l=remote" } };
    await w.box.phase1_indeed();
    check(`preselected (${label}): the card in the pane is not clicked again`, !w.rec.clicks.includes(JK_FIT), JSON.stringify(w.rec.clicks));
    const j = w.rec.judged.find((x) => x.link === LINK(JK_FIT));
    check(`preselected (${label}): it is judged with its own posting`, j && j.description.includes(`posting ${JK_FIT}`),
      JSON.stringify(w.rec.judged.map((x) => x.link.slice(-16))));
  }

  // --- identical descriptions (one employer, two postings) are both read ---------------------
  {
    const same = PANE.replace("</div>", " <p>same text for both</p></div>");
    const o = world({ store: { ...KW }, verdicts: answer({}), paneFor: () => same, sameText: true });
    await o.box.phase1_indeed();
    check("identical descriptions: both cards judged", o.rec.judged.length === 2 &&
      o.rec.judged[0].description === o.rec.judged[1].description, JSON.stringify(o.rec.judged.map((x) => x.link.slice(-16))));
  }

  // --- 2. a late pane is never credited to the next card -----------------------------------
  {
    // Card 1's posting is slow (lands 8 s after its click, past the 5 s wait and the settle);
    // card 2's never comes. Card 1's text then lands while card 2 is being read — vjk says
    // card 2, the pane says card 1. Crediting it would store card 1's skip under card 2.
    const o = world({
      store: { ...KW },
      verdicts: answer({}),
      paneFor: (jk) => (jk === JK_OFF ? "" : PANE),
      delayFor: (jk) => (jk === JK_FIT ? 8000 : 0),
    });
    await o.box.phase1_indeed();
    check("late pane: both cards were clicked", o.rec.clicks.length === 2, JSON.stringify(o.rec.clicks));
    check("late pane: card 1's posting is not judged as card 2", !o.rec.judged.some((j) => j.link === LINK(JK_OFF)),
      JSON.stringify(o.rec.judged.map((x) => [x.link.slice(-16), x.description.slice(-30)])));
    check("late pane: nothing read = old path, every card opened", pending(o.store).length === 2, JSON.stringify(pending(o.store)));
  }
  {
    // Slow but inside the wait (3 s): still read, and with its own text.
    const o = world({ store: { ...KW }, verdicts: answer({}), delayFor: () => 3000 });
    await o.box.phase1_indeed();
    check("slow pane (3 s): both cards judged with their own text",
      o.rec.judged.length === 2 && o.rec.judged.every((j) => j.description.includes(`posting ${j.link.slice(-16)}`)),
      JSON.stringify(o.rec.judged.map((x) => x.link.slice(-16))));
  }
  {
    // The pane shows another posting under vjk = this card (a title that names someone else).
    const o = world({ store: { ...KW }, verdicts: answer({}), titleFor: (jk, t) => (jk === JK_OFF ? "Some Other Job" : t) });
    await o.box.phase1_indeed();
    check("pane titled for another posting: that card is not judged",
      o.rec.judged.length === 1 && o.rec.judged[0].link === LINK(JK_FIT), JSON.stringify(o.rec.judged.map((x) => x.link.slice(-16))));
    check("pane titled for another posting: the card still goes to its page", pending(o.store).includes(JK_OFF));
  }

  {
    // Skeptic of the blast-radius pass: card 1's posting lands 9.5 s late, while a LATER card
    // with the same title ("Marketing Coordinator", jk 1111…) is being read. The title check
    // can't tell them apart, so that card must not be read in the pane at all.
    const o = world({ store: { ...KW }, verdicts: answer({}), more: true, delayFor: (jk) => (jk === JK_FIT ? 9500 : 0) });
    await o.box.phase1_indeed();
    check("same title as an unread card: not clicked, not judged",
      !o.rec.clicks.includes("1111aaaa2222bbbb") && !o.rec.judged.some((j) => j.link === LINK("1111aaaa2222bbbb")),
      JSON.stringify({ clicks: o.rec.clicks, judged: o.rec.judged.map((x) => x.link.slice(-16)) }));
    check("same title as an unread card: it still goes to its page", pending(o.store).includes("1111aaaa2222bbbb"));
    check("no card judged on another card's posting",
      o.rec.judged.every((j) => j.description.includes(`posting ${j.link.slice(-16)}`)),
      JSON.stringify(o.rec.judged.map((x) => [x.link.slice(-16), x.description.slice(-30)])));
  }

  // --- the same posting twice on a page is judged and logged once ---------------------------
  {
    const o = world({ store: { ...KW }, dupe: true, verdicts: answer({ [JK_OFF]: { job_id: "b", decision: "skip", fit_score: 12, reason: "no" } }) });
    await o.box.phase1_indeed();
    check("duplicate jk: one skip line", o.rec.backend.filter((t) => t.startsWith("⏭️ Skipped (fit 12)")).length === 1, JSON.stringify(o.rec.backend));
    check("duplicate jk: judged once", o.rec.judged.filter((j) => j.link === LINK(JK_OFF)).length === 1);
  }

  // --- a long read is not silence (drive.py calls 180 s without a line a stall) --------------
  {
    const o = world({ store: { ...KW }, verdicts: answer({}), more: 2 });
    await o.box.phase1_indeed();
    check("a progress line per chunk sent", o.rec.backend.some((t) => /^Search-page judge: read 5 of 6 postings on this page…$/.test(t)),
      JSON.stringify(o.rec.backend));
  }

  // --- 3. no pane = no clicks (a click there navigates) ---------------------------------------
  {
    const o = world({ store: { ...KW }, verdicts: answer({}), narrow: true });
    await o.box.phase1_indeed();
    check("narrow window: no card clicked", o.rec.clicks.length === 0, JSON.stringify(o.rec.clicks));
    check("narrow window: old path, says why", pending(o.store).length === 2 &&
      o.rec.backend.some((t) => /no results pane \(window too narrow\?\)/.test(t)), JSON.stringify(o.rec.backend));
  }
  {
    const o = world({ store: { ...KW }, verdicts: answer({}), hidden: true });
    await o.box.phase1_indeed();
    check("hidden window: no card clicked", o.rec.clicks.length === 0, JSON.stringify(o.rec.clicks));
    check("hidden window: old path, says why", pending(o.store).length === 2 &&
      o.rec.backend.some((t) => /automation window is hidden/.test(t)), JSON.stringify(o.rec.backend));
  }
  {
    // The window shrinks after the first card: stop clicking before the next one.
    const o = world({ store: { ...KW }, verdicts: answer({}) });
    const firstLink = o.box.document.querySelector(`a[data-jk="${JK_FIT}"]`);
    firstLink.addEventListener("click", () => { o.view.narrow = true; });
    await o.box.phase1_indeed();
    check("pane gone mid-walk: the next card is not clicked", JSON.stringify(o.rec.clicks) === JSON.stringify([JK_FIT]),
      JSON.stringify(o.rec.clicks));
    check("pane gone mid-walk: old path, says why", pending(o.store).length === 2 &&
      o.rec.backend.some((t) => /results pane went away/.test(t)), JSON.stringify(o.rec.backend));
  }
  {
    // Four real cards, four titles, a pane that never shows any: give up after three, not four.
    const o = world({ store: { ...KW }, verdicts: answer({}), paneFor: () => "", more: "distinct" });
    await o.box.phase1_indeed();
    check("3 unreadable in a row: stops clicking", o.rec.clicks.length === 3, JSON.stringify(o.rec.clicks));
    check("3 unreadable in a row: old path, says why", !o.rec.sent.includes("PREJUDGE_CARDS") &&
      o.rec.backend.some((t) => /showed none of 3 postings in a row/.test(t)), JSON.stringify(o.rec.backend));
  }

  // --- Stop in the middle of reading ---------------------------------------------------------
  {
    // Running for the walk's own first checks, then the person presses Stop after card 1.
    const o = world({ store: { ...KW }, verdicts: answer({}), running: (_n, rec) => rec.clicks.length < 1 });
    await o.box.phase1_indeed();
    check("Stop mid-read: no further card clicked", o.rec.clicks.length === 1, JSON.stringify(o.rec.clicks));
    check("Stop mid-read: nothing opened, nothing pending", o.rec.navTo === null && o.store.pendingJobs === undefined,
      `${o.rec.navTo} ${JSON.stringify(o.store.pendingJobs)}`);
  }
  {
    // Stop lands while the judge's answers are in flight (10-08: verdicts kept arriving and
    // being acted on after a Stop). Nothing about this page may be logged as judged.
    const o = world({
      store: { ...KW },
      verdicts: answer({
        [JK_FIT]: { job_id: "row-fit", decision: "apply", fit_score: 61 },
        [JK_OFF]: { job_id: "row-off", decision: "skip", fit_score: 12, reason: "no" },
      }),
      running: (_n, rec) => !rec.sent.includes("PREJUDGE_CARDS"),
    });
    await o.box.phase1_indeed();
    check("Stop while the judge answers: no verdict or summary line for the page",
      !o.rec.backend.some((t) => /^⏭️ Skipped|^⚡ Judged/.test(t)), JSON.stringify(o.rec.backend));
    check("…nothing opened, nothing pending", o.rec.navTo === null && o.store.pendingJobs === undefined,
      `${o.rec.navTo} ${JSON.stringify(o.store.pendingJobs)}`);
  }
  {
    // Stop lands in the 3-7 s pause between "Opening job" and the navigation.
    const o = world({
      store: { ...KW },
      verdicts: answer({ [JK_FIT]: { job_id: "row-fit", decision: "apply", fit_score: 61 } }),
      running: (_n, rec) => !rec.backend.some((t) => t.startsWith("Opening job:")),
    });
    await o.box.phase1_indeed();
    check("Stop in the pause before opening the first job: no navigation",
      o.rec.backend.some((t) => t.startsWith("Opening job:")) && o.rec.navTo === null, `${o.rec.navTo}`);
  }

  // --- 4. a person's pace and a person's click ------------------------------------------------
  {
    const reader = slice("  async function readCardInPane(card, cards, timeoutMs = 5000) {", "  // After a card the pane never showed");
    const loop = slice("  async function prejudgeIndeedCards(pageCards) {", "    const answers = await Promise.all(inFlight);");
    check("cards are clicked with humanClick, not a bare .click()",
      /await humanClick\(card\.clickEl\)/.test(reader) && !/clickEl\.click\(\)/.test(reader));
    check("~4 s between cards (humanDelay(2500, 7000))", /await sleep\(humanDelay\(2500, 7000\)\)/.test(loop));
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
