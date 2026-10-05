// A step with no button yet must wait for the BUTTON, not for "the page has a field". Run:
//
//   node <repo>/jobflow/chrome-extension/tests/review-wait-button.test.js
//
// Live 10-05 (ext 1.8.32, 22:59–23:35Z): Indeed's review-module was abandoned 9 times,
// each one ~1 s after FORM DIAG, although the no-button branch promised a 12 s wait. It
// waited on waitForFormReady(), which answers "ready" on any input in the page shell —
// so the wait ended at once and findFormButton() was still null. On the visits that went
// through, Submit appeared 2–8 s in; one saved draft (A&J Chiropractic) was dropped in the
// 21:37 run and submitted in the 22:59 run. The 🔘 census showed no Submit in the DOM at
// all at the moment of giving up — not hidden, not fixed: not rendered yet.
//
// The fixture is that shape: a shell with an input, Submit mounted 1.5 s later.

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
  slice("  // Poll for an actionable form button", "  // ZipRecruiter's LAST step button") +
  slice("  function findFormButtonIn(scope) {", "  function isFormVisible() {") +
  `
  function findFormButton() { return findFormButtonIn(document); }
  function visibleApplyDialogs() { return []; }
  function formScope() { return document; }
  function findFieldBySelectorsOrLabel() { return null; }
  function findResumeInput() { return null; }
  async function isCampaignRunning() { return true; }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  `;

function page() {
  const { window } = new JSDOM(
    `<body><header><input placeholder="search"></header><main id="m"><h1>Please review your application</h1></main></body>`,
    { url: "https://smartapply.indeed.com/beta/indeedapply/form/review-module", pretendToBeVisual: true },
  );
  Object.defineProperty(window.HTMLElement.prototype, "offsetParent", { get() { return this.parentElement; } });
  window.Element.prototype.getBoundingClientRect = () => ({ width: 300, height: 48, top: 0, left: 0, right: 300, bottom: 48, x: 0, y: 0 });
  setTimeout(() => {
    const b = window.document.createElement("button");
    b.setAttribute("data-testid", "submit-application-button");
    b.textContent = "Submit your application";
    window.document.getElementById("m").appendChild(b);
  }, 1500);
  const sb = { window, document: window.document, getComputedStyle: window.getComputedStyle.bind(window), setTimeout, out: null };
  vm.createContext(sb);
  vm.runInContext(CODE, sb);
  return sb;
}

(async () => {
  // The bug, pinned: the old wait returns at once with no button to press.
  {
    const sb = page();
    const t0 = Date.now();
    const ready = await vm.runInContext("waitForFormReady(12000)", sb);
    const dt = Date.now() - t0;
    const btn = vm.runInContext("findFormButton()", sb);
    check("waitForFormReady says ready on the shell's input (the 10-05 shape)", ready === true && dt < 1000, `ready=${ready} ${dt}ms`);
    check("…while no button exists yet — the old branch gave up here", btn === null);
  }
  // The fix: wait for the button itself.
  {
    const sb = page();
    const t0 = Date.now();
    const got = await vm.runInContext("waitForFormButton(15000)", sb);
    const dt = Date.now() - t0;
    check("waitForFormButton waits until Submit renders", got === true && dt >= 1400 && dt < 4000, `got=${got} ${dt}ms`);
    const a = vm.runInContext("classifyFormButton()", sb);
    check("…and it is the SUBMIT", !!a.btn && a.submit === true, JSON.stringify(a.label));
  }
  // Gives up when nothing comes (bounded, no hang).
  {
    const { window } = new JSDOM(`<body><main></main></body>`, { pretendToBeVisual: true });
    const sb = { window, document: window.document, getComputedStyle: window.getComputedStyle.bind(window), setTimeout, out: null };
    vm.createContext(sb);
    vm.runInContext(CODE, sb);
    const t0 = Date.now();
    const got = await vm.runInContext("waitForFormButton(1200)", sb);
    check("waitForFormButton returns false after its timeout", got === false && Date.now() - t0 < 2500);
  }
  // Wiring: the no-button branch uses the button wait, not the field wait.
  {
    const at = SRC.indexOf("Form step had no Continue/Submit button");
    const before = SRC.slice(at - 1400, at);
    check("no-button branch waits on waitForFormButton", /await waitForFormButton\(15000\)/.test(before));
    check("…and no longer on waitForFormReady(12000)", !/waitForFormReady\(12000\)\) && findFormButton\(\)/.test(before));
    check("census carries page headings", /heads=\$\{JSON\.stringify\(heads\)\}/.test(SRC));
  }
  console.log(failures ? `\n${failures} failure(s)` : "\nall good");
  process.exit(failures ? 1 : 0);
})();
