// Greenhouse hand-backs must name what really stopped the submit. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/gh-react-select-honest.test.js
//
// 10-02, prod data: 12 of 14 Greenhouse hand-backs since 09-28 said "submit blocked — 9
// required fields still empty: country, location (city), are you legally authorized…" for a
// user whose profile holds every one of those answers. Two causes, both checked here on
// markup captured live from job-boards.greenhouse.io/twilio/jobs/8170555 (SVG icons cut):
//
//   1. collectUnfilledRequired() judged a react-select by `input.value`, which stays "" after
//      a pick — the answer renders beside it as `.select__single-value`. Every answered
//      dropdown was reported empty.
//   2. The night shift saw what the page actually did on those forms: Greenhouse emailed a
//      verification code (#security-input-0..7). The extension never looked for it — and with
//      (1) fixed alone, a fully filled form waiting for that code would leave no leftover and
//      be counted "Applied (unconfirmed)" while unsent. So the code prompt is checked first.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const CONTENT = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

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

const shownFn = slice(CONTENT, "function reactSelectShownValue(el) {", "  ");
const codeFn = slice(CONTENT, "function greenhouseAsksEmailCode() {", "  ");
const collectFn = slice(CONTENT, "function collectUnfilledRequired() {", "  ");
check("found reactSelectShownValue / greenhouseAsksEmailCode / collectUnfilledRequired",
  !!shownFn && !!codeFn && !!collectFn);

// Captured live 10-02 — the "authorized to work" dropdown before any pick.
const EMPTY = `<div class="select"><div class="select__container"><label id="question_68921598-label" for="question_68921598" class="label select__label">Are you legally authorized to work in the United States?<span aria-hidden="true">*</span></label><div class="select-shell remix-css-b62m3t-container"><span id="react-select-question_68921598-live-region" class="remix-css-7pg0cj-a11yText"></span><span aria-live="polite" aria-atomic="false" aria-relevant="additions text" role="log" class="remix-css-7pg0cj-a11yText"></span><div><div class="select__control remix-css-13cymwt-control"><div class="select__value-container remix-css-hlgwow"><div class="select__placeholder remix-css-1jqq78o-placeholder" id="react-select-question_68921598-placeholder">Select...</div><div class="select__input-container remix-css-19bb58m" data-value=""><input class="select__input" autocapitalize="none" autocomplete="off" autocorrect="off" id="question_68921598" spellcheck="false" tabindex="0" type="text" aria-autocomplete="list" aria-expanded="false" aria-haspopup="true" aria-errormessage="question_68921598-error" aria-invalid="false" aria-labelledby="question_68921598-label" aria-required="true" role="combobox" aria-activedescendant="" aria-describedby="react-select-question_68921598-placeholder question_68921598-error" enterkeyhint="done" value=""></div></div><div class="select__indicators remix-css-1wy0on6"><button type="button" class="icon-button icon-button--sm" aria-label="Toggle flyout" tabindex="-1"></button></div></div></div><input required="" tabindex="-1" aria-hidden="true" class="remix-css-1a0ro4n-requiredInput" value=""></div></div></div>`;

