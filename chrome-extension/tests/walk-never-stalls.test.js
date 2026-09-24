// Fixture test: no exit from a form may leave the walk stalled (../content.js).
// Same manual run as verify-baseline.test.js:
//
//   node chrome-extension/tests/run-all.js       (or this file directly with jsdom on NODE_PATH)
//
// Two confirmed bugs, one disease (a "running" campaign dead-stops silently):
//   1. fillRadioQuestions: a radio input with NO name attribute → r.name === "" →
//      `input[name=""]` matches nothing → group = [] → group[0].closest(...) threw.
//      In phase3 the wrapper caught it and skipped the job EVERY time (that posting
//      could never be auto-applied); in phase_ats the throw propagated further.
//   2. _phase3_fillForm had two early returns that advanced NOTHING — the review-mode
//      skip and the no-resume fail-closed guard. The form stayed open, detectPhase()
//      kept answering "form", the phase observer only fires on CHANGE → dead-stop.
// Invariant: every exit from a form advances the walk (submit / hand-back / skip).

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

// ---- Extract the real functions from content.js --------------------------------------
function slice(startMarker, endMarker) {
  const s = SRC.indexOf(startMarker);
  const e = SRC.indexOf(endMarker, s);
  if (s < 0 || e < 0) {
    console.error(`Could not locate [${startMarker}] .. [${endMarker}] — markers moved.`);
    process.exit(2);
  }
  return SRC.slice(s, e);
}

const CODE =
  slice("  // Best-effort human-readable label for any form field.", "  // Find a visible element matching") +
  slice("  // Demographic / EEO self-identification", "  // Pick a dropdown option deterministically") +
  slice("  // Fill unanswered radio-button screener questions.", "  // Tick required attestation");

function makeSandbox(html) {
  const { window } = new JSDOM(`<body>${html}</body>`, {
    url: "https://smartapply.indeed.com/beta/indeedapply/form/questions",
  });
  const sandbox = {
    window,
    document: window.document,
    // jsdom's window.CSS has no escape(); the tested ids are plain, so identity works.
    CSS: (window.CSS && window.CSS.escape) ? window.CSS : { escape: (s) => String(s) },
    Event: window.Event,
    formScope: () => window.document,
    // Deterministic stand-ins for the human-behavior helpers (jsdom has no layout
    // or native label activation); the group/target choice under test is untouched.
    humanClick: async (el) => {
      const doc = el.ownerDocument;
      const t = el.tagName === "LABEL"
        ? (doc.getElementById(el.getAttribute("for") || "") || el) : el;
      if (t.type === "radio" || t.type === "checkbox") t.checked = true;
    },
    humanDelay: () => 0,
    sleep: async () => {},
    done: null,
  };
  vm.createContext(sandbox);
  return sandbox;
}

async function run(sandbox, expr) {
  vm.runInContext(CODE + `\ndone = (async () => (${expr}))();`, sandbox);
  return await sandbox.done;
}

