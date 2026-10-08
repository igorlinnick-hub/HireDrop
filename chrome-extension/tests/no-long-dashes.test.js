// background.js noLongDashes must agree with modules/text_style.no_long_dashes: the
// backend cleans at generation, the extension repeats it on the relay as a belt — if the
// two drift, a backend fix gets undone on the last hop. Both sides run THIS shared table
// (fixtures/no-long-dashes-cases.json; the Python side is tests/test_text_style.py). Run:
//
//   node <repo>/jobflow/chrome-extension/tests/no-long-dashes.test.js

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const BG = fs.readFileSync(path.join(__dirname, "..", "background.js"), "utf8");
const CASES = JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", "no-long-dashes-cases.json"), "utf8"));

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

const start = BG.indexOf("function noLongDashes(text) {");
const end = BG.indexOf("\n}\n", start);
check("found noLongDashes in background.js", start >= 0 && end > start);

const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(BG.slice(start, end + 3) + "\nglobalThis.__fn = noLongDashes;", sandbox);

for (const [raw, clean] of CASES) {
  const got = sandbox.__fn(raw);
  check(JSON.stringify(raw).slice(0, 60), got === clean, `got ${JSON.stringify(got)}`);
}
check("output never keeps a long dash",
  CASES.every(([raw]) => !/—|–|(?<!-)--(?!-)/.test(sandbox.__fn(raw) || "")));

// The relays actually call it (free text + letter), and an options answer stays verbatim.
check("ANSWER_QUESTION relay cleans only when no options were sent",
  /return \{ answer: Array\.isArray\(q\.options\) && q\.options\.length \? answer : noLongDashes\(answer\) \}/.test(BG));
check("GENERATE_COVER_LETTER relay cleans the letter", /letter = noLongDashes\(result\.letter\)/.test(BG));

process.exitCode = failures ? 1 : 0;
console.log(failures ? `\n${failures} failure(s)` : "\nall good");
