// The fillers answer only what the profile holds, and a choice counts only once it TOOK.
// Run:
//
//   node <repo>/jobflow/chrome-extension/tests/filler-honest.test.js
//
// 1. Pay. content.js read `profile.desired_salary` (exists nowhere), so a salary question
//    was never answered from the user's own figure (profile.salary_expectation, asked at
//    signup since #228). The rules are ported from scripts/night_shift/common.py and the
//    cases below are the ones tests/test_night_shift_rules.py pins, so browser and server
//    say the same thing — or nothing.
// 2. School. A react-select typeahead took fillReactSelect's first row when nothing
//    matched — a wrong university on a real application. Now: the exact school, a unique
//    whole-word near match, Greenhouse's "Other", or nothing.
// 3. Dropdowns. fillComboboxes counted the click as filled and marked the widget done
//    BEFORE checking it took, so the "did it register" check was dead code: a choice that
//    never committed reported combo×1 and vanished from every later pass (Indeed
//    demographic ×3: filled=[combo×1] → filled=[] → blind hand-back). And `li > [role=
//    option]` returned both nodes, the outer one first, so the click could miss the handler.
// 4. A required race "select all that apply" group: the generic checkbox loop refuses any
//    "decline" label, so the only safe answer was never ticked.
//
// The DOM below is generic (role=combobox/option, a fieldset of checkboxes) plus the
// Greenhouse react-select captured live by #307 (twilio/8170555); it pins our logic, not
// a claim about a specific Indeed page.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

function extract(signature) {
  const at = SRC.indexOf(signature);
  if (at < 0) return null;
  const open = SRC.indexOf("{", at + signature.length - 1);
  let depth = 0;
  for (let i = open; i < SRC.length; i++) {
    if (SRC[i] === "{") depth++;
    else if (SRC[i] === "}") {
      depth--;
      if (depth === 0) return SRC.slice(at, i + 1);
    }
  }
  return null;
}

// ---- 1. Pay: parity with the night shift ---------------------------------------------
const payStart = SRC.indexOf("  const PAY_SRC =");
const payEnd = SRC.indexOf("  // Demographic / EEO self-identification");
check("pay + school helpers found", payStart > 0 && payEnd > payStart);
const helpers = SRC.slice(payStart, payEnd);
const pure = {};
vm.createContext(pure);
vm.runInContext(`${helpers}\nglobalThis.H = { payQuestion, payAmounts, salaryAnswer, pickSchoolOption, SCHOOL_FIELD_RE, stateListPick };`, pure);
const H = pure.H;

const PQ = [
  ["What are your salary expectations for this role?", "expectation"],
  ["What is your desired salary?", "expectation"],
  ["Desired salary", "expectation"],
  ["Salary", "expectation"],
  ["desired base pay", "expectation"],
  ["Expected OTE?", "expectation"],
  ["desired income", "expectation"],
  ["How much do you expect to be paid?", "expectation"],
  ["Desired hourly pay", "expectation"],
  ["Are you comfortable with the salary range of $55k-$65k for this role?", "expectation"],
  ["What is your current base pay?", "current"],
  ["Current salary", "current"],
  ["How many years of experience do you have in compensation and benefits administration?", null],
  ["Describe your experience designing compensation plans", null],
  ["Have you ever paid for our product?", null],
  ["Why are you interested in working at Suno?", null],
];
for (const [label, kind] of PQ) check(`payQuestion(${JSON.stringify(label).slice(0, 50)}) = ${kind}`, H.payQuestion(label) === kind, H.payQuestion(label));

