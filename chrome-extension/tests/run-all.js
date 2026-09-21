// Run every *.test.js in this directory, one process each, and fail on the first
// non-zero exit. This is what CI calls (`npm run test:ext`) and what you can call by
// hand:  node chrome-extension/tests/run-all.js
//
// Why a runner instead of `node --test`: these tests are plain scripts that print their
// own PASS/FAIL lines and signal the verdict through the exit code — not node:test
// suites. The built-in runner would wrap them in a second layer of reporting that says
// nothing extra. Why not a shell one-liner in package.json: this stays readable, works
// the same on macOS and the Ubuntu runner, and names which file failed.
//
// Discovery is by directory listing on purpose: a new test file is picked up without
// anyone remembering to register it. That is the whole point — the JS suite sat OUTSIDE
// CI until 09-21 (ci.yml ran Python only), so three working tests were never checked and
// two more had been broken by a missing dependency with nobody noticing.

const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const dir = __dirname;
const files = fs
  .readdirSync(dir)
  .filter((f) => f.endsWith(".test.js"))
  .sort();

if (!files.length) {
  console.error("No *.test.js files found — did this directory move?");
  process.exit(2);
}

let failed = [];
for (const f of files) {
  console.log(`\n=== ${f} ===`);
  try {
    execFileSync(process.execPath, [path.join(dir, f)], { stdio: "inherit" });
  } catch {
    failed.push(f);
  }
}

console.log(`\n${files.length - failed.length}/${files.length} test files passed`);
if (failed.length) {
  console.error(`failed: ${failed.join(", ")}`);
  process.exit(1);
}
