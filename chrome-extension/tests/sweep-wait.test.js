// Fixture test for buildAtsQueue in ../background.js — does the walk wait for the sweep
// it just started? No JS test runner in this repo (see consent-gate.test.js for the same
// reasoning); run it by hand:
//
//   node <repo>/jobflow/chrome-extension/tests/sweep-wait.test.js
//
// Why it exists: POST /jobs/find-ats returns the instant it spawns its backend thread,
// and this function used to read the queue on the very next line. On a search whose
// boards had never been swept — a new account, or the morning after the keywords changed
// — the walk built its queue from the pool as it was BEFORE that search existed, and the
// rows the sweep saved half a minute later were only picked up by the NEXT run. What must
// hold now:
//   1. Sweep started + nothing in the queue → keep asking until the rows land.
//   2. The queue already has work → never wait. The sweep is a top-up, not a gate.
//   3. The server refused the sweep (cooldown) → never wait; there is nothing coming.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
const START = SRC.indexOf("// How long the walk is willing to wait");
const END = SRC.indexOf("// TAP-POOL queue", START);
if (START < 0 || END < 0) {
  console.error("Could not locate buildAtsQueue in background.js — markers moved.");
  process.exit(2);
}
// Real waits are 45s/5s. The behaviour under test is "does it re-ask", not how long it
// sleeps, so the fixture shrinks both — otherwise this file would take a minute to run.
const BLOCK = SRC.slice(START, END)
  .replace("SWEEP_WAIT_MS = 45_000", "SWEEP_WAIT_MS = 400")
  .replace("SWEEP_POLL_MS = 5_000", "SWEEP_POLL_MS = 20");

function run({ sweep, queues }) {
  const calls = { queueReads: 0, log: [] };
  const box = {
    apiPost: async () => sweep,
    apiGet: async () => {
      const i = Math.min(calls.queueReads, queues.length - 1);
      calls.queueReads += 1;
      return queues[i];
    },
    addToActivityLog: async (text) => { calls.log.push(text); },
    chrome: { storage: { local: { get: async () => ({ appliedUrls: [], appliedJobKeys: [] }) } } },
    setTimeout,
    Date,
    Math,
    Promise,
    encodeURIComponent,
  };
  vm.createContext(box);
  vm.runInContext(BLOCK, box);
  return box.buildAtsQueue("greenhouse", 5).then((out) => ({ out, calls }));
}

const job = { link: "https://boards.greenhouse.io/x/jobs/1", title: "Event Manager", company: "X" };
const EMPTY = { jobs: [], pool: 0, off_search: 0 };
const FULL = { jobs: [job], pool: 1, off_search: 0 };

let failures = 0;
function ok(name, cond) {
  console.log(`  ${cond ? "ok " : "FAIL"}  ${name}`);
  if (!cond) failures += 1;
}

(async () => {
  // 1. The sweep is running and the pool has nothing for this search yet.
  {
    const { out, calls } = await run({
      sweep: { started: true, search_changed: true },
      queues: [EMPTY, EMPTY, FULL],
    });
    ok("waits for a started sweep and picks up the rows it saved", out.queue.length === 1);
    ok("re-asks the queue instead of reading it once", calls.queueReads >= 3);
    ok("says what it is waiting for", calls.log.length === 1);
  }

  // 2. There is already work — the sweep is a top-up, not a gate.
  {
    const { out, calls } = await run({ sweep: { started: true }, queues: [FULL] });
    ok("does not wait when the queue already has jobs", calls.queueReads === 1);
    ok("returns that work immediately", out.queue.length === 1);
    ok("stays silent when it did not wait", calls.log.length === 0);
  }

  // 3. The server refused the sweep — waiting would be waiting for nothing.
  {
    const { calls } = await run({
      sweep: { started: false, cooldown: true, retry_in_secs: 300 },
      queues: [EMPTY],
    });
    ok("does not wait when no sweep was started", calls.queueReads === 1);
  }

  console.log(failures === 0 ? "\nall passed" : `\n${failures} failed`);
  process.exit(failures === 0 ? 0 : 1);
})();