const BRACKETS = ["Under $60,000", "$60,000 - $80,000", "$80,000 - $110,000", "$110,000+"];
const SA = [
  ["$100,000 per year", BRACKETS, "$80,000 - $110,000"],
  ["$150,000", BRACKETS, "$110,000+"],
  ["$50,000", BRACKETS, "Under $60,000"],
  ["85-95k", BRACKETS, "$80,000 - $110,000"],
  ["$100,000+", ["$75,000 - $100,000", "$100,000 - $125,000"], "$100,000 - $125,000"],
  ["$125,000", ["$75,000 - $100,000", "$100,000 - $125,000"], "$100,000 - $125,000"],
  ["$30/hr", ["Less than $25/hr", "$25-$35/hr", "More than $35/hr"], "$25-$35/hr"],
  ["$100,000 per year", ["$55,000 - $65,000", "$60,000 - $75,000", "$70,000 - $85,000"], ""],
  ["$100,000 per year", ["$40,000 - $60,000", "$60,000 - $70,000"], ""],
  ["$100,000 per year", ["Prefer not to say", "$50k-$75k"], ""],
  ["$35/hour", BRACKETS, ""],
  ["$8,000/month", BRACKETS, ""],
  ["$30,000", ["Less than $25/hr", "$25-$35/hr", "More than $35/hr"], ""],
  ["$100,000 per year", ["$20 - $25", "$25 - $30"], ""],
  ["$100,000 per year", ["Yes", "No"], ""],
  ["negotiable", BRACKETS, ""],
];
for (const [stated, options, picked] of SA) {
  const got = H.salaryAnswer({ salary_expectation: stated }, options);
  check(`bracket for ${stated} among ${options.length} → ${JSON.stringify(picked)}`, got === picked, got);
}
const yearly = { salary_expectation: "$100k per year" }, hourly = { salary_expectation: "$35/hour" };
check("numeric: annual box gets the annual figure", H.salaryAnswer(yearly, null, true, "Desired salary") === "100000");
check("numeric: 85-95k → 85000", H.salaryAnswer({ salary_expectation: "85-95k" }, null, true, "Salary") === "85000");
check("numeric: an hourly figure never goes into an annual box", H.salaryAnswer(hourly, null, true, "Desired salary") === "");
check("numeric: hourly box gets the hourly figure", H.salaryAnswer(hourly, null, true, "Desired hourly rate") === "35");
check("numeric: an annual figure never goes into an hourly box", H.salaryAnswer(yearly, null, true, "Desired hourly rate") === "");
check("free text gets the user's own words", H.salaryAnswer(hourly) === "$35/hour" && H.salaryAnswer({ salary_expectation: "negotiable" }) === "negotiable");
check("'I'd rather not name a number' wins over a stale figure",
  H.salaryAnswer({ salary_expectation: "$100,000", no_salary_expectation: true }) === "");
check("nothing on file → nothing", H.salaryAnswer({}, BRACKETS) === "" && H.salaryAnswer({ salary_min: 100000 }) === "");

// ---- 2. School ------------------------------------------------------------------------
check("the school FIELD is matched by its whole label",
  ["School", "University", "School Name", "Name of your school", "School / University"].every((l) => H.SCHOOL_FIELD_RE.test(l)));
check("…not a list or a yes/no that mentions one",
  !["Highest level of school completed", "Did you graduate from college?", "College major"].some((l) => H.SCHOOL_FIELD_RE.test(l)));
check("exact school wins", H.pickSchoolOption("University of Florida", ["University of Central Florida", "University of Florida"]) === "University of Florida");
check("MIT is not Smith College", H.pickSchoolOption("MIT", ["Smith College", "Mitchell College"]) === null);
check("an ambiguous campus list gets nothing",
  H.pickSchoolOption("University of Hawaii", ["University of Hawaii at Manoa", "University of Hawaii at Hilo"]) === null);
check("a longer name is a different school, not a near match",
  H.pickSchoolOption("Columbia College Chicago", ["Columbia College", "Columbia College Hollywood", "Other"]) === null &&
  H.pickSchoolOption("Columbia College", ["Columbia College Chicago", "Other"]) === null &&
  H.pickSchoolOption("Texas A&M University Corpus Christi", ["Texas A&M University", "Other"]) === null);
check("the same name with a note in brackets is taken",
  H.pickSchoolOption("University of Hawaii at Manoa", ["University of Hawaii at Manoa (Honolulu)", "Chaminade University"]) === "University of Hawaii at Manoa (Honolulu)");

