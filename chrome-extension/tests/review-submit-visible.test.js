// A pinned Submit must count as a button. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/review-submit-visible.test.js
//
// What went wrong (prod activity_log, 10-02..10-05): Indeed's last page,
// /indeedapply/form/review-module, ended 15 of 18 visits with `btn="-" (none) → no button`
// and "Form abandoned without submit" — after every step before it was filled. The button
// finder skipped anything whose offsetParent is null, and offsetParent is null for an
// element that is itself position:fixed (a Submit pinned to the bottom of the screen).
// That is a hypothesis about Indeed's page, not a capture of it: buttonCensus() logs what
// the page really offered the next time no button is found. What this test pins is the
// finder's rule — "has a layout box and is not hidden", not "has an offsetParent".
//
// jsdom does no layout, so offsetParent and getBoundingClientRect are stubbed from the
// inline style: fixed → offsetParent null; display:none on self/ancestor → zero box.

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

function slice(from, to) {
  const a = SRC.indexOf(from);
  const b = SRC.indexOf(to, a);
  if (a < 0 || b < 0) {
    console.error(`markers moved: ${from} … ${to}`);
    process.exit(2);
  }
  return SRC.slice(a, b);
}

const CODE =
  slice("  function isVisibleBox(el, minW, minH) {", "  // Returns { gated: bool, label: string }") +
  slice("  function findFormButtonIn(scope) {", "  function isFormVisible() {") +
  "\n  function findFormButton() { return findFormButtonIn(document); }\n";

function page(body) {
  const { window } = new JSDOM(`<body>${body}</body>`, {
    url: "https://smartapply.indeed.com/beta/indeedapply/form/review-module",
    pretendToBeVisual: true,
  });
  const hiddenUp = (el) => {
    for (let n = el; n; n = n.parentElement) {
      if (n.style && n.style.display === "none") return true;
    }
    return false;
  };
  Object.defineProperty(window.HTMLElement.prototype, "offsetParent", {
    get() {
      if (hiddenUp(this) || this.style.position === "fixed") return null;
      return this.parentElement;
    },
  });
  window.Element.prototype.getBoundingClientRect = function () {
    const w = hiddenUp(this) ? 0 : 300, h = hiddenUp(this) ? 0 : 48;
    return { width: w, height: h, top: 0, left: 0, right: w, bottom: h, x: 0, y: 0 };
  };
  const sb = { window, document: window.document, getComputedStyle: window.getComputedStyle.bind(window), out: null };
  vm.createContext(sb);
  vm.runInContext(CODE, sb);
  return sb;
}
const run = (sb, expr) => vm.runInContext(`out = ${expr}; out`, sb);

// ---- 1. the bug: a Submit that is itself position:fixed ---------------------------------
{
  const sb = page(`<h1>Please review your application</h1>
    <button style="position:fixed" data-testid="submit-application-button">Submit your application</button>`);
  const a = run(sb, "classifyFormButton()");
  check("fixed Submit is found", !!a.btn, JSON.stringify(a.label));
  check("…and classified as the SUBMIT", a.submit === true, String(a.submit));
}
// Same, found by its text (no data-testid) — the fallback path had the same check.
{
  const sb = page(`<div><button style="position:fixed">Submit your application</button></div>`);
  const a = run(sb, "classifyFormButton()");
  check("fixed Submit found by text fallback", !!a.btn && a.submit === true, JSON.stringify(a));
  check("isSubmitStep sees the fixed Submit", run(sb, "isSubmitStep()") === true);
}

// ---- 2. hidden stays hidden ---------------------------------------------------------------
{
  const sb = page(`<div style="display:none"><button data-testid="submit-application-button">Submit your application</button></div>`);
  check("display:none Submit is NOT a button", run(sb, "findFormButton()") === null);
}
{
  // In-flow + visibility:hidden keeps the OLD answer (offsetParent is set, so it counted
  // before too); only the new fixed path must not let a hidden pinned bar through.
  const sb = page(`<button style="position:fixed;visibility:hidden" data-testid="submit-application-button">Submit your application</button>`);
  check("fixed + visibility:hidden Submit is NOT a button", run(sb, "findFormButton()") === null);
}

// ---- 3. no regression: the ordinary in-flow Continue ---------------------------------------
{
  const sb = page(`<main><button data-testid="continue-button">Continue</button></main>`);
  const a = run(sb, "classifyFormButton()");
  check("in-flow Continue still found, not a submit", !!a.btn && a.submit === false, JSON.stringify(a));
}
// Deny list still wins over visibility: a fixed "Save and exit" bar is never pressed.
{
  const sb = page(`<button style="position:fixed">Save and exit</button>`);
  check("fixed Save-and-exit is still denied", run(sb, "findFormButton()") === null);
}

// A pinned job-page CTA must not become the form button (it is above the Submit in DOM order).
{
  const sb = page(`<a href="#apply" style="position:fixed">Apply for this job</a>
    <form><button type="button" id="btn-x">Send</button></form>`);
  check("fixed 'Apply for this job' CTA is NOT taken", run(sb, "findFormButton()") === null);
}

// ---- 4. the census names why nothing was taken --------------------------------------------
{
  const sb = page(`<button style="position:fixed" data-testid="submit-application-button">Submit your application</button>
    <div style="display:none"><button>Continue</button></div>
    <iframe></iframe>`);
  const c = run(sb, "buttonCensus()");
  check("census counts iframes", /iframes=1/.test(c), c);
  check("census flags the fixed button op + fx:fixed + box",
    /submit-application-button:Submit your application\[op,fx:fixed,box\]/.test(c), c);
  check("census flags the hidden one op without box", /button:Continue\[op\]/.test(c), c);
}

// ---- 5. wiring: the no-button exit writes the census ----------------------------------------
{
  const at = SRC.indexOf("Form step had no Continue/Submit button");
  const near = SRC.slice(at, at + 400);
  check("no-button exit logs 🔘 census", /🔘 @\$\{location\.pathname\.slice\(-70\)\} \$\{buttonCensus\(\)\}/.test(near), near.slice(0, 200));
}

console.log(failures ? `\n${failures} failure(s)` : "\nall good");
process.exit(failures ? 1 : 0);
