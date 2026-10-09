// collectFormAnswers: the copy of what the employer reads, for History. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/form-answers.test.js
//
// Questions with the answer the form carries; contact fields and the letter left out.

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const SUNO = fs.readFileSync(path.join(__dirname, "fixtures", "ashby-suno-form.html"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}
function fn(sig) {
  const at = SRC.indexOf(sig);
  if (at < 0) { console.error(`moved: ${sig}`); process.exit(2); }
  const open = SRC.indexOf("{", at + sig.length - 1);
  let d = 0;
  for (let i = open; i < SRC.length; i++) {
    if (SRC[i] === "{") d++;
    else if (SRC[i] === "}" && --d === 0) return SRC.slice(at, i + 1);
  }
  return "";
}
const CODE = [
  SRC.match(/const FIELDISH_SELECTOR =\s*\n?\s*'[^']*';/)[0],
  SRC.match(/ {2}const QA_SKIP_RE = .*;/)[0],
  SRC.match(/ {2}const QA_PLACEHOLDER_RE = .*;/)[0],
  fn("  function getFieldLabel(el) {"),
  fn("  function reactSelectShownValue(el) {"),
  fn("  function isReactSelectField(el) {"),
  fn("  function collectFormAnswers() {"),
].join("\n");

function run(html, mutate) {
  const dom = new JSDOM(`<!doctype html><body><form>${html}</form></body>`, { runScripts: "outside-only" });
  const w = dom.window;
  Object.defineProperty(w.HTMLElement.prototype, "offsetParent", { get() { return this.parentElement; } });
  w.CSS = { escape: (s) => String(s) };
  w.formScope = () => w.document;
  if (mutate) mutate(w.document);
  w.eval(`${CODE}\nwindow.__r = collectFormAnswers();`);
  return w.__r;
}

const suno = run(SUNO, (doc) => {
  doc.querySelector('[data-field-path="_systemfield_location"] input').value = "Honolulu, Hawaii, United States";
  doc.querySelector('input[name="Yes."]').checked = true;
  doc.querySelector('[data-option="yes"]').setAttribute("aria-pressed", "true");
});
const get = (rows, re) => rows.find((r) => re.test(r.q))?.a;
check("Ashby location typeahead → its value", get(suno, /^Location$/) === "Honolulu, Hawaii, United States", JSON.stringify(suno));
check("Ashby office checkbox → “Yes.”", get(suno, /5 days per week/) === "Yes.", JSON.stringify(suno));
check("Ashby yes/no button → “Yes”", get(suno, /legally authorized/) === "Yes", JSON.stringify(suno));

const synth = run(`
  <label for="e">Email</label><input type="email" id="e" value="a@b.c">
  <label for="fn">First Name</label><input type="text" id="fn" value="Igor">
  <label for="y">Years of experience with SQL</label><input type="text" id="y" value="5">
  <label for="cl">Cover letter</label><textarea id="cl">${"x".repeat(900)}</textarea>
  <label for="s">How did you hear about us?</label>
  <select id="s"><option>Select...</option><option selected>LinkedIn</option></select>
  <label for="s2">Pronouns</label><select id="s2"><option selected>Select...</option><option>He/him</option></select>
  <fieldset><legend>Do you require sponsorship?</legend>
    <label><input type="radio" name="sp" checked> No</label><label><input type="radio" name="sp"> Yes</label></fieldset>
  <label for="em">Why this company?</label><input type="text" id="em" value="">
`);
check("text answer kept", get(synth, /Years of experience/) === "5", JSON.stringify(synth));
check("select answer kept", get(synth, /hear about/) === "LinkedIn", JSON.stringify(synth));
check("radio answer kept", get(synth, /sponsorship/) === "No", JSON.stringify(synth));
check("contact fields left out", !get(synth, /email/i) && !get(synth, /first name/i), JSON.stringify(synth));
check("the letter and unanswered fields left out", synth.length === 3, JSON.stringify(synth));

process.exitCode = failures ? 1 : 0;
console.log(failures ? `\n${failures} failure(s)` : "\nall good");
