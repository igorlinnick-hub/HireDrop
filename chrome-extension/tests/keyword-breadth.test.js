// Fixture test for the KEYWORD WALK in ../content.js — which phrase the run searches
// next, and which page of it. No JS test runner in this repo (see consent-gate.test.js
// for the same reasoning); run it by hand:
//
//   node <repo>/jobflow/chrome-extension/tests/keyword-breadth.test.js
//
// Why it exists (live measure 2026-09-19, 84 applications across two users): the walk
// went DEPTH-first — pagesPerKeyword() pages of keyword #1 before keyword #2 — and the
// per-board cap (15/day) ran out inside that first phrase. Jedyn: 39 of 39 applications
// on "Welder", zero on "Fabrication" and "Fitter". What must hold now:
//   1. Every list nav rotates: one page per phrase, then the whole list again.
//   2. A lap = the page number, so lap 0 is page 1 of EVERY phrase (sort=date → page 1
//      is the freshest head; depth spent the cap on stale tails of one query).
//   3. The board cap is sliced per phrase, so phrase #1 cannot spend the day's budget.
//   4. Phrases that are retired (no results) or spent are skipped, not re-walked.
//   5. The walk terminates — when the page budget is gone, the caller gets "stop".
//   6. A single keyword still walks deep (24 pages), exactly as before.
//   7. A pool / ATS queue walk is never governed by the slice (it has no phrase).
//   8. The ledger dates ITSELF and is keyed by phrase (10-06): yesterday's counts under
//      today's todayDate (what background.js's day rollover leaves behind) spend nothing,
//      and a phrase keeps its count when the server rotates it to another index.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const START = SRC.indexOf("  // ── keyword rotation ──");
const END = SRC.indexOf("  async function goBackToJobList() {", START);
// Prefix: the signature also takes options ({ chargeKeyword }).
const REC_START = SRC.indexOf("  async function recordLocalApplication(platform");
const REC_END = SRC.indexOf("  // Council 2026-08-04 \"frequency ledger\"", REC_START);
if (START < 0 || END < 0 || REC_START < 0 || REC_END < 0) {
  console.error("Could not locate the keyword rotation block in content.js — markers moved.");
  process.exit(2);
}

// --- a chrome.storage.local good enough for the block under test ------------------------
function makeSandbox(store) {
  const box = {
    MAX_APPLICATIONS_PER_PLATFORM: 15,
    localDay: () => "2026-09-19",
    log: () => {},
    logBackend: (t) => (box.feed = box.feed || []).push(t),
    // The block under test reads storage through the orphan-guard gateway now; the
    // gateway itself lives outside the sliced region, so shim it straight onto the fake.
    storageGet: (keys) => box.chrome.storage.local.get(keys),
    storageSet: (patch) => box.chrome.storage.local.set(patch),
    chrome: {
      storage: {
        local: {
          async get(keys) {
            const list = typeof keys === "string" ? [keys] : keys;
            const out = {};
            for (const k of list) if (k in store) out[k] = store[k];
            return out;
          },
          async set(patch) { Object.assign(store, patch); },
        },
      },
    },
  };
  vm.createContext(box);
  vm.runInContext(SRC.slice(START, END) + SRC.slice(REC_START, REC_END), box);
  return box;
}

let failures = 0;
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures++;
  console.log(`${ok ? "  ok" : "FAIL"}  ${name}${ok ? "" : `  (got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)})`}`);
}

const THREE = ["Welder", "Fabrication", "Fitter"];

