// The cover letter must reach the form that asks for one — and must not be written when
// nothing asks. Run:
//
//   node <repo>/jobflow/chrome-extension/tests/cover-letter-delivery.test.js
//
// What went wrong (measured 2026-09-23, scripts/measure_letter_delivery.py, whole
// install): a letter was generated on EVERY application — 103 of 107 rows carry one —
// and typed into a form exactly ONCE in 274 steps. Two different causes:
//
//   * Indeed never asks. Its wizard is modular and a cover-letter module has not appeared
//     in 214 form loads, so every letter written for an Indeed application was paid for,
//     stored, and shown in History as part of what the employer received. It wasn't.
//   * Greenhouse asks in 267 of the 320 schemas we hold — behind an "Enter manually"
//     chooser. The filler only ever looked for a visible textarea, so it saw nothing, and
//     the field fell through to the AI screener answerer: a SECOND model call that
//     answered "Cover Letter" as if it were a question.
//
// Pinned two ways, deterministically and with no live applications:
//   BEHAVIORAL — the real revealCoverLetterField() runs against jsdom fixtures rebuilt
//     from the live forms (Greenhouse chooser, open Lever textarea, Indeed's wizard).
//   STRUCTURAL — generation happens at the field, not before it, and the screener branch
//     answers a cover-letter label from the letter rather than the question answerer.

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

// ---- BEHAVIORAL: run the real reveal against real form shapes ------------------------
function slice(fnName) {
  const start = SRC.indexOf(`  async function ${fnName}(`) >= 0
    ? SRC.indexOf(`  async function ${fnName}(`)
    : SRC.indexOf(`  function ${fnName}(`);
  if (start < 0) return null;
  // Functions in content.js are two-space indented; the next line that starts a
  // sibling declaration ends this one.
  const rest = SRC.slice(start + 10);
  const end = rest.search(/\n  (?:async )?function |\n  const |\n\}\)\(\);/);
  return SRC.slice(start, start + 10 + (end < 0 ? rest.length : end));
}

const REVEAL = slice("revealCoverLetterField");
check("revealCoverLetterField() exists", !!REVEAL);

const FIXTURES = [
  {
    // VERBATIM from a live form (job-boards.greenhouse.io/zocdoc/jobs/7974986, captured
    // 2026-09-25). The first version of this fixture was invented, and it passed while
    // the real thing failed on six live forms: the trigger's nearest div reads only
    // "Enter manually", so a closest("div") block check rejected every real Greenhouse
    // field. Fixtures for this are copied, not imagined.
    name: "Greenhouse — the real chooser markup",
    html: `<div class="field-wrapper"><div role="group" aria-labelledby="upload-label-cover_letter" class="file-upload">
             <div id="upload-label-cover_letter" class="label upload-label">Cover Letter</div>
             <div class="file-upload__wrapper"><div class="button-container">
               <div class="secondary-button"><div>
                 <button type="button" class="btn btn--pill">Attach</button>
                 <input id="cover_letter" class="visually-hidden" type="file">
               </div></div>
               <div class="secondary-button"><button type="button" data-testid="cover_letter-dropbox">Dropbox</button></div>
               <div class="secondary-button"><div>
                 <button type="button" id="manual" data-testid="cover_letter-text">Enter manually</button>
                 <label class="visually-hidden" for="cover_letter_text">Enter manually</label>
               </div></div>
             </div></div></div></div>`,
    // Clicking swaps in the textarea Greenhouse actually renders: id cover_letter_text,
    // no name, and a label that says "Enter manually" — useless for matching.
    onClick: (doc) => {
      const ta = doc.createElement("textarea");
      ta.id = "cover_letter_text";
      doc.querySelector(".file-upload").appendChild(ta);
    },
    expect: true,
  },
  {
    name: "Lever — the textarea is already open",
    html: `<form><div class="field"><label for="c">Cover Letter</label>
             <textarea id="c" name="comments"></textarea></div></form>`,
    expect: true,
  },
  {
    name: "Indeed — resume step, nothing asks for a letter",
    html: `<form><div><label>Resume</label><input type="file"></div>
             <button type="button">Continue</button></form>`,
    expect: false,
  },
  {
    name: "the resume chooser is NOT mistaken for the letter's",
    html: `<div class="field-wrapper"><div role="group" class="file-upload">
             <div class="label upload-label">Resume/CV</div>
             <div class="secondary-button"><div>
               <button type="button" id="manual" data-testid="resume-text">Enter manually</button>
               <label class="visually-hidden" for="resume_text">Enter manually</label>
             </div></div></div></div>`,
    onClick: (doc) => {
      const ta = doc.createElement("textarea");
      ta.id = "resume_text";
      doc.querySelector(".file-upload").appendChild(ta);
    },
    expect: false,
  },
];

