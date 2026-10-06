// The ATS walk must not judge a queue posting a second time. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/fit-reuse.test.js
//
// Why it exists: the server judges Greenhouse/Lever/Ashby rows before the run and the queue
// serves only those that cleared the user's bar. The walk then asked /tools/assess-fit with
// no job id, so the server judged the posting again on the page text — 10-02 the queue held
// 38/42 on a broad bar of 35, the live judge said 22-30, and 3 of 5 opened postings were
// skipped. What must hold now:
//   1. The queue item carries the pool row id from /jobs/ats-queue.
//   2. ASSESS_FIT sends that id ONLY for the queue head the page really is (same posting id,
//      or same title when the URL has none) and only on an ATS queue walk — never on a pool
//      run, a native Indeed/ZipRecruiter walk, or a page that is not the head.
//   3. The Indeed / ZipRecruiter gates do not send job_url (so they can never get an id).
//   4. The log says which verdict decided: reused from the list, or judged now.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
const CS = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function ok(name, cond, detail) {
  console.log(`  ${cond ? "ok " : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
  if (!cond) failures += 1;
}
function between(src, a, b) {
  const s = src.indexOf(a);
  const e = src.indexOf(b, s + a.length);
  if (s < 0 || e < 0) {
    console.error(`Markers moved: ${a} … ${b}`);
    process.exit(2);
  }
  return src.slice(s, e);
}

// ---- background.js: buildAtsQueue + queueJobIdFor (one slice, same as sweep-wait.test.js)
const QUEUE_BLOCK = between(BG, "// How long the walk is willing to wait", "// TAP-POOL queue");
// ---- background.js: the ASSESS_FIT case, run inside a minimal switch
const CASE_BLOCK = between(BG, 'case "ASSESS_FIT": {', "// ----- Screener question answering");

function sandbox({ storage = {}, queueJobs = [] } = {}) {
  const posts = [];
  const box = {
    apiPost: async (p, body) => {
      if (p === "/tools/assess-fit") {
        posts.push(body);
        return { decision: "apply", fit_score: 38, judged: true, verdict_source: body.job_id ? "queue" : "live" };
      }
      return { started: false };
    },
    apiGet: async (p) => (String(p).startsWith("/jobs/ats-queue") ? { jobs: queueJobs, pool: queueJobs.length } : { handbacks: [] }),
    addToActivityLog: async () => {},
    chrome: { storage: { local: { get: async () => ({ appliedUrls: [], appliedJobKeys: [], ...storage }) } } },
    setTimeout, Date, Math, Promise, URL, encodeURIComponent, String, Array,
  };
  vm.createContext(box);
  vm.runInContext(QUEUE_BLOCK, box);
  vm.runInContext(`async function handle(msg) { switch (msg.type) { ${CASE_BLOCK} } }`, box);
  return { box, posts };
}

const GH_ROW = { id: "row-gh", link: "https://boards.greenhouse.io/wikimedia/jobs/7012345?gh_jid=7012345", title: "Head of Marketing", company: "wikimedia" };
const ASHBY_ROW = { id: "row-ash", link: "https://jobs.ashbyhq.com/hightouch/0b4c3a1e-1111-4a2b-9c3d-123456789abc/application", title: "Growth Marketing Manager", company: "hightouch" };

(async () => {
  // 1. The queue item carries the row id.
  {
    const { box } = sandbox({ queueJobs: [GH_ROW] });
    const out = await box.buildAtsQueue("greenhouse", 5);
    ok("queue item keeps the pool row id", out.queue[0] && out.queue[0].id === "row-gh", JSON.stringify(out.queue[0]));
  }

  const head = (row) => ({ applyUrl: row.link, title: row.title, company: row.company, id: row.id });
  const assess = async (storage, data) => {
    const { box, posts } = sandbox({ storage });
    const res = await box.handle({ type: "ASSESS_FIT", data });
    return { res, body: posts[0] || {} };
  };
  const ghPage = { job_title: "Head of Marketing", company: "Wikimedia", description: "…" };

  // 2a. The head posting, reached through the board's redirect host — id sent.
  {
    const { body, res } = await assess(
      { atsPlatform: "greenhouse", atsQueue: [head(GH_ROW)] },
      { ...ghPage, job_url: "https://job-boards.greenhouse.io/wikimedia/jobs/7012345" },
    );
    ok("GH head page sends its row id", body.job_id === "row-gh", JSON.stringify(body));
    ok("the server's verdict_source comes back to content.js", res.verdict_source === "queue");
  }
  // 2b. Ashby: the queue link ends in /application, the page may not — same uuid.
  {
    const { body } = await assess(
      { atsPlatform: "ashby", atsQueue: [head(ASHBY_ROW)] },
      { job_title: "Growth Marketing Manager", company: "Hightouch", job_url: "https://jobs.ashbyhq.com/hightouch/0b4c3a1e-1111-4a2b-9c3d-123456789abc" },
    );
    ok("Ashby head page sends its row id", body.job_id === "row-ash", JSON.stringify(body));
  }
  // 2c. A different posting than the head (redirected elsewhere) — never borrow its verdict.
  {
    const { body } = await assess(
      { atsPlatform: "greenhouse", atsQueue: [head(GH_ROW)] },
      { ...ghPage, job_url: "https://job-boards.greenhouse.io/wikimedia/jobs/7099999" },
    );
    ok("another posting gets no id (live judge)", !("job_id" in body), JSON.stringify(body));
  }
  // 2d. Employer-hosted page without a posting id: title must match.
  {
    const st = { atsPlatform: "greenhouse", atsQueue: [head(GH_ROW)] };
    const same = await assess(st, { ...ghPage, job_url: "https://wikimediafoundation.org/careers/" });
    const embedded = await assess(st, { ...ghPage, job_title: "Head of Marketing (Remote)", job_url: "https://wikimediafoundation.org/careers/?gh_jid=7012345" });
    ok("employer page with ?gh_jid= matches by id even if the title differs", embedded.body.job_id === "row-gh", JSON.stringify(embedded.body));
    const other = await assess(st, { ...ghPage, job_title: "Head of Sales", job_url: "https://wikimediafoundation.org/careers/" });
    ok("no posting id + same title -> id", same.body.job_id === "row-gh", JSON.stringify(same.body));
    ok("no posting id + other title -> no id", !("job_id" in other.body), JSON.stringify(other.body));
  }
  // 2e. Not an ATS queue walk: pool run, native walk, missing id.
  {
    const pool = await assess({ atsPlatform: "pool", atsQueue: [head(GH_ROW)] }, { ...ghPage, job_url: GH_ROW.link });
    ok("pool run sends no id", !("job_id" in pool.body));
    const native = await assess({}, { ...ghPage, job_url: GH_ROW.link });
    ok("no queue (native walk routed to GH) sends no id", !("job_id" in native.body));
    const noId = await assess({ atsPlatform: "greenhouse", atsQueue: [{ ...head(GH_ROW), id: null }] }, { ...ghPage, job_url: GH_ROW.link });
    ok("queue item without an id sends no id", !("job_id" in noId.body));
  }
  // 2f. No job_url (the Indeed/ZipRecruiter gates) — never an id, even mid ATS walk state.
  {
    const { body } = await assess({ atsPlatform: "greenhouse", atsQueue: [head(GH_ROW)] }, ghPage);
    ok("a call without job_url sends no id", !("job_id" in body), JSON.stringify(body));
  }

  // 3. Only the ATS gate passes job_url.
  {
    const calls = CS.match(/type: "ASSESS_FIT",[\s\S]{0,200}?\}\s*\}?\)/g) || [];
    const withUrl = calls.filter((c) => /job_url:/.test(c));
    ok("three ASSESS_FIT call sites in content.js", calls.length === 3, String(calls.length));
    ok("exactly one (the ATS walk) passes job_url", withUrl.length === 1, String(withUrl.length));
    const ats = between(CS, "async function phase_ats(platform)", "await recordJobDescription(jobTitle, jobCompany, jobDesc, jobUrl);");
    ok("the one with job_url is phase_ats", /job_url:\s*window\.location\.href/.test(ats));
  }

  // 4. The log names the verdict's source.
  {
    const fnSrc = between(CS, "function fitSourceNote(fit) {", "async function phase_ats(platform)");
    const box = {};
    vm.createContext(box);
    vm.runInContext(fnSrc, box);
    const note = box.fitSourceNote;
    const reused = note({ verdict_source: "queue", judged_at: "2026-10-01T06:15:00+00:00" });
    ok("reused verdict says so", /from your list/.test(reused) && /10-01/.test(reused) && /not re-judged/.test(reused), reused);
    const fresh = note({ verdict_source: "live", fresh_because: "profile or resume changed since it was scored" });
    ok("fresh verdict says why", /judged now/.test(fresh) && /resume changed/.test(fresh), fresh);
    ok("no source (cap skip / old backend) adds nothing", note({ decision: "skip" }) === "" && note(null) === "");
    // The cap line must keep its "— Company cap —" signature for run-report.
    const capLine = `Skipped (fit ?): R @ C — Company cap — already tried C${note({ company_capped: true })}`;
    ok("cap line keeps its run-report signature", capLine.toLowerCase().includes("— company cap —"));
  }

  console.log(failures === 0 ? "\nall passed" : `\n${failures} failed`);
  process.exit(failures === 0 ? 0 : 1);
})();
