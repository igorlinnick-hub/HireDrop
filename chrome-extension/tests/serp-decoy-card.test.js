// Indeed's decoy SERP cards must never reach the walk or the pool. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/serp-decoy-card.test.js
//
// Live 10-05/06: the pool held Indeed rows on jk 0f1e2d3c4b5a6978, 123456789abcdef0,
// cdef0123456789ab, fedcba9876543210 — one per user per jk, under real titles and companies,
// none of them in our code. Each run opened them as "Dead link" (4 of 11 opens on 10-06).
// A logged-out SERP capture showed the source: Indeed renders a clone of a real card with a
// made-up jk (a1b2c3d4e5f67890 there), aria-hidden + tabindex -1, 0 px tall, /viewjob link.
// The fixture is that capture verbatim: real card, its decoy clone, next real card.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const HTML = fs.readFileSync(path.join(__dirname, "fixtures", "indeed-serp-decoy.html"), "utf8");

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

const CODE = slice("  function extractCardInfo(card) {", "  async function phase1_jobList() {");

function scan(html) {
  const { window } = new JSDOM(`<body>${html}</body>`, { url: "https://www.indeed.com/jobs?q=marketing+coordinator&l=remote" });
  const ctx = vm.createContext({ document: window.document, window });
  vm.runInContext(CODE + "\nthis.extractCardInfo = extractCardInfo; this.isDecoyCard = isDecoyCard;", ctx);
  return [...window.document.querySelectorAll(".job_seen_beacon")].map((card) => {
    const info = ctx.extractCardInfo(card);
    return { jk: info.jk, title: info.title, decoy: ctx.isDecoyCard(info) };
  });
}

const cards = scan(HTML);
check("fixture has 3 cards", cards.length === 3, JSON.stringify(cards));
const byJk = Object.fromEntries(cards.map((c) => [c.jk, c]));
check("decoy reads like a real job (why it slipped through)",
  byJk.a1b2c3d4e5f67890?.title === "Marketing Coordinator", JSON.stringify(byJk.a1b2c3d4e5f67890));
check("decoy is skipped", byJk.a1b2c3d4e5f67890?.decoy === true);
check("real card above it is kept", byJk["9a46b4b76cdc3d37"]?.decoy === false);
check("real card below it is kept", byJk["689d5e3af5fd2ec8"]?.decoy === false);

// Indeed drops aria-hidden: the clone still carries the original's title span id.
const noAria = scan(HTML.replace(/ aria-hidden="true" tabindex="-1"/, ""));
check("decoy without aria-hidden still skipped by the cloned title id",
  noAria.find((c) => c.jk === "a1b2c3d4e5f67890")?.decoy === true, JSON.stringify(noAria));

// A real card with no title span id at all must not be judged on its absence.
const noSpanIds = scan(HTML.replace(/ id="jobTitle-[0-9a-z]+"/g, ""));
check("real cards without title span ids are kept",
  noSpanIds.filter((c) => c.jk !== "a1b2c3d4e5f67890").every((c) => !c.decoy), JSON.stringify(noSpanIds));

if (failures) {
  console.log(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