(async () => {
  // ---- Bug 1: nameless radios must not crash the form fill ---------------------------
  {
    // The real shape (React-controlled screener): radios with no name attribute at
    // all, grouped only by their fieldset. Plus a normal named group after them —
    // if the nameless one throws, the named one never fills and the job is lost.
    const sb = makeSandbox(`
      <fieldset>
        <legend>Are you willing to relocate?</legend>
        <input type="radio" id="nr-yes"><label for="nr-yes">Yes</label>
        <input type="radio" id="nr-no"><label for="nr-no">No</label>
      </fieldset>
      <fieldset>
        <legend>Are you authorized to work in the US?</legend>
        <input type="radio" name="auth" id="a-yes"><label for="a-yes">Yes</label>
        <input type="radio" name="auth" id="a-no"><label for="a-no">No</label>
      </fieldset>`);
    let threw = null;
    let filled = -1;
    try { filled = await run(sb, "fillRadioQuestions()"); } catch (e) { threw = e; }
    check("nameless radio group does not throw", threw === null, threw && threw.message);
    const namelessChecked = [sb.document.getElementById("nr-yes"), sb.document.getElementById("nr-no")]
      .filter((o) => o.checked).length;
    check("nameless group gets exactly one pick", namelessChecked === 1, `checked=${namelessChecked}`);
    check("the named group after it still fills (job proceeds)",
      sb.document.getElementById("a-yes").checked === true, "auth=Yes not picked");
    check("both groups counted as filled", filled === 2, `filled=${filled}`);
  }
  {
    // A nameless radio with NO enclosing fieldset/radiogroup either — worst case,
    // the input alone is the group. Must still not throw.
    const sb = makeSandbox(`
      <div><input type="radio" id="lone"><label for="lone">Yes</label></div>`);
    let threw = null;
    try { await run(sb, "fillRadioQuestions()"); } catch (e) { threw = e; }
    check("scope-less nameless radio does not throw", threw === null, threw && threw.message);
    check("scope-less nameless radio gets picked", sb.document.getElementById("lone").checked === true);
  }
  {
    // Regression: named radios behave exactly as before — Yes/No eligibility → Yes,
    // an already-answered group is left alone.
    const sb = makeSandbox(`
      <fieldset>
        <legend>Are you 18 or older?</legend>
        <input type="radio" name="age" id="g-yes"><label for="g-yes">Yes</label>
        <input type="radio" name="age" id="g-no"><label for="g-no">No</label>
      </fieldset>
      <fieldset>
        <legend>Can you commute?</legend>
        <input type="radio" name="commute" id="c-yes" checked><label for="c-yes">Yes</label>
        <input type="radio" name="commute" id="c-no"><label for="c-no">No</label>
      </fieldset>`);
    const filled = await run(sb, "fillRadioQuestions()");
    check("named Yes/No group still picks Yes", sb.document.getElementById("g-yes").checked === true);
    check("already-answered named group untouched",
      sb.document.getElementById("c-yes").checked === true &&
      sb.document.getElementById("c-no").checked === false);
    check("only the unanswered group is counted", filled === 1, `filled=${filled}`);
  }

  // ---- Bug 2 (STRUCTURAL): every _phase3_fillForm exit advances the walk -------------
  // Same technique as verify-baseline's call-site checks: assert on the source, so a
  // future bare `return;` regression at these exits fails loudly.
  function exitWindow(marker) {
    const i = SRC.indexOf(marker);
    if (i < 0) { console.error(`marker moved: [${marker}]`); process.exit(2); }
    const w = SRC.slice(i, i + 900);
    return { firstReturn: w.indexOf("return;"), text: w };
  }
  {
    // Review-mode "skip" verdict: must advance past the job before returning.
    const w = exitWindow("Skipped by you: ${jobInfo.title}");
    const adv = w.text.indexOf("await skipToNextJob();");
    check("phase3 review-mode skip advances the walk",
      adv >= 0 && adv < w.firstReturn,
      "expected `await skipToNextJob();` before the return");
  }
  {
    // Resume fail-closed guard: honest outcome is a hand-back (reason + ledger),
    // then the walk advances.
    const w = exitWindow("Skipped (no resume attached)");
    const hb = w.text.indexOf("await handBackJob(");
    const adv = w.text.indexOf("await skipToNextJob();");
    check("phase3 resume guard hands the job back",
      hb >= 0 && hb < w.firstReturn,
      "expected `await handBackJob(` before the return");
    check("phase3 resume guard then advances the walk",
      adv >= 0 && adv < w.firstReturn,
      "expected `await skipToNextJob();` before the return");
  }
  {
    // The phase_ats counterparts this fix mirrors must stay intact.
    const w = exitWindow("Skipped by you: ${jobTitle}");
    const adv = w.text.indexOf('await sendMsg({ type: "ATS_JOB_DONE" });');
    check("phase_ats review-mode skip still advances",
      adv >= 0 && adv < w.firstReturn, "ATS_JOB_DONE before the return");
    check("phase_ats resume guard still hands back",
      /if \(resumeRequired && !resumeOk\) \{\s*\n\s*await handBackJob\(/.test(SRC),
      "handBackJob on the ATS resume guard");
  }

  console.log(failures ? `\n${failures} FAILED` : "\nall passed");
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