// ---- 2b. State of residence: the profile's state or nothing ----------------------------
// Option lists copied from the captured Greenhouse schemas (data/gh_form_schemas.jsonl):
// codes (oura), names (affirm, + Canadian provinces), "(US) "-prefixed (instacart).
{
  const schemas = fs.readFileSync(path.join(__dirname, "..", "..", "data", "gh_form_schemas.jsonl"), "utf8")
    .split("\n").filter(Boolean).map((l) => JSON.parse(l));
  const listOf = (board) => {
    for (const d of schemas) {
      if (d.board !== board) continue;
      for (const q of d.questions) {
        const f = q.fields[0];
        if (f.type === "multi_value_single_select" && /\bstate\b/i.test(q.label) && f.values.length > 40)
          return f.values.map((v) => ({ text: v.label }));
      }
    }
    return null;
  };
  const codes = listOf("oura"), names = listOf("affirm"), prefixed = listOf("instacart");
  check("captured state lists found", codes && names && prefixed);
  const pick = (opts, state) => H.stateListPick(opts, { state }).option?.text ?? null;
  check("a Californian is not Alabama (the old first-row fallback)",
    pick(names, "California") === "California" && pick(codes, "California") === "CA" && pick(prefixed, "California") === "(US) California");
  check("a code on file finds the name, and HI is not Michigan (the old substring)",
    pick(names, "HI") === "Hawaii" && pick(prefixed, "TX") === "(US) Texas" && pick(codes, "HI") === "HI");
  check("no state on file → nothing, never a guess", pick(names, "") === null && pick(codes, "Ontario") === null);
  check("a short list is not a state list (yes/no stays with the other rules)",
    H.stateListPick([{ text: "Yes" }, { text: "No" }], { state: "CA" }).isList === false);
}

// ---- 3. Dropdowns: a choice counts only once it took ---------------------------------
const comboParts = [
  extract("  function reactSelectShownValue(el) {"),
  extract("  function getFieldLabel(el) {"),
  extract("  function visibleApplyDialogs() {"),
  extract("  function formScope() {"),
  extract("  function findComboMenu(combo) {"),
  extract("  function explicitLabel(el) {"),
  extract("  function getComboLabel(combo) {"),
  extract("  async function fillComboboxes() {"),
];
const fieldish = SRC.match(/const FIELDISH_SELECTOR =\s*\n?\s*'[^']*';/);
check("combobox filler + helpers found", comboParts.every(Boolean) && !!fieldish);