// revealCoverLetterField is async — assert in one awaited pass.
(async () => {
  for (const f of FIXTURES) {
    const { window } = new JSDOM(`<body>${f.html}</body>`, { pretendToBeVisual: true });
    const doc = window.document;
    Object.defineProperty(window.HTMLElement.prototype, "offsetParent", {
      get() { return this.ownerDocument.body; },
    });
    if (f.onClick) {
      doc.querySelectorAll("button").forEach((b) => {
        b.addEventListener("click", () => { if (b.id === "manual") f.onClick(doc); });
      });
    }
    const sandbox = {
      document: doc,
      window,
      formScope: () => doc,
      findFieldBySelectorsOrLabel: () => {
        // Mirrors SELECTORS.fields.coverLetter in content.js.
        const ta = doc.querySelector('textarea[id*="cover_letter" i], textarea[name*="cover_letter" i], textarea[name*="comments" i]');
        return ta && !ta.value.trim() ? ta : null;
      },
      getFieldLabel: (el) => {
        const lab = el.id ? doc.querySelector(`label[for="${el.id}"]`) : null;
        return lab ? lab.textContent : "";
      },
      humanClick: async (el) => { el.click(); },
      humanDelay: () => 0,
      sleep: async () => {},
      LETTER_LABEL_RE: /cover\s*letter|motivation(al)? letter/i,
      out: null,
    };
    vm.createContext(sandbox);
    vm.runInContext(`${REVEAL}\nout = revealCoverLetterField();`, sandbox);
    const el = await sandbox.out;
    check(f.name, !!el === f.expect, `got ${el ? el.tagName : "null"}`);
  }

  // ---- STRUCTURAL: the letter is written at the field, never before it ----------------
  check(
    "no path generates a letter before a form is seen",
    !/Generate cover letter\n\s+let coverLetter/.test(SRC),
    "an upfront generation block is back",
  );
  check(
    "GENERATE_COVER_LETTER is reached only through ensureCoverLetter()",
    (SRC.match(/type: "GENERATE_COVER_LETTER"/g) || []).length === 1,
    "more than one generation call site",
  );
  const ensure = slice("ensureCoverLetter") || "";
  check(
    "a stored letter is reused only for the job it was written for",
    /coverLetterFor === key/.test(ensure) && /coverLetterFor: key/.test(ensure),
    "the key guard is missing — the previous job's letter can leak into this one",
  );
  check(
    "the ATS path fills the field before answering screener questions",
    SRC.indexOf("fillCoverLetterIfAsked(label)") < SRC.indexOf("text: await fillTextQuestions()"),
    "reordered — the screener would answer the letter field with the question answerer",
  );
  check(
    "a cover-letter label is answered with the letter, not the question answerer",
    /LETTER_LABEL_RE\.test\(rawLabel\)[\s\S]{0,400}?value = await ensureCoverLetter\(\)/.test(SRC),
    "the screener branch is gone — this is the second model call we removed",
  );
  const labelRe = /const LETTER_LABEL_RE = (\/.*\/i);/.exec(SRC);
  check(
    "'why do you want to work here?' stays a screener question",
    !!labelRe && !eval(labelRe[1]).test("Why do you want to work here?"),
    "the label regex swallowed an open question the answerer handles better",
  );
  check(
    "a Cover Letter label still matches",
    !!labelRe && eval(labelRe[1]).test("Cover Letter"),
    "the regex no longer recognises the field it exists for",
  );

  console.log(failures ? `\n${failures} failure(s)` : "\nall checks passed");
  process.exit(failures ? 1 : 0);
})();
