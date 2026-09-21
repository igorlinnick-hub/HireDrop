// Fixture test for the title-relevance gate in ../content.js. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/title-match.test.js
//
// This gate runs BEFORE the fit judge and the cover letter, so everything it drops is
// dropped unread — and unlike a fit-skip, nothing in the log says a good job was passed
// over. That asymmetry is why it gets its own fixtures.
//
// It compared EXACT words, so "event" never matched "Events". Measured on Igor's live
// 09-20 run: "Director of Special Events" and "Special Events Assistant" were both
// thrown away under the keyword "event manager" — the two most relevant titles the run
// saw that day.
//
// What must hold:
//   1. Singular/plural is one word (the live failure).
//   2. The gate still blocks genuinely unrelated work — the ATS pool is shared across
//      users, so a marketing search walks past welders, and judging those costs money.
//   3. No keywords = no filter, exactly as at harvest.
//   4. The stemmer doesn't invent matches ("marketing" is not "market").

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const START = SRC.indexOf("  // ── Title relevance");
const END = SRC.indexOf("  function collectUnfilledRequired() {", START);
if (START < 0 || END < 0) {
  console.error("Could not locate the title-relevance block in content.js — markers moved.");
  process.exit(2);
}
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(SRC.slice(START, END), sandbox);
const { titleMatchesKeywords } = sandbox;

let failures = 0;
function check(name, actual, expected) {
  const ok = actual === expected;
  if (!ok) failures++;
  console.log(`${ok ? "  ok" : "FAIL"}  ${name}${ok ? "" : `  (got ${actual}, want ${expected})`}`);
}

const EVENT = ["event manager", "sales & marketing"];

// --- 1: the live failure --------------------------------------------------------------
check("Director of Special Events passes (09-20 false skip)",
  titleMatchesKeywords("Director of Special Events", EVENT), true);
check("Special Events Assistant passes (09-20 false skip)",
  titleMatchesKeywords("Special Events Assistant", EVENT), true);
check("Director Marketing Events & Sponsorships passes",
  titleMatchesKeywords("Director, Marketing Events & Sponsorships", EVENT), true);
check("plural keyword vs singular title also matches",
  titleMatchesKeywords("Event Coordinator", ["events manager"]), true);
check("the exact title still passes",
  titleMatchesKeywords("Event Manager", EVENT), true);

// --- 2: the gate still earns its keep ---------------------------------------------------
check("welder is still skipped", titleMatchesKeywords("Welder / Fabricator", EVENT), false);
check("jeweller is still skipped", titleMatchesKeywords("Bench Jeweler", EVENT), false);
check("nurse is still skipped", titleMatchesKeywords("Registered Nurse", EVENT), false);

// --- 3: no keywords = no filter ---------------------------------------------------------
check("no keywords lets everything through", titleMatchesKeywords("Anything At All", []), true);
check("blank keywords behave the same", titleMatchesKeywords("Anything At All", undefined), true);

// --- 4: the stemmer must not invent matches ---------------------------------------------
check("'marketing' is not stemmed to 'market'",
  titleMatchesKeywords("Market Research Analyst", ["marketing manager"]), false);
check("short words are left alone (ops is not a plural)",
  titleMatchesKeywords("Warehouse Ops Lead", ["event manager"]), false);
check("double-s words are not stripped",
  titleMatchesKeywords("Business Analyst", ["event manager"]), false);
check("-ies becomes -y, not a stray stem",
  titleMatchesKeywords("Strategy Lead", ["strategies manager"]), true);

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
