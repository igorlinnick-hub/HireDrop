// fillComboboxes must reach every dropdown on a long form. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/combo-pass-budget.test.js
//
// Live 10-05 (ext 1.8.33): both DoorDash Greenhouse applications were handed back with
// "submit blocked — 2 required fields still empty", diag invalid=[Disability Status], after
// "answered … combo×14". The loop had a flat budget of 14 passes, one widget per pass, and
// the form has 16 react-selects — Disability Status is the last one on the page and was
// never opened. The fixture is that form, captured verbatim; opening a menu and picking an
// option are emulated the way react-select renders them (menu of options, then
// .select__single-value in place of the placeholder).

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
function fn(signature) {
  const at = SRC.indexOf(signature);
  if (at < 0) { console.error(`missing: ${signature}`); process.exit(2); }
  const end = SRC.indexOf("\n  }\n", at);
  return SRC.slice(at, end + 4);
}

const CODE = [
  fn("  function reactSelectShownValue(el) {"),
  fn("  function findComboMenu(combo) {"),
  fn("  async function fillComboboxes() {"),
].join("\n");

const OPTIONS = ["Yes", "No", "I don't wish to answer"];

const { window } = new JSDOM(`<!doctype html><body>${FORM}</body>`, {
  url: "https://job-boards.greenhouse.io/doordashusa/jobs/8207993",
});
const doc = window.document;
// jsdom has no layout: attached counts as shown, except what the page's CSS hides —
// the phone widget's country picker sits in .iti__hide (display:none) until opened.
const shown = (el) => el.isConnected && !el.closest(".iti__hide");
Object.defineProperty(window.HTMLElement.prototype, "offsetParent", {
  get() { return shown(this) ? this.ownerDocument.body : null; },
});
window.HTMLElement.prototype.getClientRects = function () { return shown(this) ? [1] : []; };

const shell = (el) => el.closest(".select__container");
const ctx = vm.createContext({
  document: doc,
  window,
  KeyboardEvent: window.KeyboardEvent,
  CSS: { escape: (s) => String(s).replace(/"/g, '\\"') },
  formScope: () => doc,
  storageGet: async () => ({ profile: {}, currentJobInfo: {} }),
  sleep: async () => {},
  humanDelay: () => 0,
  logBackend: () => {},
  getComboLabel: (c) => (shell(c)?.querySelector("label")?.textContent || "").trim(),
  // react-select: the menu mounts inside the shell while open.
  openCombobox: async (c) => {
    const box = shell(c);
    if (!box || box.querySelector(".select__menu")) return;
    const menu = doc.createElement("div");
    menu.className = "select__menu";
    menu.innerHTML = `<div role="listbox">${OPTIONS.map((o) => `<div role="option" class="select__option">${o}</div>`).join("")}</div>`;
    box.appendChild(menu);
  },
  chooseOption: async (_label, options) => options[options.length - 1],
  // A pick: the menu closes and the value is drawn where the placeholder was.
  humanClick: async (el) => {
    const box = shell(el);
    if (!box || !el.classList.contains("select__option")) return;
    const ph = box.querySelector(".select__placeholder");
    if (ph) { ph.className = "select__single-value"; ph.textContent = el.textContent; }
    box.querySelector(".select__menu")?.remove();
  },
});
vm.runInContext(CODE + "\nthis.fillComboboxes = fillComboboxes;", ctx);

(async () => {
  const boxes = [...doc.querySelectorAll(".select__container")];
  check("fixture holds DoorDash's 16 react-selects", boxes.length === 16, boxes.length);
  const n = await ctx.fillComboboxes();
  const label = (b) => (b.querySelector("label")?.textContent || "").trim().slice(0, 40);
  const empty = boxes.filter((b) => !b.querySelector(".select__single-value")).map(label);
  check("every dropdown answered", empty.length === 0, `still empty: ${JSON.stringify(empty)}`);
  const dis = boxes.find((b) => /Disability Status/.test(label(b)));
  check("Disability Status (last on the page) answered",
    dis?.querySelector(".select__single-value")?.textContent === "I don't wish to answer");
  check("more widgets than the old flat budget of 14 got a pick", n > 14, `filled=${n}`);

  if (failures) { console.log(`\n${failures} failed`); process.exit(1); }
  console.log("\nall passed");
})();
