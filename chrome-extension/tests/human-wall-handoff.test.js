// Fixture test for the human-wall pause in ../content.js — what happens when a captcha
// or a terms gate is NOT cleared by the human. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/human-wall-handoff.test.js
//
// Why it exists (Igor, 09-21): the pause used to wait 2h and then STOP the whole
// campaign. That was right when waiting was the only alternative to killing the run —
// but these walls are account-wide WITHIN one platform, and Indeed's captcha says
// nothing about Greenhouse. Stopping dead threw away every other board the user had
// selected. The wait is now 5 minutes and ends in a hand-off, not a stop.
//
// What must hold:
//   1. A wall cleared inside the window resumes the run — nothing is handed off.
//   2. A wall still up at the deadline hands the PLATFORM on, and does NOT stop.
//   3. The hand-off clears the "solve the captcha" state first, so the dashboard CTA
//      cannot outlive the pause it describes (a CTA for a board we already left is the
//      same class of lie as a campaign reading "live" after it died — #98).
//   4. The user's own Stop during the wait still wins immediately.
//   5. The deadline is minutes, not hours — a regression back to 2h is the whole bug.

const fs = require("fs");
const path = require("path");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

// --- 5: the clock itself -------------------------------------------------------------
const waitDecl = /const HUMAN_WALL_WAIT_MS = (\d+) \* 60 \* 1000;/.exec(SRC);
check("one named clock for both walls", !!waitDecl, "HUMAN_WALL_WAIT_MS not found");
const minutes = waitDecl ? Number(waitDecl[1]) : -1;
check("the wait is 5 minutes", minutes === 5, `got ${minutes}`);
check("no 2h wall-pause survives", !/< 2 \* 60 \* 60 \* 1000/.test(SRC),
  "a `< 2 * 60 * 60 * 1000` loop is still there");
check("both walls read the same clock",
  /const CAPTCHA_WAIT_MS = HUMAN_WALL_WAIT_MS;/.test(SRC) &&
  /const CONSENT_WAIT_MS = HUMAN_WALL_WAIT_MS;/.test(SRC));

// --- 2+3: what the deadline does -----------------------------------------------------
// Both branches: everything between the deadline check and its `return`.
const branches = [...SRC.matchAll(/if \((?:isDetected\(\)\.detected|detectConsentGate\(\)\.gated)\) \{([\s\S]*?)\n        \}/g)]
  .map((m) => m[1]);
check("found both deadline branches", branches.length === 2, `got ${branches.length}`);

for (const [i, body] of branches.entries()) {
  const which = i === 0 ? "captcha" : "terms";
  check(`${which}: hands the platform on`, /PLATFORM_EXHAUSTED/.test(body));
  check(`${which}: does NOT stop the campaign itself`, !/STOP_CAMPAIGN/.test(body),
    "stopping here would skip every other selected board");
  check(`${which}: clears the hand-off state before leaving`,
    body.indexOf("DETECTION_CLEARED") > -1 &&
    body.indexOf("DETECTION_CLEARED") < body.indexOf("PLATFORM_EXHAUSTED"));
  check(`${which}: says the wall stays solvable`, /come back to this board/.test(body),
    "the user must not read this as 'that board is lost'");
}

// --- the third door: the login wall ------------------------------------------------
// Same shape, same answer: being signed out of Indeed says nothing about Greenhouse.
// Leaving this one at 2h would mean the bug still exists, just in a different doorway.
const loginBranch = /if \(detectPlatformAuth\(authPlatform\) === "logged_out"\) \{([\s\S]*?)\n        \}/.exec(SRC);
check("found the login-wall deadline branch", !!loginBranch);
if (loginBranch) {
  const body = loginBranch[1];
  check("login: hands the platform on", /PLATFORM_EXHAUSTED/.test(body));
  check("login: does NOT stop the campaign itself", !/STOP_CAMPAIGN/.test(body));
  check("login: says the board comes back", /come back to this board/.test(body));
}
check("login wall waits on the shared clock",
  /_loginPauseStart < HUMAN_WALL_WAIT_MS/.test(SRC));

// --- 1+4: inside the window ----------------------------------------------------------
// The wait loop must keep both early exits: cleared → resume, user stopped → return.
const loops = [...SRC.matchAll(/while \(Date\.now\(\) - _(?:pauseStart|gateStart|loginPauseStart) < \w+\) \{([\s\S]*?)\n        \}/g)]
  .map((m) => m[1]);
check("found all three wait loops", loops.length === 3, `got ${loops.length}`);
for (const [i, body] of loops.entries()) {
  const which = ["captcha", "terms", "login"][i];
  // The login wall clears by signing in, not by DETECTION_CLEARED — the shared
  // property is that a wall the human DID clear resumes the run instead of ending it.
  check(`${which}: a cleared wall resumes the run`, /break;/.test(body) && /resuming campaign/.test(body));
  check(`${which}: the user's own Stop still wins`, /isCampaignRunning\(\)/.test(body));
}

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
