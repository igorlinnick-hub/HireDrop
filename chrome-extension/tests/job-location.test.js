// An Indeed posting's place must reach the pool row — from the search card, or from the
// open job's own page — and never from a neighbour. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/job-location.test.js
//
// Prod 09-22 → 10-06: 0 of 344 Indeed rows in `jobs` had a location (Greenhouse/Lever/Ashby:
// ~all). The card selectors never read the place line, and the job page was never asked.
// So the deck's city filter passed every Indeed job as "unknown", and an applications row
// like "Events Associate @ (blank)" could not be told apart from any other city's job when
// the employer texted back (Adventure Loom Htx, Houston — 10-06).
//
// Fixtures are live captures (header comment in each file). The jk fallback is tested on an
// empty document: it makes no claim about anyone's DOM.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM, VirtualConsole } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const fixture = (f) => fs.readFileSync(path.join(__dirname, "fixtures", f), "utf8");

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

const CARD = slice("  function extractCardInfo(card) {", "  async function phase1_jobList() {");
const PAGE = slice("  // ── company on the job page", "  async function phase2_jobDetail() {");

function load(html, url, code, exports) {
  // Silent console: jsdom can't parse the captured pages' modern CSS.
  const { window } = new JSDOM(html, { url, virtualConsole: new VirtualConsole() });
  const ctx = vm.createContext({ document: window.document, window, URL });
  vm.runInContext(code + "\n" + exports.map((f) => `this.${f} = ${f};`).join(" "), ctx);
  return { ctx, doc: window.document };
}

const VIEWJOB = "https://www.indeed.com/viewjob?jk=d8b40b219fcb72ca";
const SERP = "https://www.indeed.com/jobs?q=marketing+coordinator&l=remote";

// ── the search card ────────────────────────────────────────────────────────
{
  const { ctx, doc } = load(fixture("indeed-serp-decoy.html"), SERP, CARD, ["extractCardInfo", "isDecoyCard"]);
  const cards = [...doc.querySelectorAll(".job_seen_beacon")].map((c) => ctx.extractCardInfo(c));
  const real = cards.filter((c) => !ctx.isDecoyCard(c));
  check("fixture: three cards, two real", cards.length === 3 && real.length === 2, `${cards.length}/${real.length}`);
  check("Doximity card → its place line", real[0].company === "Doximity" && real[0].location === "Remote in San Francisco, CA",
    JSON.stringify(real[0].location));
  check("next real card → its own place, not the one above", real[1].location === "Remote", JSON.stringify(real[1].location));
  check("a card without a place line gives \"\", not undefined",
    ctx.extractCardInfo(new JSDOM("<div class='job_seen_beacon'><h2 class='jobTitle'><a href='/viewjob?jk=abc'>X</a></h2></div>")
      .window.document.querySelector("div")).location === "");
}

// ── the open job's page ────────────────────────────────────────────────────
const PAGE_EXPORTS = ["readIndeedJobLocation", "readIndeedJobCompany", "cardLocationFor", "jobIdFromUrl"];
{
  const { ctx, doc } = load(fixture("indeed-viewjob-no-cmp.html"), VIEWJOB, PAGE, PAGE_EXPORTS);
  check("viewjob: the header's place line", ctx.readIndeedJobLocation(doc) === "Houston, TX 77074",
    JSON.stringify(ctx.readIndeedJobLocation(doc)));
  check("…and the employer is not mistaken for it", ctx.readIndeedJobCompany(doc) === "Adventure Loom Htx");
}
{
  // The header line alone carries it: a <title> with no place must not matter.
  const { ctx, doc } = load(fixture("indeed-viewjob-no-cmp.html"), VIEWJOB, PAGE, PAGE_EXPORTS);
  doc.title = "Events Associate - Indeed.com";
  check("header read does not lean on <title>", ctx.readIndeedJobLocation(doc) === "Houston, TX 77074");
}
{
  // Next redesign drops the metadata block: <title> still names the open job's place.
  const { ctx, doc } = load(fixture("indeed-viewjob-no-cmp.html"), VIEWJOB, PAGE, PAGE_EXPORTS);
  doc.querySelector('[data-testid="company-info-metadata"]').remove();
  check("no metadata block → the place from <title>", ctx.readIndeedJobLocation(doc) === "Houston, TX 77074",
    JSON.stringify(ctx.readIndeedJobLocation(doc)));
  doc.title = "Events Associate - Indeed.com";
  check("…and a <title> with no place segment gives \"\"", ctx.readIndeedJobLocation(doc) === "");
  doc.title = "Events Associate - Full-time - Indeed.com";
  check("…and a segment that isn't shaped like a place gives \"\"", ctx.readIndeedJobLocation(doc) === "");
}
{
  // A results page has no job root and its <title> names the search, not a job: a place
  // read there would be a neighbour card's.
  const { ctx, doc } = load(fixture("indeed-serp-decoy.html"), SERP, PAGE, PAGE_EXPORTS);
  check("fixture: SERP carries a place line a document-wide read would hit",
    !!doc.querySelector('[data-testid="text-location"]'));
  doc.title = "Marketing Coordinator - Remote in San Francisco, CA - Indeed.com";
  check("SERP → \"\", never a neighbour card's place", ctx.readIndeedJobLocation(doc) === "",
    JSON.stringify(ctx.readIndeedJobLocation(doc)));
}

// ── card fallback by jk (empty document — no DOM claim) ────────────────────
{
  const { ctx } = load("<body></body>", VIEWJOB, PAGE, PAGE_EXPORTS);
  const pending = [
    { jk: "1111111111111111", location: "Austin, TX" },
    { jk: "d8b40b219fcb72ca", location: "Houston, TX 77074" },
    { jk: "2222222222222222", location: "Remote" },
  ];
  const jk = ctx.jobIdFromUrl(VIEWJOB);
  check("matching jk → that card's place (not index 0)", ctx.cardLocationFor(jk, pending) === "Houston, TX 77074");
  check("jk not in pendingJobs → \"\"", ctx.cardLocationFor("3333333333333333", pending) === "");
  check("no jk → \"\"", ctx.cardLocationFor("", pending) === "");
  check("blank card place → \"\", not a neighbour's",
    ctx.cardLocationFor("7777777777777777", [{ jk: "7777777777777777", location: " " }, { jk: "x", location: "Dallas, TX" }]) === "");
}

// ── the place reaches the server ───────────────────────────────────────────
{
  // Read where it is sent, so a rename of either side can't silently drop it.
  check("harvest sends the card's place", /location:\s*j\.location \|\| ""/.test(slice("HARVEST-TO-POOL (Igor", "// Save pending jobs")));
  check("pendingJobs keep the card's place for the page fallback",
    /pendingJobs: easyApplyCards\.map\(\(j\) => \(\{[^}]*location: j\.location/.test(SRC));
  check("Indeed detail phase sends the place with the posting text",
    SRC.includes("recordJobDescription(jobTitle, jobCompany, jobDesc, jobUrl, jobLocation)"));
  check("describe message carries it",
    /type: "SAVE_JOB_DESCRIPTION",\s*data: \{ title, company, location,/.test(SRC));
  const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
  const bgDescribe = BG.slice(BG.indexOf('case "SAVE_JOB_DESCRIPTION"'), BG.indexOf('case "GET_RESUME_URL"'));
  check("background forwards it to /jobs/describe", bgDescribe.includes('location: j.location || ""'));
}

console.log(failures ? `\n${failures} FAILED` : "\nall ok");
process.exit(failures ? 1 : 0);
