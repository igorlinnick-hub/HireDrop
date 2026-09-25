// A labelled, empty, required field must be fillable no matter which INPUT TYPE the
// site declared. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/typed-inputs-filled.test.js
//
// What went wrong (live 2026-09-23, user 2251ccff): six "Continue refused 3× with
// nothing left to fill" hand-backs in one evening. One of them (Mach 1 Stores,
// GENERAL MANAGER ASSISTANT) blocked on a single field whose label was "today's date"
// — a label the filler maps deterministically to localDay(). The mapping was never
// reached: fillTextQuestions() collected its candidates with
//
//     'input[type="text"], input[type="number"], textarea'
//
// so an <input type="date"> (and tel/email/url) was never even a candidate. The step
// then validated against a blank required field, "Continue" was refused, and the
// application went back to the human — for a value we already had.
//
// This pins the CANDIDATE SET, not the label rules: label mapping is covered where it
// lives, and the failure here was that a field never entered the loop at all.

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

// ---- Pull the real selector string out of fillTextQuestions -------------------------
const fnAt = SRC.indexOf("  async function fillTextQuestions(");
check("fillTextQuestions exists", fnAt > 0);
const body = SRC.slice(fnAt, fnAt + 3000);
const selMatch = body.match(/scope\.querySelectorAll\(\s*([\s\S]*?)\)\)/);
check("candidate selector found", !!selMatch);

// The selector is written as concatenated string literals — evaluate just those.
const selector = selMatch
  ? selMatch[1].split("+").map((s) => s.trim().replace(/^['"]|['"]$/g, "")).join("")
  : "";

// ---- The types a real apply form uses must all be candidates ------------------------
const dom = new JSDOM(`<!doctype html><body><form>
  <label for="d">Today's date</label><input id="d" type="date" required>
  <label for="t">Phone</label><input id="t" type="tel" required>
  <label for="e">Email</label><input id="e" type="email" required>
  <label for="u">LinkedIn URL</label><input id="u" type="url">
  <label for="x">First name</label><input id="x" type="text" required>
  <label for="n">Years of experience</label><input id="n" type="number">
  <label for="a">Why this role?</label><textarea id="a"></textarea>
  <input id="hidden-one" type="hidden" value="nope">
</form></body>`);

const matched = new Set(
  Array.from(dom.window.document.querySelectorAll(selector)).map((el) => el.id)
);

for (const [id, label] of [
  ["d", "date — the live Mach 1 blocker"],
  ["t", "tel"],
  ["e", "email"],
  ["u", "url"],
  ["x", "text"],
  ["n", "number"],
  ["a", "textarea"],
]) {
  check(`candidate: ${label}`, matched.has(id), `selector: ${selector}`);
}

// Guard the other direction: a hidden input must never become a candidate — the loop's
// own filters drop it, but it should not be collected in the first place.
check("hidden input is not a candidate", !matched.has("hidden-one"));

// ---- The date value we would type is what input[type=date] accepts ------------------
// localDay() returns en-CA (YYYY-MM-DD). A date input silently REJECTS any other shape,
// which would look exactly like the bug this test exists for.
const localDay = SRC.slice(SRC.indexOf("  function localDay("), SRC.indexOf("  function localDay(") + 200);
check("localDay uses en-CA (YYYY-MM-DD)", /toLocaleDateString\("en-CA"\)/.test(localDay), localDay.slice(0, 80));

const probe = dom.window.document.getElementById("d");
probe.value = new Date().toLocaleDateString("en-CA");
check("date input accepts that value", probe.value !== "", "a date input blanks a value it cannot parse");

console.log(failures === 0 ? "\nall passed" : `\n${failures} FAILED`);
process.exit(failures === 0 ? 0 : 1);
