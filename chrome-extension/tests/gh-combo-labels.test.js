// A Greenhouse dropdown must be read under ITS OWN label. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/gh-combo-labels.test.js
//
// Live 10-06 on DoorDash's Greenhouse form: fillComboboxes walks react-select controls
// (bare DIVs, no id), and getComboLabel took the first label of the shared wrapper —
// Country read "Phone", Location read "First Name", and work authorization, both visa
// sponsorship questions and "Have you worked at DoorDash?" all read "LinkedIn Profile*".
// The profile's sponsorship answer never applied, the model got a nonsense question, and
// the first-option fallback put "Yes" on sponsorship. The fixture is that form, verbatim.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const FORM = fs.readFileSync(path.join(__dirname, "fixtures", "gh-doordash-form.html"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}
function extract(signature) {
  const at = SRC.indexOf(signature);
  if (at < 0) { console.error(`missing: ${signature}`); process.exit(2); }
  const open = SRC.indexOf("{", at + signature.length - 1);
  let depth = 0;
  for (let i = open; i < SRC.length; i++) {
    if (SRC[i] === "{") depth++;
    else if (SRC[i] === "}" && --depth === 0) return SRC.slice(at, i + 1);
  }
  return "";
}

const { window } = new JSDOM(`<!doctype html><body>${FORM}</body>`, {
  url: "https://job-boards.greenhouse.io/doordashusa/jobs/8207993",
});
const doc = window.document;

const payStart = SRC.indexOf("  const PAY_SRC =");
const payEnd = SRC.indexOf("  // Demographic / EEO self-identification");
const asked = [];
const ctx = vm.createContext({
  document: doc,
  CSS: { escape: (s) => String(s).replace(/"/g, '\\"') },
  _aiAnswersUsed: 0, MAX_AI_ANSWERS_PER_FORM: 15, _aiBudgetNotified: false,
  log() {}, logBackend() {},
  // The model is reached but answers nothing — the case that fell to the first option.
  sendMsg: async (m) => { asked.push(m.data.question); return { answer: "" }; },
});
vm.runInContext([
  SRC.slice(payStart, payEnd),
  extract("  function getFieldLabel(el) {"),
  extract("  function explicitLabel(el) {"),
  extract("  function getComboLabel(combo) {"),
  extract("  function isDemographicQuestion(label, optionTexts) {"),
  extract("  function sponsorshipSaysYes(label, needsSponsorship) {"),
  extract("  function pickOptionDeterministic(label, options, profile) {"),
  extract("  async function chooseOption(label, options, profile, jobInfo) {"),
  "globalThis.T = { getComboLabel, chooseOption };",
].join("\n"), ctx);
const { getComboLabel, chooseOption } = ctx.T;

(async () => {
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const boxes = [...doc.querySelectorAll(".select__container")];
  check("fixture holds DoorDash's 16 react-selects", boxes.length === 16, boxes.length);

  // fillComboboxes meets the control DIV first (document order), so that is what gets labelled.
  const wrong = boxes
    .map((b) => ({ want: norm(b.querySelector("label")?.textContent),
      got: getComboLabel(b.querySelector('[class*="select__control"]')) }))
    .filter((r) => r.got !== r.want);
  check("every dropdown is read under its own label", wrong.length === 0,
    JSON.stringify(wrong.map((r) => `${r.want.slice(0, 30)} ← ${r.got.slice(0, 30)}`)));

  const control = (re) => boxes.find((b) => re.test(b.querySelector("label")?.textContent || ""))
    ?.querySelector('[class*="select__control"]');
  const yesNo = ["Yes", "No"].map((text) => ({ text }));
  // "No" first, so a positional fallback can't pass a check that expects "Yes".
  const noYes = ["No", "Yes"].map((text) => ({ text }));
  const sponsorNow = getComboLabel(control(/now require immigration sponsorship/));
  const sponsorLater = getComboLabel(control(/future require immigration sponsorship/));
  const auth = getComboLabel(control(/legally authorized/));

  // The profile answers sponsorship and authorization once the label is right.
  const p = { needs_sponsorship: false, work_authorized_us: true };
  check("sponsorship (now) → the profile's No", (await chooseOption(sponsorNow, yesNo, p, {}))?.text === "No");
  check("sponsorship (future) → the profile's No", (await chooseOption(sponsorLater, yesNo, p, {}))?.text === "No");
  check("work authorization → the profile's Yes", (await chooseOption(auth, noYes, p, {}))?.text === "Yes");
  const p2 = { needs_sponsorship: true };
  check("sponsorship with needs_sponsorship=true → Yes", (await chooseOption(sponsorNow, noYes, p2, {}))?.text === "Yes");

  // The reverse wording: Yes = needs NO sponsorship. Reached on dropdowns only once the
  // real label is read (skeptic 10-06: ~4 of 320 GH schemas) — a plain read answered "No",
  // i.e. "not authorized to work".
  const without = "Are you legally authorized to work in the United States for any employer without the need for sponsorship (now or in the future)?";
  check("“authorized … without sponsorship”, needs none → Yes", (await chooseOption(without, noYes, p, {}))?.text === "Yes");
  check("“authorized … without sponsorship”, needs it → No", (await chooseOption(without, yesNo, p2, {}))?.text === "No");
  check("“without requiring employer visa sponsorship” reads the same way",
    (await chooseOption("Are you legally authorized to work in the country where this role is based, now and in the future, without requiring employer visa sponsorship?", noYes, p, {}))?.text === "Yes");
  check("“without restriction, or will you require sponsorship?” is not reversed — it goes to the model",
    (await chooseOption("Are you able to work without restriction, or will you require sponsorship?", yesNo, p, {})) === null);

  // No profile answer and a silent model: blank (→ hand-back), never the first option.
  check("sponsorship, nothing on file, model silent → left blank",
    (await chooseOption(sponsorNow, yesNo, {}, {})) === null);
  const worked = getComboLabel(control(/worked at DoorDash/));
  const workedOpts = ["I am a previous employee", "I am a current employee", "I have never worked at DoorDash"]
    .map((text) => ({ text }));
  check("“Have you worked at DoorDash?”, model silent → left blank, not “previous employee”",
    (await chooseOption(worked, workedOpts, {}, {})) === null);
  check("the model was asked the real question, not “LinkedIn Profile”",
    asked.length > 0 && asked.every((q) => !/linkedin/i.test(q)), JSON.stringify(asked));

  // Other negated wordings: which way Yes points is unclear → no profile answer, and with
  // the model silent the field stays blank instead of "No" (= "not authorized").
  for (const q of ["Are you authorized to work in the US without being sponsored?",
    "Are you authorized to work in the US and do not require visa sponsorship?"]) {
    check(`unclear negation left to the model/person: “${q.slice(36, 70)}…”`,
      (await chooseOption(q, noYes, p, {})) === null);
  }
  check("“How long will you require visa sponsorship?” is still a knockout",
    (await chooseOption("How long will you require visa sponsorship?", [{ text: "1 year" }, { text: "Not needed" }], {}, {})) === null);
  check("“How many years have you worked for a SaaS company?” is not a knockout",
    (await chooseOption("How many years have you worked for a B2B SaaS company?", [{ text: "0-2" }, { text: "3-5" }], {}, {}))?.text === "0-2");
  // A benign dropdown still gets the first-option fallback — the guard is for knockouts only.
  check("benign dropdown keeps the first-option fallback",
    (await chooseOption("Preferred office", [{ text: "Austin" }, { text: "Denver" }], {}, {}))?.text === "Austin");

  if (failures) { console.log(`\n${failures} failed`); process.exit(1); }
  console.log("\nall passed");
})();
