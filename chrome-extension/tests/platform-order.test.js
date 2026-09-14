// Fixture test for the campaign STAGE ORDER in ../background.js — which platform a run
// opens on, and which one it hands off to. Run it the same way as the others here:
//
//   node <repo>/jobflow/chrome-extension/tests/platform-order.test.js
//
// Why it exists (live failure 2026-09-13, Igor's account, all six platforms selected):
// the run opened on the Greenhouse pool because greenhouse was merely PRESENT in the
// list, walked 15 stale saved jobs, and ended the campaign on "pool complete" — Indeed
// and ZipRecruiter were never searched. Two bugs, one order. What must hold:
//   1. A selected board outranks the pool at open.
//   2. The pool still opens a run when no board is selected.
//   3. Hand-off goes Indeed → ZR → pool, and the pool is LAST.
//   4. A platform the user did NOT select is never switched to (consent, #143/#180).
//   5. ZR needs a live connection record; Indeed does not.
//   6. The ledger terminates — every stage tried means stop, no revisits, no loop.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
const START = SRC.indexOf("// ---- STAGE ORDER");
// Search for the closing rule AFTER the block opens — background.js has other banner
// comments above it, and an END that lands before START silently yields empty source.
const END = SRC.indexOf("// ------------------------------------------------------------------------------------", START);
if (START < 0 || END < 0) {
  console.error("Could not locate the STAGE ORDER block in background.js — markers moved.");
  process.exit(2);
}

const sandbox = { AUTO_APPLY_PLATFORMS: ["indeed", "ziprecruiter", "linkedin"] };
vm.createContext(sandbox);
vm.runInContext(
  'const ATS_ZERO_TOUCH_PLATFORMS = ["greenhouse"];\n' + SRC.slice(START, END),
  sandbox,
);
const { pickAtsOpener, pickNextStage } = sandbox;

let failures = 0;
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures++;
  console.log(`${ok ? "  ok" : "FAIL"}  ${name}${ok ? "" : `  (got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)})`}`);
}

// --- 1+2: what a run opens on ---------------------------------------------------------
const ALL_SIX = ["indeed", "ziprecruiter", "greenhouse", "lever", "ashby", "remoteok"];
check("opener: the 09-13 list opens on a board, NOT the pool", pickAtsOpener(ALL_SIX), null);
check("opener: board wins even when listed after greenhouse", pickAtsOpener(["greenhouse", "indeed"]), null);
check("opener: no board selected → the pool opens the run", pickAtsOpener(["greenhouse", "lever"]), "greenhouse");
check("opener: no board, no zero-touch pool → nothing to open", pickAtsOpener(["lever", "ashby"]), null);
check("opener: empty selection is not a crash", pickAtsOpener([]), null);

// --- 3+4+5: hand-off ------------------------------------------------------------------
const CONN = { ziprecruiter: { status: "connected" } };
check("handoff: Indeed exhausted → ZipRecruiter", pickNextStage(["indeed"], ALL_SIX, CONN), "ziprecruiter");
check("handoff: both boards spent → the pool, last", pickNextStage(["indeed", "ziprecruiter"], ALL_SIX, CONN), "greenhouse");
check("handoff: the 09-13 run now continues instead of ending",
  pickNextStage(["greenhouse"], ALL_SIX, CONN), "indeed");
check("handoff: everything tried → stop", pickNextStage(["indeed", "ziprecruiter", "greenhouse"], ALL_SIX, CONN), null);
check("handoff: never switches to a board the user didn't select",
  pickNextStage(["greenhouse"], ["greenhouse", "lever"], CONN), null);
check("handoff: ZR without a connection record is skipped",
  pickNextStage(["indeed"], ALL_SIX, {}), "greenhouse");
check("handoff: ZR logged_out is not 'connected'",
  pickNextStage(["indeed"], ALL_SIX, { ziprecruiter: { status: "logged_out" } }), "greenhouse");
check("handoff: Indeed needs no connection record", pickNextStage([], ALL_SIX, {}), "indeed");

// --- 6: the ledger terminates ---------------------------------------------------------
// A hand-off chain that can revisit a stage is an infinite campaign. Walk it to the end.
const seen = [];
let tried = [];
for (let i = 0; i < 10; i++) {
  const nxt = pickNextStage(tried, ALL_SIX, CONN);
  if (!nxt) break;
  seen.push(nxt);
  tried = tried.concat(nxt);
}
check("ledger: the full chain is Indeed → ZR → pool, then stop", seen, ["indeed", "ziprecruiter", "greenhouse"]);

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
