// The employer on the job page must be the OPEN job's employer — or the card's, matched by
// id — and never a neighbour's. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/job-page-company.test.js
//
// Prod 09-07 → 09-28: Indeed walk postings judged with an EMPTY company rose 0% → 16%; ten
// applications rows have company "" (all /viewjob?jk=…), and activity shows
// "Opening job: X @ Your Houston ENT & Wellness" then "Good fit (78): X @ " — the card had the
// name, the page lost it. The capture shows why: an employer without an Indeed company page
// has no /cmp/ link in the rebuilt header, only [data-testid="vj-company-name"].
// ZipRecruiter's right-pane /co/ link read as "Learn more about XPOexternal" (link text plus
// the icon's <title>), which is what landed in the database.
//
// Fixtures are live captures (header comment in each file). The jk-matching fallback is
// tested on an empty document: it makes no claim about anyone's DOM.

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

const CODE = slice("  // ── company on the job page", "  async function phase2_jobDetail() {");

function load(html, url) {
  // Silent console: jsdom can't parse the captured pages' modern CSS and would print a
  // "Could not parse CSS stylesheet" line per <style>. Nothing here reads styles.
  const { window } = new JSDOM(html, { url, virtualConsole: new VirtualConsole() });
  const ctx = vm.createContext({ document: window.document, window, URL });
  vm.runInContext(
    CODE +
      "\nthis.readIndeedJobCompany = readIndeedJobCompany; this.cardCompanyFor = cardCompanyFor;" +
      " this.jobIdFromUrl = jobIdFromUrl; this.readZipRecruiterCompany = readZipRecruiterCompany;",
    ctx
  );
  return { ctx, doc: window.document };
}

// ── Indeed: the page itself ────────────────────────────────────────────────
{
  const { ctx, doc } = load(fixture("indeed-viewjob-no-cmp.html"), "https://www.indeed.com/viewjob?jk=d8b40b219fcb72ca");
  check("fixture: header has no /cmp/ link (the prod shape)",
    !!doc.querySelector('[data-testid="desktop-job-header"]') &&
      !doc.querySelector('[data-testid="viewjob-main-content"] a[href*="/cmp/"]'));
  const got = ctx.readIndeedJobCompany(doc);
  check("viewjob without /cmp/ link reads vj-company-name", got === "Adventure Loom Htx", JSON.stringify(got));
}

{
  // The SERP has no job root. The old code fell through to an UNSCOPED
  // [data-testid="company-name"] and read the first card's employer ("Doximity").
  const { ctx, doc } = load(fixture("indeed-serp-decoy.html"), "https://www.indeed.com/jobs?q=marketing+coordinator&l=remote");
  check("fixture: SERP card carries a company-name a document-wide read would hit",
    doc.querySelector('[data-testid="company-name"]')?.textContent.trim() === "Doximity");
  const got = ctx.readIndeedJobCompany(doc);
  check("no job root → empty, never a neighbour card's employer", got === "", JSON.stringify(got));
}

// ── Indeed: card fallback by jk (empty document — no DOM claim) ────────────
{
  const { ctx } = load("<body></body>", "https://www.indeed.com/viewjob?jk=d8b40b219fcb72ca");
  const pending = [
    { jk: "1111111111111111", company: "Neighbour Above Inc" },
    { jk: "d8b40b219fcb72ca", company: "Your Houston ENT & Wellness" },
    { jk: "2222222222222222", company: "Neighbour Below LLC" },
  ];
  const jk = ctx.jobIdFromUrl("https://www.indeed.com/viewjob?jk=d8b40b219fcb72ca");
  check("jk read from /viewjob URL", jk === "d8b40b219fcb72ca", jk);

  const hit = ctx.cardCompanyFor(jk, pending, null, null);
  check("matching jk → that card's company (not index 0)",
    hit.company === "Your Houston ENT & Wellness" && hit.source === "card", JSON.stringify(hit));

  const miss = ctx.cardCompanyFor("3333333333333333", pending, null, null);
  check("jk not in pendingJobs → stays empty (no wrong employer)", miss.company === "", JSON.stringify(miss));

  check("no jk → stays empty", ctx.cardCompanyFor("", pending, null, null).company === "");

  const queue = [
    { applyUrl: "https://www.indeed.com/viewjob?jk=4444444444444444", company: "Queue Neighbour" },
    { applyUrl: "https://www.indeed.com/viewjob?jk=5555555555555555", company: "Pool Employer Co" },
  ];
  const pool = ctx.cardCompanyFor("5555555555555555", [], "pool", queue);
  check("pool run: queue row with the same jk → its company",
    pool.company === "Pool Employer Co" && pool.source === "pool row", JSON.stringify(pool));
  check("pool run: jk in no queue row → stays empty",
    ctx.cardCompanyFor("6666666666666666", [], "pool", queue).company === "");
  check("queue is only consulted on a pool run",
    ctx.cardCompanyFor("5555555555555555", [], "greenhouse", queue).company === "");
  check("blank card company falls through to empty, not to a neighbour",
    ctx.cardCompanyFor("7777777777777777", [{ jk: "7777777777777777", company: "  " }, { jk: "x", company: "Other" }], null, null).company === "");
}

// ── ZipRecruiter right pane ────────────────────────────────────────────────
{
  const UUID = "SJQj3h4stZuPhfYM0UTJjA";
  const URL_ = `https://www.ziprecruiter.com/jobs-search?search=social+media+content+creator&location=Houston%2C+TX&lk=${UUID}`;
  const WANT = "SCENT Houston - Sinus Center & ENT Specialists of Houston";
  const html = fixture("ziprecruiter-serp-right-pane.html");

  {
    const { ctx, doc } = load(html, URL_);
    const a = doc.querySelector('[data-testid="right-pane"] a[href*="/co/"]');
    check("fixture: the pane's /co/ link textContent is the prod garbage",
      /^Learn more about .*external$/.test(a?.textContent || ""), a?.textContent);
    check("uuid read from lk=", ctx.jobIdFromUrl(URL_) === UUID);
    const got = ctx.readZipRecruiterCompany(doc, UUID, [], null, null);
    check("card in the DOM matched by uuid → clean name", got === WANT, JSON.stringify(got));
  }
  {
    // No card in the DOM for this uuid (pool by-link open, list re-rendered): the link's
    // own text nodes, prefix off, icon title ignored.
    const { ctx, doc } = load(html, URL_);
    doc.querySelectorAll('article[id^="job-card-"]').forEach((el) => el.remove());
    const got = ctx.readZipRecruiterCompany(doc, UUID, [], null, null);
    check("no card → link text without 'Learn more about' / 'external'", got === WANT, JSON.stringify(got));
  }
  {
    const { ctx, doc } = load(html, URL_);
    const got = ctx.readZipRecruiterCompany(doc, UUID, [{ jk: "otherUuid", company: "Wrong Co" }, { jk: UUID, company: "Stored Card Co" }], null, null);
    check("stored card matched by uuid wins", got === "Stored Card Co", JSON.stringify(got));
  }
  {
    const { ctx, doc } = load(html, URL_);
    doc.querySelectorAll('article[id^="job-card-"]').forEach((el) => el.remove());
    const got = ctx.readZipRecruiterCompany(doc, UUID, [], "pool", [{ applyUrl: URL_, company: "Pool Row Co" }]);
    check("pool row matched by lk beats link text", got === "Pool Row Co", JSON.stringify(got));
  }
}

if (failures) {
  console.log(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
