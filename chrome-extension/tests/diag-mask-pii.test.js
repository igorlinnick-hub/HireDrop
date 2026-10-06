// Diagnostic strings must not carry an email or phone number to the activity log. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/diag-mask-pii.test.js
//
// Pre-release review of 1.8.34 (10-06): formBlockers() masked emails/phones, but its siblings
// did not — buttonCensus() sent 3-word button labels and h1/h2/alert headings as is, and
// structuredReviewSnapshot() claimed "masked as in formBlockers" with no mask at all. An
// account-menu button or a contact heading on the resume-review step echoes the person.

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
  slice("  function maskPii(s) {", "  function formBlockers() {") +
  slice("  function structuredReviewSnapshot() {", "  function formBlockersLine(fb) {") +
  slice("  function buttonCensus() {", "  function btnLabel(b) {");

const { window } = new JSDOM(
  `<body><main>
     <h1>Contact jane.doe@example.com</h1>
     <div role="alert">Call (808) 555-1234 to verify</div>
     <button>jane.doe@example.com account menu</button>
     <button aria-label="Call +1 808 555 1234">x</button>
     <a href="#">Edit Senior Engineer</a>
     <span>Missing jane.doe@example.com</span>
     <button>Continue</button>
   </main></body>`,
  { url: "https://smartapply.indeed.com/beta/indeedapply/form/resume-module/structured-data-review", pretendToBeVisual: true },
);
Object.defineProperty(window.HTMLElement.prototype, "offsetParent", { get() { return this.parentElement; } });
Object.defineProperty(window.HTMLElement.prototype, "offsetWidth", { get() { return 100; } });
window.Element.prototype.getClientRects = () => [{ width: 100, height: 20 }];

const ctx = vm.createContext({ document: window.document, window, getComputedStyle: window.getComputedStyle.bind(window) });
vm.runInContext(CODE + "\nthis.census = buttonCensus; this.sdr = structuredReviewSnapshot;", ctx);

const census = ctx.census();
const sdr = ctx.sdr();
for (const [name, out] of [["buttonCensus", census], ["structuredReviewSnapshot", sdr]]) {
  check(`${name}: no email`, !/jane\.doe|@example/.test(out), out);
  check(`${name}: no phone digits`, !/555/.test(out), out);
}
check("census still names the controls", /Continue/.test(census) && /<email>/.test(census), census);
check("census heading kept, masked", /Contact <email>/.test(census), census);
check("sdr still lists actions", /Edit Senior/.test(sdr), sdr);

if (failures) {
  console.log(`\n${failures} failed`);
  process.exit(1);
}
console.log("\nall passed");
