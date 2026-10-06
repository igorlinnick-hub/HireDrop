// A hand-back must name the JOB, not the form screen it stopped on. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/handback-posting-url.test.js
//
// What went wrong (prod, 09-28…10-05): the backend keeps one OPEN hand-back row per URL,
// and handBackJob sent window.location.href. Every Indeed application runs through the
// same smartapply step URLs (…/resume-module/structured-data-review,
// …/questions-module/questions/1 — captured from activity_log, no query at all), so each
// new Indeed hand-back overwrote the person's previous one: 15 jobs, 5 rows.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

// Fixed window, not brace-matching: the default `extra = {}` would end the match early.
const hbAt = SRC.indexOf("  async function handBackJob(reason, extra = {}) {");
check("handBackJob exists", hbAt >= 0);
if (failures) process.exit(1);
const hbSrc = SRC.slice(hbAt, SRC.indexOf("\n  }\n", hbAt) + 4);

const STEP = "https://smartapply.indeed.com/beta/indeedapply/form/resume-module/structured-data-review";

async function run(extra) {
  const sent = [];
  const ctx = {
    window: { location: { href: STEP } },
    addHandedBackKey: async () => {},
    sendMsg: async (m) => { sent.push(m); return {}; },
    collectUnfilledRequired: () => [],
    detectPlatform: () => "indeed",
  };
  vm.createContext(ctx);
  vm.runInContext(`${hbSrc}\nthis.handBackJob = handBackJob;`, ctx);
  await ctx.handBackJob("refused", extra);
  return sent[0] && sent[0].data;
}

(async () => {
  const withPosting = await run({
    title: "Event Coordinator", company: "Bowtech Archery",
    url: "https://www.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b",
  });
  check("a caller's posting URL is what the hand-back carries",
    withPosting && withPosting.url === "https://www.indeed.com/viewjob?jk=1a2b3c4d5e6f7a8b",
    JSON.stringify(withPosting));

  const atsPage = await run({ title: "PM", company: "Acme", platform: "greenhouse" });
  check("without one (an ATS form page IS its posting) the page URL is kept",
    atsPage && atsPage.url === STEP, JSON.stringify(atsPage));

  // Both hand-backs of the multi-step Indeed/ZipRecruiter walker pass the job's URL: the
  // stall guard (step refused) and the resume guard (blocked submit).
  const calls = SRC.split("await handBackJob(").slice(1).map((s) => s.slice(0, 600));
  const walker = calls.filter((c) => /title: jobInfo\.title, company: jobInfo\.company/.test(c));
  // Two at #377 (stall guard, resume guard); #375 added a third (step budget ran out).
  // What matters is that EVERY walker hand-back names the posting, however many there are.
  check("the multi-step walker hands back from at least two places", walker.length >= 2, String(walker.length));
  check("…and every one passes url: jobInfo.url",
    walker.length >= 2 && walker.every((c) => /url: jobInfo\.url/.test(c)),
    walker.map((c) => c.slice(0, 160)).join(" | "));

  console.log(failures ? `\n${failures} failure(s)` : "\nall good");
  process.exit(failures ? 1 : 0);
})();