// --- 1+2: breadth, and the lap IS the page ---------------------------------------------
(async () => {
  const store = { campaignFilters: { keywords: THREE }, kwIndex: 0, kwLap: 0, todayDate: "2026-09-19" };
  const box = makeSandbox(store);
  const walk = [];
  for (let i = 0; i < 6; i++) {
    if (!(await box.advanceKeyword("indeed"))) break;
    walk.push(`${THREE[store.kwIndex]}#${store.kwLap + 1}`);
  }
  check("breadth: the whole list is searched before any phrase goes deeper",
    walk, ["Fabrication#1", "Fitter#1", "Welder#2", "Fabrication#2", "Fitter#2", "Welder#3"]);

  // --- 3: the cap is sliced ---------------------------------------------------------
  check("sub-cap: 15 applications over 3 phrases = 5 each", await box.keywordSubCap(), 5);
  store.kwIndex = 0;
  store.keywordCounts = { day: "2026-09-19", indeed: { welder: 5 } };
  check("sub-cap: phrase #1 with its 5 spent must yield the board",
    await box.keywordCapReached("indeed"), true);
  check("sub-cap: an untouched phrase still has its turn",
    (store.kwIndex = 1, await box.keywordCapReached("indeed")), false);

  // --- 4: spent and retired phrases are skipped -------------------------------------
  const skipStore = {
    campaignFilters: { keywords: THREE }, kwIndex: 0, kwLap: 0, todayDate: "2026-09-19",
    keywordCounts: { day: "2026-09-19", indeed: { fabrication: 5 } },   // "Fabrication" spent its slice
    kwDone: [2],                            // "Fitter" returned no results this run
  };
  const skipBox = makeSandbox(skipStore);
  await skipBox.advanceKeyword("indeed");
  check("skip: a spent phrase and a retired one are stepped over, not re-walked",
    [THREE[skipStore.kwIndex], skipStore.kwLap], ["Welder", 1]);

  // --- 5: the walk terminates --------------------------------------------------------
  const endStore = { campaignFilters: { keywords: THREE }, kwIndex: 2, kwLap: 7, todayDate: "2026-09-19" };
  const endBox = makeSandbox(endStore);
  check("stop: the page budget (8 laps for 3 phrases) ends the board",
    await endBox.advanceKeyword("indeed"), false);
  const spentStore = {
    campaignFilters: { keywords: THREE }, kwIndex: 0, kwLap: 0, todayDate: "2026-09-19",
    keywordCounts: { day: "2026-09-19", indeed: { welder: 5, fabrication: 5, fitter: 5 } },
  };
  check("stop: every slice spent ends the board too",
    await makeSandbox(spentStore).advanceKeyword("indeed"), false);

  // --- 6: one keyword still goes deep ------------------------------------------------
  const soloStore = { campaignFilters: { keywords: ["Welder"] }, kwIndex: 0, kwLap: 0, todayDate: "2026-09-19" };
  const soloBox = makeSandbox(soloStore);
  check("solo: a single phrase keeps its 24 pages", await soloBox.pagesPerKeyword(), 24);
  check("solo: no slicing when there is nothing to slice between",
    await soloBox.keywordSubCap(), 15);
  // The run's FIRST page is opened by background.js (lap 0), so the nav loop supplies
  // the remaining 23 of the 24-page budget and then stops.
  let laps = 0;
  while (await soloBox.advanceKeyword("indeed")) laps++;
  check("solo: it walks out the 24-page budget, then stops", laps + 1, 24);

  // --- 7: the pool walk is not governed by the slice ---------------------------------
  const poolStore = {
    campaignFilters: { keywords: THREE }, kwIndex: 0, kwLap: 0, todayDate: "2026-09-19",
    keywordCounts: { day: "2026-09-19", indeed: { welder: 5 } }, atsPlatform: "pool",
  };
  check("pool: a queue walk is never rotated by the keyword slice",
    await makeSandbox(poolStore).keywordCapReached("indeed"), false);

  // --- 8: the ledger dates itself and follows the phrase ----------------------------
  // background.js rolls the day by stamping todayDate = today and zeroing todayCount +
  // platformCounts — and nothing else. Live 10-06 (0 Indeed applications that day):
  // every phrase read "spent", the run stopped 2 minutes in.
  const staleStore = {
    campaignFilters: { keywords: THREE }, kwIndex: 0, kwLap: 0, todayDate: "2026-09-19",
    keywordCounts: { day: "2026-09-18", indeed: { welder: 5, fabrication: 5, fitter: 5 } },
  };
  const staleBox = makeSandbox(staleStore);
  check("day: yesterday's ledger under today's todayDate spends nothing",
    await staleBox.keywordCapReached("indeed"), false);
  check("day: ... and the board is not 'exhausted' on the first rotation",
    await staleBox.advanceKeyword("indeed"), true);
  const oldShape = {
    campaignFilters: { keywords: THREE }, kwIndex: 0, kwLap: 0, todayDate: "2026-09-19",
    keywordCounts: { indeed: { 0: 5, 1: 5, 2: 5 } },   // the pre-10-06 index-keyed, undated shape
  };
  check("day: the live 10-06 state — undated index ledger from yesterday, todayDate = today — spends nothing",
    await makeSandbox(oldShape).keywordCapReached("indeed"), false);

  const recStore = { campaignFilters: { keywords: THREE }, kwIndex: 1, kwLap: 0, todayDate: "2026-09-18",
    keywordCounts: { day: "2026-09-18", indeed: { fabrication: 4 } } };
  const recBox = makeSandbox(recStore);
  await recBox.recordLocalApplication("indeed");
  check("record: a new day starts the ledger fresh, stamped with the day, charged to the phrase",
    recStore.keywordCounts, { day: "2026-09-19", indeed: { fabrication: 1 } });
  // Next run the server leads with "Fitter": the list is rotated, indexes move.
  recStore.campaignFilters = { keywords: ["Fitter", "Welder", "Fabrication"] };
  recStore.kwIndex = 2;
  for (let n = 0; n < 4; n++) await recBox.recordLocalApplication("indeed");
  check("rotate: the phrase keeps its count when it moves to another index",
    await recBox.keywordCapReached("indeed"), true);
  recStore.kwIndex = 0;
  check("rotate: the phrase now at index 0 was not charged for it",
    await recBox.keywordCapReached("indeed"), false);
  recStore.kwIndex = 2;
  await recBox.subtractLocalApplication("indeed");
  check("subtract: a blocked submit gives the phrase its slot back",
    recStore.keywordCounts.indeed.fabrication, 4);

  // --- 9: a phrase the server calls dry skips its later laps this run --------------------
  const STARTED = "2026-10-09T23:17:08.630Z";
  const dryStore = { campaignFilters: { keywords: THREE }, kwIndex: 1, kwLap: 2, campaignStartedAt: STARTED };
  const dryBox = makeSandbox(dryStore);
  const tag = await dryBox.judgeKeywordTag();
  check("tag: the phrase searched and a page key unique to this run, lap and phrase",
    tag, { index: 1, keyword: "Fabrication", page: `${STARTED}:1:2` });
  check("tag: a pool / ATS queue walk has no phrase to credit",
    await makeSandbox({ ...dryStore, atsPlatform: "greenhouse" }).judgeKeywordTag(), null);
  const DRY = { keyword: "fabrication", pages: 3, judged: 28, fits: 0, dry: true };
  check("dry: not dry → nothing retired",
    [await dryBox.retireDryKeyword(tag, { ...DRY, dry: false }, "indeed"), dryStore.kwDone], [false, undefined]);
  check("dry: retired for the run, with one feed line in the agreed wording",
    [await dryBox.retireDryKeyword(tag, DRY, "indeed"), dryStore.kwDone, dryBox.feed],
    [true, [1], ['⏭️ "Fabrication": 0 of 28 fit in 3 pages — skipping it this run']]);
  check("dry: a second dry answer for the same page retires nothing twice",
    [await dryBox.retireDryKeyword(tag, DRY, "indeed"), dryStore.kwDone, dryBox.feed.length], [false, [1], 1]);
  dryStore.kwIndex = 0;
  await dryBox.advanceKeyword("indeed");
  check("dry: the walk steps over the retired phrase", THREE[dryStore.kwIndex], "Fitter");

  const longStore = { campaignFilters: { keywords: ["x".repeat(250), "Welder"] }, kwIndex: 0 };
  check("tag: a phrase longer than the server takes is cut to 200, not sent whole",
    (await makeSandbox(longStore).judgeKeywordTag()).keyword.length, 200);

  const lastStore = { campaignFilters: { keywords: THREE }, kwIndex: 0, kwDone: [2],
    keywordCounts: { day: "2026-09-19", indeed: { fabrication: 5 } } };
  const lastBox = makeSandbox(lastStore);
  check("dry: the last live phrase keeps going (others retired or spent)",
    [await lastBox.retireDryKeyword(await lastBox.judgeKeywordTag(), DRY, "indeed"), lastStore.kwDone], [false, [2]]);
  const oneStore = { campaignFilters: { keywords: ["Welder"] }, kwIndex: 0 };
  const oneBox = makeSandbox(oneStore);
  check("dry: a single phrase is never retired",
    [await oneBox.retireDryKeyword(await oneBox.judgeKeywordTag(), DRY, "indeed"), oneStore.kwDone], [false, undefined]);

  console.log(failures ? `\n${failures} failure(s)` : "\nall good");
  process.exit(failures ? 1 : 0);
})();
