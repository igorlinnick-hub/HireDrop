// Fixture test for the DOM channel of isDetected() in ../content.js — "is there a VISIBLE
// challenge element on this page". Same manual run as consent-gate.test.js:
//
//   mkdir -p /tmp/hd-gate && cd /tmp/hd-gate && npm i jsdom
//   NODE_PATH=/tmp/hd-gate/node_modules node <repo>/jobflow/chrome-extension/tests/detection-visibility.test.js
//
// Why this channel deserves fixtures: isDetected() runs before EVERY apply. A false
// negative fake-submits into a live captcha; a false positive parks the campaign for 2h.
// Both directions are pinned below.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const START = SRC.indexOf("  const FALLBACK_DETECTION = {");
const END = SRC.indexOf("  async function loadSelectors()");
if (START < 0 || END < 0) {
  console.error("Could not locate isDetected() in content.js — markers moved.");
  process.exit(2);
}
const DETECTOR_SRC = SRC.slice(START, END);

// Positions an element the way a modal is positioned: fixed, so offsetParent is null.
// That single property is what the old check got wrong, so every fixture states it.
const CASES = [
  { name: "fixed reCAPTCHA modal (the bug: offsetParent is null for fixed)",
    html: `<div style="position:fixed" class="g-recaptcha" data-sitekey="x"></div>`,
    fixed: true, detected: true },
  { name: "fixed Indeed captcha overlay",
    html: `<div style="position:fixed" data-testid="captcha-modal">Verify</div>`,
    fixed: true, detected: true },
  { name: "static reCAPTCHA widget still detected (no regression)",
    html: `<div class="g-recaptcha" data-sitekey="x"></div>`,
    fixed: false, detected: true },
  { name: "hidden first match must not mask a visible second one",
    html: `<div class="g-recaptcha" data-sitekey="tpl" data-box="zero"></div>
           <div class="g-recaptcha" data-sitekey="real"></div>`,
    fixed: false, detected: true },
  { name: "collapsed/zero-size widget alone is NOT a challenge",
    html: `<div class="g-recaptcha" data-sitekey="x" data-box="zero"></div>`,
    fixed: false, detected: false },
  { name: "display:none widget is NOT a challenge",
    html: `<div class="g-recaptcha" data-sitekey="x" style="display:none"></div>`,
    fixed: false, detected: false },
  { name: "visibility:hidden widget is NOT a challenge (old check let this through)",
    html: `<div class="g-recaptcha" data-sitekey="x" style="visibility:hidden"></div>`,
    fixed: false, detected: false },
  { name: "clean job page is clean",
    html: `<h1>Senior Engineer</h1><button>Apply now</button>`,
    fixed: false, detected: false },
];

let failures = 0;
for (const c of CASES) {
  const { window } = new JSDOM(`<body>${c.html}</body>`, {
    url: "https://www.indeed.com/viewjob?jk=abc",
    pretendToBeVisual: true,
  });
  window.Element.prototype.getBoundingClientRect = function () {
    const zero = this.dataset && this.dataset.box === "zero";
    const w = zero ? 0 : 300, h = zero ? 0 : 74;
    return { width: w, height: h, top: 0, left: 0, right: w, bottom: h, x: 0, y: 0 };
  };
  // jsdom computes offsetParent as null for everything (no layout engine), so asserting
  // "fixed" here is documentation rather than simulation — the point of the fixture is
  // that the detector no longer consults offsetParent at all.
  const sandbox = {
    window,
    document: window.document,
    getComputedStyle: window.getComputedStyle.bind(window),
    FALLBACK_SELECTORS: {}, // the slice declares `let SELECTORS = FALLBACK_SELECTORS`
    result: null,
  };
  vm.createContext(sandbox);
  vm.runInContext(DETECTOR_SRC + "\nresult = isDetected();", sandbox);
  const got = sandbox.result;
  const ok = got.detected === c.detected;
  if (!ok) failures++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${c.name}  → ${JSON.stringify(got)}`);
}
console.log(`\n${CASES.length - failures}/${CASES.length} passed`);
process.exit(failures ? 1 : 0);
