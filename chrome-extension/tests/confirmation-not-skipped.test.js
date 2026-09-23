// Fixture test for the confirmation-page branch in ../content.js (#217). Run:
//
//   node <repo>/jobflow/chrome-extension/tests/confirmation-not-skipped.test.js
//
// The bug (live 2026-09-21, Amwell): a Greenhouse submit ends with a FULL navigation to
// the ATS thank-you URL, which kills the phase_ats context before it records anything.
// The unknown-phase pool branch woke up ON /confirmation, didn't recognise it, and logged
// "Couldn't open this job page — skipping". A SENT application became a skip: no
// applications row, and cross-run dedup blind to the company.
//
// Proving this LIVE is unreliable — the pool is an archive, so a random Greenhouse pick
// often hits an expired posting (redirects to the board root, correctly skipped) or a
// validation wall, never reaching /confirmation. So the fix is pinned two ways here,
// deterministically and with zero real applications:
//
//   BEHAVIORAL — the discriminator (POSTAPPLY_URL_HINTS) is run against the exact URLs
//     from the 2026-09-21 runs, so the test would have told confirmation from board-root.
//   STRUCTURAL — the confirmation check sits BEFORE the "Couldn't open" skip and records
//     applied_unconfirmed, so a future reorder that reintroduces the bug fails here.

const fs = require("fs");
const path = require("path");
const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

// ---- BEHAVIORAL: run the real discriminator on real URLs -----------------------------
// Extract POSTAPPLY_URL_HINTS from the source and evaluate the exact predicate the
// branch uses: POSTAPPLY_URL_HINTS.some((h) => path.includes(h)).
const arrMatch = /const POSTAPPLY_URL_HINTS = \[([\s\S]*?)\];/.exec(SRC);
check("POSTAPPLY_URL_HINTS is defined", !!arrMatch);
const HINTS = arrMatch
  ? arrMatch[1].split(",").map((s) => s.trim().replace(/^["']|["'],?$/g, "")).filter(Boolean)
  : [];
const isPostApply = (url) => HINTS.some((h) => url.toLowerCase().includes(h));

// The exact URLs seen on 2026-09-21:
const cases = [
  // The bug's own page — Greenhouse confirmation. MUST be recognised as post-apply.
  ["/amwell/jobs/4369654009/confirmation", true, "Amwell confirmation (the bug)"],
  ["/oura/jobs/4393754009/confirmation", true, "a GH job confirmation"],
  // Expired posting → Greenhouse board root. MUST NOT be — it is a genuine skip.
  ["/oura", false, "expired job → board root (correct skip)"],
  ["/oura?error=true", false, "expired job with error flag"],
  // Lever apply URL after a no-hint re-init. MUST NOT — no confirmation signal (the
  // known Lever gap: its confirmation carries no URL hint; a separate concern, not #217).
  ["/wpromote/1b103d87-c7ef-491c-bfc1-f3d1ca", false, "Lever apply URL, no hint"],
];
for (const [url, want, label] of cases) {
  check(`discriminator: ${label}`, isPostApply(url) === want,
    `isPostApply(${url}) = ${isPostApply(url)}, wanted ${want}`);
}
check("'/confirmation' is one of the hints", HINTS.includes("/confirmation"));

// ---- STRUCTURAL: the branch is wired the honest way ----------------------------------
// Anchor on the confirmation check itself (unique in the file), not on the literal
// "Couldn't open" — that phrase also appears in a COMMENT, which an earlier draft of
// this test mistook for the skip.
const confIdx = SRC.indexOf("POSTAPPLY_URL_HINTS.some");
check("the confirmation check exists", confIdx > -1,
  "no POSTAPPLY_URL_HINTS check — the bug is back");

// The ACTUAL skip is the logBackend statement (backtick template), not the comment that
// quotes the phrase. It must come AFTER the confirmation check, so a re-init on
// /confirmation is handled before the code can decide "couldn't open".
const skipCallIdx = SRC.indexOf("logBackend(`Couldn't open this job page", confIdx);
check("the confirmation check runs BEFORE the real skip", skipCallIdx > confIdx,
  "the POSTAPPLY check must precede the 'Couldn't open' skip statement");

// Everything the confirmation sub-branch does, up to the late-hydration poll that follows.
const confBlock = SRC.slice(confIdx, SRC.indexOf("Async ATS forms", confIdx));
check("records the application, not a skip", /APPLICATION_SAVED/.test(confBlock));
check("records it as applied_unconfirmed", /status: "applied_unconfirmed"/.test(confBlock),
  "must be unconfirmed — this context never saw the form succeed");
check("advances the queue after recording", /ATS_JOB_DONE/.test(confBlock));
check("leaves the branch before reaching the skip", /\n\s*break;/.test(confBlock));

// The letter reached the employer; only the RECORD was lost. Writing "" here made a
// sent-with-letter application indistinguishable in the database from one sent without
// (live: 2 of a0775013's 3 empty-letter rows came through this branch — Amwell 09-21,
// Glossier 07-19). The letter is in storage; recover it instead of recording a blank.
check("recovers the cover letter from storage", /generatedCoverLetter/.test(confBlock),
  "this branch must read the stored letter, not write an empty string");
check("does not hard-code an empty cover_letter", !/cover_letter:\s*""/.test(confBlock),
  'cover_letter: "" is the bug — a sent letter recorded as none');
// The key holds the LAST generation, so it belongs to this row only if it was generated
// for the job the queue head names. Without the guard a re-init on someone else's
// confirmation page would attach the wrong employer's letter.
check("guards the letter against a mismatched job", /currentJobInfo/.test(confBlock),
  "must confirm the stored letter was generated for this queue item");

console.log(failures ? `\n${failures} FAILED` : "\nall passed");
process.exit(failures ? 1 : 0);
