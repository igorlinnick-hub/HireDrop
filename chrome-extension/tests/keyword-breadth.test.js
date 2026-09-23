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

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const START = SRC.indexOf("  // ── keyword rotation ──");
const END = SRC.indexOf("  async function goBackToJobList() {", START);
if (START < 0 || END < 0) {
  console.error("Could not locate the keyword rotation block in content.js — markers moved.");
  process.exit(2);
}

// --- a chrome.storage.local good enough for the block under test ------------------------
function makeSandbox(store) {
  const box = {
    MAX_APPLICATIONS_PER_PLATFORM: 15,
    localDay: () => "2026-09-19",
    log: () => {},
    logBackend: () => {},
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
  vm.runInContext(SRC.slice(START, END), box);
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
  store.keywordCounts = { indeed: { 0: 5 } };
  check("sub-cap: phrase #1 with its 5 spent must yield the board",
    await box.keywordCapReached("indeed"), true);
  check("sub-cap: an untouched phrase still has its turn",
    (store.kwIndex = 1, await box.keywordCapReached("indeed")), false);

  // --- 4: spent and retired phrases are skipped -------------------------------------
  const skipStore = {
    campaignFilters: { keywords: THREE }, kwIndex: 0, kwLap: 0, todayDate: "2026-09-19",
    keywordCounts: { indeed: { 1: 5 } },   // "Fabrication" spent its slice
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
    keywordCounts: { indeed: { 0: 5, 1: 5, 2: 5 } },
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
    keywordCounts: { indeed: { 0: 5 } }, atsPlatform: "pool",
  };
  check("pool: a queue walk is never rotated by the keyword slice",
    await makeSandbox(poolStore).keywordCapReached("indeed"), false);

  console.log(failures ? `\n${failures} failure(s)` : "\nall good");
  process.exit(failures ? 1 : 0);
})();
