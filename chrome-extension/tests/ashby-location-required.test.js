// Ashby: the Location autocomplete must be fillable, and a blocked submit must name
// what stopped it. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/ashby-location-required.test.js
//
// Live 10-08, Suno "Product Manager, Marketing Landing Pages": the submit was refused
// with "Missing entry for required field: Location" and the hand-back arrived blind
// (unfilled: [], questions: []). Two causes, both checked here on markup captured from
// the live form (fixtures/ashby-suno-form.html):
//
//   1. Ashby's autocomplete is an <input> with NO type attribute. fillTextQuestions
//      collected only typed inputs (text/number/date/tel/email/url), so the one filler
//      that can drive a typeahead never saw the field; fillComboboxes saw role=combobox
//      but a typeahead opens no menu without typing → hdSkip. Nothing filled it.
//      (The fill mechanics themselves are fine: typing via setNativeValue opens the menu
//      on the live form and a plain option click commits the full city string.)
//   2. Ashby marks required on the LABEL's class (_required_…), never on the control, so
//      collectUnfilledRequired() found nothing and the hand-back could not tell the
//      person (or the ledger) what to finish.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const CONTENT = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const FIXTURE = fs.readFileSync(path.join(__dirname, "fixtures", "ashby-suno-form.html"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

function slice(src, signature, indent) {
  const start = src.indexOf(signature);
  if (start < 0) return null;
  const end = src.indexOf(`\n${indent}}\n`, start);
  if (end < 0) return null;
  return src.slice(start, end + indent.length + 3);
}

const collectFn = slice(CONTENT, "function collectUnfilledRequired() {", "  ");
const shownFn = slice(CONTENT, "function reactSelectShownValue(el) {", "  ");
const isRsFn = slice(CONTENT, "function isReactSelectField(el) {", "  ");
check("found collectUnfilledRequired / reactSelectShownValue / isReactSelectField",
  !!collectFn && !!shownFn && !!isRsFn);

// The text filler's selector list must include typeless inputs — that one string is the
// difference between seeing and never seeing Ashby's autocomplete.
const textQ = slice(CONTENT, "async function fillTextQuestions() {", "  ") || "";
check("fillTextQuestions collects input:not([type]) (Ashby autocomplete has no type attr)",
  textQ.includes('input:not([type])'));
// …but NOT Greenhouse's hidden react-select mirror (<input required aria-hidden
// tabindex="-1" class="…requiredInput">, 14 on DoorDash's live form): typing into those
// burns the AI budget on invisible fields and hides unanswered dropdowns.
check("the typeless selector excludes aria-hidden / tabindex=-1 mirrors",
  textQ.includes('input:not([type]):not([aria-hidden="true"]):not([tabindex="-1"])'));

function run(mutate) {
  const dom = new JSDOM(`<!doctype html><html><body><form>${FIXTURE}</form></body></html>`);
  const { window } = dom;
  Object.defineProperty(window.HTMLElement.prototype, "offsetParent", {
    get() { return this.ownerDocument.body; },
  });
  if (mutate) mutate(window.document);
  const sandbox = {
    document: window.document,
    CSS: { escape: (s) => String(s).replace(/"/g, '\\"') },
    formScope: () => window.document,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    `${shownFn}\n${isRsFn}\n${collectFn}\n` +
    "globalThis.__r = { leftover: collectUnfilledRequired()," +
    "  loc: document.querySelector('[data-field-path=\"_systemfield_location\"] input')," +
    "  combo: isReactSelectField(document.querySelector('[data-field-path=\"_systemfield_location\"] input')) };",
    sandbox
  );
  return sandbox.__r;
}

// Untouched form: both blockers of the live hand-back are named.
const blank = run();
check("empty required Location is reported (label-class marker, no required attr)",
  blank.leftover.some((l) => l === "location"), JSON.stringify(blank.leftover));
check("empty required office MultiValueSelect is reported",
  blank.leftover.some((l) => l.includes("willing to work 5 days")), JSON.stringify(blank.leftover));
check("empty required Boolean (aria-pressed yes/no) is reported",
  blank.leftover.some((l) => l.includes("legally authorized")), JSON.stringify(blank.leftover));
check("the location input is seen as a typeahead (fillReactSelect path)", blank.combo === true);
check("the location input matches the text filler's selector",
  (() => { try { return blank.loc && blank.loc.matches('input:not([type])'); } catch { return false; } })());

// Location picked (Ashby writes the chosen city into input.value) + a box ticked →
// nothing left over: the detector must not cry wolf on an answered form.
const done = run((doc) => {
  doc.querySelector('[data-field-path="_systemfield_location"] input').setAttribute("value", "Honolulu, Hawaii, United States");
  doc.querySelector('[data-field-path="70ddf418-1404-466d-9777-a96b2ef8719d"] input[type="checkbox"]').setAttribute("checked", "");
  doc.querySelector('[data-field-path="936bcdfc-1e6e-434f-912d-99e719f0ab8e"] [data-option="yes"]').setAttribute("aria-pressed", "true");
});
check("an answered Ashby form reports nothing",
  done.leftover.length === 0, JSON.stringify(done.leftover));

process.exitCode = failures ? 1 : 0;
console.log(failures ? `\n${failures} failure(s)` : "\nall good");