function comboWorld(html, { want } = {}) {
  const dom = new JSDOM(`<!doctype html><body><main>${html}</main></body>`, { runScripts: "outside-only" });
  const w = dom.window;
  w.Element.prototype.getClientRects = function () { return this.closest("[hidden]") ? [] : [{}]; };
  Object.defineProperty(w.HTMLElement.prototype, "offsetParent", {
    get() { return this.closest("[hidden]") ? null : this.parentElement; },
  });
  w.CSS = { escape: (s) => String(s).replace(/["\\\]\[#.:]/g, "\\$&") };
  w.__logs = [];
  w.__clicked = [];
  w.storageGet = async () => ({ profile: {}, currentJobInfo: {} });
  w.openCombobox = async (c) => {
    c.setAttribute("aria-expanded", "true");
    const m = w.document.getElementById(c.getAttribute("aria-controls"));
    if (m) m.hidden = false;
  };
  w.chooseOption = async (label, options) => options.find((o) => o.text === (want || "Yes")) || options[0];
  w.humanClick = async (el) => { w.__clicked.push(el.outerHTML.slice(0, 60)); el.dispatchEvent(new w.MouseEvent("click", { bubbles: true })); };
  w.sleep = async () => {};
  w.humanDelay = () => 0;
  w.logBackend = (t) => w.__logs.push(t);
  w.eval(`${fieldish[0]}\n${comboParts.join("\n")}\nwindow.__fill = fillComboboxes;`);
  return w;
}

(async () => {
  // A widget that never commits the click (the Indeed demographic shape we suspect).
  {
    const w = comboWorld(`
      <label id="g">Gender</label>
      <div role="combobox" aria-labelledby="g" aria-controls="m1">Select an option</div>
      <ul id="m1" role="listbox" hidden><li role="option">Yes</li><li role="option">No</li></ul>`);
    const n = await w.__fill();
    const combo = w.document.querySelector('[role="combobox"]');
    check("a choice that never took is NOT counted as filled", n === 0, `filled=${n}`);
    check("…it was retried once, then given up on", w.__clicked.length === 2 && combo.dataset.hdSkip === "1", `clicks=${w.__clicked.length}`);
    check("…and the give-up is said out loud", w.__logs.some((l) => l.startsWith("Dropdown didn't take our choice")), JSON.stringify(w.__logs));
  }
  // A widget that commits: its text changes to the answer.
  {
    const w = comboWorld(`
      <div role="combobox" aria-label="Are you 18 or older?" aria-controls="m2">Select an option</div>
      <ul id="m2" role="listbox" hidden><li role="option">Yes</li><li role="option">No</li></ul>`);
    const combo = w.document.querySelector('[role="combobox"]');
    w.document.getElementById("m2").addEventListener("click", (e) => { combo.textContent = e.target.textContent; });
    const n = await w.__fill();
    check("a choice that took counts once and is not re-opened", n === 1 && w.__clicked.length === 1 && combo.dataset.hdDone === "1", `filled=${n} clicks=${w.__clicked.length}`);
  }
  // li > [role=option]: the handler lives on the inner node.
  {
    const w = comboWorld(`
      <div role="combobox" aria-label="Willing to travel?" aria-controls="m3">Select an option</div>
      <ul id="m3" role="listbox" hidden><li><div role="option" class="opt">Yes</div></li><li><div role="option" class="opt">No</div></li></ul>`);
    const combo = w.document.querySelector('[role="combobox"]');
    for (const o of w.document.querySelectorAll(".opt")) {
      o.addEventListener("click", (e) => { if (e.target === o) combo.textContent = o.textContent; });
    }
    const n = await w.__fill();
    check("the click lands on the inner [role=option], not the outer <li>", n === 1 && w.__clicked[0].includes('role="option"'), w.__clicked[0]);
  }
  // Greenhouse react-select already answered (captured live by #307): left alone.
  {
    const FILLED = `<div class="select__control"><div class="select__value-container select__value-container--has-value"><div class="select__single-value">Yes</div><div class="select__input-container"><input class="select__input" id="q1" type="text" aria-autocomplete="list" aria-label="Are you legally authorized to work in the United States?" aria-required="true" role="combobox" value=""></div></div></div>`;
    const w = comboWorld(FILLED);
    const n = await w.__fill();
    check("an answered react-select is not re-opened (its value is drawn beside the input)", n === 0 && w.__clicked.length === 0, `clicks=${w.__clicked.length}`);
  }

  // ---- 4. A required race group gets its decline box ---------------------------------
  const cbParts = [extract("  function getFieldLabel(el) {"), extract("  function visibleApplyDialogs() {"),
    extract("  function formScope() {"), extract("  function isDemographicQuestion(label, optionTexts) {"),
    extract("  function isDeclineOption(text) {"),
    SRC.slice(SRC.indexOf("  // ── Legal work status: ONE reading"), SRC.indexOf("  // Pick a dropdown option deterministically")),
    extract("  async function fillCheckboxes() {")];
  check("checkbox filler + helpers found", cbParts.every(Boolean));
  function cbWorld(html) {
    const dom = new JSDOM(`<!doctype html><body><main>${html}</main></body>`, { runScripts: "outside-only" });
    const w = dom.window;
    Object.defineProperty(w.HTMLElement.prototype, "offsetParent", { get() { return this.parentElement; } });
    w.CSS = { escape: (s) => String(s).replace(/["\\\]\[#.:]/g, "\\$&") };
    w.humanClick = async (el) => { el.click(); };
    w.sleep = async () => {};
    w.humanDelay = () => 0;
    // Work-status collaborators (not reached by these cases; work-status.test.js drives them).
    w.storageGet = async () => ({ profile: {} });
    w.sendMsg = async () => ({ answer: "" });
    w.logBackend = () => {};
    w._aiAnswersUsed = 0; w.MAX_AI_ANSWERS_PER_FORM = 15;
    w.eval(`${fieldish[0]}\n${cbParts.join("\n")}\nwindow.__fill = fillCheckboxes;`);
    return w;
  }
  {
    const w = cbWorld(`
      <fieldset><legend>Race / Ethnicity (select all that apply)</legend>
        <label><input type="checkbox" id="r1" required> Hispanic or Latino</label>
        <label><input type="checkbox" id="r2" required> White</label>
        <label><input type="checkbox" id="r3" required> I don't wish to answer</label></fieldset>
      <label><input type="checkbox" id="ok" required> I certify the information above is true</label>`);
    const n = await w.__fill();
    const checked = Array.from(w.document.querySelectorAll("input:checked")).map((c) => c.id);
    check("the race group gets ONLY its decline box", checked.includes("r3") && !checked.includes("r1") && !checked.includes("r2"), checked.join(","));
    check("a required attestation outside it is still ticked", checked.includes("ok") && n === 2, `${checked.join(",")} n=${n}`);
  }
  {
    // An outer wrapper must not inherit the nested "Race" legend and swallow the attestation
    // (skeptic A, 10-02: checked=r2 only, n=1 before the fix).
    const w = cbWorld(`
      <div role="group">
        <fieldset><legend>Race / Ethnicity</legend>
          <label><input type="checkbox" id="r1" required> White</label>
          <label><input type="checkbox" id="r2" required> I don't wish to answer</label></fieldset>
        <label><input type="checkbox" id="ok" required> I certify the information above is true</label></div>`);
    const n = await w.__fill();
    const checked = Array.from(w.document.querySelectorAll("input:checked")).map((c) => c.id);
    check("a nested race group inside an outer wrapper: decline + the wrapper's attestation",
      checked.includes("r2") && checked.includes("ok") && !checked.includes("r1") && n === 2, `${checked.join(",")} n=${n}`);
  }
  {
    const w = cbWorld(`
      <fieldset><legend>Race</legend>
        <label><input type="checkbox" id="r1" required> Asian</label>
        <label><input type="checkbox" id="r2" required> Black or African American</label></fieldset>`);
    await w.__fill();
    check("a race group with no decline option is left alone (never an identity value)",
      w.document.querySelectorAll("input:checked").length === 0);
  }

  // ---- 5. Text step: dropdown search boxes are not text questions ----------------------
  const textFn = extract("  async function fillTextQuestions() {") || "";
  check("the text step skips comboboxes except the school/city typeaheads",
    /const isCombo = isReactSelectField\(el\) \|\| el\.getAttribute\("role"\) === "combobox";\s*\n\s*if \(isCombo && !SCHOOL_FIELD_RE\.test\(rawLabel\) && !\/\\bcity\\b\|\\blocations\?\\b\/\.test\(label\)\) continue;/.test(textFn));
  check("…and skips a react-select that already shows an answer", /if \(reactSelectShownValue\(el\)\) return false;/.test(textFn));
  check("a school typeahead never falls back to the first row",
    /typeahead === "school"\) \{\s*\n\s*if \(!\(await fillSchoolTypeahead\(el, value\)\)\) continue;/.test(textFn));
  check("the select picker no longer reads the phantom desired_salary", !/desired_salary \|\|/.test(SRC.replace(/\/\/.*$/gm, "")));
  const choose = extract("  async function chooseOption(label, options, profile, jobInfo) {") || "";
  check("a pay dropdown never reaches the model or the first-option fallback",
    /if \(payQuestion\(label\)\) return chosen \|\| null;/.test(choose) && choose.indexOf("payQuestion(label)") < choose.indexOf("ANSWER_QUESTION"));

  {
    // The real chooseOption with the AI budget spent — the path that put Alabama on a
    // Californian (skeptic B, 10-01). The model must not be reached at all.
    const ctx = { _aiAnswersUsed: 99, MAX_AI_ANSWERS_PER_FORM: 15, _aiBudgetNotified: true,
      logBackend() {}, chrome: { runtime: { sendMessage() { throw new Error("model reached"); } } } };
    vm.createContext(ctx);
    const wsBlock = SRC.slice(SRC.indexOf("  // ── Legal work status: ONE reading"), SRC.indexOf("  // Pick a dropdown option deterministically"));
    vm.runInContext(`${helpers}\n${extract("  function isDemographicQuestion(label, optionTexts) {")}\n${extract("  function isDeclineOption(text) {")}\n${wsBlock}\n` +
      `${extract("  function pickOptionDeterministic(label, options, profile) {")}\n${choose}\n` +
      "globalThis.choose = chooseOption;", ctx);
    const opts = ["Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
      "Florida", "Georgia", "Hawaii", "Texas", "Not in the US"].map((text) => ({ text }));
    const got = await ctx.choose("Please select your current state of residence.", opts, { state: "CA" }, {});
    const none = await ctx.choose("What state do you live in?", opts, { state: "" }, {});
    check("chooseOption: state of residence = the profile's, budget or not; none on file → nothing",
      got?.text === "California" && none === null, `${got?.text} / ${none?.text}`);
  }

  console.log(failures ? `\n${failures} failure(s)` : "\nall good");
  process.exit(failures ? 1 : 0);
})();
