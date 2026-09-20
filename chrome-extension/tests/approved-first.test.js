// Fixture test for APPROVED-FIRST — the rule that a run sends the jobs the human
// swiped before the ones the machine found, in BOTH submit modes. Run it like the
// others here:
//
//   node <repo>/jobflow/chrome-extension/tests/approved-first.test.js
//
// Why it exists (live 09-11, a real account): `approved` rows are consumed by a tap run
// and nothing else, so a user who swiped and then ran Auto left a stack nobody would
// ever pick up — 4 approvals from 09-02, never sent. The dashboard dock (website #169)
// made that visible; this makes the run stop producing it.
//
// The rules this pins, each of which had to be reasoned about once and must not be
// re-reasoned by the next person reading START_CAMPAIGN:
//   1. Auto builds the approved queue too (it used to skip it entirely).
//   2. Auto drops Lever approvals — its submit needs a human at a captcha, and nobody
//      is watching an auto run. Tap keeps them: there the human IS at the wheel.
//   3. Tap with nothing approved still refuses to start (the footgun guard). Auto with
//      nothing approved starts the way it always did.
//   4. An approved-led run seeds the failover ledger with "pool", NOT with a board —
//      seeding the board would burn it before it ran (the 09-13 failure, in reverse).
//   5. When the approved cards run out: auto hands off to the boards, tap goes idle
//      inside the pool and waits for more swipes.
//
// These are pure decisions extracted from background.js by their source text, so the
// test fails loudly if the shapes move rather than passing against a stale copy.

const fs = require("fs");
const path = require("path");

const SRC = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");

let failures = 0;
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    failures += 1;
    console.error(`FAIL ${name}\n  expected: ${JSON.stringify(expected)}\n  actual:   ${JSON.stringify(actual)}`);
  } else {
    console.log(`ok   ${name}`);
  }
}

// ---- 1. the queue is built in both modes -------------------------------------------
// The old shape was `let tapPoolQueue = []; if (tapMode) { tapPoolQueue = await ... }`.
// The new one calls it unconditionally; the mode only decides the skip list.
const buildsUnconditionally =
  /let tapPoolQueue = await buildApprovedAtsQueue\(/.test(SRC) &&
  !/let tapPoolQueue = \[\];\s*\n\s*if \(tapMode\) \{/.test(SRC);
check("auto builds the approved queue too", buildsUnconditionally, true);

// ---- 2. lever is auto-excluded, tap-included ---------------------------------------
const leverSkip = /skipPlatforms: tapMode \? \[\] : \["lever"\]/.test(SRC);
check("auto skips Lever approvals, tap keeps them", leverSkip, true);

// The mid-walk rebuild must apply the same rule — an auto run that picked up a Lever
// card on its second lap would stall exactly the same way.
const leverSkipOnRebuild = /skipPlatforms: poolLeadMode === "auto" \? \["lever"\] : \[\]/.test(SRC);
check("the rebuild skips Lever for auto as well", leverSkipOnRebuild, true);

// ---- 3. the footgun guard stays TAP-ONLY -------------------------------------------
const tapOnlyGuard = /if \(tapMode && !tapPoolQueue\.length\) \{[\s\S]{0,600}?error: "no_approved_jobs"/.test(SRC);
check("empty-approved refusal is tap-only", tapOnlyGuard, true);

// ---- 4. an approved-led run seeds the ledger with the pool -------------------------
const seedsPool = /triedPlatforms: \[tapPoolQueue\.length \? "pool" : \(atsTarget \|\| primaryPlatform\)\]/.test(SRC);
check("approved-led run seeds triedPlatforms with 'pool'", seedsPool, true);

// ---- 5. what happens when the approved cards run out -------------------------------
const leadModeStored = /poolLeadMode: tapPoolQueue\.length \? \(tapMode \? "tap" : "auto"\) : null/.test(SRC);
check("the run records who leads the pool", leadModeStored, true);

// Auto must NOT return early on an empty rebuild (that return is the idle-and-wait
// behaviour); it must fall through to the PLATFORM_EXHAUSTED hand-off below.
const autoFallsThrough =
  /if \(poolLeadMode === "auto"\) \{[\s\S]{0,500}?Sent the jobs you approved[\s\S]{0,300}?\} else \{[\s\S]{0,300}?Caught up on approved jobs[\s\S]{0,120}?return;/.test(SRC);
check("auto hands off to the boards, tap idles for more swipes", autoFallsThrough, true);

// The idle refill loop is the tapalka's — an auto run must never park in it.
const refillGuard = /if \(d\.poolLeadMode === "auto"\) return;/.test(SRC);
check("idle refill refuses to hold an auto run", refillGuard, true);

// ---- 6. a stopped run keeps no lead mode -------------------------------------------
const cleared = /remove\(\["atsQueue", "atsPlatform", "poolLeadMode"\]\)/.test(SRC);
check("STOP_CAMPAIGN clears poolLeadMode", cleared, true);

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nall approved-first checks passed");