// The same widget after picking "Yes" — input.value is still "" (read back live).
const FILLED = `<div class="select"><div class="select__container"><label id="question_68921598-label" for="question_68921598" class="label select__label">Are you legally authorized to work in the United States?<span aria-hidden="true">*</span></label><div class="select-shell remix-css-b62m3t-container"><span id="react-select-question_68921598-live-region" class="remix-css-7pg0cj-a11yText"></span><span aria-live="polite" aria-atomic="false" aria-relevant="additions text" role="log" class="remix-css-7pg0cj-a11yText"></span><div><div class="select__control remix-css-13cymwt-control"><div class="select__value-container select__value-container--has-value remix-css-hlgwow"><div class="select__single-value remix-css-1dimb5e-singleValue">Yes</div><div class="select__input-container remix-css-19bb58m" data-value=""><input class="select__input" autocapitalize="none" autocomplete="off" autocorrect="off" id="question_68921598" spellcheck="false" tabindex="0" type="text" aria-autocomplete="list" aria-expanded="false" aria-haspopup="true" aria-errormessage="question_68921598-error" aria-invalid="false" aria-labelledby="question_68921598-label" aria-required="true" role="combobox" aria-activedescendant="" enterkeyhint="done" value="" aria-describedby="question_68921598-error"></div></div><div class="select__indicators remix-css-1wy0on6"><div aria-hidden="false"><button type="button" class="icon-button icon-button--sm" aria-label="Clear selections" data-testid="clear-selection"></button></div><button type="button" class="icon-button icon-button--sm" aria-label="Toggle flyout" tabindex="-1"></button></div></div></div></div></div></div>`;

// A plain required text field next to it, so the gate is shown to still catch real gaps.
const TEXT = (v) => `<label for="first_name">First Name*</label><input id="first_name" aria-required="true" type="text" value="${v}">`;

// The code boxes Greenhouse grows after a low-score submit (night shift, live 10-01).
const CODE = `<div><label for="security-input-0">Security code</label><input id="security-input-0" maxlength="1"><input id="security-input-1" maxlength="1"></div>`;

function run(body) {
  const dom = new JSDOM(`<!doctype html><html><body><form>${body}</form></body></html>`);
  const { window } = dom;
  // jsdom does no layout, so offsetParent is always null and the gate would see nothing.
  Object.defineProperty(window.HTMLElement.prototype, "offsetParent", {
    get() { return this.ownerDocument.body; },
  });
  const sandbox = {
    document: window.document,
    CSS: { escape: (s) => String(s).replace(/"/g, '\\"') },
    formScope: () => window.document,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    `${shownFn}\n${codeFn}\n${collectFn}\n` +
    "globalThis.__r = { leftover: collectUnfilledRequired(), code: greenhouseAsksEmailCode() };",
    sandbox
  );
  return sandbox.__r;
}

const filled = run(FILLED + TEXT("Igor"));
check("an answered react-select is NOT reported empty (input.value is \"\")",
  !filled.leftover.some((l) => l.includes("authorized")), JSON.stringify(filled.leftover));
check("a fully answered form leaves nothing", filled.leftover.length === 0, JSON.stringify(filled.leftover));

const empty = run(EMPTY + TEXT(""));
check("an unanswered react-select IS still reported empty",
  empty.leftover.some((l) => l.includes("authorized")), JSON.stringify(empty.leftover));
check("a real empty text field is still caught",
  empty.leftover.includes("first name"), JSON.stringify(empty.leftover));

check("no code boxes → not an email-code wall", run(FILLED).code === false);
const walled = run(FILLED + TEXT("Igor") + CODE);
check("code boxes → recognised as Greenhouse's emailed-code wall", walled.code === true);

// The submit path must look for the code wall BEFORE trusting an empty leftover; otherwise
// the filled-and-waiting form above falls through to "Applied (unconfirmed)".
const gate = CONTENT.indexOf("if (!result.verified && submitBtn.isConnected && window.location.href === jobUrl) {");
const codeAt = CONTENT.indexOf("if (greenhouseAsksEmailCode()) {", gate);
const leftAt = CONTENT.indexOf("const leftover = collectUnfilledRequired();", gate);
check("post-submit gate checks the email-code wall before the leftover",
  gate > 0 && codeAt > gate && leftAt > codeAt, `gate=${gate} code=${codeAt} leftover=${leftAt}`);
const codeBlock = CONTENT.slice(codeAt, leftAt);
check("the code wall un-counts the optimistic application and hands back with that reason",
  /subtractLocalApplication\(platform\)/.test(codeBlock) && /handBackJob\(/.test(codeBlock) &&
  /verification code/.test(codeBlock) && /nothing was sent/.test(codeBlock));

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
