// Progress has to be REAL: a visible empty field we cannot fill is not "filled". Run:
//
//   node <repo>/jobflow/chrome-extension/tests/stall-needs-real-fill.test.js
//
// What went wrong (candidate #4, docs/reviews/2026-09-25-delivery-honesty.md; verified on
// the live source 09-26): the step loop called `typeValue(el, profile.phone || "")` and set
// `filledAny = true` on the next line regardless of the result. typeValue returns false when
// there is nothing to type, so ONE empty profile.phone in front of a visible phone field
// made every round report progress — `stallRounds` was reset each time and the hand-back that
// exists precisely for "we cannot finish this form" never fired. The invariant of the whole
// engine ("submitted-complete-and-honest OR handed back with a reason") was unreachable.
//
// This test DRIVES the real function out of content.js — it does not read it — so a future
// rewrite that loses the rule fails here instead of failing on a user's form.

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");

let failures = 0;
function check(name, cond, detail) {
  if (!cond) failures++;
  console.log(`${cond ? "  ok" : "FAIL"}  ${name}${cond ? "" : `  (${detail || ""})`}`);
}

// ---- pull the two real functions out of the IIFE ------------------------------------
function extract(signature) {
  const at = SRC.indexOf(signature);
  if (at < 0) return null;
  // Brace-match from the first "{" after the signature.
  const open = SRC.indexOf("{", at);
  let depth = 0;
  for (let i = open; i < SRC.length; i++) {
    if (SRC[i] === "{") depth++;
    else if (SRC[i] === "}") {
      depth--;
      if (depth === 0) return SRC.slice(at, i + 1);
    }
  }
  return null;
}

const fillSrc = extract("  async function fillIdentityFields(");
const typeSrc = extract("  async function typeValue(");
check("fillIdentityFields exists", !!fillSrc);
check("typeValue exists", !!typeSrc);
if (!fillSrc || !typeSrc) process.exit(1);

// The one line the bug lived on. Load-bearing enough to pin by shape as well as by
// behaviour: `filledAny` must never be set next to an UNAWAITED/UNCHECKED typeValue.
check(
  "the fill result is what marks progress",
  /if \(await typeValue\(/.test(fillSrc),
  "fillIdentityFields must branch on typeValue's return value"
);
check(
  "typeValue still reports 'nothing to type' as false",
  /if \(!el \|\| !value\) return false;/.test(typeSrc),
  "the false return is the signal this whole rule reads"
);

// ---- run it against a DOM, with the closure's helpers stubbed -----------------------
async function run(html, profile) {
  const dom = new JSDOM(`<!doctype html><body><form>${html}</form></body>`);
  const { document } = dom.window;
  const typed = [];
  const ctx = {
    document,
    // Real typeValue semantics, minus the human delays: write the value, report whether
    // there WAS one. Keeping the real early return is the point of the test.
    async typeValue(el, value) {
      if (!el || !value) return false;
      el.value = value;
      typed.push(value);
      return true;
    },
    findFieldBySelectorsOrLabel: (key) => document.querySelector(`[data-key="${key}"]`),
    sleep: async () => {},
    humanDelay: () => 0,
    resolveEmail: async (p) => p.email || "",
  };
  const fn = new Function(
    ...Object.keys(ctx),
    `return (${fillSrc.replace("async function fillIdentityFields", "async function")})`
  )(...Object.values(ctx));
  const filled = [];
  const gaps = [];
  const any = await fn(profile, filled, gaps);
  return { any, filled, gaps, typed };
}

(async () => {
  // 1. THE BUG: a visible, empty phone field and a profile with no phone.
  const empty = await run(
    '<label for="p">Phone</label><input id="p" data-key="phone">',
    { name: "", last_name: "", email: "", phone: "" }
  );
  check("an unfillable field is NOT progress", empty.any === false, `any=${empty.any}`);
  check("and it is named as a profile gap", empty.gaps.join(",") === "phone", empty.gaps.join(","));
  check("nothing was typed", empty.typed.length === 0, empty.typed.join(","));

  // 2. The same field with data behind it is progress, as always.
  const full = await run(
    '<label for="p">Phone</label><input id="p" data-key="phone">',
    { name: "", last_name: "", email: "", phone: "+1 808 555 0100" }
  );
  check("a filled field IS progress", full.any === true);
  check("and reports no gap", full.gaps.length === 0, full.gaps.join(","));
  check("the value reached the input", full.typed.join(",") === "+1 808 555 0100");

  // 3. Mixed: one field we can fill, one we cannot. Progress is true (we did something),
  //    but the gap still travels, so the hand-back reason can name it when the step stalls.
  const mixed = await run(
    '<input id="f" data-key="firstName"><input id="p" data-key="phone">',
    { name: "Igor", last_name: "", email: "", phone: "" }
  );
  check("progress on one field still reports the other as a gap",
    mixed.any === true && mixed.gaps.join(",") === "phone",
    `any=${mixed.any} gaps=${mixed.gaps.join(",")}`);

  // 4. A field the form already filled is left alone (no re-typing, no gap).
  const prefilled = await run(
    '<input id="e" data-key="email" value="someone@example.com">',
    { name: "", last_name: "", email: "other@example.com", phone: "" }
  );
  check("a pre-filled field is untouched",
    prefilled.any === false && prefilled.gaps.length === 0 && prefilled.typed.length === 0,
    `gaps=${prefilled.gaps.join(",")} typed=${prefilled.typed.join(",")}`);

  // ---- and the hand-back has to SAY the gap ----------------------------------------
  check(
    "the hand-back reason carries the profile gaps",
    /your profile has nothing for: \$\{\[\.\.\.new Set\(profileGaps\)\]/.test(SRC),
    "the reason string must name the missing fields"
  );
  check(
    "the step trace carries them too",
    /gaps=\[\$\{profileGaps\.join\(","\)\}\]/.test(SRC),
    "STEP line must show gaps so History explains the stall"
  );

  console.log(failures ? `\n${failures} failure(s)` : "\nall good");
  process.exit(failures ? 1 : 0);
})();
