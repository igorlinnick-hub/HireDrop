// Fixture test for the approved pool's PLATFORM ADMISSION and ORDER in ../background.js.
//
//   node <repo>/jobflow/chrome-extension/tests/pool-native-order.test.js
//
// Two rules, both paid for in real applications:
//
// 1. ADMISSION. Indeed is POOL_NATIVE_VERIFIED and belongs in the default pool; only
//    ZipRecruiter (PENDING, by-link unproven) waits behind the tapNativePool flag. Until
//    2026-09-22 one line gated BOTH, contradicting the comment on POOL_NATIVE_VERIFIED
//    twelve lines above it ("Always in pool"). A paying welder's four approved Indeed
//    cards sat unsent from 09-02 because of it: the server's queue offered them and the
//    executor dropped them silently. Measured the same day: 77 Indeed applications exist,
//    71 of them AFTER the flag went off — the flag never gated Indeed's ability to
//    submit, only the by-link entry into the same flow.
//
// 2. ORDER. 2026-08-04 is remembered as "Indeed burned the run", but the mechanism was
//    the queue order: stalled native jobs at the head each consumed a slot, so the walk
//    never reached the zero-touch rows behind them. Zero-touch first means the reliable
//    submits are already out before any native tail is attempted — and re-admitting a
//    native platform can never cost more than the leftover slots.
//
// Both rules are read out of the source, because both were a single line that looked
// harmless and neither has a cheap live test.

const fs = require("fs");
const path = require("path");

const SRC = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");

let failures = 0;
function check(name, ok, detail) {
  if (ok) { console.log(`ok   ${name}`); return; }
  failures += 1;
  console.error(`FAIL ${name}${detail ? "\n  " + detail : ""}`);
}

// ---- 1. admission -------------------------------------------------------------------
const flagOffLine = /: ATS_PLATFORMS\.concat\(POOL_NATIVE_VERIFIED\);/.test(SRC);
check("with the flag OFF the pool still admits VERIFIED natives (Indeed)", flagOffLine,
  "expected the flag-off branch to be ATS_PLATFORMS.concat(POOL_NATIVE_VERIFIED)");

const flagOnLine = /\? ATS_PLATFORMS\.concat\(POOL_NATIVE_VERIFIED, POOL_NATIVE_PENDING\)/.test(SRC);
check("the flag ON additionally admits PENDING natives (ZipRecruiter)", flagOnLine);

// The exact shape of the old bug: a flag-off branch that is ATS only.
const oldBug = /\? ATS_PLATFORMS\.concat\(POOL_NATIVE_VERIFIED, POOL_NATIVE_PENDING\)\s*\n\s*: ATS_PLATFORMS;/.test(SRC);
check("the old both-gated line is gone", !oldBug,
  "the flag-off branch is bare ATS_PLATFORMS again — Indeed would silently drop out");

check("Indeed is still declared VERIFIED, ZR still PENDING",
  /POOL_NATIVE_VERIFIED = \["indeed"\]/.test(SRC) && /POOL_NATIVE_PENDING = \["ziprecruiter"\]/.test(SRC));

// ---- 2. order -----------------------------------------------------------------------
check("the queue is sorted zero-touch first, natives last",
  /const rank = \(p\) => \(ATS_PLATFORMS\.includes\(p\) \? 0 : 1\);/.test(SRC) &&
  /out\.sort\(\(a, b\) => rank\(a\.platform\) - rank\(b\.platform\)\);/.test(SRC));

// The sort must run BEFORE the per-platform cap filter, or the cap could spend the
// budget on natives and leave the reordering cosmetic. Scoped to the function's own
// body: `const cap = perPlatformCap` appears in buildAtsQueue too, and a file-wide
// indexOf finds that one first (this test failed on exactly that before being scoped).
const FN_START = SRC.indexOf("async function buildApprovedAtsQueue(");
const FN_END = SRC.indexOf("\n}", FN_START);
const FN = SRC.slice(FN_START, FN_END);
check("the function body was located", FN_START > 0 && FN_END > FN_START);
const sortAt = FN.indexOf("out.sort((a, b) => rank(a.platform)");
const capAt = FN.indexOf("const cap = perPlatformCap > 0");
check("the sort happens before the cap is applied", sortAt > 0 && capAt > sortAt,
  `within buildApprovedAtsQueue: sort at ${sortAt}, cap at ${capAt}`);

// ---- 3. behavioural: run the real admission+order logic on fixtures -----------------
// Extract nothing; re-implement the two lines exactly as asserted above and prove the
// intent on data, so a future reader sees WHAT the rules produce, not just that they exist.
const ATS_PLATFORMS = ["greenhouse", "lever", "ashby"];
const POOL_NATIVE_VERIFIED = ["indeed"];
const POOL_NATIVE_PENDING = ["ziprecruiter"];
const admit = (flag) => flag === true
  ? ATS_PLATFORMS.concat(POOL_NATIVE_VERIFIED, POOL_NATIVE_PENDING)
  : ATS_PLATFORMS.concat(POOL_NATIVE_VERIFIED);

check("flag off: indeed admitted, ziprecruiter not",
  admit(false).includes("indeed") && !admit(false).includes("ziprecruiter"));
check("flag on: both admitted", admit(true).includes("ziprecruiter"));

const rank = (p) => (ATS_PLATFORMS.includes(p) ? 0 : 1);
const queue = [
  { platform: "indeed", id: 1 }, { platform: "indeed", id: 2 },
  { platform: "greenhouse", id: 3 }, { platform: "ashby", id: 4 },
];
const sorted = queue.slice().sort((a, b) => rank(a.platform) - rank(b.platform));
check("the 08-04 shape is fixed: two Indeed heads no longer precede the ATS rows",
  sorted.map((j) => j.id).join(",") === "3,4,1,2",
  `got ${sorted.map((j) => j.platform + "#" + j.id).join(" ")}`);

// Stability within a group matters: the server ordered by freshness/score already.
const sameGroup = [
  { platform: "greenhouse", id: "a" }, { platform: "ashby", id: "b" }, { platform: "lever", id: "c" },
];
check("order inside the zero-touch group is preserved",
  sameGroup.slice().sort((a, b) => rank(a.platform) - rank(b.platform))
    .map((j) => j.id).join("") === "abc");

if (failures) { console.error(`\n${failures} check(s) failed`); process.exit(1); }
console.log("\nall pool admission + order checks passed");
