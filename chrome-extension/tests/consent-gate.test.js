// Fixture test for detectConsentGate() in ../content.js — the "Accept Terms" wall that
// pauses a campaign the way a captcha does.
//
// There is no JS test runner in this repo, and adding one for a single detector wasn't
// worth the dependency. Run it by hand when you touch the detector or its phrase lists:
//
//   mkdir -p /tmp/hd-gate && cd /tmp/hd-gate && npm i jsdom
//   NODE_PATH=/tmp/hd-gate/node_modules node <repo>/jobflow/chrome-extension/tests/consent-gate.test.js
//
// The detector lives inside content.js's IIFE, so we slice its source out of the file
// instead of importing it — that way the test always exercises the shipped code.

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const START = SRC.indexOf("  const FALLBACK_CONSENT_GATE");
const END = SRC.indexOf("  async function loadSelectors()");
if (START < 0 || END < 0) {
  console.error("Could not locate detectConsentGate() in content.js — markers moved.");
  process.exit(2);
}
const DETECTOR_SRC = SRC.slice(START, END);

// The false-positive guards are the point of this file. A detector that fires on a Lever
// apply form or a cookie banner would stall every run — strictly worse than not having it.
const CASES = [
  { name: "Indeed-style updated-terms modal",
    html: `<div role="dialog" aria-modal="true"><h2>We've updated our Terms of Service</h2>
           <p>Please review and accept the terms of service to continue.</p>
           <button>Decline</button><button>Accept</button></div>`,
    gated: true, label: "Accept" },
  { name: "<dialog open> with an Agree link-button",
    html: `<dialog open><p>Accept the terms and conditions to continue.</p>
           <a role="button">I Agree</a></dialog>`,
    gated: true, label: "I Agree" },
  { name: "alertdialog that names the terms",
    html: `<div role="alertdialog"><p>Accept the terms of service to continue.</p>
           <button>Accept all</button></div>`,
    gated: true, label: "Accept all" },
  { name: "cookie banner (privacy policy only) must NOT pause",
    html: `<div role="alertdialog"><p>We use cookies. See our privacy policy.</p>
           <button>Accept all cookies</button><button>Manage</button></div>`,
    gated: false },
  { name: "Lever apply form modal with an 'I agree to terms' checkbox",
    html: `<div role="dialog"><h2>Apply to Acme</h2>
           <input type="text" name="name"><input type="email" name="email">
           <textarea name="cover"></textarea>
           <label><input type="checkbox"> I agree to the terms of service</label>
           <button>Submit application</button></div>`,
    gated: false },
  { name: "login modal quoting the terms in its fine print",
    html: `<div role="dialog"><h2>Sign in</h2><input type="email"><input type="password">
           <p>By continuing you agree to our terms of use.</p>
           <button>Agree and continue</button></div>`,
    gated: false },
  { name: "promo modal with an Accept button but no terms language",
    html: `<div role="dialog"><h2>Try Indeed Plus</h2><button>Accept</button></div>`,
    gated: false },
  { name: "terms text on the page but not in a dialog",
    html: `<footer><a>Terms of Service</a></footer><button>Accept</button>`,
    gated: false },
  { name: "collapsed aria-only dialog node left in the DOM",
    html: `<div role="dialog" data-box="zero"><p>terms of service</p><button>Accept</button></div>`,
    gated: false },
  { name: "hidden dialog (display:none)",
    html: `<div role="dialog" style="display:none"><p>terms of service</p><button>Accept</button></div>`,
    gated: false },
];

let failures = 0;
for (const c of CASES) {
  const { window } = new JSDOM(`<body>${c.html}</body>`, { pretendToBeVisual: true });
  // jsdom does no layout — every rect is 0x0, which would fail isVisibleBox for every
  // fixture. Hand out a real box, except where the fixture is testing the collapsed case.
  window.Element.prototype.getBoundingClientRect = function () {
    const zero = this.dataset && this.dataset.box === "zero";
    const w = zero ? 0 : 600, h = zero ? 0 : 400;
    return { width: w, height: h, top: 0, left: 0, right: w, bottom: h, x: 0, y: 0 };
  };
  const sandbox = {
    window,
    document: window.document,
    getComputedStyle: window.getComputedStyle.bind(window),
    detection: () => ({}), // exercise the in-file fallback config
    result: null,
  };
  vm.createContext(sandbox);
  vm.runInContext(DETECTOR_SRC + "\nresult = detectConsentGate();", sandbox);
  const got = sandbox.result;
  const ok = got.gated === c.gated && (c.label === undefined || got.label === c.label);
  if (!ok) failures++;
  console.log(`${ok ? "PASS" : "FAIL"}  ${c.name}  → ${JSON.stringify(got)}`);
}
console.log(`\n${CASES.length - failures}/${CASES.length} passed`);
process.exit(failures ? 1 : 0);
