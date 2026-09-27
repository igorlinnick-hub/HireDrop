// Two cheap rules about what the extension does to a tab it is NOT working in. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/keepalive-cadence.test.js
//
// 1. The screenshot ping used to fire every 300 ms — 3.3 messages per second, on EVERY tab
//    matching the manifest (the user's own Indeed/LinkedIn browsing included), campaign or
//    no campaign, forever. Its own comment said 2.5 s. That is the "service worker kept
//    alive on stale code" trap documented elsewhere in this codebase, paid for in battery on
//    every open job page. MV3 idles a worker out at 30 s, so 2.5 s keeps 12x the margin.
//
// 2. The approved queue asks the SERVER for its work list, and the server slices today's
//    budget over what it returns. An auto run cannot submit Lever (captcha needs a human),
//    so the run's mode has to travel with the request — otherwise the slice is spent on rows
//    the run then discards and a Lever-heavy approval stack comes back short or empty.
//    Semantics are pinned server-side (tests/test_campaign_queue.py); here we only prove the
//    client SENDS the mode, and that it matches the platforms it is about to skip.

const fs = require("fs");
const path = require("path");

const CONTENT = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

// ---- 1. the ping cadence ------------------------------------------------------------
const ms = CONTENT.match(/const SCREENSHOT_PING_MS = (\d+);/);
check("the screenshot cadence is a named constant", !!ms, "inline literal is how 300 hid");
check("…and it is 2500ms, as the comment always claimed", ms && Number(ms[1]) === 2500, ms && ms[1]);
check(
  "the interval uses that constant",
  /setInterval\(\s*\(\)\s*=>\s*\{[\s\S]{0,120}?CAPTURE_SCREENSHOT[\s\S]{0,60}?\},\s*SCREENSHOT_PING_MS\s*\)/.test(CONTENT),
  "a second literal would drift from the constant again"
);
check(
  "still well inside the MV3 30s idle limit",
  ms && Number(ms[1]) < 30_000,
  "a keep-alive slower than the idle timeout keeps nothing alive"
);

// ---- 2. the run's mode reaches the server ------------------------------------------
check(
  "the queue request carries the mode",
  /apiGet\(`\/campaign\/queue\?mode=\$\{mode\}`\)/.test(BG),
  "without it the server slices the budget over rows this run will drop"
);
const modeLine = BG.match(/const mode = ([^;]+);/);
check("the mode is derived, not hardcoded", !!modeLine, "hardcoding it would lie on tap runs");
check(
  "…from the very platforms this run skips",
  modeLine && /skip\.includes\("lever"\)\s*\?\s*"auto"\s*:\s*"tap"/.test(modeLine[1]),
  modeLine && modeLine[1]
);
// The declaration has to come BEFORE the fetch, or `mode` is a TDZ error at runtime — the
// kind of thing a shape test catches for free.
check(
  "mode is computed before the request that uses it",
  BG.indexOf("const mode = ") < BG.indexOf("/campaign/queue?mode="),
  "declaration must precede the apiGet call"
);

console.log(failures ? `\n${failures} failure(s)` : "\nall good");
process.exit(failures ? 1 : 0);
